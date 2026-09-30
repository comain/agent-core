"""Where the harness looks for a provider's rate-limit response.

The consumer's own CLI writes agent run logs, and the harness reads them back
hunting for a 429. A mismatch between the two paths fails nothing loudly: the
evidence is simply never found, and the run walks the entire provider chain
instead of backing off.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_core.config import HarnessConfig, current_config, set_default_config
from agent_core.harness.rate_limit import debug_log_dir


@pytest.fixture(autouse=True)
def restore_default():
    original = current_config()
    yield
    set_default_config(original)


def test_the_default_is_unchanged():
    set_default_config(HarnessConfig())
    assert debug_log_dir() == Path(tempfile.gettempdir()) / "agent-run-logs"


def test_a_bare_name_resolves_under_the_temp_directory():
    """How a consumer keeps the name its deployed nodes already write."""
    set_default_config(HarnessConfig(agent_debug_log_dir="uta-run-logs"))
    assert debug_log_dir() == Path(tempfile.gettempdir()) / "uta-run-logs"


def test_an_absolute_path_is_taken_as_given(tmp_path):
    set_default_config(HarnessConfig(agent_debug_log_dir=str(tmp_path / "logs")))
    assert debug_log_dir() == tmp_path / "logs"


def test_blank_and_whitespace_fall_back_to_the_default():
    for value in ("", "   "):
        set_default_config(HarnessConfig(agent_debug_log_dir=value))
        assert debug_log_dir() == Path(tempfile.gettempdir()) / "agent-run-logs"


def test_recent_log_files_reads_the_configured_directory(tmp_path):
    """The reason the setting exists at all."""
    from agent_core.harness.rate_limit import recent_log_files

    log_dir = tmp_path / "consumer-logs"
    log_dir.mkdir()
    written = log_dir / "20260813_opencode_turn.log"
    written.write_text("service=llm error={}")
    set_default_config(HarnessConfig(agent_debug_log_dir=str(log_dir)))

    assert written in recent_log_files()
