# Model Discovery Operations

Launch scaffolding; the model-selection CLI is implemented separately. This change
does not install a cron job. Do not enable discovery until the released CLI, cache,
worker integration and deployment checks below pass. See design sections
[3.5 and 4](design-model-discovery.md#35-shared-deployment-input).

## Exact Launch Interface

```sh
/opt/agent-core/scripts/refresh-model-catalog \
  --python /opt/agent-core/venv/bin/python \
  --config /etc/agent-model-selection/uta.json \
  --secret-env-file /etc/agent-model-refresh/credentials.json
```

All three arguments are required, in this order, and must be absolute paths.
`--secret-env-file` means a JSON object, NOT shell syntax or dotenv. The launcher
parses it using Python's standard library; it never sources, evaluates or executes
the file. It requires no dotenv dependency and adds no CLI env-file option.
Use Python >=3.11 with the released agent-core installed in that interpreter's
environment; the launcher changes CWD to `/` and uses isolated mode (`-I`), so
neither a target checkout nor `PYTHONPATH` supplies the module. The exact exec is:

```sh
/opt/agent-core/venv/bin/python -I -m agent_core.model_selection refresh --config /etc/agent-model-selection/uta.json
```

It also sets `AGENT_MODEL_SELECTION_CONFIG` to that same explicit path. The CLI
owns full schema validation, HTTP budgets, atomic catalog publication and bounded
refresh status (`last_success_at`/`last_error_code`); the launcher does not
reimplement them. It passes the CLI's exit status through. Launcher validation
errors exit 2 with a fixed, secret-free message; lock contention exits 75 without
invoking the CLI. Monitor launcher failures separately because they cannot update
CLI status. Missing CLI is a failure, not a successful refresh.

## Shared Trusted Configuration

Ops supplies a JSON file outside target repositories, readable by the worker and
refresher but not writable by the worker or target tools. Set worker startup
`AGENT_MODEL_SELECTION_CONFIG=/etc/agent-model-selection/uta.json` to exactly the
path passed above. Never discover it from CWD, `.env`, task input or a checkout.
Use separate files per service; reject conflicting legacy provider settings.

Example structure (placeholder provider; empty bindings are not rollout approval):

```json
{
  "schema_version": 1,
  "cache_root": "/var/lib/agent-model-selection/uta",
  "availability_db": "/var/lib/agent-model-selection/uta/availability.sqlite",
  "providers": [
    {
      "id": "pool",
      "base_url": "https://provider.example/v1",
      "credential_scope_id": "production-pool",
      "credential_generation": "1",
      "api_key_env": "POOL_API_KEY",
      "pricing_discount": 1
    }
  ],
  "bindings": {},
  "policy": {"application_id": "uta", "minimum_coding_score": 70,
             "ranking_strategy": "price-efficient"}
}
```

AA requests require HTTPS. Trusted, explicitly configured provider endpoints
support HTTP or HTTPS; preserve the approved internal HTTP endpoint rather than
rewriting it to HTTPS. This is not permission to send credentials to arbitrary
HTTP hosts. Use the exact field `credential_generation`,
not `generation`. Store only environment variable references, never literal API
keys. Bindings associate provider/model identity with a stable AA benchmark ID,
exact effort, supported adapter variant and approved tool capability; use the
shared schema, not name guessing. Application `policy` supplies filters/effort.
Null allowlist means unrestricted; an empty allowlist denies everything. Shared
denials still win. A known score below the threshold cannot be allowlisted in.

### Preserve Cross-Provider Fallback

The order of `providers` is the fallback priority, matching the original provider
chain: put the primary provider first. Discovery ranks models/efforts **within**
each provider using `price-efficient` or `best-score`, then concatenates those
groups in provider order. A faster or higher-scoring backup cannot outrank an
eligible primary. Admission and availability checks still apply: an exhausted
or unavailable primary falls through to the next provider. This order is shared
by CLI `explain`, preferred-model selection, and process/session fallback.

Discovery does not inherit providers from the manual chain. A config listing
only a token pool has no OpenAI or DeepSeek fallback, even if those providers
remain in the application's legacy environment. Register every intended provider
in `providers`, with its own endpoint, credential scope and `api_key_env`.
Provision each key separately for both worker and refresher. Add verified
provider/model bindings and exact effort options, then refresh inventory before
activation. Do not copy capability approvals across endpoints without verification.

The OpenCode factory rejects an explicit `opencode_provider_chain` whose provider
set is not contained in discovery's `providers`. This is a migration check, not
automatic admission: models in that chain can still be excluded by discovery's
score, capability, inventory and availability policy. Intentionally removing a
provider requires removing it from the explicit chain too. Ambient legacy env
and target-repository configuration are not imported into discovery.

Before rollout, inspect `explain` for eligible models from each intended fallback
provider; merely listing providers does not prove a usable fallback. Verify a
controlled primary-provider failure reaches a secondary provider in both process
and session execution. Auth quarantine applies to one credential scope, not to
other providers. No eligible secondary means `NoAvailableModels`, never a bypass
to unapproved manual models. Existing one-provider installations need an operator
config/credential/binding update; upgrading agent-core alone cannot add providers.

`policy.ranking_strategy` defaults to `price-efficient`: rank eligible scored
bindings by discounted price first, then effort, then descending Coding Index,
then identity. Configs naming the previous `best-efficient` load as
`price-efficient`; that ranking is unchanged wherever prices tie. Effort
order is none, minimal, low, medium, high, xhigh, max/default. Empty/default
effort is max only for sorting; provider options remain unchanged. Unrecognized
effort labels rank last. Scored unsuffixed/default rows and every xhigh/max
binding are rejected as `prohibited_effort` and are not selected. Unscored
empty-effort identities can still be admitted by explicit unscored approval. Use `best-score` to restore descending-score-first
ordering. Invalid strategy values fail configuration loading. Approved unscored
models remain after all scored models under either strategy.

The ranker selects the best eligible effort from each model's approved bindings.
Register multiple exact benchmark bindings as a list and supply each variant's
provider options; unregistered variants are not implicitly capability-approved.
Effort is an efficiency proxy, not a
cross-provider token budget. Existing configs without this field change to
efficiency-first when upgraded; set `best-score` explicitly to preserve old order.
Neither strategy changes the application's allowed bindings or threshold.

### Provider Pricing Discounts

Each provider carries `pricing_discount`, the fraction of published list price
the application actually pays through that endpoint: `1` (the default, and the
value for a direct vendor account such as `openai`) is full price, `0` an
internal pool such as `token-pool` whose models cost the application nothing.
Any value in [0,1] is accepted; blank, nonnumeric, nonfinite or out-of-range
values fail configuration loading. The discount is applied at selection time
and never baked into the cached catalog, so changing it takes effect without a
refresh. It affects ranking only; it is not a budget, a quota or a billing record.

Under `price-efficient` this produces the intended split within each provider
group: a `pricing_discount` of 0 prices every model at 0, every price ties, and
the pool is ranked by efficiency alone, so the most efficient eligible model
(typically a low-effort flagship) leads. At full price the same models are
ranked cheapest first, so an expensive flagship no longer leads its group even
at low effort. Provider order still wins over price: a cheaper backup provider
never outranks an eligible primary.

Prices come from OpenRouter's public catalog
(`https://openrouter.ai/api/v1/models`), fetched unauthenticated by the same
daily refresh and cached with the catalog. Only the uncached `prompt` (input)
and `completion` (output) rates are retained, in USD per 1M tokens. Cache-read,
cache-write, image, audio, web-search, internal-reasoning and long-context
`overrides` rates are deliberately not used: they depend on traffic shape the
catalog cannot observe.

Those two rates are blended into one comparable price by `price_weights`,
default `{"prompt": 10, "completion": 1}` — agent traffic is input-dominated, and
an unweighted sum systematically favours cheap-output models over cheap-input
ones. Weights are per-config, accept [0,1000] each, must not both be zero, and
scale every model alike, so only their ratio matters. Like the discount they are
applied when ranking, so changing them needs no refresh. A runtime model ID, minus any effort
suffix, must match exactly one published model slug; ambiguous matches, listings
with no stated price (OpenRouter publishes `-1`) and unlisted models leave the
price **unknown**, which ranks after every known price rather than as free. Pin
an exact listing with a binding's `pricing_id` (for example
`"pricing_id": "openai/gpt-5.5"`); an explicit ID never falls back to slug
matching. A zero-discount provider is free even where no listing matched.

Losing the public price source does not stale the whole catalog: refresh keeps
the previously published prices, marks them `"stale": true`, and reports
`pricing_error_code` in `refresh-status.json` alongside `priced_models`. Monitor
that field; long-running pricing failures silently freeze the price ranking.
`explain` reports each candidate's blended `list_price` and discounted `price`,
the configured `pricing_discounts` and `price_weights`, and credits OpenRouter
as the price source.

The effective minimum is the production-owned `AGENT_MODEL_CODING_INDEX_MIN`,
then application `minimum_coding_score`, then 70. Zero is valid; blank, nonnumeric,
nonfinite or values outside [0,100] fail. The CLI's `explain` JSON reports
`minimum_coding_score`, `threshold_source`, and `ranking_strategy`; automatic worker startup logging
is not guaranteed by this launcher or the shared runtime.
Tasks and target repositories cannot override it. The launcher preserves this
one operator environment override; refresh validates policy but publishes the raw
catalog without application filtering. Selection uses the configured ranking
strategy; approved unscored models follow. Credit scores to
Artificial Analysis; do not substitute Coding Agent Index or another effort's score.

## Explicit Variant Registration

To let the strategy choose the effort, use a list per model (single-binding
configs remain valid):

```json
{
  "bindings": {
    "pool/model-a": [
      {"benchmark_id": "aa-model-a-low", "effort": "low", "variant": "quick", "capability_approved": true},
      {"benchmark_id": "aa-model-a-high", "effort": "high", "variant": "careful", "capability_approved": true}
    ]
  },
  "variant_options": {
    "pool/model-a": {
      "quick": {"reasoningEffort": "low"},
      "careful": {"reasoningEffort": "high"}
    }
  }
}
```

Use real verified benchmark IDs, not these illustrative values. Omit prohibited
efforts (such as xhigh/max) from the approved binding list. Above-threshold low
wins under price-efficient at equal price; best-score instead uses the highest
measured score.
If low is below threshold or has no valid mapping, the eligible high variant can
still win. Only the selected variant is forwarded to OpenCode, and the model
appears once in the fallback chain. Availability cooldowns are not reset.

For a custom provider, a binding's `variant` name alone does not register the
options OpenCode must send. Merge the following fields into the shared config
only after verifying the real provider/model, stable AA ID, exact benchmark effort
and tool capability. These identifiers are illustrative, not rollout approvals:

```json
{
  "bindings": {
    "pool/model-a": {
      "benchmark_id": "aa-model-a-high",
      "effort": "high",
      "variant": "high",
      "capability_approved": true
    }
  },
  "variant_options": {
    "pool/model-a": {"reasoningEffort": "high"}
  }
}
```

`variant_options` is keyed by the full runtime identity, not by variant name;
its value is the option object itself, not another `high` wrapper. The runtime
combines this object with `bindings["pool/model-a"].variant` to register
`{"high": {"reasoningEffort": "high"}}` for that admitted OpenCode model.
This explicit registration prevents a custom provider from silently using default
effort while selection claims the benchmark's high-effort score. Each typed
`variant_options` entry currently requires exactly one field, `reasoningEffort`,
with one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`.
Arbitrary SDK fields are not allowed. Other adapters and effort mechanisms remain
pending until exact mappings are implemented and tested; do not represent them
with unvalidated option objects. The mapping is operator-owned configuration,
not inferred from benchmark metadata or proof of live provider support.

A missing entry for a named variant is excluded with `variant_mapping_required`.
An empty entry, unsupported value or extra field (including `disabled`) fails
typed configuration validation. `reasoningEffort` must equal the binding's effort
exactly; otherwise exclusion is `variant_effort_mismatch`. With no remaining
candidate the runtime fails before invocation, with no manual-chain bypass.
`explain` reports these decisions; it does not prove live provider execution or
production readiness.

## Refresh-Only Credentials

The credential JSON maps the configured provider `api_key_env` names and the
canonical `ARTIFICIAL_ANALYSIS_API_KEY` to nonempty secret strings. Ops must create
it through its secret provisioning process; do not place real keys in command
arguments, cron, docs, logs, Git or shared config. JSON preserves `$()`, quotes and
newlines as data, not shell commands. Duplicate/undeclared keys, nonstring values
and NUL are rejected. All configured provider keys and an AA key are required.
`ARTIFICAL_ANALYSIS_KEY` is a refresher-only legacy alias, normalized to canonical;
conflicting alias/canonical values fail.

Only these declared credentials enter the child environment, alongside fixed
`PATH`, explicit config and optional production threshold. No other inherited
environment is forwarded (including proxies, custom CA settings and other service
secrets). Provider credential names must be environment identifiers, not runtime
controls: `PATH`, `HOME`, `SHELL`, `ENV`, `BASH_ENV`, `LANG`, `LC_ALL`, config/threshold
variables, AA names and `PYTHON*`/`LD_*`/`DYLD_*` are reserved.

Use a dedicated refresh OS user, e.g. `agent-model-refresh`. Its secret directory
must be owned by that user with mode **0700**, and the credential file **0600**.
The launcher checks ownership/modes and rejects a symlink file or immediate parent.
Ops must ensure all ancestors are trusted and no ACL grants worker access.
The worker is a different OS identity, cannot read the secret directory/file,
and must never receive either AA environment key or the secret-file path. Provision
worker provider keys separately. Removing AA from child environments is necessary
but is not a sandbox: worker-readable refresh secrets are a deployment failure.

## Scheduling And Shared State

Ops provisions the cache root before launch with a dedicated group shared by
refresher and worker. Operational directories are **2770**, files including SQLite
database/WAL/SHM and lock/status/catalog files **0660**, and both services use
**umask 0007**. Setgid directories preserve shared group ownership. Do not apply
0700/0600 to shared operational state, and never put secrets under that root.
The launcher sets the umask, but does not create/chmod/chown the cache hierarchy;
explicit temporary-file modes must also be enforced by the cache implementation.

The launcher takes nonblocking `fcntl.flock` on `<cache_root>/refresh-launcher.lock`
and retains the descriptor through exec. The CLI independently takes `refresh.lock`
for catalog writes; distinct locks avoid self-contention. Manual and cron launches
must use this same launcher/cache path. Never unlink either lock during operation.
Use a local filesystem with flock and atomic same-filesystem rename support; no overlap is
allowed. This uses the flock syscall via stdlib, not a PATH-resolved utility.

[Cron template](../scripts/model-catalog.cron.example) uses `/etc/cron.d` syntax:
daily **03:15 in the host cron daemon's timezone**, with an explicit refresh user
and absolute launcher/interpreter/config/credential/log paths. Ops records the
actual host timezone and DST behavior, substitutes per-service paths and provisions
a restricted, rotated log before installation. The template contains no secrets.
This task does not install it or generate a privileged wrapper/service.

## Initial Refresh And Recovery

Before enabling discovery, ops must verify:

1. Released module installed in the absolute interpreter; trusted config is the
   same for refresh and worker. Run the launcher manually as the refresh user.
2. CLI exits zero, catalog is valid and refresh status shows success. Run
   `python -m agent_core.model_selection explain --config PATH` diagnostics show
   admitted identities/efforts and exclusions; confirm CLI availability first.
3. Worker and refresher can both open/write availability SQLite and sidecars,
   including after restart. Worker cannot read refresh secrets, and both OpenCode
   process/server child environments lack canonical and legacy AA keys.
4. Rotate provider keys in both identities' secret provisioning and increment
   `credential_generation` in trusted config. Refresh and restart/resume using the
   same cache root/provider scope; verify exact-scope auth recovery and that other
   scopes' cooldowns survive. Never include raw or hashed keys in evidence.
5. Verify cron parsing, recorded timezone, overlapping-run exit 75, nonzero
   refresh failure, preserved last-good catalog and bounded status diagnostics.
   Only then install the reviewed job and enable new discovery invocations.

Design budgets: one AA fetch and one inventory fetch per provider daily, 10s per
request, 30s total, 10 MiB decoded response and 10,000 records per source. No inline
retry storm or worker AA calls. Both inputs must be at most seven days old;
backward clock jumps are stale. These bounds belong to the CLI/cache, not this
launcher, and require integration verification before deployment.

On timeout/429/5xx/malformed input, retain last-good catalog and inspect sanitized
status. Missing/expired cache fails new discovery invocations closed: repair inputs
and manually refresh. Auth failures quarantine the exact endpoint/provider/scope/
generation until a successful authenticated inventory refresh, without clearing
unrelated rate limits. Review `benchmark_mapping_required`, unknown capability,
effort mismatch, score/filter exclusions and unavailable scopes in diagnostics;
never guess bindings or fall back to a manual chain inside discovery mode.

Rollback changes new invocations to the application's explicit manual mode and
preserves the last-good cache/availability data for diagnosis. Coordinate with ops;
do not interrupt active calls or silently switch an in-flight conversation.

## Launcher Regression Tests

Run `.venv/bin/python -m pytest tests/test_model_refresh_launcher.py -q` from
agent-core using a development venv with this repository installed editable.
The test checks that `-I` imports that installed package without `PYTHONPATH`,
then uses a separate Python shim to run the actual CLI with only HTTP transport
mocked. It verifies locks, credential filtering, catalog/status publication,
worker reloads, rotation and auth failure/recovery without live network or keys.
Cross-user permission denial and production scheduling still require ops checks.
