# Model Discovery Verification

Status: implementation checkpoint; release gates NOT passed.

## 1. Live Sources

2026-09-07, read-only checks with configured credentials and no service restart:

- Production token-pool inventory returned HTTP 200 and 42 model IDs. The
  configured internal HTTP endpoint was preserved exactly; no endpoint or
  production environment edits were made.
- Artificial Analysis returned 643 records; all parsed, with 255 Coding Index
  scores. No API keys or authentication headers were retained in evidence.
- These checks establish inventory/schema compatibility, not model tool support
  or proof of each SDK reasoning-effort mapping. Live agent invocation remains
  a release gate.

## 2. Regression Review

Latest full shared suite: 2,089 passed, two dependency deprecation warnings;
wheel build passed. Initial pre-review shared suite: 1,984 passed.
Review then identified and fixed:

1. Disabled fallback incorrectly attempted multiple discovered models.
2. An older in-flight success could clear a newer availability failure.
3. Explicit capability approval was lost for genuinely unscored models.

Each fix has a failing regression reproduced before implementation. Fallback and
health fixes passed 344 focused tests; source/policy fixes passed 126 tests.
UTA consumer checks: 13 passed. CR consumer checks: 35 passed. These consumer
checks used local agent-core source, not a newly released dependency.

## 3. Enforcement Findings

The initial dry-run before implementation commits reported no targets; it is
NOT pass evidence. After committing, the gate discovered 16 changed targets.

The initial complete gate failed, including 11.54% coverage for harness config.
It used UTA's Python 3.13 environment, so it also exposed interpreter/install
differences in subprocess tests. Subsequent focused checks used agent-core's
Python 3.11.12 environment, coverage 7.16.0, mutmut 3.5.0, and origin/main.

| Target | Coverage | Mutation | Disposition |
| --- | --- | --- | --- |
| model_selection/sources.py | 97.7273% | 114/119, 95.7983% | gate passed after boundary and diagnostic assertions |
| model_selection/policy.py | 100% | 23/23, 100% | gate passed after test-name correction and diagnostic assertions |
| model_selection/configuration.py | 100% | 13/13, 100% | gate passed after strict test-name alignment and validation boundary tests |
| model_selection/__main__.py | 96.875% | 21/21, 100% | gate passed with automatic strict discovery and in-process CLI contract tests |
| model_selection/runtime.py | 100% | 50/51, 98.0392% | focused gate passed after exact execution configuration and exclusion assertions |
| model_selection/cache.py | 100% | 35/36, 97.2222% | focused gate passed after digest, size/age boundary and lock/publication tests |
| model_selection/refresh.py | 100% | 33/34, 97.0588% | focused gate passed after deadline and last-success status assertions |

Configuration previously failed at 92.9412% coverage. Added tests for schema,
duplicate/empty providers, document shape, absolute config paths, the exact 1 MiB
boundary, and valid effort options. Production endpoint behavior is unchanged.
Source tests cover exact inventory limits, malformed effort identities, streamed
wire-byte limits, and deadline boundaries. No production logic or gate thresholds
were changed to achieve these two passes.

Evidence: `/tmp/agent-core-model-sources-gate.json` and
`/tmp/agent-core-model-configuration-gate.json`. Both used the standalone
`tools/python-enforcement/python_test_enforce.py` with `--repo .`,
`--base-ref origin/main`, the corresponding `--target`, `--json-output`, and
`--evidence-output`. Sources additionally specified
`--test-path tests/test_model_catalog_sources.py`; configuration used automatic
strict discovery of `tests/test_model_configuration.py`.

CLI evidence: `/tmp/agent-core-model-main-gate.json`, using the same command
with `--target src/agent_core/model_selection/__main__.py` and no test-path
override. Strict discovery selects `tests/test_model_selection___main__.py`.
The earlier filename without the source stem's underscores did not qualify.
In-process tests now cover refresh success, score-ordered cached explanation,
empty availability diagnostics, and sanitized errors. Subprocess invocation
checks remain in `tests/test_model_selection_cli.py` as integration tests:
running them under mutmut's statistics collection failed because a fresh child
inherited collection mode without the in-process collector. The equivalent CLI
contracts are mutation-tested directly; no source behavior was changed.

Other failures include missing target-specific test names and insufficient
selected-file coverage. In particular, discovery tests were not selected for
existing harness targets merely because their content references those targets.
Do not weaken strict discovery or lower thresholds to make this pass.

Renamed test_model_selection.py to test_model_policy.py to identify the source
under test. Fixed the isolated process-environment fixture to preserve mutmut's
instrumentation control variable without preserving benchmark secrets.
On macOS, targeted mutation checks use OS_ACTIVITY_MODE=disable to avoid the
observed setproctitle crash. Crashed runs are not accepted as passing evidence.

## 4. Release Work Remaining

### 4.1. Full Release Gate, 2026-09-07

After the requested main/release/production rollout, reran the entire changed
scope at commit `1422d7a`, using the Python 3.11 environment above, no target or
test-path overrides, and `--base-ref origin/main`. Evidence is
`/tmp/agent-core-model-release-gate.json`. Result: **failed, 8/16 targets pass**.

| Blocking target | Coverage | Mutation |
| --- | --- | --- |
| harness/config.py | 11.5385% | not reached |
| harness/opencode.py | 23.6842% | not reached |
| harness/runner.py | 50% | not reached |
| harness/sessions.py | 69.2308% | not reached |
| model_selection/availability.py | 94.5578% | not reached |
| model_selection/cache.py | 89.1304% | not reached |
| model_selection/refresh.py | 94.6429% | not reached |
| model_selection/runtime.py | 97.0149% | 56.8627% |

The CLI, configuration, policy and sources focused passes reproduced in the
full run. Shared config, environment, process and server also passed. No merge,
release tag, consumer pin change or production restart was performed: this
failed gate blocks publishing. Existing full pytest/build results do not replace
the required enforcement evidence.

### 4.2. Remaining Steps

Follow-up evidence: `/tmp/agent-core-model-runtime-gate.json` and
`/tmp/agent-core-model-support-gate.json` establish the new runtime/cache/refresh
passes. Availability passed a separate retry at 95% mutation (57 killed, one
survived, two timed out out of 60); the prior run was 93.3333%. Added explicit
permission-repair coverage for all SQLite sidecar types to strengthen this
borderline result; the latest tests still need full enforcement replay.

Renamed discovery suites to identify their tested modules:
`test_model_discovery_config_opencode.py` and
`test_model_discovery_runner_sessions.py`. Automatic discovery now includes
them. This alone did not pass harness enforcement: added assertions for exact
config keys, manual-mode health behavior, continuation health loss, workspace
isolation, and bootstrap token accounting. Removed the redundant inactive
session-model sentinel; the active model derives from the candidate index.
Harness gate replay remains required before release.

The replay at `8efbdcd` passed 14/16 targets. It reproduced all model-selection
passes, including availability (98.6395% coverage, 96.6667% mutation), plus
harness config and sessions (100%/100%). The remaining runner/OpenCode gaps were
assertion gaps, not missing execution: both had 100% coverage in their discovery
tests. Hardened provider-quarantine reason assertions, single empty-stream retry,
health checks before workspace setup and between candidates, and the exact model
sent to the session client. Focused automatic-selection replay now passes:

| Target | Coverage | Mutation |
| --- | --- | --- |
| harness/runner.py | 100% | 47/48, 97.9167% |
| harness/opencode.py | 100% | 55/57, 96.4912% |

Evidence: `/tmp/agent-core-model-final-harness-gate.json`. The command used both
targets without a test-path override. No thresholds or suppression policies were
changed. A combined final 16-target run remains to confirm this checkpoint.

Combined runs at `31d080b` remain blocked by execution instability, despite the
focused target passes. `/tmp/agent-core-model-release-final.json` failed on
OpenCode (four timeouts), sessions (three timeouts), and policy (five timeouts).
Do not describe all-branch enforcement as passed.

This exposed a separate confirmed UTA defect: its mutmut adapter honored
`max_children` for generation but omitted it from the run CLI, defaulting to
CPU-count execution workers. UTA commit `e485a23` forwards `--max-children` in
both CLI paths. Six regression cases failed before the fix and passed afterward;
28 standalone adapter/enforcer tests pass. The fix does not change timeouts,
candidate selection or scoring.

Single-worker full replay (`/tmp/agent-core-model-release-bounded.json`) still
failed: sessions had three timeouts and two suspicious results; availability had
seven timeouts; policy had five timeouts. The worker-limit bug is fixed, but it
does **not** explain or resolve all execution instability. Remaining workers
showed low CPU and long waits on this macOS host. Root cause needs further
investigation and a controlled Linux verification before release; do not weaken
the gate or relabel these results as killed mutants.

- Complete target-specific coverage and mutation verification for all changes.
- Rerun independent review after enforcement-related fixes.
- Verify admitted models with exact effort and tool execution through OpenCode.
- Publish the shared release, pin and fully verify both consumers.
- Deploy via Git only when both enforcement and repair workers are idle.

### Release Exception Approved 2026-09-07

The user explicitly approved skipping the isolated Linux mutation diagnostic and
proceeding to production deployment. Combined mutation enforcement remains
failed on execution timeouts/suspicious results as documented above; this is an
accepted release risk, not passing gate evidence. Full pytest passed 2,089 tests.
Release 0.8.19 preserves existing manual mode unless trusted discovery configuration
is provisioned. Production rollout must preserve provider endpoints, wait for idle
workers, verify exact dependency pins and health, and retain rollback revisions.

### Production Code Deployment 2026-09-07

Published tag `v0.8.19` and main both resolve to `6114ad4`. Production UTA
`18eb1d0` and CR `ded9afa` were pulled through Git and installed with that exact
package version after confirming idle queues and stopping their workers.
Post-install production tests: UTA 28 passed, CR 41 passed. CR's full local suite
passed 415 tests against the released package. Both health endpoints returned 200,
and APIs/daemons have new live PIDs. CR registry fixtures were adapted to the newer
policy capability contract, with explicit unsupported-policy rejection coverage.

### Production Activation 2026-09-07

The user explicitly waived the root-worker isolation blocker. Both apps and the
refresher remain root: 0700/0600 refresh secret permissions and AA-free child
environments do not protect against root workers reading those files. Non-root
identity migration remains security debt, not an implemented boundary.

Both applications now use `/etc/agent-model-selection/{uta,cr}.json`, production
threshold 70, separate state under `/var/lib/agent-model-selection/{uta,cr}`,
and their original provider credentials/HTTP endpoint. Refresh ran successfully:
643 AA records, 42 provider IDs. Launcher overlap returned 75 for both apps.
Shared files are 0660, state directories 2770. Cron refresh is 03:15 UTA / 03:20 CR
Asia/Shanghai, with rotated logs and the Git v0.8.19 launcher.

Initial admitted order (Artificial Analysis Coding Index, exact configured effort):

| Model | Effort | Score |
| --- | --- | --- |
| gpt-5.6-sol | xhigh | 78.3 |
| gpt-6-astra | high | 77.1 |
| gpt-5.6-terra | max | 76.7 |
| kimi-k3 | max | 76.2 |
| gpt-5.5 | xhigh | 74.9 |
| glm-5.3 | max | 74.8 |
| qwen3.8-flash-next | unsuffixed | 73.1 |
| glm-5.3-flash | unsuffixed | 71.5 |
| gpt-5.6-luna | max | 71.4 |

All nine passed direct tool-call probes. Kimi/GLM reject forced named tool choice
but pass normal tool calls. Astra/high initially timed out twice, but a subsequent
streaming probe returned HTTP 200 and the correct tool call in 13.35 seconds.
Its binding was approved for both applications and verified second in score order.
The earlier transient timeouts were not evidence of permanent unavailability.
Bindings use explicit stable AA IDs; daily inventory discovery does not auto-approve
unknown capability or an ambiguous benchmark/effort mapping. Such models remain
excluded pending validation. Scores and availability refresh automatically.

Both real product factories selected Sol. Live OpenCode turns with xhigh read a
file with an undisclosed nonce and returned it correctly, without fallback:
UTA `ses_f85525164ffewzTyNWfyvzjH6a` (18,642 total tokens), CR
`ses_f8550f91dffeW3jetdJsz9apMx` (18,035 total tokens). These prove factory/config,
provider access, tool use and accounting, not a complete business repair/review.
Both apps restarted only while idle. Health endpoints returned 200. All four
new API/daemon process environments contain the correct policy, threshold 70,
and provider key, with neither AA credential variable present.

### Exclude Only Xhigh: 2026-09-07

User explicitly requested excluding only xhigh, not max. Both production policies
were atomically updated: Sol now binds the exact max benchmark (77.4), GPT-5.5
the exact high benchmark (71.6). Both configurations passed direct tool calls
(13.61s and 4.10s respectively). All other bindings and threshold 70 are unchanged.
No xhigh effort/variant remains in either policy. New harness construction uses
these bindings; running sessions were not interrupted or reconfigured.

Quality order: Sol/max 77.4, Astra/high 77.1, Terra/max 76.7, Kimi K3/max 76.2,
GLM-5.3/max 74.8, Qwen3.8-Flash-Next/default 73.1, GPT-5.5/high 71.6,
GLM-5.3-Flash/default 71.5, Luna/max 71.4. The initial xhigh smoke evidence above
is historical, not a live OpenCode validation of the new Sol/max configuration.
UTA resolved all nine in this order. At verification CR temporarily excluded Sol
and Terra with scoped provider_transport_error cooldowns and therefore started
with Astra. Those availability records were preserved, not cleared by the policy edit.

### Exclude Xhigh And Max: 2026-09-07

This supersedes the xhigh-only policy above following the user's clarification.
Both production configurations now contain no xhigh/max binding. Sol/high (77.2)
and Kimi K3/low (72.0) passed direct token-pool tool calls with the requested
reasoning_effort and correct function arguments before their bindings were added.
This checks request compatibility, not whether the proxy internally honors effort.

Eligible quality order at threshold 70: Sol/high 77.2, Astra/high 77.1,
Qwen3.8-Flash-Next/default 73.1, Kimi K3/low 72.0, GPT-5.5/high 71.6,
GLM-5.3-Flash/default 71.5. Availability cooldowns remain authoritative and were
not cleared. No services were restarted; existing harnesses retain their config.

AA records are not a complete effort matrix. The retrieved catalog contains only
low/max for Kimi K3 and max for GLM-5.3. Terra/high (67.1) and Luna/high (63.3)
are measured but below threshold; GLM-5.3 has no eligible measured variant.
Never substitute a max score for an unmeasured lower-effort request. Unspecified
effort on the two Flash records means provider default, not verified low/high.

### Rank Unsuffixed Default As Max: 2026-09-15

User directed that empty/default effort rank as max, not medium, after
Qwen3.8-Flash-Next (Coding Index 73.1, unsuffixed) was selected on production
UTA. Bound effort and provider options stay empty. Admission now rejects scored
empty/default rows together with xhigh/max (`prohibited_effort`), so treating
default as max actually prevents selection. Unscored empty-effort identities
remain eligible only with explicit unscored approval. Production also dropped
the unsuffixed Flash bindings until this pin is released. Historical quality
orders above used the previous medium ranking.
