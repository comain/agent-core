"""Tests pinning the deliberate deviations from the source harness.

These are new tests, kept out of the ported files so the oracle stays exactly
what it was. Each one exists because the port intentionally diverged, and each
divergence should fail loudly if someone later "tidies" it away.

Deviation register lives in the design doc; the identifiers below match it.
"""

from __future__ import annotations

import inspect

import pytest
from pathlib import Path

from agent_core.config import HarnessConfig
from agent_core.harness import process, rate_limit


# -- D1: product-branded public helper and runtime path -------------------------


def test_debug_log_dir_is_not_product_branded():
    assert hasattr(rate_limit, "debug_log_dir")
    assert not hasattr(rate_limit, "uta_debug_log_dir")
    assert "agent-run-logs" in str(rate_limit.debug_log_dir())
    assert "uta" not in str(rate_limit.debug_log_dir()).lower()


# -- D5: cross-repo contracts kept alive during a compatibility window ----------
#
# The source repo's Python verifier reads UTA_SERVICE_PYTHON_BIN, and 63 sites in
# that repo read `.uta_cache`. Renaming outright would break them silently on
# migration, so both are emitted/configurable until those consumers move.


def test_build_env_emits_both_service_python_names(monkeypatch, tmp_path):
    service_bin = tmp_path / "venv" / "bin"
    service_bin.mkdir(parents=True)
    service_python = service_bin / "python"
    service_python.write_text("", encoding="utf-8")
    monkeypatch.setattr("agent_core.harness.process.sys.executable", str(service_python))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = process._build_env(str(tmp_path))

    resolved = str(service_python.resolve())
    assert env["AGENT_SERVICE_PYTHON_BIN"] == resolved, "neutral name must be emitted"
    assert env["UTA_SERVICE_PYTHON_BIN"] == resolved, "legacy name still has a live consumer"


def test_agent_cache_dir_is_configurable_with_a_neutral_default():
    assert HarnessConfig().agent_cache_dir == ".agent_cache"
    assert HarnessConfig(agent_cache_dir=".uta_cache").agent_cache_dir == ".uta_cache"


def test_prompt_file_path_honours_the_configured_cache_dir(tmp_path):
    from agent_core.config import HarnessConfig, use_config

    with use_config(HarnessConfig(agent_cache_dir=".custom_cache")):
        written = process._write_prompt_file(str(tmp_path), "hello")
    assert ".custom_cache" in str(written)
    assert ".uta_cache" not in str(written)


# -- D2: PROJECT_ROOT must not be derived from package depth --------------------
#
# The source computed it as parents[2], which resolved to that repo's root. Under
# this package's layout the same expression resolves to `src/`, so it stays valid
# Python while silently changing meaning.


def test_project_root_is_not_depth_derived():
    from agent_core.harness import config as harness_config

    source = inspect.getsource(harness_config)
    assert "parents[2]" not in source, "depth-derived PROJECT_ROOT changes meaning under this layout"


def test_project_root_is_not_exported():
    from agent_core import harness

    assert "PROJECT_ROOT" not in getattr(harness, "__all__", [])


# -- C2: a knob that has never worked must not start working --------------------


def test_prompt_file_threshold_stays_undeclared():
    assert "opencode_prompt_file_threshold_chars" not in HarnessConfig.model_fields


# -- AC3 / AC4: the package must not carry the source product into a shared home -


def _harness_sources():
    root = Path(__file__).resolve().parents[1] / "src" / "agent_core"
    return sorted(root.rglob("*.py"))


def test_no_source_repo_imports_anywhere():
    """AC3."""
    offenders = []
    for path in _harness_sources():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "import uta" in line or "from uta." in line or "unit_test_agent" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "source-repo imports leaked into the package"


def test_only_allowlisted_references_to_the_source_product():
    """AC4, widened after review found `uta-run-logs` slipping past the original grep.

    Every surviving mention must be a recorded deviation. Anything else means a
    product-specific value rode along into a package three products will share.
    """
    allowed_substrings = (
        "Ported from ``uta/config.py``",          # provenance note
        "source default was ``.uta_cache/",        # D3
        "the source hard-coded ``.uta_cache``",    # D5
        "``UTA_OPENAI_API_KEY``, ``UTA_BASE_URL``",  # D4
        'env.setdefault("UTA_SERVICE_PYTHON_BIN"',   # D5, load-bearing
    )
    false_positives = ("mutat", "mutab", "executa", "refuta", "comput")

    offenders = []
    for path in _harness_sources():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            lowered = line.lower()
            if "uta" not in lowered:
                continue
            if any(fp in lowered for fp in false_positives):
                continue
            if any(allowed in line for allowed in allowed_substrings):
                continue
            offenders.append(f"{path.relative_to(path.parents[2])}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "unallowlisted references to the source product:\n  " + "\n  ".join(offenders)
    )


# -- D6: attachments ------------------------------------------------------------
#
# `opencode run` takes -f/--file, which is how an image reaches a vision model.
# Validated live: a 64x64 blue PNG attached this way was correctly described as
# "Blue" by gpt-5.5 through the harness.


def _cmd(**kw):
    from agent_core.config import HarnessConfig, use_config
    with use_config(HarnessConfig(opencode_bin="/x/opencode", opencode_provider_chain="p:p/m")):
        return process.OpenCodeProcess()._build_cmd(
            "hello", session_id=None, model_id="p/m", repo_path="/repo", **kw
        )


def test_command_is_unchanged_when_no_attachments():
    """Additive: every existing caller must emit exactly what it did before."""
    assert _cmd() == _cmd(attachments=None) == _cmd(attachments=[])
    assert "-f" not in _cmd()


def test_attachments_are_appended_as_file_flags():
    cmd = _cmd(attachments=["/tmp/a.png", "/tmp/b.png"])
    assert cmd[-4:] == ["-f", "/tmp/a.png", "-f", "/tmp/b.png"]


def test_attachments_come_after_the_message():
    """-f takes an array; placing it before the message makes the CLI swallow it.

    The observed failure is 'File not found: <the entire prompt>'.
    """
    cmd = _cmd(attachments=["/tmp/a.png"])
    assert cmd.index("-f") > cmd.index("hello")


def test_attachment_paths_are_stringified():
    from pathlib import Path
    assert _cmd(attachments=[Path("/tmp/a.png")])[-1] == "/tmp/a.png"


# -- D7/D8: capabilities a consumer's fork had and the source did not -----------
#
# Found by attempting a real migration. The spec had recorded that fork as a
# "trimmed adaptation" of the source -- true by module count, false by
# capability. Without these, migrating it would silently lose the ability to
# stop a running turn.


def test_turn_result_carries_model_id_and_cost():
    r = process.TurnResult(type="completed", model_id="p/m", cost_usd=0.42)
    assert r.model_id == "p/m" and r.cost_usd == 0.42


def test_turn_result_new_fields_default_to_none():
    """Additive: callers that ignore them are unaffected."""
    r = process.TurnResult(type="completed")
    assert r.model_id is None and r.cost_usd is None


def test_run_turn_accepts_a_cancellation_predicate():
    import inspect
    sig = inspect.signature(process.OpenCodeProcess.run_turn)
    assert "is_cancelled" in sig.parameters
    assert sig.parameters["is_cancelled"].default is None


def test_read_stream_accepts_a_cancellation_predicate():
    import inspect
    sig = inspect.signature(process.OpenCodeProcess._read_stream)
    assert "is_cancelled" in sig.parameters


def test_cancellation_is_checked_and_terminates_the_process():
    src = inspect.getsource(process.OpenCodeProcess._read_stream)
    assert "if is_cancelled is not None and is_cancelled():" in src
    assert "_terminate_opencode_process(proc)" in src


def test_cancelled_result_is_not_fallback_eligible():
    """An operator pressing stop must not roll on to the next provider.

    Otherwise cancelling a turn just moves the spend to another model.
    """
    src = inspect.getsource(process.OpenCodeProcess._read_stream)
    assert 'type="cancelled"' in src
    assert "fallback_eligible=False" in src


# -- D10: prompt file as a first-class input ------------------------------------
#
# The source only writes a prompt file above a size threshold, and that threshold
# reads an undeclared setting (C2), so short prompts always travelled as an argv
# string. A caller-supplied file is the better contract: argv has an OS length
# limit, argv is visible in `ps` to every user on the host, and the file is a
# durable artifact that makes a failed turn reproducible.


def test_prompt_file_is_referenced_not_inlined(tmp_path):
    from agent_core.config import HarnessConfig, use_config
    prompt = tmp_path / "review.md"
    prompt.write_text("SECRET PROMPT BODY")
    with use_config(HarnessConfig(opencode_bin="/x/opencode", opencode_provider_chain="p:p/m")):
        cmd = process.OpenCodeProcess()._build_cmd(
            None, session_id=None, model_id="p/m", repo_path="/repo", prompt_file=prompt
        )
    assert "--file" in cmd and str(prompt) in cmd
    assert "SECRET PROMPT BODY" not in " ".join(cmd), "prompt body must not reach argv"
    assert cmd.index("--file") > cmd.index(
        f"Read and follow the attached prompt file exactly: {prompt.name}"
    ), "--file is an array option; the instruction must precede it"


def test_run_turn_requires_exactly_one_of_message_or_prompt_file(tmp_path):
    proc = process.OpenCodeProcess()
    with pytest.raises(ValueError, match="exactly one"):
        proc.run_turn(None, repo_path=str(tmp_path))
    with pytest.raises(ValueError, match="exactly one"):
        proc.run_turn("hi", prompt_file=tmp_path / "p.md", repo_path=str(tmp_path))


def test_message_path_is_unchanged(tmp_path):
    """Existing string callers must emit exactly what they did before."""
    from agent_core.config import HarnessConfig, use_config
    with use_config(HarnessConfig(opencode_bin="/x/opencode", opencode_provider_chain="p:p/m")):
        cmd = process.OpenCodeProcess()._build_cmd(
            "short prompt", session_id=None, model_id="p/m", repo_path="/repo"
        )
    assert cmd[-1] == "short prompt"
    assert "--file" not in cmd


# -- D12/D13: permission block and chain-order selection -------------------------


def test_permission_block_is_configurable(tmp_path):
    """A reviewer that can edit the code it reviews is a real hazard."""
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness import build_opencode_config_dict

    with use_config(HarnessConfig(opencode_permissions={"edit": "deny", "*": "allow"})):
        config = build_opencode_config_dict(str(tmp_path))
    assert config["permission"]["edit"] == "deny"
    assert config["permission"]["*"] == "allow"
    # the repo permissions the source always emitted are still there
    assert "external_directory" in config["permission"]


def test_permission_block_defaults_to_source_behaviour(tmp_path):
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness import build_opencode_config_dict

    with use_config(HarnessConfig()):
        config = build_opencode_config_dict(str(tmp_path))
    assert set(config["permission"]) == {"external_directory"}


def test_extra_permission_dirs_are_granted(tmp_path):
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness import build_opencode_config_dict

    extra = tmp_path / "audit"
    extra.mkdir()
    with use_config(HarnessConfig(opencode_permission_dirs=str(extra))):
        config = build_opencode_config_dict(str(tmp_path))
    assert f"{extra.resolve()}/**" in config["permission"]["external_directory"]


def test_selection_walks_the_chain_in_order(tmp_path):
    """Not 'whichever model was named': first usable link in the chain."""
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness import build_opencode_config_dict, reset_model_health

    reset_model_health()
    cfg = HarnessConfig(
        opencode_provider_chain="p:p/first,p/second",
        opencode_model="",
        opencode_provider_fallback_enabled=True,
    )
    with use_config(cfg):
        assert build_opencode_config_dict(str(tmp_path))["model"] == "p/first"


def test_unusable_first_link_is_skipped(tmp_path):
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness import build_opencode_config_dict, mark_model_unhealthy, reset_model_health

    reset_model_health()
    cfg = HarnessConfig(
        opencode_provider_chain="p:p/first,p/second",
        opencode_model="",
        opencode_provider_fallback_enabled=True,
    )
    with use_config(cfg):
        mark_model_unhealthy("p/first", reason="rate_limited", retry_after_seconds=60)
        assert build_opencode_config_dict(str(tmp_path))["model"] == "p/second"
    reset_model_health()


def test_explicit_model_wins_only_while_usable(tmp_path):
    """Pinning a model that has been rate-limited must not strand the task on it."""
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness import build_opencode_config_dict, mark_model_unhealthy, reset_model_health

    reset_model_health()
    cfg = HarnessConfig(
        opencode_provider_chain="p:p/first,p/second",
        opencode_model="p/second",
        opencode_provider_fallback_enabled=True,
    )
    with use_config(cfg):
        assert build_opencode_config_dict(str(tmp_path))["model"] == "p/second"
        mark_model_unhealthy("p/second", reason="rate_limited", retry_after_seconds=60)
        assert build_opencode_config_dict(str(tmp_path))["model"] == "p/first"
    reset_model_health()


# -- D17: --model on the command line is optional --------------------------------


def _cmd_with(**cfg):
    from agent_core.config import HarnessConfig, use_config
    with use_config(HarnessConfig(opencode_bin="/x/opencode",
                                  opencode_provider_chain="p:p/m", **cfg)):
        return process.OpenCodeProcess()._build_cmd(
            "hi", session_id=None, model_id="p/m", repo_path="/repo"
        )


def test_model_flag_is_passed_by_default():
    assert "--model" in _cmd_with()


def test_model_flag_can_be_omitted():
    """A consumer lets opencode.json carry the model so OpenCode resolves it
    through its own provider configuration rather than being told directly."""
    cmd = _cmd_with(opencode_pass_model_flag=False)
    assert "--model" not in cmd
    assert "p/m" not in cmd


# -- D18: permission skipping is conditional -------------------------------------
#
# Upstream passed --dangerously-skip-permissions unconditionally, which overrides
# the permission block entirely. That made D12 inert: a product setting
# {"edit": "deny"} still got an agent that could edit.


def test_permissions_are_skipped_when_no_policy_is_configured():
    """Upstream behaviour, preserved for products that set no policy."""
    assert "--dangerously-skip-permissions" in _cmd_with()


def test_configuring_a_policy_stops_it_being_skipped():
    """The whole point: an agent told it may not edit must not be handed a flag
    that overrides that."""
    cmd = _cmd_with(opencode_permissions={"edit": "deny"})
    assert "--dangerously-skip-permissions" not in cmd


def test_skip_can_be_forced_either_way():
    assert "--dangerously-skip-permissions" in _cmd_with(
        opencode_permissions={"edit": "deny"}, opencode_skip_permissions=True
    )
    assert "--dangerously-skip-permissions" not in _cmd_with(opencode_skip_permissions=False)



# -- D20: the availability probe can be switched off ------------------------------


def test_probe_is_skipped_when_the_timeout_is_not_positive():
    """Otherwise a suite with base URLs configured makes live HTTP calls, and
    results depend on which earlier test warmed the cache."""
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness import tiered_router

    calls = []

    def spy(url, **kw):
        calls.append(url)
        raise AssertionError("must not be called")

    cfg = HarnessConfig(
        opencode_provider_chain="p:p/m",
        opencode_provider_base_urls="p.base_url=http://example.invalid/v1",
        opencode_model_api_timeout_seconds=0,
    )
    with use_config(cfg):
        assert tiered_router._provider_available_models("p", http_get=spy) is None
    assert calls == []


def test_probe_runs_when_a_timeout_is_configured():
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness import tiered_router

    calls = []

    class R:
        status_code = 200
        def json(self):
            return {"data": [{"id": "m"}]}

    def spy(url, **kw):
        calls.append(url)
        return R()

    cfg = HarnessConfig(
        opencode_provider_chain="p:p/m",
        opencode_provider_base_urls="p.base_url=http://example.invalid/v1",
        opencode_model_api_timeout_seconds=5,
        opencode_model_api_cache_seconds=0,
    )
    with use_config(cfg):
        tiered_router.reset_model_availability_cache()
        assert tiered_router._provider_available_models("p", http_get=spy) == {"m"}
    assert calls
