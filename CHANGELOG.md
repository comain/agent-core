# Changelog

## Version

**0.8.0** is a breaking release for [spec_generator_agent](https://github.com/comain/spec_generator_agent) adoption. See
[ADR-005](docs/decisions/ADR-005-breaking-08-for-reuse-first-harness-and-git.md).

[spec_generator_agent](https://github.com/comain/spec_generator_agent) pins **0.8.x**. [cragent](https://github.com/comain/code-review-agent) stays on last **0.7.x**. Mixed pairs
(a 0.7 consumer on 0.8 core, or the reverse) are unsupported.

### 0.8.0 breaks

- `OpenCodeHarness.run_turn` forwards `session_id`. First candidate may
  continue; later candidates are a fresh session and take
  `bootstrap_message` **iff set**. `SessionAffinity.run` takes a `Harness`,
  not an OpenCode process. Products must not import `OpenCodeHarness`.
- `GitWorkspace.repo_lock` is POSIX `fcntl.flock` plus `RLock`. Windows is
  unsupported for this lock.
- `ModelHealthTracker` skips unhealthy models even when `models=` is set;
  rate_limit 60s, timeout 5m; prefer last-success; if all cooling, try anyway.

Additive on the same tag: stdin / `--title` / per-turn `pure`,
`render_placeholders`, `update_refs_with_lease`.

Patch **0.8.1** maps the neutral `HarnessSpec.options["isolate_attempts"]`
setting into the OpenCode harness so products can reuse a stable lane sandbox.

Patch **0.8.2** keeps complete structured response envelopes when they contain
nested JSON objects. Without this fix, a consumer could receive the last child
record instead of the planner or writer response.

Patch **0.8.3** preserves flat `cacheRead` / `cacheWrite` aliases emitted by
older OpenCode streams so shared token accounting does not under-report cache
usage.

Patch **0.8.4** recognizes plain-string provider authentication failures so
the configured provider chain can fail over instead of terminating the task.

Patch **0.8.5** quarantines every model behind a provider whose credential is
invalid. The quarantine survives later turns and tasks until the runtime reloads
its provider configuration, preventing repeated paid authentication failures.

Patch **0.8.6** also quarantines a provider when its model-discovery endpoint
explicitly rejects the configured credential, before a paid agent turn reaches
that provider.

Patch **0.8.47** probes the configured `opencode_bin` for `--version`. 0.8.46
parsed `v2.0.6` correctly, but still probed a bare `opencode` on PATH. A
systemd unit that sets `CR_AGENT_OPENCODE_BIN=/root/.opencode/bin/opencode`
and a PATH without that directory then fail-closed to 1.x flags, including
`--pure`, which 2.x rejects immediately.

Patch **0.8.46** recognizes OpenCode's `v2.0.6` version banner. 0.8.45 treated
that string as 1.x and passed `--pure`, which 2.x rejects immediately.

Patch **0.8.45** talks to OpenCode 2.x as well as 1.x. The harness probes
the configured binary's `--version` (override with `AGENT_OPENCODE_MAJOR=1|2`).
On 2.x it uses `--standalone` so turns do not join the user-level background
service, emits native `permissions` / `providers` config, turns snapshots
and warming off, leaves checkpoint compaction on, and waits through
`/api/experimental/session/{id}/wait`. The Cursor OAuth plugin is no longer
bootstrapped; V1 plugin implementations do not run on OpenCode 2.

**0.7.0** remains documented at
[`docs/release-0.7.0.md`](docs/release-0.7.0.md).
