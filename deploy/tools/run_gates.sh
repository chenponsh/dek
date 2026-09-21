#!/bin/bash
# Every check that must be green before a commit, judged by REAL exit codes.
# (Never write `cmd; echo ok`: that hides failures. Twice a failing test reached origin that way,
# and the builder runs these same tests, so one failing test blocks every later publish.)
# Usage: deploy/tools/run_gates.sh   (from anywhere; exits 0 only if everything passed)
set -u
cd "$(dirname "$0")/../.." || exit 2
PY=/var/lib/dek-qa/venv/bin/python
ok=1
python3 deploy/tools/gen_package_manifest.py >/dev/null || { echo "manifest generation failed"; exit 2; }
run() { name=$1; shift; "$@" >"/tmp/gate_$name.out" 2>&1; rc=$?
        echo "$name rc=$rc $(grep -E '^Ran|^FAILED|^OK' "/tmp/gate_$name.out" | tr '\n' ' ')"
        [ $rc -eq 0 ] || { ok=0; echo "   -> see /tmp/gate_$name.out"; }; }
run ingestion /usr/bin/python3 -m unittest discover -s ingestion/automation/tests
run web       $PY -m unittest discover -s web/tests
run qa        $PY -m unittest discover -s qa/tests
run audit     /usr/bin/python3 -m ingestion.automation.audit --root .
out=$(mktemp -d)
run site      $PY -m web.site --vault . --output "$out/site"
run index     $PY -m qa.dek_qa.build_index --vault . --output "$out/dek-kb.json"
run pipeline  $PY -m unittest tests.test_review_snapshot_refresh tests.test_source_ingest_clone_proxy \
  tests.test_builder_failure_handling tests.test_autopublish_state_machine tests.test_static_suffixes \
  tests.test_package_manifest tests.test_latest_independent_review_blocks \
  tests.test_dek_source_ingest_install_imports tests.test_source_ingest_isolated_pythonpath \
  web.tests.test_replacement_architecture
rm -rf "$out"
git diff --quiet -- deploy/PACKAGE.sha256 || echo "note: deploy/PACKAGE.sha256 was regenerated; commit it"
[ $ok -eq 1 ] && echo "ALL GATES PASSED" || echo "GATES FAILED - do not commit"
[ $ok -eq 1 ]
