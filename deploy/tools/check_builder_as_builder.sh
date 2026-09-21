#!/bin/bash
# Run the builder's six fixed steps on the committed tree exactly as dek-builder runs them
# (its user, no network, its HOME). NEVER run those steps as root with HOME=/var/empty/dek-builder:
# the qa tests create ~/.hermes there (root-owned, 0700) and the real builder then fails with
# PermissionError on the next publish.
set -eu
cd "$(dirname "$0")/../.."
snap=$(mktemp -d /tmp/snapB.XXXXXX); out=$(mktemp -d /tmp/outB.XXXXXX)
git archive HEAD | tar -x -C "$snap"; chmod -R a+rX "$snap"; chmod 777 "$out"
cat > "$snap/.run_steps.py" <<'PY'
import sys, time
from pathlib import Path
sys.path.insert(0, "/opt/dek-builder/current/deploy"); sys.path.insert(0, "/opt/dek-builder/current")
import release_bundle as rb
snap, out = Path(sys.argv[1]), Path(sys.argv[2]); bad = 0
for cmd in rb.BundleBuilder.FIXED_COMMANDS:
    exp = tuple(p.format(snapshot=str(snap), output=str(out)) for p in cmd)
    t = time.time(); r = rb._run_status(exp, cwd=snap)
    print("OK  " if r.returncode == 0 else "FAIL", f"{time.time()-t:5.1f}s", " ".join(exp[-4:])[:90])
    if r.returncode: bad += 1; print(rb._failure_message(exp, r))
print("static output:", len(rb.BundleBuilder.validate_static_output(out)), "files validated")
sys.exit(1 if bad else 0)
PY
chmod a+r "$snap/.run_steps.py"
rc=0
unshare -n setpriv --reuid=dek-builder --regid=dek-builder --init-groups python3 "$snap/.run_steps.py" "$snap" "$out" || rc=$?
left=$(ls -A /var/empty/dek-builder | wc -l)
echo "entries left in /var/empty/dek-builder: $left (must be 0)"
rm -rf "$snap" "$out"
[ $rc -eq 0 ] && [ "$left" = "0" ]
