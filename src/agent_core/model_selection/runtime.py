"""Bridge cached quality policy and scoped availability to harness execution."""

from collections.abc import Mapping
from dataclasses import replace

from .availability import AvailabilityStore, CredentialScope
from .cache import CatalogCache, CatalogUnavailable
from .configuration import SelectionConfig
from .policy import Candidate, ResolvedSelection, resolve_selection
from .sources import BenchmarkRecord, PriceRecord, bind_candidates, price_candidates


class NoAvailableModels(ValueError):
    """Discovery may not fall through to a manual/default model."""


def credential_scope(provider) -> CredentialScope:
    return CredentialScope(provider.id, provider.base_url,
                           provider.credential_scope_id, provider.credential_generation)


class DiscoveryRuntime:
    """One application's trusted config; resolve catalog anew for each invocation."""

    def __init__(self, config: SelectionConfig, *, environ: Mapping[str, str]):
        self.config = config
        self._environ = dict(environ)
        self._providers = {p.id: p for p in config.providers}
        self.store = AvailabilityStore(config.availability_db)

    def _scope(self, model: str):
        provider, separator, local = model.partition("/")
        if not separator or provider not in self._providers:
            raise ValueError("model is outside configured provider scopes")
        return credential_scope(self._providers[provider]), local

    def is_healthy(self, model: str) -> bool:
        scope, local = self._scope(model)
        return self.store.status(scope, local) is None

    def mark_unhealthy(self, model: str, *, reason: str) -> None:
        scope, local = self._scope(model)
        self.store.record_failure(scope, local, reason)

    def mark_success(self, model: str, *, observed_at: float) -> None:
        """A completed submission may clear only failures older than its start."""
        scope, local = self._scope(model)
        self.store.record_success(scope, local, observed_at=observed_at)

    def resolve(self, *, require_available: bool = True, effort_strategy: str = "default") -> ResolvedSelection:
        if (self.config.cache_root / "benchmark-auth-invalid.json").exists():
            raise CatalogUnavailable("benchmark_auth_failed; authenticated refresh required")
        catalog = CatalogCache(self.config).load()
        records = tuple(BenchmarkRecord.model_validate(r) for r in catalog["benchmarks"]["records"])
        candidates = bind_candidates(catalog["inventory"], records, self.config.bindings,
                                     default_effort=self.config.default_effort)
        # Prices are published evidence; the paid price is the provider's
        # discounted one, so a catalog without pricing leaves it unknown.
        prices = tuple(PriceRecord.model_validate(r)
                       for r in catalog.get("pricing", {}).get("records", []))
        candidates = price_candidates(candidates, prices, self.config.bindings,
                                      {p.id: p.pricing_discount for p in self.config.providers},
                                      self.config.price_weights)
        result = resolve_selection(candidates, self.config.policy,
                                   shared_denylist=self.config.shared_denylist,
                                   admission_check=self._runtime_rejection,
                                   effort_strategy=effort_strategy)
        # Stable grouping preserves the policy's model/effort ranking within
        # each provider, while keeping configured fallback providers secondary.
        priority = {provider.id: index for index, provider in enumerate(self.config.providers)}
        result = replace(result, candidates=tuple(sorted(
            result.candidates, key=lambda candidate: priority[candidate.identity.partition("/")[0]])))
        if not result.candidates and require_available:
            reasons = sorted({d.reason for d in result.decisions})
            raise NoAvailableModels("no_available_models: " + ", ".join(reasons))
        return result

    def _variant_options(self, candidate: Candidate) -> dict:
        options = self.config.variant_options.get(candidate.identity, {})
        return options if "reasoningEffort" in options else options.get(candidate.variant, {})

    def _runtime_rejection(self, candidate: Candidate) -> str | None:
        # Check each variant BEFORE deduplicating: an unusable low-effort option
        # must not hide a valid high-effort option for the same runtime model.
        options = self._variant_options(candidate)
        provider = self._providers[candidate.identity.partition("/")[0]]
        if candidate.variant and not options:
            return "variant_mapping_required"
        if "reasoningEffort" in options and options["reasoningEffort"] != candidate.effort:
            return "variant_effort_mismatch"
        if not self._environ.get(provider.api_key_env):
            return "provider_credential_missing"
        if not self.is_healthy(candidate.identity):
            return "model_unavailable"
        return None

    def config_updates(self, selection: ResolvedSelection, model: str) -> dict:
        candidate = next((c for c in selection.candidates if c.identity == model), None)
        if candidate is None:
            raise ValueError("model is outside resolved discovery selection")
        provider = model.partition("/")[0]
        # Provider registration is scoped to the admitted list, never the old chain.
        groups = {}
        for c in selection.candidates:
            groups.setdefault(c.identity.partition("/")[0], []).append(c.identity)
        return {
            "opencode_selection_mode": "discovery",
            "opencode_model": model,
            "opencode_small_model": model,
            "opencode_provider": provider,
            "opencode_variant": candidate.variant,
            "opencode_discovery_variants": {
                c.identity: {c.variant: self._variant_options(c)}
                for c in selection.candidates if c.variant
            },
            "opencode_provider_chain": ";".join(f"{p}:{','.join(ids)}" for p, ids in groups.items()),
            "opencode_provider_tokens": ";".join(
                f"{p.id}.token={{env:{p.api_key_env}}}" for p in self.config.providers
                if self._environ.get(p.api_key_env)),
            "opencode_credential_env": {
                p.api_key_env: self._environ[p.api_key_env] for p in self.config.providers
                if self._environ.get(p.api_key_env)
            },
            "opencode_provider_base_urls": ";".join(
                f"{p.id}.base_url={p.base_url}" for p in self.config.providers),
        }
