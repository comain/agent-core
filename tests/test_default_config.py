"""Replacing the fallback configuration for a whole process.

`use_config` scopes to a block and does not reach threads started outside it.
A consumer that keeps its settings under its own environment prefix needs the
fallback itself replaced, or every entry point has to be wrapped -- and a
missed one fails silently, because the default instance answers every read
with a plausible wrong value.
"""

from __future__ import annotations

import threading

import pytest
from pydantic_settings import SettingsConfigDict

from agent_core.config import (
    HarnessConfig,
    current_config,
    set_default_config,
    use_config,
)


class PrefixedConfig(HarnessConfig):
    model_config = SettingsConfigDict(env_prefix="UTA_", extra="ignore")


@pytest.fixture(autouse=True)
def restore_default():
    original = current_config()
    yield
    set_default_config(original)


def test_the_installed_config_answers_reads():
    installed = set_default_config(PrefixedConfig(opencode_bin="/opt/uta/opencode"))
    assert current_config() is installed
    assert current_config().opencode_bin == "/opt/uta/opencode"


def test_it_returns_what_it_installed():
    config = PrefixedConfig()
    assert set_default_config(config) is config


def test_it_reaches_threads_started_anywhere():
    """The reason this exists: `use_config` would not."""
    set_default_config(PrefixedConfig(opencode_bin="/opt/uta/opencode"))
    seen = []

    thread = threading.Thread(target=lambda: seen.append(current_config().opencode_bin))
    thread.start()
    thread.join()

    assert seen == ["/opt/uta/opencode"]


def test_a_scoped_config_still_wins_while_active():
    set_default_config(PrefixedConfig(opencode_bin="/default/opencode"))

    with use_config(HarnessConfig(opencode_bin="/scoped/opencode")):
        assert current_config().opencode_bin == "/scoped/opencode"

    assert current_config().opencode_bin == "/default/opencode"


def test_the_settings_proxy_follows_the_new_default():
    """Harness modules hold `settings` at import time; it must not go stale."""
    from agent_core.config import settings

    set_default_config(PrefixedConfig(opencode_bin="/opt/uta/opencode"))

    assert settings.opencode_bin == "/opt/uta/opencode"


def test_an_environment_prefix_is_honoured(monkeypatch):
    monkeypatch.setenv("UTA_OPENCODE_BIN", "/from/uta/env")

    set_default_config(PrefixedConfig())

    assert current_config().opencode_bin == "/from/uta/env"
