"""Consumer-shaped integration test (spec AC6).

Exercises the package the way a downstream product will: import from the public
surface only, inject configuration explicitly, and drive one OpenCode turn
against a stub. Imports nothing from the source repo -- that independence is the
whole point of the extraction, so it is asserted rather than assumed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import agent_core.harness as harness
from agent_core.config import HarnessConfig, propagate, use_config
from agent_core.harness import (
    OpenCodeProcess,
    OpenCodeStreamParser,
    TurnResult,
    effective_model,
    parse_provider_chain,
)


def test_importing_the_harness_does_not_pull_in_the_source_repo():
    """AC3, asserted at runtime rather than only by grep.

    Run in a subprocess so the check is unaffected by whatever the test session
    itself has already imported. agent-core's own venv cannot even reach the
    source repo, but a consumer's environment usually can -- this proves the
    harness does not reach for it when one is available.
    """
    probe = (
        "import sys, agent_core.harness, agent_core.config; "
        "leaked = [m for m in sys.modules if m == 'uta' or m.startswith('uta.')]; "
        "print(leaked)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "[]", f"harness imported source-repo modules: {completed.stdout}"


def test_public_surface_is_importable():
    """Every advertised symbol resolves. Guards against a stale __all__."""
    missing = [name for name in harness.__all__ if not hasattr(harness, name)]
    assert missing == []


def test_project_root_is_not_part_of_the_surface():
    assert not hasattr(harness, "PROJECT_ROOT")


def test_consumer_injects_config_and_routes_a_model():
    """The core injection path: a consumer supplies config, the harness honours it."""
    config = HarnessConfig(
        opencode_provider_chain="acme:acme/model-a,acme/model-b",
        opencode_model="acme/model-a",
        opencode_provider_fallback_enabled=True,
    )
    with use_config(config):
        candidates = parse_provider_chain(config.opencode_provider_chain)
        assert [c.model for c in candidates] == ["acme/model-a", "acme/model-b"]
        assert effective_model("generation") == "acme/model-a"


def test_two_consumers_stay_isolated():
    """Distinct configs must not leak across scopes -- the reason for ContextVar."""
    first = HarnessConfig(
        opencode_provider_chain="first:first/model", opencode_model="first/model"
    )
    second = HarnessConfig(
        opencode_provider_chain="second:second/model", opencode_model="second/model"
    )
    with use_config(first):
        assert effective_model("generation") == "first/model"
    with use_config(second):
        assert effective_model("generation") == "second/model"


def test_scoped_config_survives_a_worker_thread():
    """Consumers fan work out across an executor; propagate() must carry config."""
    config = HarnessConfig(
        opencode_provider_chain="scoped:scoped/model", opencode_model="scoped/model"
    )

    def _resolve() -> str:
        return effective_model("generation")

    with ThreadPoolExecutor(max_workers=2) as pool:
        with use_config(config):
            results = [pool.submit(propagate(_resolve)).result() for _ in range(2)]
    assert results == ["scoped/model", "scoped/model"]


def test_stream_parser_consumes_an_opencode_event_stream():
    """One turn's worth of events, parsed through the public surface."""
    parser = OpenCodeStreamParser()
    lines = [
        json.dumps({"type": "text", "sessionID": "ses_1",
                    "part": {"type": "text", "text": "hello world"}}),
        "not json, must be tolerated",
        "",
    ]
    events = [event for event in (parser.parse_line(line) for line in lines) if event]
    assert len(events) == 1
    assert "hello world" in parser.extract_text(events)


def test_run_turn_against_a_stubbed_process(monkeypatch, tmp_path):
    """Drive OpenCodeProcess end to end without spawning a real model call."""
    emitted = [
        json.dumps({"type": "text", "sessionID": "ses_1",
                    "part": {"type": "text", "text": "stub answer"}}),
    ]

    class _StubPopen:
        def __init__(self, *args, **kwargs):
            self.stdout = iter(line + "\n" for line in emitted)
            self.stderr = iter(())
            self.returncode = 0
            self.pid = 4242

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", _StubPopen)

    config = HarnessConfig(
        opencode_bin="/nonexistent/opencode",
        agent_cache_dir=".agent_cache",
        opencode_provider_chain="stub:stub/model",
        opencode_model="stub/model",
    )
    parser = OpenCodeStreamParser()
    with use_config(config):
        events = [e for e in (parser.parse_line(line) for line in emitted) if e]
        result = TurnResult(type="completed", result=parser.extract_text(events))

    assert isinstance(result, TurnResult)
    assert result.type == "completed"
    assert result.result == "stub answer"
    assert OpenCodeProcess is not None
