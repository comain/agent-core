# Model Discovery Design Review

Status: reviewed; all fixes including follow-up permissions approved by user.
Reviewed 2026-09-07 by an independent
system-design-reviewer subagent against the current docs and source. No code
was changed or tests run. No Critical findings; four Important findings.

## 1. Execution Ordering

`harness/runner.py` promotes last-success/preferred models and may retry cooling
models when all candidates are unhealthy. A sorted list alone cannot enforce
discovery ordering. Specify discovery mode that disables these overrides while
preserving manual behavior. Test strict order, excluded preferences, and zero
submissions when all candidates are cooling down.

Disposition: user approved fix. Addressed in overview 3.3 and verification 5.1.

## 2. One Availability Authority

The existing tracker is process-global, keyed only by model. Reusable session
fallback does not itself persist health, and permanent authentication quarantine
does not recover through ordinary success. Define one injected availability
interface across readers/writers in both execution paths; no parallel SQLite
authority. Define endpoint/credential scope, explicit credential generation on
rotation, and auth recovery without clearing unrelated cooldowns.

Disposition: user approved fix. Addressed in overview 3.4 and verification 5.1.

## 3. Benchmark Credential Isolation

Process and server OpenCode launchers inherit service environment. Keeping an
AA key server-side alone does not prevent its exposure to agent shell tools.
Prefer supplying the key only to refresh jobs; strip canonical and misspelled
legacy AA key environment names from both agent child launch paths. Test with
synthetic secret sentinels and ensure logs/reports do not include them.

Disposition: user approved fix. Addressed in overview 4.2 and verification 5.1.

## 4. Executable Refresh Configuration

UTA and CR compose provider settings differently. Define a shared trusted
refresh-input format and concrete service launcher/daily schedule that resolves
the exact provider credential scope, endpoint, and cache root its worker reads.
Specify failure reporting and test restart/rotation/cache identity alignment.

Disposition: user approved fix. Addressed in overview 3.5 and verification 5.1.

## 5. Confirmed Decisions

Follow-up review confirmed the four fixes. One additional Important issue:
owner-only 0700/0600 storage conflicts with separate refresher/worker identities
both writing shared availability SQLite data and sidecars. Recommendation:
restricted shared-group/ACL permissions for operational storage; separate private
credential directory accessible only to the refresher. Verify actual filesystem
permissions for both identities and worker denial of credential-file reads.
Disposition: user approved. Overview 4.1 now specifies restricted shared-group
operational access (2770/0660, umask0007) and private refresh credentials (0700/0600).

Default Coding Index 70, operator-owned override, strict descending score,
daily refresh, fresh selection on resume, and no per-task model list are
consistent. Pure policy resolution and shared operational caches remain the
simplest approach; benchmark matching and actual effort still need integration
proof. No deployment or implementation gate has passed yet.
