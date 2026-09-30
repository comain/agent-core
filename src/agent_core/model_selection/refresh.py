"""Refresh-only network access; workers consume its validated local output."""

import asyncio
import time
from collections.abc import Mapping

from .availability import AvailabilityStore
from .cache import CatalogCache, atomic_json
from .configuration import SelectionConfig
from .runtime import credential_scope
from .sources import SourceError, fetch_json, parse_benchmarks, parse_inventory, parse_pricing

AA_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
# Public, unauthenticated list prices, refreshed on the same daily schedule.
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"


async def _refresh_pricing(cache, fetch) -> tuple[dict, str | None]:
    """Fetch list prices, keeping the last published ones if the source fails.

    Pricing is public supplementary evidence: losing it must not stale the whole
    catalog and strand every application, so the previous prices are carried
    forward and the failure is reported in refresh status instead.
    """
    try:
        records = parse_pricing(await fetch(OPENROUTER_URL, headers={}))
    except (SourceError, OSError, TimeoutError) as error:
        try:
            previous = cache.load()["pricing"]["records"]
        except (ValueError, KeyError, TypeError):
            previous = []
        code = error.code if isinstance(error, SourceError) else type(error).__name__
        return {"records": previous, "stale": True}, code
    return {"records": [r.model_dump(mode="json") for r in records]}, None


async def refresh_catalog(config: SelectionConfig, *, environ: Mapping[str, str], fetch=fetch_json) -> dict:
    """One atomic catalog publication with bounded HTTP and auth-state recovery."""
    key = environ.get("ARTIFICIAL_ANALYSIS_API_KEY") or environ.get("ARTIFICAL_ANALYSIS_KEY")
    if not key:
        raise ValueError("benchmark_credential_missing")
    cache = CatalogCache(config)
    store = AvailabilityStore(config.availability_db)
    status_path = config.cache_root / "refresh-status.json"
    auth_block = config.cache_root / "benchmark-auth-invalid.json"
    started = time.time()
    with cache.refresh_lock():
        try:
            async with asyncio.timeout(30):
                try:
                    records = parse_benchmarks(await fetch(AA_URL, headers={"x-api-key": key}))
                except SourceError as error:
                    if error.status_code in (401, 403):
                        atomic_json(auth_block, {"reason": "benchmark_auth_failed", "observed_at": time.time()})
                    raise
                # A transient refresh failure must not clear an earlier auth
                # rejection. Only authenticated benchmark recovery may clear it.
                auth_block.unlink(missing_ok=True)
                inventory = {}
                for provider in config.providers:
                    token = environ.get(provider.api_key_env)
                    if not token:
                        raise ValueError("provider_credential_missing")
                    scope = credential_scope(provider)
                    observed = time.time()
                    try:
                        payload = await fetch(provider.base_url + "/models",
                                              headers={"Authorization": f"Bearer {token}"},
                                              **({"allow_http": True} if provider.base_url.startswith("http://") else {}))
                        inventory[provider.id] = parse_inventory(payload)
                    except SourceError as error:
                        if error.status_code in (401, 403):
                            store.record_failure(scope, "__inventory__", "provider_auth_failed", observed_at=observed)
                        raise
                    store.clear_auth_quarantine(scope, observed_at=observed)
                pricing, pricing_error = await _refresh_pricing(cache, fetch)
                cache.publish(inventory=inventory,
                              benchmarks={"records": [r.model_dump(mode="json") for r in records]},
                              pricing=pricing)
            status = {"last_success_at": time.time(), "last_error_code": None,
                      "duration_seconds": time.time() - started,
                      "benchmark_models": len(records),
                      "priced_models": len(pricing["records"]),
                      "pricing_error_code": pricing_error,
                      "provider_models": sum(map(len, inventory.values()))}
            atomic_json(status_path, status)
            return status
        except (ValueError, OSError, TimeoutError) as error:
            code = error.code if isinstance(error, SourceError) else type(error).__name__
            previous_success = None
            try:
                previous_success = cache.load()["fetched_at"]
            except ValueError:
                pass
            atomic_json(status_path, {"last_success_at": previous_success,
                                      "last_error_code": code, "failed_at": time.time()})
            raise
