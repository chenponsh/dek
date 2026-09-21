#!/bin/bash
# After a reviewer publishes: what is live, did every stage run, is anything left dirty.
# A build failure of the CURRENT decision is silent (the manual publish service still ends
# "success"), so ALWAYS run this after a publish.
cd "$(dirname "$0")/../.."
python3 - <<'PY'
import json
a = json.load(open("/var/lib/dek-activate/control/active.json"))
print("LIVE: sequence", a["sequence"], "| commit", a["commit"][:8], "| decision", a["decision_id"][:12])
PY
git fetch -q origin && echo "origin/main tip: $(git rev-parse --short origin/main)   (live commit should be the latest 'publish:' commit)"
echo "last decisions (CST):"
tail -3 /var/lib/dek-review/decisions/decisions.jsonl | python3 -c "
import sys, json, datetime
for l in sys.stdin:
    d = json.loads(l); t = datetime.datetime.fromisoformat(d['created_at']).astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%H:%M:%S')
    print(' ', t, d['decision_id'][:10], d['action'], d['rough_path'].split('/')[-1][:44], '->', d.get('wiki_path'))"
echo "errors in the last 15 min (builder / publisher / activator / refresh):"
journalctl -u dek-builder.service -u dek-review-publish.service -u dek-activator.service -u dek-source-refresh.service --since "-15min" --no-pager 2>/dev/null \
  | grep -E "DEK build failed|BundleError|DEK activation failed|DEK publish skipped|Traceback" | sed 's/.*python3\[[0-9]*\]: //' | cut -c1-200 | sort | uniq -c | head -8
echo "(a 'DEK publish skipped ... cannot be retried' line is the known dead decision, not a new failure)"
echo "builder home entries (must be 0): $(ls -A /var/empty/dek-builder | wc -l)"
echo "review snapshot: $(stat -c %y /var/lib/dek-review/input/repository.bundle | cut -c1-19)"
