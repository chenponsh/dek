# Source ingestion automation

This directory contains the conservative, database-free ingestion CLI used by
the host systemd timer. The default command is always a dry run.

```bash
python3 -m ingestion.automation.cli dry-run
python3 -m ingestion.automation.cli approve --report _/ingestion/dry-run-....json
python3 -m ingestion.automation.cli run
python3 -m ingestion.automation.cli scheduled-run
```

On the current Ubuntu host, dependencies are installed under the ignored
`_/python` directory and the service sets `PYTHONPATH` to that directory.

CDE uses a headed, persistent Playwright Chromium context under Xvfb. Its
profile is stored in the Git-ignored `_/browser/cde-profile/` directory with
mode `0700`. The adapter only waits for the site's own navigation flow; it
does not synthesize or call challenge endpoints. Any challenge HTTP 400 keeps
the CDE sources at `skipped_browser_unavailable`. This availability condition
does not block independently verified low-risk sources; CDE content revisions,
schema errors, and other general failures still block the whole run.
The current configuration keeps CDE disabled after the confirmed HTTP 400;
dry-runs record `attempted: false` and do not retry the validation flow.

During the initial installation only, `dry-run --commissioning` permits the
new automation/deployment files to be uncommitted; unrelated changes still
stop execution. Normal scheduled runs require a clean tree.

`approve` records a local approval marker under `_/ingestion/`; it does not
write source notes or run Git. A real run refuses to proceed unless the
approved report hash, 24-hour validity window, clean workspace snapshot, and
current repository/configuration baseline all still match.
The approved report also binds the per-source results, planned writes, and
rough-draft list; a changed remote result requires another dry-run approval.
Dry-run reports and approval markers are written through a secure temporary
file and atomically installed with mode `0600`, including when replacing an
existing file; they do not pass through a world- or group-readable mode.

`scheduled-run` is the separate timer entry point. It never reads or creates
an approval marker. A write is eligible only when the source is explicitly
configured with both `auto_classified: true` and `auto_ingest: true`, every
planned path exactly matches the resulting source/rough pair, all global
safety checks pass, and the workspace, HEAD, remote head, and target preimages
remain unchanged immediately before writing. `blocking: false` alone never
authorizes an automatic write. No-change runs write only a mode-0600 ignored
diagnostic report and do not commit or push.

Scheduled source, rough draft, and tracked ingestion report writes are one
atomic batch. Write, staging, or commit failures restore pre-commit files and
unstage the exact plan. A push failure or a remote change after commit retains
the local commit, refuses force-push or history rewriting, and causes later
runs to stop at the ahead/behind gate until an operator resolves it.

At present there are zero active automatic-write sources: the only explicitly
eligible source is CDE problem type 4, and CDE remains disabled after its HTTP
400 browser validation failure. Shanghai additions and all new CPC articles
still require manual classification. Consequently, the current scheduled
entry point primarily performs checks, exits as a no-op, or stops safely.

When classification is required, candidate metadata is saved in the
per-source `candidates` field of the mode-0600 ignored report at
`_/ingestion/scheduled-run-YYYYMMDD_HHMM.json`. The nonzero service result and
report path are visible with `journalctl -u dek-source-ingest.service`; alerts
remain journald-only. An administrator must inspect that fixed report, verify
the source content and classification, make or authorize the appropriate
source and rough-note changes through the manual workflow, and rerun a dry-run.
The scheduler never promotes a candidate or creates an approval itself.

The CLI holds a non-blocking process lock at `_/ingestion/source-ingest.lock`
for dry-run, approval, and real runs. Source notes, rough drafts, and the run
report are prepared before a batch replacement; a failed final workspace
check restores the files from that batch.
Every automatically appended source row is accompanied by a dated rough note
under `ingestion/rough/`. Remote HTML anchors with HTTP(S) targets are retained
as Markdown links.

Changes limited to `last_updated`, run timestamps, fetch timestamps, or other
run metadata are reported as `no_change`: they produce no planned writes, log
file, commit, or push. CPC existing articles are revision-checked only when an
explicit `source_content_hash: "sha256:..."` baseline exists in the article
note and the remote detail can be normalized reliably. A hash mismatch blocks
the whole run; a missing/unavailable comparison is
`skipped_revision_check_unavailable` and never updates `last_updated`.

CPC content hashes use algorithm version `cpc-source-content-v2`. The detail
parser prefers `result.news` whenever that key exists; the legacy `result`
object is used only when `result.news` is absent. A present but invalid nested
object never falls back to legacy fields. Within the selected object,
`newsContentText` takes precedence over `newsContent`. An absent or empty
normalized body is a safety failure even when a link or attachment is present.

Normalization removes script/style content, HTML tags, zero-width markers,
and non-semantic block/line-break formatting; decodes HTML entities; applies
Unicode NFKC; and collapses whitespace. It preserves substantive characters,
including numbers, dates, medicine names, punctuation, and standard numbers.
Local validation removes Markdown link destinations before comparing body
numbers, dates, and standard identifiers, retaining only the visible label.
The local body and explicit attachment section are compared separately with
the official body and `annexFileList` names. This prevents download-path IDs
and query signatures from being treated as body content.

The local attachment list preferentially consists only of Markdown link
labels in the first `附件：` block. Parsing stops at an
`附件《…》文本：` marker, so extracted PDF pages and tables are attachment
content rather than additional filenames. Plain-text attachment names, when
supported, must be consecutive bullet items in that first block. Attachment
names are compared as normalized multisets: ordering is ignored, while count
and duplicates remain significant. Name normalization applies Unicode NFKC,
trims surrounding whitespace, normalizes full-/half-width punctuation, and
case-folds common filename extensions.

The SHA-256 input is UTF-8 canonical JSON with sorted keys and compact
separators. Its fields are `algorithm`, normalized `title`, ISO-date `date`,
normalized `body`, and sorted attachments containing normalized `name` and,
when present, the official stable `stable_id`. CPC annex IDs are treated as
stable because they are the official download identifiers and remained equal
across repeated detail responses. If an attachment has no such ID, the ID
field is omitted and the normalized official name remains hash-covered.
External URLs, domains, paths, query parameters, temporary signatures,
tokens, cookies, response headers, and page-navigation templates are never
included in the canonical hash.

A controlled CPC baseline operation has a separate strict workspace gate. It
may tolerate only the known untracked commissioning paths `deploy/`,
`ingestion/automation/`, and `requirements-ingestion.txt`, plus an explicit
allowlist of CPC article files for that batch. Any other tracked or untracked
change, including an unexpected change under `source/`, `wiki/`, or
`ingestion/rough/`, is a safety stop. This does not weaken the normal clean-tree
checks used by scheduled ingestion.

Missing CPC baselines are recorded per article as
`skipped_revision_check_unavailable`; hash-backed articles remain checked.
CPC additions always require manual classification and block automatic
writing, regardless of the availability of other article baselines.

The service currently runs as `root` because the repository and Git runtime
are root-owned. This is a deliberate first-stage deployment risk. Replacing it
with a dedicated least-privilege service account should be evaluated before
broadening the automation scope. First-stage failure notification is limited
to journald through `dek-source-ingest-alert@.service`; no external messaging
credentials or integrations are configured.
The service sets both `NO_PROXY` and `no_proxy` only for `www.chp.org.cn` and
`chp.org.cn`, so CPC requests use the verified direct HTTPS path while other
sources retain the host's existing proxy environment.
