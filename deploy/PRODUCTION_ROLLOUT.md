# DEK replacement architecture — manual Stage A runbook

This is a fixed, checkpointed manual runbook. Stage A must be performed by an administrator from an independently verified package whose SHA-256 manifest was approved out of band. Nothing in Stage B has privilege to install software, alter services, use sudo, call systemctl or D-Bus, or modify production infrastructure.

Do not execute this runbook from this development checkout. The commands below are the exact production commands after the bracketed site facts have been recorded and independently approved. Stop at every failed checkpoint. The current production remains unchanged until A6.

## Checkpoint A0 — record fail-closed site facts

Record `[PACKAGE]`, `[PACKAGE_SHA256]`, `[BACKUP]`, `[OLD_WEB_UNIT]`, `[OLD_QA_UNIT]`, `[OLD_WEB_PATH]`, `[OLD_QA_PATH]`, `[REVIEWER_IDS]`, and the observed nginx worker name/UID/GID as `[NGINX_WORKER_USER]`, `[NGINX_WORKER_UID]`, and `[NGINX_WORKER_GID]`. Require a nonempty enterprise-ID allowlist, exact OAuth callback `https://regkb.chenponai.com/review/auth/callback`, a valid TLS chain/SAN for `regkb.chenponai.com`, and an application readiness response before enabling review or automation. Register the new callback in DingTalk before cutover; never comma-concatenate callback values. The legacy reviewer hostname remains only a root-page redirect after cutover and must not proxy callbacks or POST requests.

Rollback is permitted only when the external digest anchor and backup-set files are each a root-owned regular file and every ancestor is root-owned and not group/world writable (a **non-root-writable ancestor** is fatal). This is checked again immediately before archive extraction.

```bash
test -n '[REVIEWER_IDS]'
test '[OLD_WEB_UNIT]' = dek-web.service
test '[OLD_QA_UNIT]' = dek-qa.service
printf '%s  %s\n' '[PACKAGE_SHA256]' '[PACKAGE]' | sha256sum -c -
test ! -e /run/dek-package-check
install -d -o root -g root -m 0755 /run/dek-package-check
tar --no-same-owner --no-same-permissions -xf '[PACKAGE]' -C /run/dek-package-check
(cd /run/dek-package-check && sha256sum -c deploy/PACKAGE.sha256)
test "$(stat -c '%U:%G:%a:%h:%F' /run/dek-package-check/deploy/rollback.py)" = 'root:root:644:1:regular file'
python3 -I /run/dek-package-check/deploy/readiness.py --configuration-only --config /root/dek-approved-readiness.json
```

## Checkpoint A1 — exact backup inventory before mutation

Create `/root/dek-backup-inventory.txt` with exactly these absolute paths, plus the four bracketed observed old paths (one per line):

```text
/etc/passwd
/etc/group
/etc/shadow
/etc/gshadow
/usr/lib/sysusers.d/dek-review-deploy.conf
/usr/lib/tmpfiles.d/dek-review-deploy.conf
/etc/systemd/system/dek-web.service
/etc/systemd/system/dek-qa.service
/etc/systemd/system/dek-review.service
/etc/systemd/system/dek-review-publish.service
/etc/systemd/system/dek-review-publish-manual.path
/etc/systemd/system/dek-review-publish-manual.service
/etc/systemd/system/dek-source-ingest.service
/etc/systemd/system/dek-source-ingest-proof.service
/etc/systemd/system/dek-source-ingest.timer
/etc/systemd/system/dek-source-ingest-alert@.service
/etc/systemd/system/dek-source-ingest-manual.path
/etc/systemd/system/dek-source-ingest-manual.service
/etc/systemd/system/dek-builder.service
/etc/systemd/system/dek-activator.service
/opt/dek-web
/opt/dek-qa
/opt/dek-review
/opt/dek-publisher
/opt/dek-source-ingest
/opt/dek-builder
/opt/dek-activator
/etc/dek-publisher.json
/etc/dek-builder.json
/etc/dek-activator.json
/etc/dek-readiness.json
/etc/dek-approval-public-key.pem
/etc/nginx/sites-available/dek-review.conf
/etc/nginx/sites-enabled/dek-review.conf
/etc/nginx/sites-available/regkb.chenponai.com
/etc/nginx/sites-enabled/regkb.chenponai.com
/etc/nginx/snippets/dek-review-location.conf
/etc/dek-review-tls
/var/lib/dek-git-auth
/var/lib/dek-web
/var/lib/dek-qa
/var/lib/dek-review
/var/lib/dek-publisher
/var/lib/dek-source-ingest
/var/lib/dek-builder
/var/lib/dek-activate
/var/spool/dek-build
/var/spool/dek-activate
/var/lib/dek-readiness
/var/lib/dek-install-transactions
[OLD_WEB_PATH]
[OLD_QA_PATH]
```

Replace both OLD placeholders with their approved absolute values, then normalize before copying. Exact duplicate OLD paths are removed in stable order; ancestor/descendant overlap and paths outside the approved boundary are fatal. Thus each retained recursive tar root occurs once and GNU tar cannot emit duplicate members. Copy the normalized file identically to `$LATEST_BACKUP/backup-inventory.txt` and `$LATEST_BACKUP/rollback-inventory.txt`; there are no globs and no inferred paths.

```bash
LATEST_BACKUP='[BACKUP]'
install -d -o root -g root -m 0700 "$LATEST_BACKUP"
PYTHONPATH=/run/dek-package-check python3 - <<'PY'
from pathlib import Path
from deploy.rollback import normalize_inventory
p=Path('/root/dek-backup-inventory.txt')
values=normalize_inventory(p.read_text(encoding='utf-8').splitlines())
p.write_text(''.join(value+'\n' for value in values),encoding='utf-8')
PY
install -o root -g root -m 0600 /root/dek-backup-inventory.txt "$LATEST_BACKUP/backup-inventory.txt"
install -o root -g root -m 0600 /root/dek-backup-inventory.txt "$LATEST_BACKUP/rollback-inventory.txt"
while IFS= read -r path; do test -e "$path" -o -L "$path" && printf '%s\0' "$path"; done < "$LATEST_BACKUP/backup-inventory.txt" > "$LATEST_BACKUP/existing.nul"
while IFS= read -r path; do test -e "$path" -o -L "$path" || printf '%s\n' "$path"; done < "$LATEST_BACKUP/backup-inventory.txt" > "$LATEST_BACKUP/absent.txt"
tar --xattrs --acls --numeric-owner --null --verbatim-files-from --files-from="$LATEST_BACKUP/existing.nul" -cpf "$LATEST_BACKUP/files.tar"
PYTHONPATH=/run/dek-package-check python3 - "$LATEST_BACKUP" <<'PY'
import sys
from deploy.rollback import write_tree_manifest
from pathlib import Path
root=Path(sys.argv[1]); write_tree_manifest(root/'files.tar',root/'tree-manifest.json')
PY
PYTHONPATH=/run/dek-package-check python3 - "$LATEST_BACKUP/unit-enablement.before" <<'PY'
import sys
from pathlib import Path
from deploy.rollback import write_enablement_snapshot
write_enablement_snapshot(Path(sys.argv[1]))
PY
sha256sum "$LATEST_BACKUP/files.tar" "$LATEST_BACKUP/backup-inventory.txt" "$LATEST_BACKUP/rollback-inventory.txt" "$LATEST_BACKUP/existing.nul" "$LATEST_BACKUP/absent.txt" "$LATEST_BACKUP/unit-enablement.before" "$LATEST_BACKUP/tree-manifest.json" | sed "s#  $LATEST_BACKUP/#  #" > /root/dek-backup-SHA256SUMS
chmod 0400 /root/dek-backup-SHA256SUMS
sync -f "$LATEST_BACKUP/files.tar"
sync -f /root/dek-backup-SHA256SUMS
```

A6 evidence is not part of the fixed backup metadata set bound by
`/root/dek-backup-SHA256SUMS`.  It is written later only below the root-owned
`0700` `$LATEST_BACKUP/ingestion-cutover/` directory; `rollback.py` accepts that
single optional evidence directory while still requiring the exact seven-file
backup metadata contract and rejecting every other top-level entry.

## Checkpoint A2 — exact identities, directories and credentials

```bash
install -o root -g root -m 0644 /run/dek-package-check/deploy/sysusers/dek-review-deploy.conf /usr/lib/sysusers.d/dek-review-deploy.conf
install -o root -g root -m 0644 /run/dek-package-check/deploy/tmpfiles/dek-review-deploy.conf /usr/lib/tmpfiles.d/dek-review-deploy.conf
systemd-sysusers /usr/lib/sysusers.d/dek-review-deploy.conf
systemd-tmpfiles --create /usr/lib/tmpfiles.d/dek-review-deploy.conf
install -o root -g root -m 0400 /root/dek-secrets/git-credentials /var/lib/dek-git-auth/git-credentials
install -o dek-publisher -g dek-publisher -m 0400 /root/dek-secrets/approval-signing-key.pem /var/lib/dek-publisher/secrets/approval-signing-key.pem
install -o dek-publisher -g dek-publisher -m 0400 /root/dek-secrets/review-decision-key /var/lib/dek-publisher/secrets/review-decision-key
install -o root -g root -m 0444 /root/dek-secrets/approval-public-key.pem /etc/dek-approval-public-key.pem
install -o dek-review -g dek-review -m 0400 /root/dek-secrets/reviewer-environment /var/lib/dek-review/secrets/environment
install -o dek-review -g dek-review -m 0400 /root/dek-secrets/review-decision-key /var/lib/dek-review/secrets/review-decision-key
install -o dek-web -g dek-web -m 0400 /root/dek-secrets/web-environment /var/lib/dek-web/secrets/environment
install -o dek-web -g dek-web -m 0400 /root/dek-secrets/web-generation-proof /var/lib/dek-web/secrets/generation-proof-secret
install -o dek-qa -g dek-qa -m 0400 /root/dek-secrets/qa-environment /var/lib/dek-qa/secrets/environment
install -o dek-activator -g dek-activator -m 0400 /root/dek-secrets/web-generation-proof /var/lib/dek-activator/secrets/web-generation-proof
umask 077
if test ! -e /var/lib/dek-readiness/marker-hmac-key; then openssl rand 32 > /var/lib/dek-readiness/marker-hmac-key; fi
chown root:root /var/lib/dek-readiness/marker-hmac-key
chmod 0400 /var/lib/dek-readiness/marker-hmac-key
openssl pkey -pubin -in /etc/dek-approval-public-key.pem -noout >/dev/null
cmp -s <(openssl pkey -in /var/lib/dek-publisher/secrets/approval-signing-key.pem -pubout 2>/dev/null) <(openssl pkey -pubin -in /etc/dek-approval-public-key.pem -pubout 2>/dev/null)
cmp -s /var/lib/dek-review/secrets/review-decision-key /var/lib/dek-publisher/secrets/review-decision-key
cmp -s /var/lib/dek-web/secrets/generation-proof-secret /var/lib/dek-activator/secrets/web-generation-proof
for p in /var/lib/dek-git-auth/git-credentials /var/lib/dek-publisher/secrets/approval-signing-key.pem /var/lib/dek-publisher/secrets/review-decision-key /var/lib/dek-review/secrets/environment /var/lib/dek-review/secrets/review-decision-key /var/lib/dek-web/secrets/environment /var/lib/dek-web/secrets/generation-proof-secret /var/lib/dek-qa/secrets/environment /var/lib/dek-activator/secrets/web-generation-proof /var/lib/dek-readiness/marker-hmac-key; do test "$(stat -c '%F:%a:%h' "$p")" = 'regular file:400:1' || exit 1; done
python3 -I /run/dek-package-check/deploy/credential_gate.py \
  --git-credential /var/lib/dek-git-auth/git-credentials --fixed-origin https://github.com/chenponsh/dek.git \
  --approval-private /var/lib/dek-publisher/secrets/approval-signing-key.pem --approval-public /etc/dek-approval-public-key.pem \
  --environment review:/var/lib/dek-review/secrets/environment --environment web:/var/lib/dek-web/secrets/environment \
  --environment qa:/var/lib/dek-qa/secrets/environment \
  --shared-secret /var/lib/dek-review/secrets/review-decision-key:/var/lib/dek-publisher/secrets/review-decision-key \
  --shared-secret /var/lib/dek-web/secrets/generation-proof-secret:/var/lib/dek-activator/secrets/web-generation-proof \
  --random-secret readiness-hmac:/var/lib/dek-readiness/marker-hmac-key \
  --tls-certificate /etc/letsencrypt/live/regkb.chenponai.com/fullchain.pem --tls-private /etc/letsencrypt/live/regkb.chenponai.com/privkey.pem
```

The content gate reads credential bytes without ever printing them. It rejects empty, `***`, placeholder/example/sample, short and low-entropy values; validates each environment class and identifier/HTTPS format; binds the Git token to the fixed origin; requires a matching Ed25519 private/public pair; and byte-compares both shared-secret pairs while independently checking random HMAC material. Any failure stops before A3.

Verify the exact setgid/DAC matrix in `deploy/DAC_MATRIX.tsv` with positive producer/consumer reads and negative cross-service reads using real accounts. Every credential loaded by systemd must be a non-symlink regular inode with mode `0400` and one link, must be non-replaceable through its credential directory, and must be actually readable by the service process; source-file ownership is not compared with the service EUID because systemd materializes credentials in its private credential directory. No other component may read it. Do not print credential contents. On the real host, inspect `$CREDENTIALS_DIRECTORY` from inside each unit and perform these checks there.

```bash
runuser -u dek-publisher -- test -r /var/lib/dek-review/decisions
runuser -u dek-builder -- test -r /var/spool/dek-build/approved
runuser -u dek-activator -- test -r /var/spool/dek-activate/builds
! runuser -u dek-activator -- test -w /var/spool/dek-activate/builds
runuser -u dek-web -- test -r /var/lib/dek-activate/releases
runuser -u dek-qa -- test -r /var/lib/dek-activate/releases
runuser -u dek-publisher -- test -r /var/lib/dek-activate/outcomes
! runuser -u dek-web -- test -r /var/lib/dek-publisher/secrets/approval-signing-key.pem
! runuser -u dek-builder -- test -r /var/lib/dek-git-auth/git-credentials
! runuser -u dek-source-ingest -- test -r /var/lib/dek-web/secrets/environment
runuser -u dek-builder -- test -x /var/lib/dek-qa/venv/bin/python
! runuser -u dek-builder -- test -r /var/lib/dek-qa/secrets
```

## Checkpoint A3 — exact, digest-addressed code and configuration installs

Never merge package files into an existing `app` tree. Each service receives a newly created `/opt/<service>/versions/$PACKAGE_SHA256` tree containing exactly its manifest-selected package files. The installer reconciles any root-only `/var/lib/dek-install-transactions/install-components.json` before doing new work, then journals the original links and install manifests before its first live mutation. Every file/link rename and journal transition is fsynced. A crash before the durable `committed` record is therefore idempotently rolled back on the next invocation; a crash after it preserves the complete new state. A3 deliberately prepares but does not switch `dek-source-ingest`: its running `app/current` remains byte-for-byte the old version until the evidenced A6 cutover.

```bash
export PACKAGE_SHA256='[PACKAGE_SHA256]'
case "$PACKAGE_SHA256" in (*[!0-9a-f]*|'') exit 1;; esac
test "${#PACKAGE_SHA256}" -eq 64
install -d -o root -g root -m 0700 "$LATEST_BACKUP/ingestion-cutover"
readlink /opt/dek-source-ingest/current > "$LATEST_BACKUP/ingestion-cutover/ingestion-current.before"
readlink /opt/dek-source-ingest/app > "$LATEST_BACKUP/ingestion-cutover/ingestion-app.before"
chmod 0400 "$LATEST_BACKUP/ingestion-cutover/ingestion-current.before" "$LATEST_BACKUP/ingestion-cutover/ingestion-app.before"
python3 -I /run/dek-package-check/deploy/install_components.py \
  --package /run/dek-package-check \
  --manifest /run/dek-package-check/deploy/PACKAGE.sha256 \
  --digest "$PACKAGE_SHA256"
# Equivalent atomic link operations used above: ln -s "versions/$PACKAGE_SHA256" .current.new && mv -Tf .current.new current
for root in /opt/dek-web /opt/dek-qa /opt/dek-review /opt/dek-publisher /opt/dek-builder /opt/dek-activator; do
  test "$(readlink "$root/current")" = "versions/$PACKAGE_SHA256"
  test "$(readlink "$root/app")" = current
  test -f "$root/install-manifest.expected" -a -f "$root/install-manifest.actual"
done
test -d "/opt/dek-source-ingest/versions/$PACKAGE_SHA256"
# Evidence that A3 did not alter the still-running ingestion entry points:
cmp -s <(readlink /opt/dek-source-ingest/current) "$LATEST_BACKUP/ingestion-cutover/ingestion-current.before"
cmp -s <(readlink /opt/dek-source-ingest/app) "$LATEST_BACKUP/ingestion-cutover/ingestion-app.before"
```

Before A3, record `readlink /opt/dek-source-ingest/current` and `readlink /opt/dek-source-ingest/app` into the two named root-only evidence files. `install_components.py` opens `/` and every lexical ancestor through held `dir_fd` descriptors with `O_NOFOLLOW|O_DIRECTORY`; every ancestor and the approved root must be root-owned and not group/other-writable. The journal uses the same strict walk, requires an exact root-owned `0700` final directory, and remains pinned for every journal create/replace/unlink. The only unsafe-ancestor exception is an explicit test-only API argument used by temporary-directory tests; the production CLI has no relaxation. The installer pins the root and `versions` inode for preparation, switching and ordinary-failure rollback, and reopens the identical lexical boundary with the same checks during crash recovery; it never `resolve()`s a supplied root and then writes through the resolved pathname. It first prepares and digest-validates all selected version trees. Its durable transaction restores every entered component's original `current`, `app`, `previous`, `install-manifest.expected`, and `install-manifest.actual` state (including a first-install `legacy-app.before-versioned`) after ordinary failure or next-start reconciliation. The source-ingest component is excluded from live mutation until the single `a6_cutover.py apply` transaction in A6.

Install the three approved JSON configs explicitly. No config may contain a command, origin override, hook, filter, helper, protocol override, or worktree path.

```bash
install -o root -g root -m 0644 /run/dek-package-check/deploy/publisher.json /etc/dek-publisher.json
install -o root -g root -m 0644 /run/dek-package-check/deploy/builder.json /etc/dek-builder.json
install -o root -g root -m 0644 /run/dek-package-check/deploy/activator.json /etc/dek-activator.json
install -o root -g root -m 0400 /root/dek-approved-readiness.json /etc/dek-readiness.json
python3 -m json.tool /etc/dek-publisher.json >/dev/null
python3 -m json.tool /etc/dek-builder.json >/dev/null
python3 -m json.tool /etc/dek-activator.json >/dev/null
python3 -m json.tool /etc/dek-readiness.json >/dev/null
```

## Checkpoint A4 — install unrelated units; retain live QA/ingestion units

```bash
install -o root -g root -m 0644 /run/dek-package-check/deploy/systemd/dek-review.service /etc/systemd/system/dek-review.service
install -o root -g root -m 0644 /run/dek-package-check/deploy/systemd/dek-review-publish.service /etc/systemd/system/dek-review-publish.service
install -o root -g root -m 0644 /run/dek-package-check/deploy/systemd/dek-review-publish-manual.path /etc/systemd/system/dek-review-publish-manual.path
install -o root -g root -m 0644 /run/dek-package-check/deploy/systemd/dek-review-publish-manual.service /etc/systemd/system/dek-review-publish-manual.service
install -o root -g root -m 0644 /run/dek-package-check/deploy/systemd/dek-source-ingest-manual.path /etc/systemd/system/dek-source-ingest-manual.path
install -o root -g root -m 0644 /run/dek-package-check/deploy/systemd/dek-source-ingest-manual.service /etc/systemd/system/dek-source-ingest-manual.service
install -o root -g root -m 0644 /run/dek-package-check/deploy/systemd/dek-builder.service /etc/systemd/system/dek-builder.service
install -o root -g root -m 0644 /run/dek-package-check/deploy/systemd/dek-activator.service /etc/systemd/system/dek-activator.service
systemd-analyze verify /etc/systemd/system/dek-*.service /etc/systemd/system/dek-*.path
systemd-analyze verify /run/dek-package-check/deploy/systemd/dek-qa.service /run/dek-package-check/deploy/systemd/dek-source-ingest.service /run/dek-package-check/deploy/systemd/dek-source-ingest-proof.service /run/dek-package-check/deploy/systemd/dek-source-ingest.timer /run/dek-package-check/deploy/systemd/dek-source-ingest-alert@.service
systemctl disable --now dek-review-publish-manual.path dek-source-ingest-manual.path
# Deliberately do not overwrite, stop, disable, mask, reload, or restart the live
# QA/source-ingest units here. Their old bytes and loaded state continue unchanged
# until the single journaled A6 transaction switches paths and unit contents together.
```

## Checkpoint A5 — seed immutable content without cutover

Place one independently built and verified static release under `/var/lib/dek-activate/releases/[GENERATION]`. It may contain only generated HTML/CSS/JS/static assets, `dek-kb.json`, signed release metadata and its signature. Verify all recorded commit, tree, full bundle and artifact digests; directories are `0550`, files `0440`, owned `dek-activator:dek-release-read`. Create the sibling lock file `release.lock` (empty, mode `0440`, owned `dek-activator:dek-release-read`) inside the release directory — the Web service opens and holds a shared `flock` on it for the lifetime of every response and `cleanup()` skips any release whose lock is held, so the seeded release must carry the lock before Web/QA start. Write the complete `active.json` to a sibling temporary file, fsync it, rename it, then fsync the control directory. Do not restart Web or QA.

```bash
export DEK_INITIAL_RELEASE='[ABSOLUTE_OFFLINE_RELEASE_DIRECTORY]'
export DEK_INITIAL_COMMIT='[40_OR_64_HEX_COMMIT]'
export DEK_INITIAL_TREE='[40_OR_64_HEX_TREE]'
export DEK_INITIAL_BUNDLE_SHA256='[64_HEX_SHA256]'
test -d "$DEK_INITIAL_RELEASE" && test ! -L "$DEK_INITIAL_RELEASE"
test -f "$DEK_INITIAL_RELEASE/repository.bundle" && test -f "$DEK_INITIAL_RELEASE/release.json" && test -f "$DEK_INITIAL_RELEASE/release.sig"
git bundle verify "$DEK_INITIAL_RELEASE/repository.bundle"
runuser -u dek-activator -- env -i PATH=/usr/bin:/bin python3 -I /run/dek-package-check/deploy/seed_release.py --release "$DEK_INITIAL_RELEASE" --public-key /etc/dek-approval-public-key.pem --releases /var/lib/dek-activate/releases --active /var/lib/dek-activate/control/active.json --expected-commit "$DEK_INITIAL_COMMIT" --expected-tree "$DEK_INITIAL_TREE" --expected-bundle-sha256 "$DEK_INITIAL_BUNDLE_SHA256"
test "$(stat -c '%U:%G:%a' /var/lib/dek-activate/control/active.json)" = dek-activator:dek-release-read:640
```

`seed_release.py` creates `active.json` only when it is absent. A retry succeeds only when the existing bytes and parsed descriptor exactly equal the canonical seed descriptor (including sequence 1, generation, provenance, and every field); any difference fails closed before generation mutation, with no downgrade path. Before any install, it computes the highest committed sequence across **all** state sources — the existing `active.json`, every `releases/*/release.json`, every `journal/*.json`, every `spent/*.json`, and every `outcomes/*.json` — and fails closed if any committed sequence exceeds the seed's sequence 1; only a virgin state (or an exact prior sequence-1 seed) may create or replay sequence 1. It verifies the offline Ed25519 `release.sig`, exact commit/tree/bundle bindings, complete artifact inventory and entry types on the source, copies to private staging, then repeats the complete signature/type/inventory/digest validation on staging before fsync and atomic `os.replace`. It finally creates the active descriptor atomically and durably. Every bracketed value is a mandatory approved input; no network lookup or inferred value is allowed.

## Checkpoint A5b — pinned QA/Hermes and main-origin review routing

`[QA_HERMES_LOCK]`, `[QA_WHEELHOUSE]`, `[QA_HERMES_WHEEL]`, `[QA_HERMES_WHEEL_SHA256]`, and `[QA_ENVIRONMENT_JSON]` are independently approved inputs. The environment JSON is the exact canonical package/version/direct-source summary produced by `verify_qa_environment.py`; its own approved digest is checked out of band. Live package resolution and modification of the running venv/profile are prohibited. The venv and migrated profile remain below a package-specific, isolated staging root throughout A5b; creating either candidate below `/var/lib/dek-qa` is prohibited.

```bash
test -x /usr/bin/python3.12
test -f '[QA_HERMES_LOCK]' && printf '%s  %s\n' '[QA_HERMES_LOCK_SHA256]' '[QA_HERMES_LOCK]' | sha256sum -c -
test -d '[QA_WHEELHOUSE]' && test -f '[QA_HERMES_WHEEL]' && test -f '[QA_ENVIRONMENT_JSON]'
printf '%s  %s\n' '[QA_HERMES_WHEEL_SHA256]' '[QA_HERMES_WHEEL]' | sha256sum -c -
STAGE_ROOT=/var/lib/dek-stage-a/$PACKAGE_SHA256
test ! -e "$STAGE_ROOT"
install -d -o root -g root -m 0711 /var/lib/dek-stage-a "$STAGE_ROOT"
install -d -o dek-qa -g dek-qa -m 0700 "$STAGE_ROOT/qa"
QA_CANDIDATE="$STAGE_ROOT/qa/venv"
runuser -u dek-qa -- /usr/bin/python3.12 -m venv "$QA_CANDIDATE"
runuser -u dek-qa -- "$QA_CANDIDATE/bin/python" -m pip install --no-index --find-links '[QA_WHEELHOUSE]' --require-hashes -r '[QA_HERMES_LOCK]'
runuser -u dek-qa -- "$QA_CANDIDATE/bin/python" -m pip install --no-deps '[QA_HERMES_WHEEL]'
runuser -u dek-qa -- "$QA_CANDIDATE/bin/python" -m pip check
runuser -u dek-qa -- "$QA_CANDIDATE/bin/python" -I /run/dek-package-check/deploy/verify_qa_environment.py --expected '[QA_ENVIRONMENT_JSON]' --hermes-source '[QA_HERMES_WHEEL]'
test -x "$QA_CANDIDATE/bin/hermes"

# Migrate the existing protected production profile; never copy qa/config/config.yaml.
# qa_profile.py preserves nonempty allowed_chats, require_mention=true, all required
# session reset/isolation fields, secrets and unknown non-tool keys. It replaces the
# complete tool surface: platform_toolsets.dingtalk=[], tool_search=off, all other
# platform adapters disabled across platforms.*, top-level aliases, gateway.<platform>
# and gateway.platforms.*, nested tool-routing keys removed, and exactly one sanitized
# dek_kb MCP definition. Non-tool secret fields remain but cannot enable a platform.
QA_PROFILE="$STAGE_ROOT/qa/profile"
install -d -o dek-qa -g dek-qa -m 0700 "$QA_PROFILE"
runuser -u dek-qa -- env -i HOME=/var/lib/dek-qa PATH=/usr/bin:/bin \
  "$QA_CANDIDATE/bin/python" -I /run/dek-package-check/deploy/qa_profile.py \
  --source /var/lib/dek-qa/hermes/profiles/dek-qa/config.yaml \
  --target "$QA_PROFILE/config.yaml" \
  --python-executable "/var/lib/dek-qa/venvs/$PACKAGE_SHA256/bin/python" \
  --skip-validation
# Full validation is deliberately deferred to A6, where systemd loads the formal
# EnvironmentFile and qa_profile.py rebinds only the two candidate execution paths
# in a private temporary copy. The staged profile itself retains final live paths.
runuser -u dek-qa -- env -i PYTHONPATH=/opt/dek-qa/app PATH=/usr/bin:/bin \
  "$QA_CANDIDATE/bin/python" -m qa.dek_qa.mcp_server --help >/dev/null
openssl x509 -in /etc/letsencrypt/live/regkb.chenponai.com/fullchain.pem -noout -checkend 86400
cmp -s <(openssl x509 -in /etc/letsencrypt/live/regkb.chenponai.com/fullchain.pem -pubkey -noout 2>/dev/null) <(openssl pkey -in /etc/letsencrypt/live/regkb.chenponai.com/privkey.pem -pubout 2>/dev/null)
install -d -o root -g root -m 0700 "$STAGE_ROOT/nginx"
install -o root -g root -m 0644 /run/dek-package-check/deploy/nginx/dek-review-location.conf "$STAGE_ROOT/nginx/dek-review-location.conf"
install -o root -g root -m 0644 /run/dek-package-check/deploy/nginx/regkb.chenponai.com "$STAGE_ROOT/nginx/regkb.chenponai.com"
install -o root -g root -m 0644 /run/dek-package-check/deploy/nginx/dek-review.conf "$STAGE_ROOT/nginx/dek-review.conf"
cmp -s "$STAGE_ROOT/nginx/dek-review-location.conf" /run/dek-package-check/deploy/nginx/dek-review-location.conf
cmp -s "$STAGE_ROOT/nginx/regkb.chenponai.com" /run/dek-package-check/deploy/nginx/regkb.chenponai.com
cmp -s "$STAGE_ROOT/nginx/dek-review.conf" /run/dek-package-check/deploy/nginx/dek-review.conf
```

## Checkpoint A6 — cut over and prove dynamic readers

First capture the still-loaded old ingestion state without changing it. Then, while every live link, service and timer remains untouched, pass the credential gate, the real Hermes parser/adapter/discovery/model assembly and a full ingestion run from the package candidate. QA candidates and every ingestion output remain under `STAGE_ROOT`; the transient ingestion namespace bind-mounts those private directories over the fixed production paths expected by the entrypoint, so neither proofs nor review input reach live storage. `--pre-cutover-proof` is dispatched into the candidate CLI as `scheduled-run --no-publication` before any source fetch begins; that inner mode returns immediately after its atomic candidate writes and cannot reach Git add/commit/push or bundle publication. The outer entrypoint returns before its own publication path as a second invariant, not as the primary guard. The production unit omits both flags and retains the formal commit/push/bundle behavior. Any failure before the explicit cutover boundary leaves live state unchanged.

```bash
STAGE_ROOT=/var/lib/dek-stage-a/$PACKAGE_SHA256
QA_CANDIDATE="$STAGE_ROOT/qa/venv"
QA_PROFILE="$STAGE_ROOT/qa/profile"
QA_ACCEPT_STATE="$STAGE_ROOT/qa/acceptance-state"
QA_TMP="$QA_ACCEPT_STATE/tmp"
test -x "$QA_CANDIDATE/bin/python" -a -f "$QA_PROFILE/config.yaml"
install -d -o dek-qa -g dek-qa -m 0700 "$QA_ACCEPT_STATE" "$QA_TMP"
install -d -o root -g root -m 0700 "$LATEST_BACKUP/ingestion-cutover"
systemctl show dek-source-ingest.timer -p LoadState -p ActiveState -p UnitFileState -p LastTriggerUSec -p NextElapseUSecRealtime > "$LATEST_BACKUP/ingestion-cutover/old.timer.properties"
systemctl show dek-source-ingest.timer -p LoadState -p ActiveState -p UnitFileState > "$LATEST_BACKUP/ingestion-cutover/old.timer.contract"
systemctl status dek-source-ingest.service --no-pager > "$LATEST_BACKUP/ingestion-cutover/old.service.status" || test "$?" -eq 3
test "$(systemctl show dek-source-ingest.timer -p LoadState --value)" = loaded

# The strict parser accepts only EnvironmentFile syntax whose value bytes are
# provably identical after systemd loading. It rejects quoting, backslashes,
# continuation, export, duplicates, ambiguous whitespace/comments and unknown keys.
python3 -I - /run/dek-package-check/deploy/credential_gate.py /var/lib/dek-qa/secrets/environment <<'PY'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('dek_credential_gate',sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module._environment(pathlib.Path(sys.argv[2]),'qa')
PY

# systemd loads the exact formal EnvironmentFile. qa_profile.py parses the
# production profile and rebinds its MCP executable/PYTHONPATH plus every writable
# runtime output (including generation-proof) below STAGE_ROOT, then runs real load_gateway_config(), DingTalkAdapter,
# discover_mcp_tools() and get_tool_definitions().
# The same process must print `candidate write confinement probe passed` after
# creation attempts in /tmp, /run and both live QA roots are denied, before assembly.
systemd-run --quiet --wait --collect --service-type=oneshot \
  --unit=dek-qa-a6-tool-acceptance --uid=dek-qa --gid=dek-qa \
  --property=SupplementaryGroups=dek-release-read \
  --property=WorkingDirectory="$STAGE_ROOT/qa" \
  --property=Environment=HOME="$QA_ACCEPT_STATE/home" \
  --property=Environment=HERMES_HOME="$QA_PROFILE" \
  --property=Environment=HERMES_ENV=/var/lib/dek-qa/secrets/environment \
  --property=Environment=HERMES_DISABLE_LAZY_INSTALLS=1 \
  --setenv=TMPDIR="$QA_TMP" \
  --property=EnvironmentFile=/var/lib/dek-qa/secrets/environment \
  --property=ReadWritePaths="$STAGE_ROOT/qa" \
  --property=ReadOnlyPaths=/tmp /run /var/lib/dek-qa /var/lib/dek-activate /opt/dek-qa \
  --property=InaccessiblePaths=/run/dek-proofs \
  --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=PrivateTmp=yes \
  --property=UMask=0077 --property=NoNewPrivileges=yes \
  "$QA_CANDIDATE/bin/python" -I /run/dek-package-check/deploy/qa_profile.py \
  --validate-existing "$QA_PROFILE/config.yaml" \
  --environment-file /var/lib/dek-qa/secrets/environment \
  --forbid-write-root /tmp --forbid-write-root /run \
  --forbid-write-root /var/lib/dek-qa --forbid-write-root /opt/dek-qa \
  --runtime-python-executable "$QA_CANDIDATE/bin/python" \
  --runtime-pythonpath /run/dek-package-check \
  --runtime-state-root "$QA_ACCEPT_STATE"

# Run the package ingestion candidate in an isolated mount namespace. The
# fixed in-namespace proof/review paths map only to package-specific staging.
install -d -o dek-source-ingest -g dek-source-ingest -m 0700 \
  "$STAGE_ROOT/ingestion" "$STAGE_ROOT/ingestion/clones" \
  "$STAGE_ROOT/ingestion/proofs" "$STAGE_ROOT/ingestion/review-input"
STAGED_REPORT="$STAGE_ROOT/ingestion/proofs/staged-proof-report.json"
STAGED_EXPECTED="$STAGE_ROOT/ingestion/proofs/staged-proof-report.expected.json"
test ! -e "$STAGED_REPORT" -a ! -e "$STAGED_EXPECTED"
systemd-run --quiet --wait --collect --service-type=oneshot \
  --unit=dek-source-ingest-a6-candidate --uid=dek-source-ingest --gid=dek-source-ingest \
  --property=WorkingDirectory=/var/empty/dek-source-ingest \
  --property=Environment=DEK_FIXED_ORIGIN=https://github.com/chenponsh/dek.git \
  --property=LoadCredential=git-credentials:/var/lib/dek-git-auth/git-credentials \
  --property=Environment=DEK_GIT_CREDENTIAL_FILE=%d/git-credentials \
  --property=Environment=PYTHONUNBUFFERED=1 \
  --property=Environment=NO_PROXY=www.chp.org.cn,chp.org.cn \
  --property=Environment=no_proxy=www.chp.org.cn,chp.org.cn \
  --property=BindPaths="$STAGE_ROOT/ingestion/proofs:/var/lib/dek-source-ingest/proofs" \
  --property=BindPaths="$STAGE_ROOT/ingestion/review-input:/var/lib/dek-review/input" \
  --property=ReadOnlyPaths=/run/dek-package-check \
  --property=ReadWritePaths="$STAGE_ROOT/ingestion/clones" \
  --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=PrivateTmp=yes --property=PrivateDevices=yes \
  --property=NoNewPrivileges=yes --property=CapabilityBoundingSet= \
  /usr/bin/xvfb-run -a -s '-screen 0 1365x768x24 -nolisten tcp' \
  /usr/bin/python3 -I /run/dek-package-check/deploy/source_ingest_entrypoint.py \
  --package-root /run/dek-package-check --pre-cutover-proof \
  --isolated-clone "$STAGE_ROOT/ingestion/clones" \
  --origin https://github.com/chenponsh/dek.git \
  --proof-output /var/lib/dek-source-ingest/proofs/staged-proof-report.json \
  --expected-output /var/lib/dek-source-ingest/proofs/staged-proof-report.expected.json scheduled-run
test "$(stat -c '%U:%G:%a:%h:%F' "$STAGED_REPORT")" = 'dek-source-ingest:dek-source-ingest:600:1:regular file'
test "$(stat -c '%U:%G:%a:%h:%F' "$STAGED_EXPECTED")" = 'dek-source-ingest:dek-source-ingest:600:1:regular file'
python3 - "$STAGED_REPORT" "$STAGED_EXPECTED" <<'PY'
import json, pathlib, sys
from datetime import datetime, timedelta, timezone
report=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
expected=json.loads(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8'))
parse=lambda value: datetime.fromisoformat(value[:-1]+'+00:00') if isinstance(value,str) and value.endswith('Z') else (_ for _ in ()).throw(ValueError())
started=parse(expected['started_at']); generated=parse(report['generated_at']); now=datetime.now(timezone.utc)
if (expected.get('proof_output')!='/var/lib/dek-source-ingest/proofs/staged-proof-report.json'
        or report.get('run_nonce')!=expected.get('run_nonce')
        or report.get('mode')!='scheduled-run' or not started<=generated<=now+timedelta(minutes=5)
        or generated-started>timedelta(hours=24)):
    raise SystemExit('invalid staged ingestion invocation binding')
PY
sha256sum "$STAGED_REPORT" > "$LATEST_BACKUP/ingestion-cutover/staged-proof-report.sha256"

# LIVE CUTOVER BEGINS — every candidate gate above has passed. One tested,
# persistent transaction now owns every QA/ingestion path, unit and service change.
# Every journal transition and rename is fsynced. An interruption leaves exactly
# one recovery command; a new apply refuses the mixed state until recovery succeeds.
# Before the first stop, A6 captures UnitFileState, ActiveState, SubState and
# FragmentPath. enabled[-runtime], disabled and masked[-runtime] are reconstructed
# with explicit unmask/disable/enable/mask commands; reconstructable stable active
# and inactive states use reset-failed/start/stop and are then compared exactly.
# Any original contract with ActiveState=failed or SubState=failed is rejected:
# reset-failed + start creates a new run and cannot reproduce the old failure.
# static, indirect and alias remain intrinsic to restored unit bytes and are verified.
# linked[-runtime], generated, transient, bad, and transitional activity are
# captured but cannot be safely preserved across this forward unit replacement,
# so apply fails closed before the first service mutation. The runbook therefore
# does not claim that those pre-cutover states are forward-reconstructable.
install -d -o dek-qa -g dek-qa -m 0700 /var/lib/dek-qa/venvs /var/lib/dek-qa/hermes/profile-versions
test ! -e "/var/lib/dek-qa/venvs/$PACKAGE_SHA256"
test ! -e "/var/lib/dek-qa/hermes/profile-versions/$PACKAGE_SHA256"
test "$(findmnt -n -o SOURCE -T "$QA_CANDIDATE")" = "$(findmnt -n -o SOURCE -T /var/lib/dek-qa/venvs)"
test "$(findmnt -n -o SOURCE -T "$QA_PROFILE")" = "$(findmnt -n -o SOURCE -T /var/lib/dek-qa/hermes/profile-versions)"
python3 -I /run/dek-package-check/deploy/a6_cutover.py apply \
  --digest "$PACKAGE_SHA256" --stage-root "$STAGE_ROOT" \
  --package-root /run/dek-package-check \
  --journal-dir /var/lib/dek-install-transactions
test "$(readlink /opt/dek-source-ingest/current)" = "versions/$PACKAGE_SHA256"
# The accepted result proves the only enabled adapter is `dingtalk` and exactly these three
# model-visible tools (Hermes namespace included):
# mcp__dek_kb__dek_kb_search
# mcp__dek_kb__dek_kb_get
# mcp__dek_kb__dek_kb_recent
systemctl is-active --quiet dek-qa.service
```

Make two authenticated Web requests and one QA request; prove all identify the seeded sequence/nonce/generation/commit/tree/bundle/artifact digests. Web must observe an atomic `active.json` change on its next request without restart. QA must swap only after loading and validating the complete index. Leave review and every automation timer disabled.

Before accepting QA, record two real DingTalk delivery receipts (timestamps, sender enterprise IDs, conversation IDs and gateway result; never message bodies or credentials). `[AUTHORIZED_QA_TESTER]` must be visible to the DingTalk application and send an **authorized message** that receives a QA answer containing the active generation proof. `[UNAUTHORIZED_QA_TESTER]` must be outside application visibility/the approved audience and send an **unauthorized message** that receives no QA answer. Confirm gateway logs bind both receipts to those exact IDs and outcomes. A synthetic HTTP probe, configuration inspection, or reusing one identity for both cases is not acceptance; any missing/ambiguous receipt keeps Stage A blocked.

If the new ingestion proof fails, do not enable the new timer and do not continue. The same tested transaction journal restores every QA/ingestion live link, candidate location, unit byte/mode/link, and exact UnitFileState/ActiveState/SubState contract. Every recovery mutation is target-state idempotent and durably recorded; this includes the legacy-directory rename intent recorded before the rename can occur. Recovery verifies the complete pre-cutover tree manifest, all link targets, and the service contract, fsyncs an `a6-cutover.recovered.json` transaction marker, then durably deletes its journal. The same one-command recovery may be repeated safely for that transaction after any interruption, including after journal deletion; a missing journal without its matching durable marker is not success. Any mixed or unverifiable result fails closed and escalates to Exact manual rollback.

An A6 journal written by an older package may already contain a pre-cutover contract that cannot be recreated exactly. Before any unit file, live link, journal recovery step, daemon-reload, or systemctl call, `recover` runs both the pure unit-file restore-plan and activity restore-plan validation for every saved service. Unknown/`bad` UnitFileState values, linked/linked-runtime entries without a canonical trusted absolute FragmentPath, and unreconstructable activity (including any `ActiveState=failed` or `SubState=failed`) all emit the same non-sensitive stable diagnostic `Exact manual rollback required: A6 journal contains an unreconstructable systemd restore contract`. A canonical absolute linked FragmentPath remains recoverable, including internationalized path components when strict UTF-8 and host-filesystem encoding both round-trip losslessly. Missing, relative, non-normalized, empty-component, `.` or `..` paths, NUL/C0/C1 controls, Unicode surrogates, and any path that cannot round-trip through `os.fsencode`/`os.fsdecode` are not trusted. On rejection it exits without any further unit, link, daemon-reload, or service mutation; the journal bytes and all live trees remain unchanged and no systemctl call is made. Do not use `reset-failed + start` to imitate the historical failure; preserve the journal and follow Exact manual rollback.

```bash
python3 -I /run/dek-package-check/deploy/a6_cutover.py recover \
  --digest "$PACKAGE_SHA256" --stage-root "$STAGE_ROOT" \
  --package-root /run/dek-package-check \
  --journal-dir /var/lib/dek-install-transactions
```

Restart (but do not enable) the review service so an already-running Stage-1 process loads the candidate code, then run the readiness check with the exact approved DNS/IP, TLS and OAuth callback facts (no `--write-marker`; this is a pure network check that fails closed into the rollback path below). Only once it passes should you independently exercise one real allowlisted login and one real denied login. Then write the single automation-ready marker, passing `--confirm-authorized-login --confirm-unauthorized-login` to attest both exercises were just performed; the writer reruns the same DNS/TLS/OAuth check and refuses to write unless both flags are given. The actual online exercises remain a deployment-time BLOCK gate and must never be fabricated from this repository.

The written automation marker is an authenticated, fixed seven-day lease. Its marker HMAC covers the configuration HMAC, both login confirmation flags, UTC `issued_at`, and UTC `expires_at`. The publisher, builder, activator, and source-ingest units re-check that HMAC, the fixed validity interval, future timestamps, and expiry against current UTC in every `ExecCondition`; write-time validation alone is not trusted. A marker with omitted, extended, or edited validity, or with either confirmation flag missing, fails closed.

For long-running automation, schedule a complete reissue at least 24 hours before expiry. Reissue means repeating the readiness check, both real login exercises, and the marker write/validate steps below; never edit timestamps or extend `expires_at`. If the lease expires, enabled timers may continue firing but their services remain skipped by `ExecCondition`; repeat the complete reissue procedure to restore eligibility. There is no permanent-marker or grace-period override.

```bash
NGINX_WORKER_USER='[NGINX_WORKER_USER]'
NGINX_WORKER_UID='[NGINX_WORKER_UID]'
NGINX_WORKER_GID='[NGINX_WORKER_GID]'
REVIEW_WAS_ACTIVE="$(systemctl is-active dek-review.service || :)"
REVIEW_WAS_SUBSTATE="$(systemctl show dek-review.service -p SubState --value)" || exit 1
case "$REVIEW_WAS_ACTIVE:$REVIEW_WAS_SUBSTATE" in active:running|inactive:dead) :;; *) exit 1;; esac
if ! (
  test "$(id -u "$NGINX_WORKER_USER")" = "$NGINX_WORKER_UID" || exit 1
  test "$(id -g "$NGINX_WORKER_USER")" = "$NGINX_WORKER_GID" || exit 1
  usermod -a -G dek-review-proxy "$NGINX_WORKER_USER" || exit 1
  id -nG "$NGINX_WORKER_USER" | tr ' ' '\n' | grep -Fx dek-review-proxy >/dev/null || exit 1
  systemctl restart dek-review.service || exit 1
  runuser -u "$NGINX_WORKER_USER" -- curl --fail --silent --show-error --unix-socket /run/dek-review/dek-review.sock http://localhost/__ready >/dev/null || exit 1
  install -o root -g root -m 0644 "$STAGE_ROOT/nginx/dek-review-location.conf" /etc/nginx/snippets/.dek-review-location.conf.new || exit 1
  mv -Tf /etc/nginx/snippets/.dek-review-location.conf.new /etc/nginx/snippets/dek-review-location.conf || exit 1
  install -o root -g root -m 0644 "$STAGE_ROOT/nginx/regkb.chenponai.com" /etc/nginx/sites-available/.regkb.chenponai.com.new || exit 1
  mv -Tf /etc/nginx/sites-available/.regkb.chenponai.com.new /etc/nginx/sites-available/regkb.chenponai.com || exit 1
  install -o root -g root -m 0644 "$STAGE_ROOT/nginx/dek-review.conf" /etc/nginx/sites-available/.dek-review.conf.new || exit 1
  mv -Tf /etc/nginx/sites-available/.dek-review.conf.new /etc/nginx/sites-available/dek-review.conf || exit 1
  ln -s /etc/nginx/sites-available/dek-review.conf /etc/nginx/sites-enabled/.dek-review.conf.new || exit 1
  mv -Tf /etc/nginx/sites-enabled/.dek-review.conf.new /etc/nginx/sites-enabled/dek-review.conf || exit 1
  ln -s /etc/nginx/sites-available/regkb.chenponai.com /etc/nginx/sites-enabled/.regkb.chenponai.com.new || exit 1
  mv -Tf /etc/nginx/sites-enabled/.regkb.chenponai.com.new /etc/nginx/sites-enabled/regkb.chenponai.com || exit 1
  nginx -t || exit 1
  systemctl reload nginx || exit 1
  systemctl is-active --quiet nginx || exit 1
  python3 -I /run/dek-package-check/deploy/readiness.py --config /etc/dek-readiness.json || exit 1
); then
  if ! python3 -I /run/dek-package-check/deploy/rollback.py "$LATEST_BACKUP" --sha256sums /root/dek-backup-SHA256SUMS --old-web-unit '[OLD_WEB_UNIT]' --old-qa-unit '[OLD_QA_UNIT]' --old-proof-command /root/dek-verify-old-content; then
    printf '%s\n' 'Exact manual rollback required: review route rollback failed' >&2
    exit 1
  fi
  if test "$REVIEW_WAS_ACTIVE" = active; then
    systemctl restart dek-review.service || exit 1
  else
    systemctl stop dek-review.service || exit 1
  fi
  test "$(systemctl show dek-review.service -p ActiveState --value)" = "$REVIEW_WAS_ACTIVE" || exit 1
  test "$(systemctl show dek-review.service -p SubState --value)" = "$REVIEW_WAS_SUBSTATE" || exit 1
  nginx -t || exit 1
  systemctl reload nginx || exit 1
  systemctl is-active --quiet nginx || exit 1
  exit 1
fi
# Perform the approved real login tests before continuing, then write and re-validate the marker:
python3 -I /run/dek-package-check/deploy/readiness.py --config /etc/dek-readiness.json --hmac-key /var/lib/dek-readiness/marker-hmac-key --write-marker /var/lib/dek-readiness/automation-ready --confirm-authorized-login --confirm-unauthorized-login || exit 1
python3 -I /run/dek-package-check/deploy/readiness.py --config /etc/dek-readiness.json --hmac-key /var/lib/dek-readiness/marker-hmac-key --validate-marker --marker /var/lib/dek-readiness/automation-ready || exit 1

# The production unit is started only after its ExecCondition can validate the marker.
# A fresh private expected-output binding and nonce make this proof specific to this invocation.
FINAL_REPORT=/var/lib/dek-source-ingest/proofs/latest-report.json
FINAL_EXPECTED=/var/lib/dek-source-ingest/proofs/latest-report.expected.json
test ! -e "$FINAL_REPORT" -a ! -e "$FINAL_EXPECTED" || exit 1
systemctl start dek-source-ingest.service || exit 1
test "$(systemctl show dek-source-ingest.service -p Result --value)" = success || exit 1
systemctl status dek-source-ingest.service --no-pager > "$LATEST_BACKUP/ingestion-cutover/new.service.status" || test "$?" -eq 3 || exit 1
test "$(stat -c '%U:%G:%a:%h:%F' "$FINAL_REPORT")" = 'dek-source-ingest:dek-source-ingest:600:1:regular file' || exit 1
test "$(stat -c '%U:%G:%a:%h:%F' "$FINAL_EXPECTED")" = 'dek-source-ingest:dek-source-ingest:600:1:regular file' || exit 1
python3 - "$FINAL_REPORT" "$FINAL_EXPECTED" <<'PY' || exit 1
import json, pathlib, sys
from datetime import datetime, timedelta, timezone
report=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
expected=json.loads(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8'))
parse=lambda value: datetime.fromisoformat(value[:-1]+'+00:00') if isinstance(value,str) and value.endswith('Z') else (_ for _ in ()).throw(ValueError())
started=parse(expected['started_at']); generated=parse(report['generated_at']); now=datetime.now(timezone.utc)
if (expected.get('proof_output')!=sys.argv[1] or report.get('run_nonce')!=expected.get('run_nonce')
        or report.get('mode')!='scheduled-run' or not started<=generated<=now+timedelta(minutes=5)
        or generated-started>timedelta(hours=24)):
    raise SystemExit('invalid production ingestion invocation binding')
PY
sha256sum "$FINAL_REPORT" > "$LATEST_BACKUP/ingestion-cutover/new.report.sha256" || exit 1
python3 -I /run/dek-package-check/deploy/a6_cutover.py finalize \
  --digest "$PACKAGE_SHA256" --stage-root "$STAGE_ROOT" \
  --package-root /run/dek-package-check \
  --journal-dir /var/lib/dek-install-transactions || exit 1
systemctl enable --now dek-source-ingest.timer || exit 1
systemctl is-active --quiet dek-source-ingest.timer || exit 1
systemctl enable --now dek-review.service dek-review-publish-manual.path dek-source-ingest-manual.path || exit 1
for unit in dek-review.service dek-review-publish-manual.path dek-source-ingest-manual.path; do
  systemctl is-active --quiet "$unit" || exit 1
  test "$(systemctl show "$unit" -p UnitFileState --value)" = enabled || exit 1
done
```

There is deliberately no periodic timer for `dek-builder.service`/`dek-review-publish.service`/`dek-activator.service`: nothing publishes automatically once a decision is approved. A reviewer must click "发布已批准内容" in the review UI, which writes `/var/lib/dek-review/state/publish-trigger-requested`; `dek-review-publish-manual.path` (verified active/enabled above) is what turns that marker into `systemctl start --wait` of builder, then publisher, then activator, in that order, each still gated by its own `ExecCondition` readiness-marker check. `dek-source-ingest-manual.path` is the same mechanism for the reviewer-facing "立即拉取最新源" button, independent of this publish chain.

`finalize` first verifies the live tree, links and exact service contract, then fsyncs `a6-cutover.finalized.json` with the transaction and layout identities before it durably unlinks the journal. Repeating `finalize` after either boundary is safe and succeeds only when that matching marker exists; a never-started, foreign-layout or corrupt marker is rejected.

## Exact manual rollback

This is mandatory when `a6_cutover.py recover` reports `Exact manual rollback required`, including for a historical unreconstructable unit-file or activity contract; do not attempt to synthesize that state with systemctl. Verify the external, read-only `/root/dek-backup-SHA256SUMS`; never trust an in-directory `sha256sum -c SHA256SUMS`. Before stopping a service or performing the first destructive file restore, `rollback.py` parses the complete enablement snapshot and builds every restore command through the same tested `unit_file_restore_plan` used by A6; a missing/unsafe linked FragmentPath, unknown state, or non-reconstructable generated/transient state fails closed without a mutating systemctl call or filesystem mutation. Stop and disable every Stage B timer, resident service, and running oneshot, and confirm all are inactive before the first restore write; `rollback-inventory.txt` contains paths, never unit names. Before extraction, `rollback.py` opens trusted parents without following symlinks, atomically moves aside and removes every inventory root, including roots that existed at backup time; this removes new package/version/clone/proof/report descendants. It then extracts `$LATEST_BACKUP/files.tar` from `/` with numeric owners, xattrs and ACLs and verifies exact path/type/mode/owner/content/link equivalence with `tree-manifest.json`. Restore `/etc/passwd`, `/etc/group`, `/etc/shadow`, and `/etc/gshadow` from the archive rather than inferring accounts. After `systemctl daemon-reload`, restore every reconstructable `dek-*` UnitFileState exactly from `unit-enablement.before`: each mutable state first clears persistent and runtime masks, enablement, and links, then establishes only the recorded target scope. Thus `enabled-runtime`, `masked-runtime`, and `linked-runtime` cannot be shadowed by a persistent state, while persistent enabled/masked/linked states cannot retain a runtime override. Disabled units are explicitly disabled in both scopes; intrinsic `static`, `indirect`, and `alias` states are preserved from restored unit files rather than forced to disabled. Restore `[OLD_WEB_PATH]`, `[OLD_QA_PATH]`, `[OLD_WEB_UNIT]`, and `[OLD_QA_UNIT]`, restart only those two old units, and verify their old content. If any digest, exact inventory entry, identity, ownership, enablement, or proof differs, stop and use the independent host backup.

```bash
cd "$LATEST_BACKUP" && sha256sum -c /root/dek-backup-SHA256SUMS
python3 - "$LATEST_BACKUP" /root/dek-backup-SHA256SUMS <<'PY'
import os, pathlib, stat, sys
for supplied in sys.argv[1:]:
    path=pathlib.Path(supplied).resolve(strict=True)
    for candidate in (path, *path.parents[:-1]):
        info=candidate.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit(f"non-root-writable ancestor: {candidate}")
    if path.is_file() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_uid != 0):
        raise SystemExit(f"not a root-owned regular file: {path}")
root=pathlib.Path(sys.argv[1]).resolve(strict=True)
for name in ('files.tar','backup-inventory.txt','rollback-inventory.txt','existing.nul','absent.txt','unit-enablement.before','tree-manifest.json'):
    item=root/name; info=item.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit(f"not a root-owned regular file: {item}")
PY
# rollback.py revalidates the external digest and archive immediately before archive extraction.
python3 -I /run/dek-package-check/deploy/rollback.py "$LATEST_BACKUP" --sha256sums /root/dek-backup-SHA256SUMS --old-web-unit '[OLD_WEB_UNIT]' --old-qa-unit '[OLD_QA_UNIT]' --old-proof-command /root/dek-verify-old-content
nginx -t
systemctl reload nginx
systemctl is-active --quiet nginx
```

The packaged `rollback.py` first requires EUID 0 and validates its own inode as a root-owned, mode `0644`, single-link regular file; its directory and every ancestor, the `0700` backup directory, and every ancestor of the external `0400` digest anchor must be root-owned and not group/world writable. It opens each path component and backup member with `openat`-style `dir_fd` operations plus `O_NOFOLLOW`, holds those descriptors through validation and use, and rehashes the held inodes at the last possible point before extraction. This narrows pathname-replacement TOCTOU rather than claiming to eliminate all host-level races. It validates the normalized, non-overlapping archive before issuing the only permitted extraction form, `tar --xattrs --acls --numeric-owner -xpf`, verifies the external digest anchor (including the JSON `unit-enablement.before` snapshot with each UnitFileState and FragmentPath, plus `tree-manifest.json`), rejects extra, duplicate, absolute, dot-segment, hardlink, device and FIFO members, and accepts a symlink only when its lexical target remains inside an approved inventory root (while rejecting any later member below that symlink). This matches GNU tar's non-dereferencing backup semantics without permitting an archive escape. It computes the entire exact enablement restore plan before the first service or filesystem mutation, stops and confirms inactive all Stage B units, validates exact absolute inventory paths, removes every inventory root through held trusted-parent descriptors before extraction, restores `files.tar` including account databases and previous nginx/TLS files, verifies exact filesystem equivalence to the recorded tree manifest, restores each `dek-*` state from `unit-enablement.before`, daemon-reloads, then re-reads every recorded unit with `systemctl is-enabled` and fails closed with a difference report if any unit's effective persistent/runtime enablement does not exactly match the snapshot, restarts both old units, and requires the supplied old-content proof to succeed. All bracketed values and the proof executable are mandatory inputs checked before mutation.

## Pre-deployment online acceptance — BLOCK until executed

Package tests do not prove target-host UID/GID membership, ACL/LSM/mount namespaces, credential ownership, DNS/TLS/OAuth, real allowlisted login, reboot enablement restoration, or slow-request generation retention. On the real host test every row of `DAC_MATRIX.tsv`: producer write, consumer read and denied write, and unrelated-account denied read/write; test every secret as readable only by its named service. Record commands, identities and exit status without credential contents. Until those positive and negative probes plus DNS/TLS/OAuth and live Web+QA generation proofs are independently captured, deployment status is **BLOCK**.

## Stage B invariant

Publisher, builder, activator, reviewer and ingestion are separate non-root services with empty capability sets. Publisher and ingestion each create isolated clones. Builder is credentialless and networkless and extracts only the approved Git bundle into a private snapshot before fixed tests/build. Activator owns only immutable static releases, `active.json`, journal, outcomes and spent nonce state. It has read-only membership in `dek-decision-read` solely to hash the decision queue and take its existing lock; it never receives the decision MAC key. Journals and spent decisions/nonces are retained; bounded cleanup never removes active or previous generations. No Stage B component can invoke sudo, systemctl or D-Bus, reload/restart a service, modify a production path, or write application code.

## build-to-activator / publisher-signs-build DAC contract

`release.json` is written by the builder, and its activator-readable `0664` mode is set only by `builder_entrypoint.make_activator_readable()` before the atomic rename into `/var/spool/dek-activate/builds`. The publisher's `finalize` overwrites `release.json` in place (truncating the existing inode, not recreating it), which never changes its mode — so no publisher `chmod` is required, and none is permitted. Finalize creates `release.sig`, but that is not an activation gate. At the irreversible push boundary the publisher takes the decision queue's writer-compatible lock, revalidates that the approval is still the newest MAC-valid decision for that rough, and retains the lock through push and atomic gate creation. The signed gate binds the exact queue byte count/digest, release/signature byte digests, Git parent, and generation/commit/tree/bundle identity. At activation the activator takes the same queue lock, hashes the bounded single-link regular queue without possessing its MAC key, and retains the lock through the active-pointer mutation. Thus any reject/return append after gate creation changes the queue binding and blocks the old gate, including publisher/activator crash and retry windows. Releases are ordered only by the signed `parent_commit -> commit` chain rooted at the active release; random decision IDs never determine order, and ambiguous branches stop activation globally. Fatal unknown-generation or committed-state inconsistency stops the scan, while malformed independent candidates are durably isolated and later candidates continue. Builder failures live under `.builder-failures`; malformed activation outcomes are isolated under publisher state and only bounded, non-symlink, exact-schema success records whose filename nonce and published identity match are accepted.

## Package manifest scope

`deploy/PACKAGE.sha256` is the complete code install manifest. It records the SHA-256 of every file under `deploy/`, `web/`, `qa/`, and `ingestion/automation/` (the four trees selected into Stage A's exact digest-addressed installs), excluding byte-compiled caches (`__pycache__/`, `*.pyc`) and the manifest itself. It deliberately does not list content data — `wiki/`, `source/`, `ingestion/logs/`, `ingestion/rough/`, `_raw/` — because that content reaches the builder only through the independently signed `repository.bundle`, never through the package. Any file added under those four code trees must be added to the manifest before the package is approved out of band.

## Non-fast-forward consequence of pinning the review snapshot

The publisher binds each release to the exact `snapshot_commit` recorded by the reviewer, then pushes that commit as `refs/heads/main`. Because the publisher never fast-forwards or rewrites history, a `git push` is rejected with a non-fast-forward error whenever `origin/main` has advanced past the pinned snapshot during the review window — for example a concurrent human push, a second publisher run, or any commit that landed after the reviewer's snapshot. This is intentional and fail-closed: the publisher does not force-push, does not merge, and does not retry with a different commit. The operational consequence is that the affected decision cannot publish until the operator re-runs ingestion and review against a fresh snapshot that descends from the current `main`; the stale decision is left unpublished and its build is never activated.

## Builder sandbox threat model

The builder is not a sandbox for untrusted code. It executes the repository's own build and test code — the `unittest` suites under `ingestion/automation/tests`, `web/tests`, `qa/tests`, `web.site`, `qa.dek_qa.build_index`, and the ingestion audit — extracted from the approved snapshot, so that code is only as trusted as the review/approval process that bound the snapshot. The sandbox bounds what that code can do, not whether it is trusted: `PrivateNetwork=true` prevents any network egress, `CapabilityBoundingSet=`/`AmbientCapabilities=` remove all capabilities, `ProtectSystem=strict` with a narrow `ReadWritePaths` limits writes to the builder private area and the build output, and `PrivateTmp`/`PrivateDevices`/`RestrictSUIDSGID` remove host devices and privilege. Within those bounds the executed code may still consume CPU and disk, read the entire approved snapshot, and write anything inside the build output directory; any vulnerability in that code or its vendored dependencies (for example the Markdown/YAML libraries in the QA virtualenv) runs with the builder's non-root, capability-empty identity. This residual risk is accepted and is the reason the builder runs networkless, credentialless, and only on an approval-bound snapshot rather than on arbitrary repository code.
