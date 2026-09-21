#!/bin/bash
# Install the COMMITTED code (git HEAD) into every service tree under /opt, then prove the ingest
# package still loads. Run after `git commit` + `git push`; it never touches uncommitted files.
# Does NOT restart anything: dek-web, dek-review and dek-qa keep running old code until restarted
# (restarting dek-review invalidates reviewers' open forms; check nobody is mid-approval first).
set -eu
cd "$(dirname "$0")/../.."
archive=/tmp/dek-deploy-archive
rm -rf "$archive" && mkdir "$archive"
git archive HEAD | tar -x -C "$archive"
cd "$archive"
bad=$(sha256sum -c deploy/PACKAGE.sha256 | grep -vc ': OK' || true)
[ "$bad" = "0" ] || { echo "PACKAGE.sha256 does not match the committed files (regenerate and commit it)"; exit 1; }
digest=$(sha256sum deploy/PACKAGE.sha256 | cut -d' ' -f1)
manifest="$archive/deploy/PACKAGE.sha256"
# source-ingest is deferred by default and needs its own second pass
python3 -I deploy/install_components.py --package "$archive" --manifest "$manifest" --digest "$digest"
python3 -I deploy/install_components.py --package "$archive" --manifest "$manifest" --digest "$digest" --cutover-source-ingest
python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("sie", "/opt/dek-source-ingest/current/deploy/source_ingest_entrypoint.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
print("ingest package loads:", len(module.load_ingestion_modules("/opt/dek-source-ingest/current")), "modules")
PY
echo "installed digest ${digest:0:12}"
echo "systemd units are NOT installed by this script: copy changed deploy/systemd/*.service to /etc/systemd/system and run 'systemctl daemon-reload'"
