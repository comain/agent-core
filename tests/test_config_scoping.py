"""Tests for the injectable harness configuration layer.

These are new tests, not ported ones: the config layer is the one part of
agent-core that has no counterpart in the source repo.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_core.config import (
    HarnessConfig,
    _default,
    current_config,
    propagate,
    settings,
    use_config,
)


def test_current_config_returns_module_default_when_unscoped():
    assert current_config() is _default


def test_use_config_scopes_and_restores():
    scoped = HarnessConfig(opencode_model="scoped/model")
    assert current_config() is _default
    with use_config(scoped):
        assert current_config() is scoped
        assert current_config().opencode_model == "scoped/model"
    assert current_config() is _default


def test_use_config_restores_on_exception():
    scoped = HarnessConfig(opencode_model="scoped/model")
    with pytest.raises(RuntimeError):
        with use_config(scoped):
            raise RuntimeError("boom")
    assert current_config() is _default


def test_nested_use_config():
    outer = HarnessConfig(opencode_model="outer/model")
    inner = HarnessConfig(opencode_model="inner/model")
    with use_config(outer):
        with use_config(inner):
            assert current_config().opencode_model == "inner/model"
        assert current_config().opencode_model == "outer/model"


def test_ported_tests_can_patch_the_module_default(monkeypatch):
    """The idiom every ported test file relies on."""
    monkeypatch.setattr("agent_core.config.settings.opencode_model", "patched/model")
    assert current_config().opencode_model == "patched/model"


# -- the ThreadPoolExecutor hazard ----------------------------------------------
#
# A worker thread starts with an empty context, so a scoped config does not reach
# it. This is not theoretical: consumers fan work out across an executor, and a
# silent fallback to the module default would use the wrong provider or
# credentials with no error. Both halves are pinned by tests so the hazard cannot
# regress into a documentation-only warning.


def _read_model() -> str:
    return current_config().opencode_model


def test_bare_submit_does_not_carry_scoped_config():
    scoped = HarnessConfig(opencode_model="scoped/model")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with use_config(scoped):
            got = pool.submit(_read_model).result()
    assert got == _default.opencode_model
    assert got != "scoped/model"


def test_propagate_carries_scoped_config_across_a_thread_hop():
    scoped = HarnessConfig(opencode_model="scoped/model")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with use_config(scoped):
            got = pool.submit(propagate(_read_model)).result()
    assert got == "scoped/model"


def test_propagate_passes_arguments_through():
    def _join(a: str, b: str = "") -> str:
        return f"{a}{b}{current_config().opencode_model}"

    scoped = HarnessConfig(opencode_model="X")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with use_config(scoped):
            got = pool.submit(propagate(_join), "a", b="b").result()
    assert got == "abX"


# -- secret redaction -----------------------------------------------------------
#
# Moving provider credentials across a package boundary is a new exposure
# surface; turn logs and debug dumps must not carry them.

SECRETS = [
    "openai_api_key",
    "deepseek_api_key",
    "tencent_api_key",
    "gemini_api_key",
    "openrouter_api_key",
    "opencode_provider_tokens",
]


@pytest.mark.parametrize("field", SECRETS)
def test_secrets_redacted_in_repr(field):
    config = HarnessConfig(**{field: "super-secret-value"})
    assert "super-secret-value" not in repr(config)
    assert "super-secret-value" not in str(config)
    assert "***" in repr(config)


@pytest.mark.parametrize("field", SECRETS)
def test_secrets_redacted_in_model_dump(field):
    config = HarnessConfig(**{field: "super-secret-value"})
    assert config.model_dump()[field] == "***"


def test_empty_secret_is_not_redacted_into_a_fake_value():
    config = HarnessConfig()
    assert config.model_dump()["openai_api_key"] is None


def test_non_secret_fields_are_not_redacted():
    config = HarnessConfig(opencode_model="visible/model")
    assert "visible/model" in repr(config)
    assert config.model_dump()["opencode_model"] == "visible/model"


# -- environment binding --------------------------------------------------------


def test_env_prefix_applies(monkeypatch):
    monkeypatch.setenv("AGENT_OPENCODE_MODEL", "env/model")
    assert HarnessConfig().opencode_model == "env/model"


def test_subclassing_overrides_env_prefix(monkeypatch):
    from pydantic_settings import SettingsConfigDict

    class CrHarnessConfig(HarnessConfig):
        model_config = SettingsConfigDict(env_prefix="CR_", extra="ignore")

    monkeypatch.setenv("CR_OPENCODE_MODEL", "cr/model")
    assert CrHarnessConfig().opencode_model == "cr/model"


def test_no_product_branded_env_names_are_claimed(monkeypatch):
    """Deviation D4: the source bound credentials to UTA_-prefixed names."""
    monkeypatch.setenv("UTA_OPENAI_API_KEY", "should-not-be-read")
    monkeypatch.setenv("UTA_BASE_URL", "should-not-be-read")
    config = HarnessConfig()
    assert config.openai_api_key is None
    assert config.openai_base_url is None


def test_undeclared_threshold_field_stays_undeclared():
    """C2: declaring it would silently activate a knob that has never worked."""
    assert "opencode_prompt_file_threshold_chars" not in HarnessConfig.model_fields
    assert getattr(HarnessConfig(), "opencode_prompt_file_threshold_chars", None) is None


# -- the settings proxy ---------------------------------------------------------
#
# Harness modules import `settings` at module scope and read `settings.X`, exactly
# as the source did. The proxy forwards each access to the active configuration,
# which is what lets the port leave all 99 read sites untouched while still
# honouring use_config(). Both patching idioms the ported tests rely on are
# pinned here.


def test_proxy_reads_follow_scoped_config():
    scoped = HarnessConfig(opencode_model="scoped/model")
    assert settings.opencode_model == _default.opencode_model
    with use_config(scoped):
        assert settings.opencode_model == "scoped/model"
    assert settings.opencode_model == _default.opencode_model


def test_proxy_raises_attribute_error_for_unknown_names():
    """So that `getattr(settings, "missing", default)` still yields its default."""
    with pytest.raises(AttributeError):
        settings.definitely_not_a_field
    assert getattr(settings, "definitely_not_a_field", "fallback") == "fallback"


def test_proxy_forwards_the_undeclared_threshold_read():
    """The exact form harness/process.py uses for the dead knob (C2)."""
    assert int(getattr(settings, "opencode_prompt_file_threshold_chars", 0) or 60000) == 60000


def test_monkeypatch_on_the_proxy_reaches_the_default(monkeypatch):
    """Ported-test idiom B: monkeypatch.setattr("agent_core.config.settings.X", v)."""
    monkeypatch.setattr("agent_core.config.settings.opencode_model", "patched/model")
    assert settings.opencode_model == "patched/model"
    assert _default.opencode_model == "patched/model"


def test_proxy_writes_land_on_the_scoped_config():
    scoped = HarnessConfig(opencode_model="scoped/model")
    with use_config(scoped):
        settings.opencode_model = "written/model"
        assert scoped.opencode_model == "written/model"
    assert _default.opencode_model != "written/model"


# -- D5 compatibility window ----------------------------------------------------


def test_agent_cache_dir_is_configurable_and_neutral_by_default():
    """63 sites in the source repo read `.uta_cache`; it had to become a setting."""
    assert HarnessConfig().agent_cache_dir == ".agent_cache"
    assert HarnessConfig(agent_cache_dir=".uta_cache").agent_cache_dir == ".uta_cache"


def test_propagate_is_reusable_across_concurrent_workers():
    """One wrapped callable, many threads at once.

    An earlier implementation captured a contextvars.Context and called
    Context.run on it. A Context cannot be entered twice simultaneously, so
    reusing the wrapped callable across a pool -- the case propagate exists for
    -- raised "cannot enter context: ... is already entered". The single-call
    tests above did not catch it.
    """
    scoped = HarnessConfig(opencode_model="scoped/model")
    with ThreadPoolExecutor(max_workers=4) as pool:
        with use_config(scoped):
            wrapped = propagate(_read_model)
            results = list(pool.map(lambda _: wrapped(), range(8)))
    assert results == ["scoped/model"] * 8


def test_propagate_restores_the_worker_context_after_each_call():
    scoped = HarnessConfig(opencode_model="scoped/model")
    with use_config(scoped):
        wrapped = propagate(_read_model)
    # called outside any scope: still sees the captured config, and leaves no residue
    assert wrapped() == "scoped/model"
    assert current_config() is _default


# -- dumping a configuration ------------------------------------------------

def test_a_dump_redacts_secrets_by_default():
    """The common use is showing this somewhere, and a token that reaches a log
    is a token to rotate."""
    from agent_core.config import HarnessConfig

    config = HarnessConfig(opencode_provider_tokens="token-pool.token=secret-value")

    assert config.model_dump()["opencode_provider_tokens"] == "***"
    assert "secret-value" not in repr(config)


def test_a_revealed_dump_round_trips():
    """The other use is rebuilding a configuration from a dump — how a consumer
    hands settings to a harness. A redacted dump is plausible rather than
    obviously broken there: every field present, the token a non-empty string,
    and the only symptom a 401 from the provider several layers away."""
    from agent_core.config import HarnessConfig
    from agent_core.harness.tiered_router import parse_provider_tokens

    config = HarnessConfig(opencode_provider_tokens="token-pool.token=secret-value")

    revealed = config.model_dump(reveal_secrets=True)
    assert revealed["opencode_provider_tokens"] == "token-pool.token=secret-value"
    assert list(parse_provider_tokens(revealed["opencode_provider_tokens"])) == ["token-pool"]

    rebuilt = HarnessConfig(**revealed)
    assert parse_provider_tokens(rebuilt.opencode_provider_tokens).get("token-pool") == (
        "secret-value"
    )


def test_a_redacted_dump_does_not_round_trip():
    """Naming the trap: rebuilding from the default dump yields a config that
    looks configured and cannot authenticate."""
    from agent_core.config import HarnessConfig
    from agent_core.harness.tiered_router import parse_provider_tokens

    config = HarnessConfig(opencode_provider_tokens="token-pool.token=secret-value")

    rebuilt = HarnessConfig(**config.model_dump())

    assert rebuilt.opencode_provider_tokens == "***"
    assert parse_provider_tokens(rebuilt.opencode_provider_tokens) == {}
