# Test suites

`deploy/tools/run_gates.sh` is the authoritative local gate. It runs four
capability groups:

- `ingestion`: source fetching, parsing, audit and draft creation.
- `web`: authentication, review UI, publishing contracts and static site.
- `qa`: read-only knowledge index and DingTalk/Hermes boundaries.
- `deploy`: every `test_*.py` in this directory, discovered automatically.

Tests belong in the component suite that owns the behavior. Cross-component
installation, release, activation, rollback and runtime contracts belong here.
Do not add an individual module to `run_gates.sh`; automatic discovery prevents
new tests from being silently omitted or an existing component suite from being
run twice.
