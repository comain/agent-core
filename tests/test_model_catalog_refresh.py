import asyncio
import json

import pytest
from test_model_configuration import write_config

from agent_core.model_selection.cache import CatalogCache
from agent_core.model_selection.configuration import load_selection_config
from agent_core.model_selection.refresh import refresh_catalog
from agent_core.model_selection.runtime import DiscoveryRuntime
from agent_core.model_selection.sources import SourceError


def config(tmp_path):
    return load_selection_config(write_config(tmp_path), environ={})


async def fake_fetch(url, *, headers, **kwargs):
    if "openrouter.ai" in url:
        assert headers == {}
        return {"data": [{"id": "vendor/a", "pricing": {"prompt": "0.000001",
                                                        "completion": "0.000003"}}]}
    if "artificialanalysis.ai" in url:
        assert headers == {"x-api-key": "benchmark-secret"}
        return {"data": [{"id": "a", "name": "a", "slug": "a",
                          "evaluations": {"artificial_analysis_coding_index": 80},
                          "untrusted_secret": "should-not-persist"}]}
    assert headers == {"Authorization": "Bearer provider-secret"}
    return {"data": [{"id": "a"}]}


def test_refresh_whitelists_and_publishes_worker_catalog(tmp_path):
    c = config(tmp_path)
    asyncio.run(refresh_catalog(c, environ={"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret",
                                           "POOL_KEY": "provider-secret"}, fetch=fake_fetch))
    data = CatalogCache(c).load()
    assert data["inventory"] == {"pool": ["a"]}
    assert data["benchmarks"]["records"][0]["score"] == 80
    assert "secret" not in json.dumps(data)


def test_provider_auth_failure_preserves_catalog_and_quarantines_scope(tmp_path):
    c = config(tmp_path)
    env = {"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret", "POOL_KEY": "provider-secret"}
    asyncio.run(refresh_catalog(c, environ=env, fetch=fake_fetch))
    original = CatalogCache(c).path.read_bytes()
    async def fail(url, **kwargs):
        if "artificialanalysis.ai" in url:
            return await fake_fetch(url, **kwargs)
        raise SourceError("auth_error", status_code=401)
    with pytest.raises(SourceError):
        asyncio.run(refresh_catalog(c, environ=env, fetch=fail))
    assert CatalogCache(c).path.read_bytes() == original
    r = DiscoveryRuntime(c, environ=env)
    assert not r.is_healthy("pool/a")
    asyncio.run(refresh_catalog(c, environ=env, fetch=fake_fetch))
    assert r.is_healthy("pool/a")


def test_refresh_failure_does_not_reset_other_model_cooldowns(tmp_path):
    c = config(tmp_path)
    r = DiscoveryRuntime(c, environ={})
    r.mark_unhealthy("pool/a", reason="rate_limit")
    asyncio.run(refresh_catalog(c, environ={"ARTIFICAL_ANALYSIS_KEY": "benchmark-secret",
                                          "POOL_KEY": "provider-secret"}, fetch=fake_fetch))
    assert not r.is_healthy("pool/a")


def test_missing_benchmark_key_is_explicit_no_fetch(tmp_path):
    with pytest.raises(ValueError, match="benchmark_credential_missing"):
        asyncio.run(refresh_catalog(config(tmp_path), environ={}, fetch=fake_fetch))


def test_missing_provider_key_records_failure_without_publishing(tmp_path):
    c = config(tmp_path)
    with pytest.raises(ValueError, match="^provider_credential_missing$"):
        asyncio.run(refresh_catalog(c, environ={"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret"}, fetch=fake_fetch))
    status = json.loads((c.cache_root / "refresh-status.json").read_text())
    assert status["last_error_code"] == "ValueError"
    assert status["last_success_at"] is None
    assert not CatalogCache(c).path.exists()


def test_internal_http_refresh_preserves_endpoint_and_explicit_transport(tmp_path):
    c = config(tmp_path)
    provider = c.providers[0].model_copy(update={"base_url": "http://pool.internal/v1"})
    c = c.model_copy(update={"providers": (provider,)})
    calls = []
    async def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return await fake_fetch(url, **kwargs)
    result = asyncio.run(refresh_catalog(c, environ={"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret",
                                                   "POOL_KEY": "provider-secret"}, fetch=fetch))
    assert calls[1] == ("http://pool.internal/v1/models", {
        "headers": {"Authorization": "Bearer provider-secret"}, "allow_http": True})
    assert result["provider_models"] == 1
    assert result["benchmark_models"] == 1
    assert result["last_error_code"] is None
    assert result["duration_seconds"] >= 0
    assert json.loads((c.cache_root / "refresh-status.json").read_text()) == result


def test_refresh_has_finite_thirty_second_deadline(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from agent_core.model_selection import refresh

    deadlines = []
    @asynccontextmanager
    async def deadline(seconds):
        deadlines.append(seconds)
        yield
    monkeypatch.setattr(refresh.asyncio, "timeout", deadline)
    asyncio.run(refresh_catalog(config(tmp_path), environ={"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret",
                                                          "POOL_KEY": "provider-secret"}, fetch=fake_fetch))
    assert deadlines == [30]


def test_failed_refresh_retains_previous_success_timestamp(tmp_path):
    c = config(tmp_path)
    CatalogCache(c).publish(inventory={}, benchmarks={"records": []})
    timestamp = CatalogCache(c).load()["fetched_at"]
    async def fail(*args, **kwargs):
        raise SourceError("http_error", status_code=500)
    with pytest.raises(SourceError):
        asyncio.run(refresh_catalog(c, environ={"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret"}, fetch=fail))
    status = json.loads((c.cache_root / "refresh-status.json").read_text())
    assert status["last_success_at"] == timestamp
    assert status["last_error_code"] == "http_error"


def test_benchmark_auth_failure_blocks_cached_execution_until_authenticated_recovery(tmp_path):
    from test_model_discovery_runtime import runtime

    from agent_core.model_selection.cache import CatalogUnavailable

    r = runtime(tmp_path)
    env = {"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret", "POOL_KEY": "provider-secret"}
    async def denied(url, **kwargs):
        raise SourceError("auth_error", status_code=401)
    with pytest.raises(SourceError):
        asyncio.run(refresh_catalog(r.config, environ=env, fetch=denied))
    with pytest.raises(CatalogUnavailable, match="benchmark_auth"):
        r.resolve()
    async def unavailable(url, **kwargs):
        raise SourceError("http_error", status_code=500)
    with pytest.raises(SourceError):
        asyncio.run(refresh_catalog(r.config, environ=env, fetch=unavailable))
    with pytest.raises(CatalogUnavailable, match="benchmark_auth"):
        r.resolve()
    asyncio.run(refresh_catalog(r.config, environ=env, fetch=fake_fetch))
    assert not (r.config.cache_root / "benchmark-auth-invalid.json").exists()


def test_refresh_publishes_public_list_prices_without_credentials(tmp_path):
    c = config(tmp_path)
    env = {"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret", "POOL_KEY": "provider-secret"}
    status = asyncio.run(refresh_catalog(c, environ=env, fetch=fake_fetch))
    records = CatalogCache(c).load()["pricing"]["records"]
    assert records == [{"pricing_id": "vendor/a", "slug": "a", "prompt": 1.0,
                        "completion": 3.0, "source": "https://openrouter.ai/",
                        "metric": "usd_per_million_tokens"}]
    assert (status["priced_models"], status["pricing_error_code"]) == (1, None)


def test_pricing_failure_keeps_last_prices_and_refreshes_the_rest(tmp_path):
    c = config(tmp_path)
    env = {"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret", "POOL_KEY": "provider-secret"}
    asyncio.run(refresh_catalog(c, environ=env, fetch=fake_fetch))
    async def pricing_down(url, **kwargs):
        if "openrouter.ai" in url:
            raise SourceError("http_error", status_code=503)
        return await fake_fetch(url, **kwargs)
    status = asyncio.run(refresh_catalog(c, environ=env, fetch=pricing_down))
    catalog = CatalogCache(c).load()
    assert catalog["pricing"] == {"records": [{"pricing_id": "vendor/a", "slug": "a",
                                               "prompt": 1.0, "completion": 3.0,
                                               "source": "https://openrouter.ai/",
                                               "metric": "usd_per_million_tokens"}],
                                  "stale": True}
    assert catalog["inventory"] == {"pool": ["a"]}
    assert (status["last_error_code"], status["pricing_error_code"]) == (None, "http_error")


def test_pricing_failure_without_previous_prices_leaves_them_unknown(tmp_path):
    c = config(tmp_path)
    async def pricing_down(url, **kwargs):
        if "openrouter.ai" in url:
            raise SourceError("invalid_pricing")
        return await fake_fetch(url, **kwargs)
    status = asyncio.run(refresh_catalog(c, environ={"ARTIFICIAL_ANALYSIS_API_KEY": "benchmark-secret",
                                                    "POOL_KEY": "provider-secret"}, fetch=pricing_down))
    assert CatalogCache(c).load()["pricing"]["records"] == []
    assert (status["priced_models"], status["pricing_error_code"]) == (0, "invalid_pricing")
