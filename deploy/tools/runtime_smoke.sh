#!/bin/sh
# Read-only production smoke check.  It deliberately stops before any reviewer
# decision, ingestion, publication, activation, restart, or file mutation.
set -eu

fail() {
  echo "RUNTIME CHECK FAILED: $*" >&2
  exit 1
}

for unit in nginx.service dek-web.service dek-review.service dek-qa.service \
            dek-source-ingest.timer dek-review-publish-manual.path dek-source-ingest-manual.path; do
  test "$(systemctl is-active "$unit")" = active || fail "$unit is not active"
done

for unit in dek-web.service dek-review.service dek-qa.service \
            dek-source-ingest.timer dek-review-publish-manual.path dek-source-ingest-manual.path; do
  test "$(systemctl is-enabled "$unit")" = enabled || fail "$unit is not enabled"
done

web_ready=$(curl --noproxy '*' --fail --silent --show-error https://regkb.chenponai.com/__ready)
printf '%s' "$web_ready" | /usr/bin/python3 -c '
import json,sys
value=json.load(sys.stdin)
assert isinstance(value,dict) and value.get("pid") and value.get("artifacts"), "web readiness proof is incomplete"
' || fail "knowledge website readiness proof is invalid"

review_ready=$(curl --noproxy '*' --fail --silent --show-error https://regkb.chenponai.com/review/__ready)
test "$review_ready" = '{"status":"ready","login_bounce":"https://regkb.chenponai.com/auth/bounce"}' \
  || fail "review readiness response is invalid"

bundle=/var/lib/dek-review/input/repository.bundle
test -f "$bundle" || fail "review repository bundle is missing"
/usr/bin/git bundle verify "$bundle" >/dev/null 2>&1 || fail "review repository bundle is invalid"
digest=$(sha256sum "$bundle" | cut -d' ' -f1)
clone=/var/lib/dek-review/clones/$digest
test -d "$clone/.git" || fail "review snapshot clone has no Git metadata"
/usr/bin/git -C "$clone" -c core.hooksPath=/dev/null -c credential.helper= \
  -c credential.interactive=never -c core.fsmonitor=false -c protocol.allow=never \
  rev-parse --verify 'HEAD^{commit}' >/dev/null 2>&1 || fail "review snapshot clone cannot resolve HEAD"

/usr/bin/python3 -I /opt/dek-publisher/app/deploy/readiness.py \
  --config /etc/dek-readiness.json \
  --hmac-key /var/lib/dek-readiness/marker-hmac-key \
  --validate-marker --marker /var/lib/dek-readiness/automation-ready \
  || fail "signed automation readiness marker is invalid"

/usr/bin/python3 -I /opt/dek-source-ingest/app/deploy/readiness.py \
  --config /etc/dek-readiness.json \
  || fail "live DNS, TLS, OAuth callback, or readiness check failed"

echo "RUNTIME CHECK OK: services, public endpoints, review Git snapshot, and readiness gates"
