#!/usr/bin/python3
"""Fail-closed Stage A rollback executor; run as root from verified package."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import secrets

# Run directly as `python3 -I .../deploy/rollback.py` in production (see
# PRODUCTION_ROLLOUT.md); -I suppresses Python's normal auto-add of the
# script's own directory to sys.path, so sibling-module imports need an
# explicit bootstrap rather than relying on that default.
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from a6_cutover import UNIT_FILE_STATES, unit_file_restore_plan
from fsutil import atomic_write_bytes

REQUIRED={"files.tar","backup-inventory.txt","rollback-inventory.txt","existing.nul","absent.txt","unit-enablement.before","tree-manifest.json"}
STAGE_B=["dek-review.service","dek-review-publish-manual.path","dek-review-publish.service","dek-source-ingest.timer","dek-source-ingest-manual.path","dek-source-ingest.service","dek-builder.service","dek-activator.service"]
UNIT=re.compile(r"dek-[a-z0-9@_.-]+\.(?:service|timer|path)\Z")
BARE_TEMPLATE_UNIT=re.compile(r"@\.(?:service|timer|path)\Z")
STATES=UNIT_FILE_STATES-{"bad"}
ROLLBACK_UNRECONSTRUCTABLE_STATES={"generated","transient"}

def run(*args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)

def _read_fd(descriptor, maximum=256 * 1024 * 1024):
    os.lseek(descriptor,0,os.SEEK_SET)
    chunks=[]; remaining=maximum+1
    while remaining:
        chunk=os.read(descriptor,min(1024*1024,remaining))
        if not chunk: break
        chunks.append(chunk); remaining-=len(chunk)
    raw=b"".join(chunks)
    if len(raw)>maximum: raise SystemExit("trusted file is too large")
    os.lseek(descriptor,0,os.SEEK_SET)
    return raw

def _digest_fd(descriptor):
    os.lseek(descriptor,0,os.SEEK_SET)
    digest=hashlib.sha256()
    while True:
        chunk=os.read(descriptor,1024*1024)
        if not chunk: break
        digest.update(chunk)
    os.lseek(descriptor,0,os.SEEK_SET)
    return digest.hexdigest()

def _trusted_ancestor_fd(path, *, label, final_mode=None):
    """Open an absolute path one component at a time without following links."""
    absolute=pathlib.Path(path)
    if not absolute.is_absolute(): raise SystemExit(f"{label} must be absolute")
    descriptor=os.open("/",os.O_RDONLY|os.O_DIRECTORY)
    try:
        root_details=os.fstat(descriptor)
        if root_details.st_uid!=0 or root_details.st_mode&0o022:
            raise SystemExit(f"non-root-writable ancestor: /")
        parts=absolute.parts[1:]
        if not parts: return descriptor
        for index,part in enumerate(parts):
            flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)
            if index<len(parts)-1 or final_mode is None: flags|=os.O_DIRECTORY
            child=os.open(part,flags,dir_fd=descriptor)
            details=os.fstat(child)
            os.close(descriptor); descriptor=child
            current=pathlib.Path("/",*parts[:index+1])
            if index<len(parts)-1 or final_mode is None:
                if not stat.S_ISDIR(details.st_mode) or details.st_uid!=0 or details.st_mode&0o022:
                    raise SystemExit(f"non-root-writable ancestor: {current}")
            elif (not stat.S_ISREG(details.st_mode) or details.st_uid!=0 or details.st_nlink!=1
                  or stat.S_IMODE(details.st_mode)!=final_mode):
                raise SystemExit(f"unsafe {label}")
        return descriptor
    except (OSError,ValueError) as exc:
        if descriptor is not None: os.close(descriptor)
        raise SystemExit(f"unsafe {label}") from exc

def validate_program(path=None):
    """The root rollback authority must not be replaceable by another user."""
    program=pathlib.Path(path or __file__).absolute()
    descriptor=_trusted_ancestor_fd(program,label="rollback program",final_mode=0o644)
    os.close(descriptor)

def _open_backup_member(root_fd, name):
    try: descriptor=os.open(name,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0),dir_fd=root_fd)
    except OSError as exc: raise SystemExit("unsafe backup member") from exc
    details=os.fstat(descriptor)
    if (not stat.S_ISREG(details.st_mode) or details.st_uid!=0 or details.st_nlink!=1
            or details.st_mode&0o022):
        os.close(descriptor); raise SystemExit("unsafe backup member")
    return descriptor

def _inventory(path, *, nul=False):
    raw=path.read_bytes()
    values=(raw.split(b"\0") if nul else raw.splitlines())
    if nul and values and values[-1]==b"": values.pop()
    try: return [item.decode("utf-8") for item in values]
    except UnicodeDecodeError as exc: raise SystemExit("inventory is not UTF-8") from exc

def _safe_absolute(values, label):
    if len(values)!=len(set(values)): raise SystemExit(f"duplicate {label}")
    for value in values:
        pure=pathlib.PurePosixPath(value)
        if not value.startswith("/") or value=="/" or ".." in pure.parts or "." in pure.parts:
            raise SystemExit(f"unsafe {label}")

def normalize_inventory(values):
    """Stable exact de-duplication, rejecting recursive root overlap."""
    result=[]
    for value in values:
        if value not in result: result.append(value)
    _safe_absolute(result,"inventory")
    paths=[pathlib.PurePosixPath(value) for value in result]
    for index,path in enumerate(paths):
        if any(path != other and (path.is_relative_to(other) or other.is_relative_to(path)) for other in paths[index+1:]):
            raise SystemExit("overlapping inventory paths")
    return result


def _tree_entry(name, details, *, digest=None, link=None):
    if stat.S_ISDIR(details.st_mode): kind="directory"
    elif stat.S_ISREG(details.st_mode): kind="file"
    elif stat.S_ISLNK(details.st_mode): kind="symlink"
    else: raise SystemExit("unsupported tree entry type")
    value={"path":name,"type":kind,"mode":stat.S_IMODE(details.st_mode),
           "uid":details.st_uid,"gid":details.st_gid}
    if digest is not None: value["sha256"]=digest
    if link is not None: value["target"]=link
    return value


def archive_tree_manifest(archive):
    """Return the exact restorable file/type/content tree recorded by a tar."""
    result=[]
    with tarfile.open(archive,"r:") as handle:
        for member in handle:
            name=pathlib.PurePosixPath(member.name).as_posix().rstrip("/")
            if member.isdir(): kind="directory"
            # A tar hardlink member (member.islnk()) has no data of its own --
            # it names an earlier member sharing the same inode -- but after
            # `tar -x` it is a plain file on disk indistinguishable from any
            # other by lstat(), which is exactly how filesystem_tree_manifest()
            # will report it; record it the same way here so the two match.
            elif member.isfile() or member.islnk(): kind="file"
            elif member.issym(): kind="symlink"
            else: raise SystemExit("unsupported tree entry type")
            value={"path":name,"type":kind,"mode":member.mode & 0o7777,
                   "uid":member.uid,"gid":member.gid}
            if member.isfile() or member.islnk():
                extracted=handle.extractfile(member)
                if extracted is None: raise SystemExit("unreadable archive member")
                digest=hashlib.sha256()
                for chunk in iter(lambda:extracted.read(1024*1024),b""): digest.update(chunk)
                value["sha256"]=digest.hexdigest()
            elif member.issym(): value["target"]=member.linkname
            result.append(value)
    return sorted(result,key=lambda item:item["path"])


def filesystem_tree_manifest(existing):
    result=[]
    for absolute in existing:
        root=pathlib.Path(absolute)
        if not (root.exists() or root.is_symlink()): raise SystemExit("restored path is missing")
        paths=[root]
        if root.is_dir() and not root.is_symlink():
            paths.extend(sorted(root.rglob("*"),key=lambda item:item.as_posix()))
        for path in paths:
            details=path.lstat(); name=path.as_posix().lstrip("/")
            digest=None; link=None
            if stat.S_ISREG(details.st_mode):
                descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
                try:
                    opened=os.fstat(descriptor)
                    if (opened.st_dev,opened.st_ino)!=(details.st_dev,details.st_ino):
                        raise SystemExit("restored file changed during verification")
                    digest=_digest_fd(descriptor)
                finally: os.close(descriptor)
            elif stat.S_ISLNK(details.st_mode): link=os.readlink(path)
            result.append(_tree_entry(name,details,digest=digest,link=link))
    return sorted(result,key=lambda item:item["path"])


def write_tree_manifest(archive, output):
    payload=(json.dumps({"schema":1,"entries":archive_tree_manifest(archive)},
                        sort_keys=True,separators=(",",":"))+"\n").encode()
    atomic_write_bytes(pathlib.Path(output), payload, mode=0o600, prefix=".tree-")


def load_tree_manifest(path):
    try: value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,UnicodeDecodeError,json.JSONDecodeError) as exc: raise SystemExit("invalid tree manifest") from exc
    if set(value) != {"schema","entries"} or value["schema"] != 1 or not isinstance(value["entries"],list):
        raise SystemExit("invalid tree manifest")
    entries=value["entries"]
    if entries != sorted(entries,key=lambda item:item.get("path","") if isinstance(item,dict) else ""):
        raise SystemExit("invalid tree manifest ordering")
    if len({item.get("path") for item in entries if isinstance(item,dict)}) != len(entries):
        raise SystemExit("invalid tree manifest entries")
    return entries


def _open_inventory_parent(path, *, require_root_owned_ancestors):
    absolute=pathlib.Path(path)
    if not absolute.is_absolute() or absolute==pathlib.Path("/") or ".." in absolute.parts:
        raise SystemExit("unsafe inventory deletion path")
    descriptor=os.open("/",os.O_RDONLY|os.O_DIRECTORY)
    try:
        root_details=os.fstat(descriptor)
        if require_root_owned_ancestors and (root_details.st_uid!=0 or root_details.st_mode&0o022):
            raise SystemExit("unsafe inventory deletion ancestor")
        for part in absolute.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0),dir_fd=descriptor)
            details=os.fstat(child)
            if require_root_owned_ancestors and (details.st_uid!=0 or details.st_mode&0o022):
                os.close(child); raise SystemExit("unsafe inventory deletion ancestor")
            os.close(descriptor); descriptor=child
        return descriptor,absolute.name
    except BaseException:
        os.close(descriptor); raise


def remove_inventory_roots(paths, *, require_root_owned_ancestors=True):
    """Remove exact inventory roots via held parent fds before archive restore."""
    for value in normalize_inventory(paths):
        parent_fd,name=_open_inventory_parent(value,require_root_owned_ancestors=require_root_owned_ancestors)
        try:
            try: details=os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
            except FileNotFoundError: continue
            if stat.S_ISDIR(details.st_mode):
                quarantine=f".dek-rollback-remove-{os.getpid()}-{secrets.token_hex(8)}"
                os.rename(name,quarantine,src_dir_fd=parent_fd,dst_dir_fd=parent_fd)
                shutil.rmtree(pathlib.Path(f"/proc/self/fd/{parent_fd}")/quarantine)
            else:
                os.unlink(name,dir_fd=parent_fd)
            try: os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise SystemExit("inventory root remains after removal")
            os.fsync(parent_fd)
        finally: os.close(parent_fd)

def validate_archive(archive, existing):
    _safe_absolute(existing,"existing inventory")
    approved=[pathlib.PurePosixPath(item.lstrip("/")) for item in existing]
    seen=set()
    symlinks=set()
    try:
        with tarfile.open(archive,"r:") as handle:
            for member in handle:
                name=member.name
                pure=pathlib.PurePosixPath(name)
                if name.startswith("/") or not name or ".." in pure.parts or "." in pure.parts or pure in seen:
                    raise SystemExit("unsafe or duplicate tar member")
                if any(pure.is_relative_to(link) and pure != link for link in symlinks):
                    raise SystemExit("tar member traverses archived symlink")
                seen.add(pure)
                if not any(pure==root or pure.is_relative_to(root) for root in approved):
                    raise SystemExit("tar member outside approved inventory")
                if member.issym():
                    target=member.linkname
                    if not target or "\0" in target: raise SystemExit("unsafe tar symlink")
                    if target.startswith("/"):
                        resolved=pathlib.PurePosixPath(target.lstrip("/"))
                    else:
                        normalized=posixpath.normpath(str(pure.parent / target))
                        resolved=pathlib.PurePosixPath(normalized)
                    if (str(resolved).startswith("../") or resolved==pathlib.PurePosixPath("..")
                            or not any(resolved==root or resolved.is_relative_to(root) for root in approved)):
                        raise SystemExit("tar symlink escapes approved inventory")
                    symlinks.add(pure)
                elif member.islnk():
                    # A tar hardlink member's linkname names an earlier member
                    # in this same archive (GNU tar always archives a shared
                    # inode's first occurrence as a regular file and every
                    # later occurrence as a hardlink back to it) rather than a
                    # filesystem path. Unlike a symlink target, a hardlink
                    # linkname is archive-root-relative, not relative to the
                    # hardlink member's own parent directory (verified against
                    # a real archive: tarfile.extractfile() resolves it that
                    # way). Validate it like a symlink target once normalized
                    # accordingly, plus require it to already be a seen,
                    # approved member -- `tar -x` would otherwise hardlink
                    # outside the approved tree or to something never archived.
                    target=member.linkname
                    if not target or "\0" in target: raise SystemExit("unsafe tar hardlink")
                    normalized=posixpath.normpath(target.lstrip("/"))
                    resolved=pathlib.PurePosixPath(normalized)
                    if (str(resolved).startswith("../") or resolved==pathlib.PurePosixPath("..")
                            or not any(resolved==root or resolved.is_relative_to(root) for root in approved)):
                        raise SystemExit("tar hardlink escapes approved inventory")
                    if resolved not in seen:
                        raise SystemExit("tar hardlink target is not an already-archived member")
                elif not (member.isreg() or member.isdir()):
                    raise SystemExit("dangerous tar member type")
    except (tarfile.TarError,OSError) as exc:
        raise SystemExit("invalid rollback archive") from exc
    archived_roots={root for root in approved if any(item==root or item.is_relative_to(root) for item in seen)}
    if archived_roots != set(approved): raise SystemExit("archive missing approved existing path")

def parse_enablement(path):
    try: raw=path.read_text(encoding="utf-8")
    except (OSError,UnicodeDecodeError) as exc: raise SystemExit("invalid unit enablement inventory") from exc
    states={}
    if raw.lstrip().startswith("{"):
        try: value=json.loads(raw)
        except json.JSONDecodeError as exc: raise SystemExit("invalid unit enablement inventory") from exc
        if set(value) != {"schema","units"} or value["schema"] != 1 or not isinstance(value["units"],dict):
            raise SystemExit("invalid unit enablement inventory")
        records=value["units"].items()
    else:
        # Backward-compatible parsing is safe for states which need no FragmentPath.
        records=[]
        for line in raw.splitlines():
            fields=line.split()
            if len(fields)<2:
                raise SystemExit("invalid unit enablement inventory")
            records.append((fields[0],{"unit_file_state":fields[1],"fragment_path":""}))
    for unit,record in records:
        if (not UNIT.fullmatch(unit) or unit in states or not isinstance(record,dict)
                or set(record) != {"unit_file_state","fragment_path"}
                or record["unit_file_state"] not in STATES
                or not isinstance(record["fragment_path"],str)):
            raise SystemExit("invalid unit enablement inventory")
        states[unit]={"unit_file_state":record["unit_file_state"],"fragment_path":record["fragment_path"]}
    if not states: raise SystemExit("empty unit enablement inventory")
    return states

def _enablement_record(value):
    if isinstance(value,str):
        return value,""
    if (not isinstance(value,dict) or set(value) != {"unit_file_state","fragment_path"}
            or not isinstance(value.get("unit_file_state"),str)
            or not isinstance(value.get("fragment_path"),str)):
        raise SystemExit("invalid unit enablement inventory")
    return value["unit_file_state"],value["fragment_path"]

def build_enablement_restore_plan(states):
    """Purely validate and plan every systemctl mutation before rollback writes."""
    commands=[]
    for unit,value in states.items():
        state,fragment_path=_enablement_record(value)
        if state in ROLLBACK_UNRECONSTRUCTABLE_STATES:
            raise SystemExit("cannot reconstruct exact unit enablement from rollback snapshot")
        try: before,after=unit_file_restore_plan(unit,state,fragment_path)
        except (TypeError,ValueError,RuntimeError) as exc:
            raise SystemExit("cannot reconstruct exact unit enablement from rollback snapshot") from exc
        commands.extend(("systemctl",*command) for command in (*before,*after))
    return commands

def capture_enablement(*, runner=subprocess.run):
    """Capture exact unit-file state plus linked target without changing systemd."""
    listed=runner(
        ("systemctl","list-unit-files","dek-*","--no-legend","--no-pager","--full","--plain"),
        check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
    )
    states={}
    for line in listed.stdout.splitlines():
        fields=line.split()
        if len(fields)<2 or not UNIT.fullmatch(fields[0]) or fields[0] in states:
            raise SystemExit("invalid systemd unit enablement response")
        unit,listed_state=fields[:2]
        if BARE_TEMPLATE_UNIT.search(unit):
            # A bare template unit (e.g. dek-foo@.service) has no single
            # invocation identity; `systemctl show` on it fails outright
            # ("neither a valid invocation ID nor unit name") rather than
            # returning empty properties, so the cross-check below cannot
            # run for it. list-unit-files is the only source of truth here
            # instead; that is no weaker, since every other branch already
            # trusts it as the listed_state to cross-check *against*.
            if listed_state not in STATES:
                raise SystemExit("invalid systemd unit enablement response")
            states[unit]={"unit_file_state":listed_state,"fragment_path":""}
            continue
        shown=runner(
            ("systemctl","show",unit,"--property=UnitFileState","--property=FragmentPath"),
            check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        )
        properties=dict(item.split("=",1) for item in shown.stdout.splitlines() if "=" in item)
        state=properties.get("UnitFileState","")
        fragment_path=properties.get("FragmentPath","")
        if state != listed_state or state not in STATES:
            raise SystemExit("inconsistent systemd unit enablement response")
        states[unit]={"unit_file_state":state,"fragment_path":fragment_path}
    if not states: raise SystemExit("empty unit enablement inventory")
    build_enablement_restore_plan(states)
    return states

def write_enablement_snapshot(path, *, runner=subprocess.run):
    """Atomically persist a prevalidated exact enablement snapshot."""
    states=capture_enablement(runner=runner)
    payload=(json.dumps({"schema":1,"units":states},sort_keys=True,
                        separators=(",",":"),ensure_ascii=True)+"\n").encode("ascii")
    atomic_write_bytes(pathlib.Path(path), payload, mode=0o600, prefix=".unit-enablement-")

def restore_enablement(states, *, runner=run, plan=None):
    """Execute a fully prevalidated exact persistent/runtime state plan."""
    commands=build_enablement_restore_plan(states) if plan is None else plan
    for command in commands:
        runner(*command)

def _query_enabled(*argv):
    return subprocess.run(argv,capture_output=True,text=True)

def verify_enablement(states, *, runner=_query_enabled):
    """Re-read every unit's effective enablement and fail closed on any mismatch."""
    mismatches=[]
    for unit,value in states.items():
        expected,_fragment_path=_enablement_record(value)
        result=runner("systemctl","is-enabled",unit)
        observed=(getattr(result,"stdout",None) or "").strip()
        if observed != expected:
            mismatches.append(f"{unit}: expected {expected!r}, observed {observed!r}")
    if mismatches:
        raise SystemExit("unit enablement mismatch after restore: "+"; ".join(mismatches))

def validate_preconditions(old_web,old_qa,proof,enablement):
    for unit in (old_web,old_qa):
        if not UNIT.fullmatch(unit): raise SystemExit("invalid old unit name")
        result=subprocess.run(("systemctl","cat",unit),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode: raise SystemExit("old unit does not exist")
    if not proof or not pathlib.Path(proof[0]).is_absolute() or not pathlib.Path(proof[0]).is_file() or not os.access(proof[0],os.X_OK):
        raise SystemExit("old proof command is not executable")
    return parse_enablement(enablement)

def verify_anchor(root, anchor):
    root=pathlib.Path(root).absolute(); anchor=pathlib.Path(anchor).absolute()
    root_fd=_trusted_ancestor_fd(root,label="backup directory")
    details=os.fstat(root_fd)
    if stat.S_IMODE(details.st_mode)!=0o700: os.close(root_fd); raise SystemExit("unsafe backup directory mode")
    try:
        if anchor.parent==root or anchor.is_relative_to(root): raise SystemExit("SHA256SUMS must be external")
        anchor_fd=_trusted_ancestor_fd(anchor,label="SHA256SUMS anchor",final_mode=0o400)
    except BaseException:
        os.close(root_fd); raise
    expected={}
    members={}
    try:
        try: lines=_read_fd(anchor_fd,65536).decode("utf-8").splitlines()
        except UnicodeDecodeError as exc: raise SystemExit("invalid SHA256SUMS anchor") from exc
        for line in lines:
            fields=line.split(None,1)
            if len(fields)!=2 or not re.fullmatch(r"[0-9a-f]{64}",fields[0]) or pathlib.PurePosixPath(fields[1]).name!=fields[1] or fields[1] in expected:
                raise SystemExit("invalid SHA256SUMS anchor")
            expected[fields[1]]=fields[0]
        if set(expected)!=REQUIRED: raise SystemExit("SHA256SUMS does not bind exact backup set")
        for name,digest in expected.items():
            descriptor=_open_backup_member(root_fd,name); members[name]=descriptor
            if _digest_fd(descriptor)!=digest: raise SystemExit("backup digest mismatch")
        return root_fd,members,expected
    except BaseException:
        for descriptor in members.values(): os.close(descriptor)
        os.close(root_fd); raise
    finally: os.close(anchor_fd)

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("backup",type=pathlib.Path); p.add_argument("--sha256sums",type=pathlib.Path,required=True); p.add_argument("--old-web-unit",required=True); p.add_argument("--old-qa-unit",required=True); p.add_argument("--old-proof-command",nargs="+",required=True); a=p.parse_args(argv)
    if os.geteuid()!=0: raise SystemExit("rollback requires root")
    validate_program()
    root_fd,members,expected_digests=verify_anchor(a.backup,a.sha256sums)
    root=pathlib.Path(f"/proc/self/fd/{root_fd}")
    entries=set(os.listdir(root_fd))
    if not REQUIRED <= entries or entries-REQUIRED-{"ingestion-cutover"}: raise SystemExit("backup set is not exact")
    if "ingestion-cutover" in entries:
        try: evidence_fd=os.open("ingestion-cutover",os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0),dir_fd=root_fd)
        except OSError as exc: raise SystemExit("unsafe A6 evidence directory") from exc
        try:
            details=os.fstat(evidence_fd)
            if details.st_uid!=0 or stat.S_IMODE(details.st_mode)!=0o700: raise SystemExit("unsafe A6 evidence directory")
            for item in os.listdir(evidence_fd):
                descriptor=_open_backup_member(evidence_fd,item); os.close(descriptor)
        finally: os.close(evidence_fd)
    member_paths={name:pathlib.Path(f"/proc/self/fd/{descriptor}") for name,descriptor in members.items()}
    inventory=normalize_inventory(_inventory(member_paths["rollback-inventory.txt"])); backup_inventory=normalize_inventory(_inventory(member_paths["backup-inventory.txt"]))
    if inventory!=backup_inventory: raise SystemExit("rollback and backup inventory differ")
    _safe_absolute(inventory,"rollback inventory")
    existing=_inventory(member_paths["existing.nul"],nul=True); _safe_absolute(existing,"existing inventory")
    if any(x not in inventory for x in existing): raise SystemExit("existing path outside inventory")
    absent=_inventory(member_paths["absent.txt"]); _safe_absolute(absent,"absent inventory")
    if set(existing)|set(absent)!=set(inventory) or set(existing)&set(absent): raise SystemExit("existing/absent partition invalid")
    validate_archive(member_paths["files.tar"],existing)
    expected_tree=load_tree_manifest(member_paths["tree-manifest.json"])
    if archive_tree_manifest(member_paths["files.tar"]) != expected_tree:
        raise SystemExit("archive and tree manifest differ")
    states=validate_preconditions(a.old_web_unit,a.old_qa_unit,a.old_proof_command,member_paths["unit-enablement.before"])
    enablement_plan=build_enablement_restore_plan(states)
    run("systemctl","disable","--now",*STAGE_B)
    run("systemctl","stop",*STAGE_B)
    for unit in STAGE_B:
        state=subprocess.run(("systemctl","is-active",unit),capture_output=True,text=True)
        if state.stdout.strip() not in {"inactive","failed","unknown"}:
            raise SystemExit(f"Stage B unit remains active: {unit}")
    # Re-read the already-open, no-follow inodes at the last possible point before mutation.
    for name,digest in expected_digests.items():
        if _digest_fd(members[name])!=digest:
            raise SystemExit("backup digest mismatch immediately before archive extraction")
    remove_inventory_roots(inventory)
    archive_fd=members["files.tar"]
    run("tar","--xattrs","--acls","--numeric-owner","-xpf",f"/proc/self/fd/{archive_fd}","-C","/",pass_fds=(archive_fd,))
    if filesystem_tree_manifest(existing) != expected_tree:
        raise SystemExit("restored filesystem differs from exact backup tree")
    run("nginx","-t")
    run("systemctl","reload","nginx")
    run("systemctl","is-active","--quiet","nginx")
    run("systemctl","daemon-reload")
    restore_enablement(states,plan=enablement_plan)
    verify_enablement(states)
    run("systemctl","restart",a.old_web_unit,a.old_qa_unit); run(*a.old_proof_command)
    for descriptor in members.values(): os.close(descriptor)
    os.close(root_fd)
    return 0
if __name__=="__main__": raise SystemExit(main())
