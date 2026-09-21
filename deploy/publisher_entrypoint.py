#!/usr/bin/python3
"""One-shot publisher for decisions that already bind an exact commit."""
import argparse
import json
import os
import shutil
import sys
import hashlib
import re
import traceback
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# Run directly as `python3 -I .../deploy/publisher_entrypoint.py` in
# production (see deploy/systemd/*.service); -I suppresses Python's normal
# auto-add of the script's own directory to sys.path, so this sibling-module
# import needs an explicit bootstrap rather than relying on that default.
# (load_modules() below does its own separate, root-validated sys.path setup
# for the review/release_bundle modules it loads from an installed root --
# fsutil is same-directory trusted code, not subject to that validation.)
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from fsutil import atomic_write_json, read_bounded_regular


def load_modules(installed_root: Path):
    root = installed_root.resolve(strict=True)
    sys.path.insert(0, str(root))
    import importlib
    names = ("deploy.release_bundle","web.review")
    modules = {name: importlib.import_module(name) for name in names}
    files = {name: str(Path(module.__file__).resolve(strict=True)) for name, module in modules.items()}
    if any(not Path(value).is_relative_to(root) for value in files.values()):
        raise SystemExit("security-critical import escaped installed root")
    return modules, files


MAX_QUEUE_BYTES = 1024 * 1024 * 1024
MAX_OUTCOME_BYTES = 65536


def _bounded_regular_bytes(path: Path, limit: int, label: str) -> bytes:
    try:
        return read_bounded_regular(path, maximum=limit)
    except OSError as exc:
        raise RuntimeError(f"unsafe or unreadable {label}") from exc


def queue_snapshot(queue: Path) -> dict:
    raw = _bounded_regular_bytes(Path(queue), MAX_QUEUE_BYTES, "decision queue")
    return {"decision_queue_sha256": hashlib.sha256(raw).hexdigest(), "decision_queue_size": len(raw)}


def authorized_queue_snapshot(review_module, queue: Path, decision_key: bytes, decision: dict,
                              quarantine_dir: Path, on_corrupt) -> dict:
    """Validate under the writer lock, then release it before the caller pushes.

    The lock only needs to cover latest-decision validation and capturing the
    queue digest -- it must not still be held during the caller's (up to 120s,
    network-bound) push, since that would block reviewers submitting new
    decisions and the activator's polling for the whole push duration. A
    decision appended after the snapshot is captured is still caught: the
    activator re-checks the recorded digest against the live queue before
    activating (deploy.activator_entrypoint.verify_gate_queue_snapshot).
    """
    with review_module.queue_lock(queue, read_only=True):
        latest = None
        for record in review_module.iter_valid_decisions(
                queue, decision_key, quarantine_dir=quarantine_dir, on_corrupt=on_corrupt):
            try:
                validated = review_module.validate_decision(record, decision_key)
            except (Exception, SystemExit):
                continue
            if validated.get("rough_path") == decision.get("rough_path"):
                latest = validated
        if (latest is None or latest.get("action") != "approve" or
                latest.get("decision_id") != decision.get("decision_id")):
            raise RuntimeError("approval was superseded before push")
        return queue_snapshot(queue)


def process_decision(publisher, decision: dict, approved_root: Path, builds_root: Path,
                     review_bundle_archive: Path, push_authorizer=None, chain_from: dict | None = None) -> str:
    """Deterministic, idempotent per-decision publish step.

    Returns "wait" when the fixed builder has not yet produced a build, otherwise
    finalizes (signs) the built release and pushes. Safe to retry from any crash
    window: an incomplete staging/preparing directory is discarded and rebuilt, and
    ``publish`` re-verifies the signed bundle before an idempotent push.
    """
    decision_id = decision["decision_id"]
    archive = review_bundle_archive / (decision["snapshot_bundle_sha256"] + ".bundle")
    if not archive.is_file():
        raise SystemExit("original review snapshot bundle is not archived")
    publisher.verify_review_snapshot(archive, decision["snapshot_commit"], decision["snapshot_tree"], decision["snapshot_bundle_sha256"])
    target = approved_root / decision_id
    if not target.exists():
        preparing = approved_root / (".preparing-" + decision_id)
        if preparing.exists():
            shutil.rmtree(preparing)
        if chain_from is None:
            publisher.prepare_change(preparing, decision)
        else:
            publisher.prepare_change(preparing, decision, chain_from=chain_from)
        os.replace(preparing, target)
    approval_meta = json.loads((target / "approval.json").read_text(encoding="utf-8"))
    generation = f'{approval_meta["decision_id"]}-{approval_meta["nonce"]}'
    build = builds_root / generation
    if not (build / "release.json").is_file():
        return "wait"
    if not (build / "release.sig").is_file():
        publisher.finalize(build)
    snapshot = push_authorizer(decision) if push_authorizer is not None else None
    if snapshot is None:
        publisher.publish(build)
    else:
        publisher.publish(build, queue_snapshot=snapshot)
    return "pushed"

def process_sync(publisher, approved_root: Path, builds_root: Path, push_authorizer=None):
    """The sync release: deletions from wiki/ that reached origin, published without a decision.

    Returns None when there is nothing to remove, else (release id, "wait" | "pushed").
    Same shape as process_decision: prepare once per state of origin, wait for the builder,
    then sign and push. Safe to run again at any point.
    """
    preparing = approved_root / ".preparing-sync"
    if preparing.exists():
        shutil.rmtree(preparing)
    approval = publisher.prepare_sync(preparing)
    if approval is None:
        return None
    release_id = approval["decision_id"]
    target = approved_root / release_id
    if target.exists():
        shutil.rmtree(preparing)          # already prepared for exactly this state: carry on with that one
    else:
        os.replace(preparing, target)
    meta = json.loads((target / "approval.json").read_text(encoding="utf-8"))
    build = builds_root / f'{meta["decision_id"]}-{meta["nonce"]}'
    if not (build / "release.json").is_file():
        return release_id, "wait"
    if not (build / "release.sig").is_file():
        publisher.finalize(build)
    snapshot = push_authorizer() if push_authorizer is not None else None
    if snapshot is None:
        publisher.publish(build)
    else:
        publisher.publish(build, queue_snapshot=snapshot)
    return release_id, "pushed"


def published_result(approved_root: Path, builds_root: Path, decision_id: str) -> dict:
    """The publisher's state record for a release that has just been pushed."""
    prepared = approved_root / decision_id
    approval_meta = json.loads((prepared / "approval.json").read_text(encoding="utf-8"))
    build = builds_root / (f'{approval_meta["decision_id"]}-{approval_meta["nonce"]}')
    approval = json.loads((build / "release.json").read_text(encoding="utf-8"))
    result = {"status": "published", "decision_id": decision_id, "decision_sha256": approval["decision_sha256"],
              "nonce": approval["nonce"], "generation": approval["generation"], "origin": approval["origin"],
              "commit": approval["commit"], "tree": approval["tree"], "bundle_sha256": approval["bundle_sha256"],
              "artifacts": approval["artifacts"]}
    if "parent_commit" in approval:
        result["parent_commit"] = approval["parent_commit"]
    return result


# Failures that a retry cannot fix: the approval names a draft that is no longer in origin, or a
# wiki path that now holds different content. Anything else (network, a busy build) stays retryable.
PERMANENT_FAILURES = (
    "rough source is unreadable",
    "wiki_path already published with different content",
    "review snapshot is not part of the published history",
)


def process_records(decisions, worker, write_state, read_state=None) -> None:
    for decision in decisions:
        decision_id=decision["decision_id"]
        if read_state is not None:
            previous=read_state(decision_id)
            if previous.get("status")=="failed" and previous.get("retryable") is False:
                print(f"DEK publish skipped decision={decision_id}: cannot be retried ({previous.get('last_error','')[:160]}); "
                      f"delete its state file to try again",file=sys.stderr)
                continue
        try:
            status=worker(decision)
            if status=="pushed": write_state(decision_id,{"status":"published","decision_id":decision_id})
            elif isinstance(status,dict): write_state(decision_id,status)
        except (Exception, SystemExit) as exc:
            # error_type alone (no message/traceback) gave no way to diagnose
            # a real retryable failure from the journal -- this masked the
            # actual cause (a permission bug) behind an apparently-successful
            # oneshot unit exit the first time this pipeline ran for real.
            traceback.print_exc(file=sys.stderr)
            permanent=any(marker in str(exc) for marker in PERMANENT_FAILURES)
            write_state(decision_id,{"status":"failed","decision_id":decision_id,"error_type":type(exc).__name__,
                                     "retryable":not permanent,"last_error":str(exc)[-300:]})


def load_approved_decisions(review_module, queue: Path, decision_key: bytes,
                            quarantine_dir: Path, on_corrupt) -> list[dict]:
    """Read and validate a stable queue snapshot under the writer's lock."""
    latest_by_path={}
    with review_module.queue_lock(queue,read_only=True):
        records=review_module.iter_valid_decisions(queue,decision_key,quarantine_dir=quarantine_dir,on_corrupt=on_corrupt)
        for record in records:
            try:
                validated=review_module.validate_decision(record,decision_key)
            except (Exception,SystemExit):
                continue
            rough_path=validated["rough_path"]
            latest_by_path.pop(rough_path,None)
            latest_by_path[rough_path]=validated
    return [record for record in latest_by_path.values() if record["action"]=="approve"]


def _write_isolation(directory: Path, name: str, reason: str) -> None:
    digest = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()
    target = directory / (digest + ".json")
    value = {"status": "isolated", "source": name, "error_type": reason}
    atomic_write_json(target, value, mode=0o600, ensure_parent_mode=0o700)


def ingest_activation_outcomes(outcomes: Path, state_root: Path, quarantine_dir: Path, write_state) -> None:
    """Consume exact successful records, isolating every malformed sibling."""
    core = {"schema_version", "sequence", "nonce", "generation", "previous_generation",
            "commit", "tree", "bundle_sha256", "artifacts"}
    provenance = {"decision_id", "decision_sha256", "origin"}
    for path in sorted(Path(outcomes).glob("*.json")):
        try:
            raw = _bounded_regular_bytes(path, MAX_OUTCOME_BYTES, "activation outcome")
            value = json.loads(raw.decode("utf-8"))
            fields = frozenset(value) if isinstance(value, dict) else frozenset()
            allowed = core | provenance | {"status", "proved_at"}
            allowed_parent = allowed | {"parent_commit"}
            if (not isinstance(value, dict) or fields not in {frozenset(allowed), frozenset(allowed_parent)}
                    or value.get("status") != "succeeded" or type(value.get("proved_at")) is not int
                    or path.name != str(value.get("nonce")) + ".json"):
                raise RuntimeError("outcome schema or filename mismatch")
            if (value.get("schema_version") != 2 or type(value.get("sequence")) is not int or value["sequence"] < 1
                    or not re.fullmatch(r"[A-Za-z0-9_-]{8,79}", str(value.get("decision_id", "")))
                    or not re.fullmatch(r"[A-Za-z0-9_-]{8,79}", str(value.get("nonce", "")))
                    or value.get("generation") != f'{value.get("decision_id")}-{value.get("nonce")}'
                    or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("decision_sha256", "")))
                    or not re.fullmatch(r"[0-9a-f]{40,64}", str(value.get("commit", "")))
                    or not re.fullmatch(r"[0-9a-f]{40,64}", str(value.get("tree", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("bundle_sha256", "")))
                    or not isinstance(value.get("origin"), str) or not isinstance(value.get("artifacts"), dict)
                    or not {"dek-kb.json", "site/index.html"}.issubset(value["artifacts"])
                    or any(not isinstance(name, str) or ".." in Path(name).parts or
                           not re.fullmatch(r"[0-9a-f]{64}", str(digest)) for name, digest in value["artifacts"].items())
                    or ("parent_commit" in value and not re.fullmatch(r"[0-9a-f]{40,64}", str(value["parent_commit"])))):
                raise RuntimeError("outcome values invalid")
            decision_id = value["decision_id"]
            state_path = Path(state_root) / (decision_id + ".json")
            state = json.loads(_bounded_regular_bytes(state_path, MAX_OUTCOME_BYTES, "publisher state"))
            expected = ("decision_id", "decision_sha256", "nonce", "generation", "origin",
                        "commit", "tree", "bundle_sha256", "artifacts")
            if "parent_commit" in value:
                expected += ("parent_commit",)
            if state.get("status") not in {"published", "activated"} or any(state.get(key) != value.get(key) for key in expected):
                raise RuntimeError("outcome does not match published state")
            state.update({"status": "activated", "outcome_sha256": hashlib.sha256(raw).hexdigest()})
            write_state(decision_id, state)
        except (Exception, SystemExit) as exc:
            _write_isolation(Path(quarantine_dir), path.name, type(exc).__name__)


def run_sync_release(modules, publisher, decisions, approved, builds, state_root, queue, write_state) -> None:
    """After the decisions: publish removals from wiki/ that origin already holds.

    Reviewed decisions come first (this waits while any is not yet published), and a
    failure here is logged and never touches the decisions' own publishing.
    """
    try:
        for decision in decisions:
            try:
                state=json.loads((state_root/(decision["decision_id"]+".json")).read_text(encoding="utf-8"))
            except (OSError,ValueError):
                state={}
            status=state.get("status")
            # A decision marked permanently unpublishable (its draft is gone, its wiki path holds
            # other content) will never publish; waiting for it would block every sync forever.
            if status=="failed" and state.get("retryable") is False:
                continue
            if status not in {"published","activated"}:
                return
        def authorize():
            with modules["web.review"].queue_lock(queue,read_only=True):
                return queue_snapshot(queue)
        outcome=process_sync(publisher,approved,builds,authorize)
        if outcome is not None and outcome[1]=="pushed":
            write_state(outcome[0],published_result(approved,builds,outcome[0]))
    except (Exception,SystemExit) as exc:
        traceback.print_exc(file=sys.stderr)
        print(f"DEK sync release failed error_type={type(exc).__name__}",file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, default=Path("/opt/dek-publisher/app"))
    parser.add_argument("--verify-imports-only", action="store_true")
    args, remaining = parser.parse_known_args(argv)
    modules, files = load_modules(args.installed_root)
    if args.verify_imports_only:
        print(json.dumps(files, sort_keys=True)); return 0
    config_parser=argparse.ArgumentParser(); config_parser.add_argument("--config",type=Path,required=True); config_args=config_parser.parse_args(remaining)
    config=json.loads(config_args.config.read_text(encoding="utf-8"))
    if set(config)!={"decisions_root","approved_root","state_root","origin","builds_root","review_bundle_archive"}: raise SystemExit("invalid publisher config")
    if config["origin"] != os.environ.get("DEK_FIXED_ORIGIN"): raise SystemExit("origin is not the fixed service policy")
    key=load_pem_private_key(Path(os.environ["DEK_APPROVAL_SIGNING_KEY_FILE"]).read_bytes(),password=None)
    publisher=modules["deploy.release_bundle"].ReleasePublisher(config["origin"],key,Path(os.environ["DEK_GIT_CREDENTIAL_FILE"]))
    decisions=Path(config["decisions_root"]).resolve(strict=True); approved=Path(config["approved_root"]).resolve(strict=True)
    builds=Path(config["builds_root"]).resolve(strict=True); archive=Path(config["review_bundle_archive"]).resolve(strict=True)
    state_root=Path(config["state_root"]); state_root.mkdir(parents=True,exist_ok=True)
    def write_state(decision_id,value):
        atomic_write_json(state_root/(decision_id+".json"),value,mode=0o600)
    queue=decisions/"decisions.jsonl"
    decision_key=Path(os.environ["DEK_REVIEW_DECISION_KEY_FILE"]).read_bytes().strip()
    def queue_alert(value):
        write_state("queue-corruption-"+value["sha256"],value)
        print("DEK queue corruption quarantined: "+value["sha256"],file=sys.stderr)
    valid=load_approved_decisions(modules["web.review"],queue,decision_key,state_root/"quarantine",queue_alert)
    # Approvals waiting together are prepared as a chain: each package sits on top of the one
    # before it, so they push (and activate) one after another without ever conflicting.
    chain={"value":None}
    def worker(decision):
        def authorize(current):
            return authorized_queue_snapshot(modules["web.review"], queue, decision_key, current,
                                             state_root/"quarantine", queue_alert)
        status=process_decision(publisher,decision,approved,builds,archive,authorize,chain_from=chain["value"])
        prepared_dir=approved/decision["decision_id"]
        if status=="pushed":
            chain["value"]=None       # origin already holds it; the next one starts from origin
        elif status=="wait":
            try:
                package=json.loads((prepared_dir/"approval.json").read_text(encoding="utf-8"))
                chain["value"]={"bundle":str(prepared_dir/"repository.bundle"),"commit":package["commit"]}
            except (OSError,ValueError,KeyError):
                pass
        if status=="pushed":
            return published_result(approved,builds,decision["decision_id"])
        return status
    def read_state(decision_id):
        try:
            value=json.loads((state_root/(decision_id+".json")).read_text(encoding="utf-8"))
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError):
            return {}
    process_records(valid,worker,write_state,read_state)
    run_sync_release(modules, publisher, valid, approved, builds, state_root, queue, write_state)
    outcomes=Path("/var/lib/dek-activate/outcomes")
    if outcomes.exists():
        ingest_activation_outcomes(outcomes, state_root, state_root/"outcome-quarantine", write_state)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
