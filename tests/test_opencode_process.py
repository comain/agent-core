"""Phase 1 gate tests for OpenCodeProcess."""

import io
import json
import os
import sys
import threading
from pathlib import Path
import subprocess
import signal
from unittest.mock import MagicMock, patch

import pytest

from agent_core.harness.process import OpenCodeProcess, TurnResult, _build_env


def _jsonl(*events) -> bytes:
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


def _step_start(session_id="ses_abc"):
    return {"type": "step_start", "sessionID": session_id, "part": {"type": "step-start"}}


def _text(text, session_id="ses_abc"):
    return {"type": "text", "sessionID": session_id, "part": {"type": "text", "text": text}}


def _step_finish(reason="stop", tokens=None, session_id="ses_abc"):
    return {
        "type": "step_finish",
        "sessionID": session_id,
        "part": {
            "reason": reason,
            "tokens": tokens or {"input": 100, "output": 20, "reasoning": 0, "cache": {"write": 0, "read": 0}, "total": 120},
        },
    }


def _error(message, status_code=None, session_id="ses_abc"):
    data = {"message": message}
    if status_code is not None:
        data["statusCode"] = status_code
    return {"type": "error", "sessionID": session_id, "error": {"name": "UnknownError", "data": data}}


def _make_proc(stdout_bytes: bytes, returncode: int = 0):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(stdout_bytes)
    proc.stderr = io.BytesIO(b"")
    proc.returncode = returncode
    proc.wait = MagicMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


@pytest.fixture
def oc_process():
    return OpenCodeProcess()


# --- _build_cmd ---

def test_build_cmd_defaults(oc_process):
    with patch("agent_core.harness.process.OpenCodeProcess._build_cmd", wraps=oc_process._build_cmd):
        cmd = oc_process._build_cmd("hello", session_id=None, model_id=None)
    assert "opencode" in cmd[0] or cmd[0].endswith("opencode")
    assert "run" in cmd
    assert "--print-logs" in cmd
    assert "--format" in cmd
    assert "json" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--pure" in cmd
    assert "hello" == cmd[-1]


def test_build_cmd_can_disable_pure_mode(monkeypatch, oc_process):
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_pure", False)
    cmd = oc_process._build_cmd("hello", session_id=None, model_id=None)
    assert "--pure" not in cmd


def test_build_cmd_with_model(oc_process):
    cmd = oc_process._build_cmd("hi", session_id=None, model_id="cursor/claude-4.5-sonnet")
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "cursor/claude-4.5-sonnet"


def test_build_cmd_with_variant(oc_process):
    cmd = oc_process._build_cmd("hi", session_id=None, model_id="openai/gpt-5.5", variant="none")
    assert "--variant" in cmd
    idx = cmd.index("--variant")
    assert cmd[idx + 1] == "none"


def test_build_cmd_uses_configured_variant(monkeypatch, oc_process):
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_variant", "low")
    cmd = oc_process._build_cmd("hi", session_id=None, model_id="openai/gpt-5.5")
    assert "--variant" in cmd
    idx = cmd.index("--variant")
    assert cmd[idx + 1] == "low"


def test_build_cmd_detects_v2_from_configured_bin_not_path_name(monkeypatch, oc_process):
    from agent_core.harness import opencode_runtime as runtime

    runtime.reset_runtime_cache()
    monkeypatch.setattr("agent_core.config.settings.opencode_major", None)
    monkeypatch.setattr(
        "agent_core.config.settings.opencode_bin",
        "/root/.opencode/bin/opencode",
    )
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args[0])

        class Result:
            stdout = "opencode v2.0.6" if args[0] == "/root/.opencode/bin/opencode" else "1.14.40"
            stderr = ""

        return Result()

    monkeypatch.setattr("agent_core.harness.opencode_runtime.subprocess.run", fake_run)
    cmd = oc_process._build_cmd("hello", session_id=None, model_id=None)
    assert seen == ["/root/.opencode/bin/opencode"]
    assert cmd[0] == "/root/.opencode/bin/opencode"
    assert "--pure" not in cmd
    assert "--standalone" in cmd
    assert "--auto" in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_build_cmd_v2_uses_standalone_and_model_variant(monkeypatch, oc_process):
    monkeypatch.setattr("agent_core.config.settings.opencode_major", 2)
    cmd = oc_process._build_cmd(
        "hi",
        session_id=None,
        model_id="token-pool/gpt-5.5",
        variant="low",
    )
    assert "--standalone" in cmd
    assert "--pure" not in cmd
    assert "--print-logs" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "--auto" in cmd
    assert "--variant" not in cmd
    assert cmd[cmd.index("--model") + 1] == "token-pool/gpt-5.5#low"


def test_build_cmd_v2_maps_attach_to_server(monkeypatch, oc_process):
    monkeypatch.setattr("agent_core.config.settings.opencode_major", 2)
    monkeypatch.setattr("agent_core.config.settings.opencode_attach_url", "http://127.0.0.1:4096")
    cmd = oc_process._build_cmd("hello", session_id=None, model_id=None)
    assert "--attach" not in cmd
    assert "--standalone" not in cmd
    assert cmd[cmd.index("--server") + 1] == "http://127.0.0.1:4096"


def test_build_cmd_v2_omits_dir_flag(monkeypatch, oc_process):
    monkeypatch.setattr("agent_core.config.settings.opencode_major", 2)
    cmd = oc_process._build_cmd(
        "hello",
        session_id=None,
        model_id=None,
        project_dir="/tmp/scratch",
        title="review",
    )
    assert "--dir" not in cmd
    assert cmd[cmd.index("--title") + 1] == "review"


def test_build_cmd_with_session(oc_process):
    cmd = oc_process._build_cmd("hi", session_id="ses_abc123", model_id=None)
    assert "--session" in cmd
    idx = cmd.index("--session")
    assert cmd[idx + 1] == "ses_abc123"
    assert "--continue" in cmd


def test_build_cmd_materializes_large_prompt_file(oc_process, tmp_path):
    message = "x" * 70000

    cmd = oc_process._build_cmd(message, session_id=None, model_id=None, repo_path=str(tmp_path))

    assert message not in cmd
    assert "--file" in cmd
    short_message_index = cmd.index("--file") - 1
    assert cmd[short_message_index].startswith("Read and follow the attached prompt file exactly:")
    prompt_path = Path(cmd[cmd.index("--file") + 1])
    assert prompt_path.is_file()
    assert prompt_path.read_text(encoding="utf-8") == message
    assert len(cmd[short_message_index]) < len(message)


def test_build_cmd_attach_url(oc_process):
    with patch("agent_core.harness.process.settings") as mock_settings:
        mock_settings.opencode_attach_url = "http://localhost:4096"
        mock_settings.opencode_spawn_cmd = None
        mock_settings.opencode_bin = None
        cmd = oc_process._build_cmd("hello", session_id=None, model_id=None, repo_path="/tmp/repo")
    assert "--attach" in cmd
    idx = cmd.index("--attach")
    assert cmd[idx + 1] == "http://localhost:4096"
    assert "--dir" not in cmd  # repo_path is cwd, not --dir (opencode 1.14.50+ compat)


def test_build_cmd_custom_spawn_cmd(oc_process):
    with patch("agent_core.harness.process.settings") as mock_settings:
        mock_settings.opencode_spawn_cmd = '["bun", "run", "src/index.ts", "run"]'
        mock_settings.opencode_attach_url = None
        mock_settings.opencode_bin = None
        cmd = oc_process._build_cmd("hello", session_id=None, model_id=None, repo_path="/tmp/repo")
    assert cmd[0] == "bun"
    assert cmd[1] == "run"
    assert cmd[2] == "src/index.ts"
    assert cmd[3] == "run"
    assert "--dir" not in cmd


def test_build_cmd_omits_message_when_delivery_is_stdin(oc_process):
    cmd = oc_process._build_cmd(
        "hello",
        session_id=None,
        model_id=None,
        delivery="stdin",
    )
    assert "hello" not in cmd
    assert "--file" not in cmd


def test_build_cmd_adds_title_and_dir(oc_process):
    cmd = oc_process._build_cmd(
        "hello",
        session_id=None,
        model_id=None,
        title="spec_docs_plan",
        project_dir="/tmp/scratch",
    )
    assert cmd[cmd.index("--title") + 1] == "spec_docs_plan"
    assert cmd[cmd.index("--dir") + 1] == "/tmp/scratch"


def test_build_cmd_pure_override_ignores_settings(monkeypatch, oc_process):
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_pure", True)
    cmd = oc_process._build_cmd("hello", session_id=None, model_id=None, pure=False)
    assert "--pure" not in cmd


def test_run_turn_uses_repo_dir_for_command_and_process_cwd(oc_process, tmp_path):
    repo_path = str(tmp_path)
    captured = {}
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(_jsonl(_step_start(), _text("ok"), _step_finish()))
    proc.stderr = io.BytesIO(b"")
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc

    with patch("agent_core.harness.process.subprocess.Popen", side_effect=fake_popen):
        result = oc_process.run_turn("hello", repo_path=repo_path, timeout=10)

    assert result.type == "completed"
    assert "--dir" not in captured["cmd"]
    assert captured["kwargs"]["cwd"] == repo_path
    assert captured["kwargs"]["env"]["PWD"] == repo_path
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize("configured,explicit,expected", [
    ("low", None, "low"), ("high", "medium", "medium"), (None, None, "default"),
])
def test_run_turn_reports_effective_selection_before_process_start(
    oc_process, tmp_path, monkeypatch, configured, explicit, expected
):
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_variant", configured)
    updates = []
    def spawn(*args, **kwargs):
        assert updates == [f"model-selected: token-pool/gpt-6-astra effort={expected}"]
        raise OSError("test stops before external process")
    with patch("agent_core.harness.process.subprocess.Popen", side_effect=spawn):
        with pytest.raises(OSError):
            oc_process.run_turn("hello", repo_path=str(tmp_path),
                                model_id="token-pool/gpt-6-astra", variant=explicit,
                                on_update=updates.append)


def test_run_turn_stdin_delivery_writes_prompt_and_merges_env(oc_process, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_credential_env",
                        {"POOL_KEY": "captured-secret"})
    captured = {}
    stdin = MagicMock()
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdin = stdin
    proc.stdout = io.BytesIO(_jsonl(_step_start(), _text("ok"), _step_finish()))
    proc.stderr = io.BytesIO(b"")
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc

    with patch("agent_core.harness.process.subprocess.Popen", side_effect=fake_popen):
        result = oc_process.run_turn(
            "page prompt",
            repo_path=str(tmp_path),
            timeout=10,
            delivery="stdin",
            title="spec_docs_write",
            env={"OPENCODE_CONFIG": "/tmp/opencode.json", "POOL_KEY": "override"},
        )

    assert result.type == "completed"
    assert "page prompt" not in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--title") + 1] == "spec_docs_write"
    assert captured["kwargs"]["stdin"] is subprocess.PIPE
    stdin.write.assert_called_once_with(b"page prompt")
    stdin.close.assert_called_once()
    assert captured["kwargs"]["env"]["OPENCODE_CONFIG"] == "/tmp/opencode.json"
    assert captured["kwargs"]["env"]["POOL_KEY"] == "captured-secret"


def test_stdin_does_not_deadlock_when_child_writes_stdout_first(oc_process, tmp_path):
    """A large prompt on stdin plus a chatty child fills both pipes if the
    parent writes stdin to completion before it starts reading stdout."""
    script = (
        "import sys\n"
        "sys.stdout.write('x' * 200000 + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.read()\n"
    )
    oc_process._build_cmd = lambda *a, **k: [sys.executable, "-c", script]

    box: dict = {}

    def go():
        box["result"] = oc_process.run_turn(
            "y" * 200000,
            repo_path=str(tmp_path),
            timeout=8,
            delivery="stdin",
        )

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "stdin write deadlocked against unread stdout"
    assert "result" in box


def test_run_turn_starts_opencode_with_child_cleanup_hook(oc_process, tmp_path):
    captured = {}
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(_jsonl(_step_start(), _text("ok"), _step_finish()))
    proc.stderr = io.BytesIO(b"")
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    def fake_popen(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return proc

    with patch("agent_core.harness.process.subprocess.Popen", side_effect=fake_popen):
        result = oc_process.run_turn("hello", repo_path=str(tmp_path), timeout=10)

    assert result.type == "completed"
    if os.name != "nt":
        assert callable(captured["kwargs"]["preexec_fn"])


def test_build_env_prepends_service_python_bin(monkeypatch, tmp_path):
    service_bin = tmp_path / "venv" / "bin"
    service_bin.mkdir(parents=True)
    service_python = service_bin / "python"
    service_python.write_text("", encoding="utf-8")
    monkeypatch.setattr("agent_core.harness.process.sys.executable", str(service_python))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = _build_env(str(tmp_path))

    assert env["PATH"].split(":")[0] == str(service_bin.resolve())
    assert env["UTA_SERVICE_PYTHON_BIN"] == str(service_python.resolve())


# --- _read_stream / _build_result ---

def test_turn_result_from_jsonl_stream(oc_process):
    events = [_step_start(), _text("Hello world"), _step_finish()]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "completed"
    assert "Hello world" in result.result
    assert result.session_id == "ses_abc"


def test_tool_call_boundary_is_not_a_completed_turn(oc_process):
    events = [
        _step_start(),
        _text("I will inspect the target first."),
        {
            "type": "tool_use",
            "sessionID": "ses_abc",
            "part": {
                "type": "tool",
                "tool": "todowrite",
                "state": {"status": "completed", "output": "[]"},
            },
        },
        _step_finish(reason="tool-calls"),
    ]

    result = oc_process._read_stream(
        _make_proc(_jsonl(*events)), timeout=10, on_update=None
    )

    assert result.type == "incomplete"
    assert result.session_id == "ses_abc"
    assert result.fallback_eligible is False


def test_read_stream_ignores_non_iterable_mock_streams(oc_process):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = object()
    proc.stderr = object()
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    result = oc_process._read_stream(proc, timeout=1, on_update=None)

    assert result.type == "stalled"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "no_output"


def test_session_id_extracted_from_first_event(oc_process):
    events = [_step_start("ses_xyz789"), _text("hi", "ses_xyz789"), _step_finish(session_id="ses_xyz789")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.session_id == "ses_xyz789"


def test_tokens_extracted_from_step_finish(oc_process):
    tokens = {"input": 500, "output": 100, "reasoning": 0, "cache": {"write": 200, "read": 100}, "total": 600}
    events = [_step_start(), _text("done"), _step_finish(tokens=tokens)]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.tokens["input"] == 500
    assert result.tokens["output"] == 100
    assert result.tokens["total"] == 600


def test_patch_count_extracted_from_stream(oc_process):
    events = [
        _step_start(),
        {
            "type": "tool_use",
            "sessionID": "ses_abc",
            "part": {
                "type": "tool",
                "tool": "apply_patch",
                "state": {"status": "completed", "output": "Success. Updated the following files:\nM A.java"},
            },
        },
        _step_finish(),
    ]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "completed"
    assert result.patch_count == 1


def test_terminal_tool_error_is_not_a_provider_failure(oc_process):
    events = [
        _step_start(),
        {
            "type": "tool_use",
            "sessionID": "ses_abc",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "error",
                    "output": "permission requested: external_directory; auto-rejecting",
                },
            },
        },
        _step_finish(reason="tool-calls"),
    ]
    proc = _make_proc(_jsonl(*events))

    result = oc_process._read_stream(proc, timeout=10, on_update=None)

    assert result.type == "error"
    assert result.fallback_reason == "tool_contract_error"
    assert result.fallback_eligible is False
    assert result.error["data"]["tool"] == "bash"


def test_aborted_error_from_rejected_question_is_tool_contract_error(oc_process):
    events = [
        _step_start(),
        {
            "type": "error",
            "sessionID": "ses_abc",
            "error": {
                "type": "aborted",
                "message": "Session interrupted: shutdown",
            },
        },
        {
            "type": "tool_use",
            "sessionID": "ses_abc",
            "part": {
                "type": "tool",
                "tool": "question",
                "state": {
                    "status": "error",
                    "error": "The user dismissed this question",
                },
            },
        },
    ]
    proc = _make_proc(_jsonl(*events))

    result = oc_process._read_stream(proc, timeout=10, on_update=None)

    assert result.type == "error"
    assert result.fallback_reason == "tool_contract_error"
    assert result.fallback_eligible is False
    assert result.error["data"] == {
        "tool": "question",
        "message": "The user dismissed this question",
    }


def test_rate_limit_error_detected(oc_process):
    events = [_error("Too many requests, quota exceeded", status_code=429)]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "rate_limited"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "rate_limit"


def test_regular_error_detected(oc_process):
    events = [_error("Model not found: tencent/glm-5")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.error is not None
    assert result.fallback_eligible is True
    assert result.fallback_reason == "model_not_found"


def test_disabled_model_error_is_fallback_eligible(oc_process):
    events = [_error("The requested model is disabled for this account")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "model_disabled"


def test_unavailable_model_error_is_fallback_eligible(oc_process):
    events = [_error("Model openai/gpt-5.5 is temporarily unavailable")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "model_unavailable"


@pytest.mark.parametrize(
    "message",
    [
        '"stream error: stream ID 1; INTERNAL_ERROR; received from peer"',
        "connection reset by peer",
        "connection closed before message completed",
        "upstream connect error or disconnect/reset before headers",
        "unexpected EOF",
        "Unexpected end of JSON input",
        "unterminated JSON response",
        "Transport",
        "Transport error",
    ],
)
def test_transient_provider_transport_error_is_fallback_eligible(oc_process, message):
    events = [_error(message)]
    proc = _make_proc(_jsonl(*events))

    result = oc_process._read_stream(proc, timeout=10, on_update=None)

    assert result.type == "error"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "provider_transport_error"


def test_opencode_v2_bare_transport_error_is_fallback_eligible(oc_process):
    events = [{
        "type": "error",
        "sessionID": "ses_abc",
        "error": {"type": "unknown", "message": "Transport"},
    }]

    result = oc_process._read_stream(
        _make_proc(_jsonl(*events)), timeout=10, on_update=None
    )

    assert result.type == "error"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "provider_transport_error"


def test_provider_auth_error_is_fallback_eligible(oc_process):
    events = [
        {
            "type": "error",
            "sessionID": "ses_abc",
            "error": {
                "name": "APIError",
                "data": {
                    "message": "Authentication Fails, Your api key: ****d353 is invalid",
                    "statusCode": 401,
                },
            },
        }
    ]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "provider_auth_failed"


def test_generic_error_is_not_fallback_eligible(oc_process):
    events = [_error("OpenCode command failed while applying patch")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.fallback_eligible is False
    assert result.fallback_reason is None


def test_rate_limit_detected_from_raw_stderr_without_events(oc_process):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(b"")
    proc.stderr = io.BytesIO(
        b'{"error":{"type":"usage_limit_reached","message":"The usage limit has been reached","resets_in_seconds":2493,"resets_at":1776925907}}\n'
    )
    proc.returncode = 1
    proc.wait = MagicMock(return_value=1)
    proc.kill = MagicMock()

    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "rate_limited"
    assert result.error is not None
    assert result.error["data"]["statusCode"] == 429
    assert result.error["retry_after_seconds"] == 2493
    assert result.error["reset_at"] == 1776925907


def test_connection_error_prompt_text_does_not_trigger_rate_limit(oc_process):
    raw = json.dumps(
        {
            "error": {
                "name": "AI_RetryError",
                "errors": [
                    {
                        "name": "AI_APICallError",
                        "cause": {"code": "ConnectionRefused", "path": "http://127.0.0.1:1234/v1/responses"},
                        "requestBodyValues": {
                            "input": [
                                {
                                    "role": "system",
                                    "content": 'Example: "implement rate limiting" -> Rate limiting implementation',
                                }
                            ]
                        },
                    }
                ],
            }
        }
    )

    assert oc_process._infer_rate_limit_from_text(f"ERROR service=llm error={raw}") is None


def test_non_terminal_stdout_rate_limit_words_do_not_trigger_rate_limit(oc_process):
    events = [_step_start(), _text("planning an implement rate limiting example")]
    proc = _make_proc(_jsonl(*events))

    result = oc_process._read_stream(proc, timeout=10, on_update=None)

    assert result.type == "completed"
    assert "rate limiting" in result.result


def test_completed_stop_beats_raw_rate_limit_noise(oc_process):
    events = [_step_start(), _text("### PickingBizImpl"), _step_finish(reason="stop")]
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(_jsonl(*events))
    proc.stderr = io.BytesIO(
        b'{"error":{"type":"usage_limit_reached","message":"The usage limit has been reached","resets_in_seconds":2493,"resets_at":1776925907}}\n'
    )
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "completed"
    assert "PickingBizImpl" in result.result


def test_rate_limit_detected_from_server_logs_without_stream_output(oc_process):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(b"")
    proc.stderr = io.BytesIO(b"")
    proc.returncode = 1
    proc.wait = MagicMock(return_value=1)
    proc.kill = MagicMock()

    with patch(
        "agent_core.harness.process.detect_rate_limit_in_logs",
        return_value={
            "provider_id": "openai",
            "model_id": "gpt-5.4",
            "status_code": 429,
            "raw_type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "retry_after_seconds": 2493,
            "reset_at": 1776925907,
        },
    ):
        result = oc_process._read_stream(
            proc,
            timeout=10,
            on_update=None,
            model_id="openai/gpt-5.4",
            started_at=1776923400.0,
        )
    assert result.type == "rate_limited"
    assert result.error is not None
    assert result.error["provider_id"] == "openai"
    assert result.error["model_id"] == "gpt-5.4"
    assert result.error["retry_after_seconds"] == 2493


def test_on_update_receives_progress(oc_process):
    updates = []
    events = [_step_start(), _text("Hello!"), _step_finish()]
    proc = _make_proc(_jsonl(*events))
    oc_process._read_stream(proc, timeout=10, on_update=updates.append)
    assert any("Hello!" in u for u in updates)


def test_malformed_lines_ignored(oc_process):
    raw = b"plugin initialized\n" + _jsonl(_step_start(), _text("real output"), _step_finish())
    proc = _make_proc(raw)
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "completed"
    assert "real output" in result.result


def test_timeout_kills_process(oc_process):
    import time

    def slow_read(*args, **kwargs):
        time.sleep(60)

    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.stdout = MagicMock()
    proc.stdout.__iter__ = slow_read
    proc.kill = MagicMock()
    proc.wait = MagicMock()

    result = oc_process._read_stream(proc, timeout=0.1, on_update=None)
    assert result.type == "timeout"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "no_output"


def test_initial_no_output_uses_short_startup_cap(monkeypatch, oc_process):
    import time

    def slow_read(*args, **kwargs):
        time.sleep(60)

    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_initial_output_timeout_seconds",
        0.1,
    )
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.stdout = MagicMock()
    proc.stdout.__iter__ = slow_read
    proc.stderr = io.BytesIO(b"")
    proc.kill = MagicMock()
    proc.wait = MagicMock()

    started = time.monotonic()
    result = oc_process._read_stream(proc, timeout=10, on_update=None)

    assert time.monotonic() - started < 1.0
    assert result.fallback_eligible is True
    assert result.fallback_reason == "no_output"


def test_progress_then_silent_response_wait_does_not_fallback(oc_process):
    import time
    from agent_core.harness.config import settings

    class PausingStream:
        def __iter__(self):
            yield b'{"type":"text","sessionID":"ses_test","part":{"text":"Inspecting tests"}}\n'
            time.sleep(1.4)

    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = PausingStream()
    proc.stderr = io.BytesIO(b'')
    proc.wait.return_value = 0
    proc.returncode = 0
    with patch.object(settings, 'opencode_stream_idle_timeout_seconds', 1), patch(
        'agent_core.harness.process._terminate_opencode_process'
    ) as terminate:
        result = oc_process._read_stream(proc, timeout=10, on_update=None)
    terminate.assert_not_called()
    assert not result.fallback_eligible


def test_usage_limit_stderr_terminates_process_and_falls_back_immediately(oc_process):
    import time

    class SlowStream:
        def __iter__(self):
            time.sleep(60)
            return iter(())

    stderr = (
        'ERROR service=llm error={"error":{"name":"AI_APICallError"},'
        '"statusCode":429,"responseBody":"{\\"error\\":{'
        '\\"type\\":\\"usage_limit_reached\\"}}"}} stream error\n'
    ).encode()
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.stdout = SlowStream()
    proc.stderr = io.BytesIO(stderr)
    proc.wait = MagicMock(return_value=0)

    started = time.monotonic()
    with patch("agent_core.harness.process._terminate_opencode_process") as terminate:
        result = oc_process._read_stream(proc, timeout=30, on_update=None)

    assert time.monotonic() - started < 1.0
    assert result.type == "rate_limited"
    assert result.fallback_reason == "rate_limit"
    terminate.assert_called_once_with(proc)


def test_timeout_after_progress_text_without_tokens_or_patch_is_fallback_eligible(oc_process):
    import time

    class SlowProgressStream:
        def __iter__(self):
            yield _jsonl(_text("Inspecting the target"))
            time.sleep(60)

    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.stdout = SlowProgressStream()
    proc.stderr = io.BytesIO(b"")
    proc.kill = MagicMock()
    proc.wait = MagicMock()

    result = oc_process._read_stream(proc, timeout=0.1, on_update=None)

    assert result.type == "timeout"
    assert result.patch_count == 0
    assert result.tokens == {}
    assert result.fallback_eligible is True
    assert result.fallback_reason == "no_output"


@pytest.mark.parametrize("patch_count", [0, 1])
def test_timeout_retains_usage_without_treating_tokens_as_edits(oc_process, patch_count):
    from agent_core.harness.process import TurnResult
    import time

    class SlowStream:
        def __iter__(self):
            yield _jsonl(_text("Working"))
            time.sleep(1)

    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.stdout = SlowStream()
    proc.stderr = io.BytesIO(b"")
    proc.poll.return_value = None
    inferred = TurnResult(type="completed", model_id="provider/model",
                          tokens={"input": 100, "output": 0}, cost_usd=0.1,
                          result="partial", patch_count=patch_count)
    with patch.object(oc_process, "_build_result", return_value=inferred):
        result = oc_process._read_stream(proc, timeout=0.001, on_update=None)
    assert result.type == "timeout"
    assert result.tokens == inferred.tokens
    assert result.model_id == inferred.model_id
    assert result.cost_usd == inferred.cost_usd
    assert result.result == "partial"
    assert result.fallback_eligible is (patch_count == 0)
    assert result.fallback_reason == "timeout"


def test_timeout_terminates_opencode_process_group(oc_process):
    import time

    def slow_read(*args, **kwargs):
        time.sleep(60)

    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.stdout = MagicMock()
    proc.stdout.__iter__ = slow_read
    proc.stderr = io.BytesIO(b"")
    proc.wait = MagicMock(side_effect=[subprocess.TimeoutExpired(["opencode"], 1), 0])
    proc.kill = MagicMock()

    with patch("agent_core.harness.process.os.name", "posix"), patch(
        "agent_core.harness.process.os.getpgid", return_value=54321
    ), patch("agent_core.harness.process.os.killpg") as killpg:
        result = oc_process._read_stream(proc, timeout=0.1, on_update=None)

    assert result.type == "timeout"
    killpg.assert_any_call(54321, signal.SIGTERM)
    killpg.assert_any_call(54321, signal.SIGKILL)


def test_raw_turn_jsonl_persisted_for_stdout_and_stderr(oc_process, tmp_path):
    events = [_step_start(), _text("Hello world"), _step_finish()]
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    proc.stdout = io.BytesIO(_jsonl(*events))
    proc.stderr = io.BytesIO(b"diagnostic stderr\n")
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()
    raw_log_path = tmp_path / "turn.jsonl"

    result = oc_process._read_stream(
        proc,
        timeout=10,
        on_update=None,
        model_id="token-pool/gpt-5.5",
        raw_log_path=raw_log_path,
    )

    assert result.type == "completed"
    assert result.raw_log_path == str(raw_log_path)
    records = [json.loads(line) for line in raw_log_path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["kind"] == "turn_start"
    assert records[0]["model_id"] == "token-pool/gpt-5.5"
    assert any(record["kind"] == "stream_line" and record["stream"] == "stdout" for record in records)
    assert any(record["kind"] == "stream_line" and record["stream"] == "stderr" for record in records)
    assert records[-1]["kind"] == "turn_finish"
    assert records[-1]["result_type"] == "completed"


def test_timeout_returns_rate_limit_if_logs_show_429(oc_process):
    import time

    def slow_read(*args, **kwargs):
        time.sleep(60)

    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = MagicMock()
    proc.stdout.__iter__ = slow_read
    proc.stderr = io.BytesIO(b"")
    proc.kill = MagicMock()
    proc.wait = MagicMock(return_value=1)

    with patch(
        "agent_core.harness.process.detect_rate_limit_in_logs",
        return_value={
            "provider_id": "openai",
            "model_id": "gpt-5.4",
            "status_code": 429,
            "raw_type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "retry_after_seconds": 2493,
            "reset_at": 1776925907,
        },
    ):
        result = oc_process._read_stream(
            proc,
            timeout=0.1,
            on_update=None,
            model_id="openai/gpt-5.4",
            started_at=1776923400.0,
        )
    assert result.type == "rate_limited"
    assert result.error is not None
    assert result.error["retry_after_seconds"] == 2493


# --- _build_env ---

@pytest.mark.parametrize("provider", ["token-pool", "openai", "google"])
def test_config_env_references_resolve_in_child_environment(monkeypatch, provider):
    from agent_core.harness.process import _build_env

    monkeypatch.setenv("POOL_KEY", "test-secret")
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_provider_tokens",
                        provider + ".token={env:POOL_KEY}")
    env = _build_env(model_id=provider + "/model")
    assert env["POOL_KEY"] == "test-secret"
    assert "{env:POOL_KEY}" not in (env.get("OPENAI_API_KEY"), env.get("GOOGLE_API_KEY"))


def test_discovery_credentials_stay_captured_and_out_of_serialization(monkeypatch):
    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness.process import _build_env

    monkeypatch.setenv("OPENAI_API_KEY", "later-value")
    config = HarnessConfig(_env_file=None, opencode_provider_tokens="openai.token={env:OPENAI_API_KEY}",
                           opencode_credential_env={"OPENAI_API_KEY": "captured-value"})
    assert "opencode_credential_env" not in config.model_dump()
    assert "captured-value" not in repr(config)
    with use_config(config):
        assert _build_env(model_id="openai/model")["OPENAI_API_KEY"] == "captured-value"


def test_env_strips_openai_key_for_openai_provider(monkeypatch):
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_provider_tokens", "")
    monkeypatch.setattr("agent_core.harness.process.settings.openai_api_key", None)
    with patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-test"}, clear=True):
        env = _build_env(model_id="openai/gpt-5.5")
    assert "OPENAI_API_KEY" not in env


def test_env_keeps_openai_key_for_other_providers():
    with patch("agent_core.harness.process._configured_providers", return_value={"cursor"}):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False):
            env = _build_env()
    assert "OPENAI_API_KEY" in env


def test_env_sets_google_key_from_settings(monkeypatch):
    monkeypatch.setattr("agent_core.harness.process.settings.gemini_api_key", "gkey-123")
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_provider_tokens", "")
    with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
        env = _build_env(model_id="google/gemini-2.5-flash")
    assert env.get("GOOGLE_GENERATIVE_AI_API_KEY") == "gkey-123"
    assert env.get("GOOGLE_API_KEY") == "gkey-123"


def test_env_sets_configured_opencode_data_home(monkeypatch, tmp_path):
    data_home = tmp_path / "opencode-data"
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_data_home", str(data_home))

    with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
        env = _build_env(repo_path="/tmp/repo", model_id="token-pool/gpt-5.5")

    assert env["PWD"] == "/tmp/repo"
    assert env["XDG_DATA_HOME"] == str(data_home.resolve())


def test_env_injects_only_selected_token_pool_token(monkeypatch):
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_chain",
        "token-pool:token-pool/gpt-5.5;openai:openai/gpt-5.5;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_tokens",
        "token-pool.token=tp-secret;openai.token=openai-secret;deepseek.token=deepseek-secret",
    )
    monkeypatch.setattr("agent_core.harness.process.settings.deepseek_api_key", None)
    monkeypatch.setattr("agent_core.harness.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
        env = _build_env(model_id="token-pool/gpt-5.5")

    assert env["OPENAI_API_KEY"] == "tp-secret"
    assert env.get("DEEPSEEK_API_KEY") != "deepseek-secret"
    assert "openai-secret" not in env.values()


def test_env_injects_only_selected_deepseek_token(monkeypatch):
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_chain",
        "token-pool:token-pool/gpt-5.5;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_tokens",
        "token-pool.token=tp-secret;deepseek.token=deepseek-secret",
    )
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_provider_base_urls", "")
    monkeypatch.setattr("agent_core.harness.process.settings.deepseek_api_key", None)
    monkeypatch.setattr("agent_core.harness.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "leaked"}, clear=True):
        env = _build_env(model_id="deepseek/deepseek-v4-pro")

    assert env["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert env.get("OPENAI_API_KEY") == "leaked"
    assert "tp-secret" not in env.values()


def test_env_sets_openai_compatible_base_url_for_selected_provider(monkeypatch):
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_chain",
        "token-pool:token-pool/gpt-5.5;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_tokens",
        "token-pool.token=tp-secret;deepseek.token=deepseek-secret",
    )
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_base_urls",
        "token-pool.base_url=http://token-pool.test/v1;deepseek.base_url=http://deepseek.test/v1",
    )
    monkeypatch.setattr("agent_core.harness.process.settings.deepseek_api_key", None)
    monkeypatch.setattr("agent_core.harness.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
        env = _build_env(model_id="deepseek/deepseek-v4-pro")

    assert env["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert env["OPENAI_API_KEY"] == "deepseek-secret"
    assert env["OPENAI_BASE_URL"] == "http://deepseek.test/v1"
    assert "tp-secret" not in env.values()


def test_env_does_not_export_openai_compatible_env_for_reserved_openai_provider(monkeypatch):
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_chain",
        "openai:openai/gpt-5.5",
    )
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_tokens",
        "openai.token=openai-secret",
    )
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_base_urls",
        "openai.base_url=https://proxy.test/v1",
    )
    monkeypatch.setattr("agent_core.harness.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "leaked"}, clear=True):
        env = _build_env(model_id="openai/gpt-5.5")

    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "openai-secret" not in env.values()


def test_env_keeps_native_openai_oauth_when_no_provider_token(monkeypatch):
    monkeypatch.setattr(
        "agent_core.harness.process.settings.opencode_provider_chain",
        "openai:openai/gpt-5.5",
    )
    monkeypatch.setattr("agent_core.harness.process.settings.opencode_provider_tokens", "")
    monkeypatch.setattr("agent_core.harness.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "leaked"}, clear=True):
        env = _build_env(model_id="openai/gpt-5.5")

    assert "OPENAI_API_KEY" not in env


def test_a_provider_without_a_token_is_named_in_a_warning(monkeypatch, caplog):
    """The only other symptom is a 401 arriving several layers away in the
    provider's own words, with nothing naming the setting that was empty."""
    import logging

    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness.process import _build_env

    # The process inherits os.environ, and a developer machine may well have
    # OPENAI_API_KEY set; clear it so the assertion is about what the config
    # contributed rather than about who is running the suite.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = HarnessConfig(
        opencode_provider_chain="token-pool:gpt-5.5",
        opencode_provider_base_urls="token-pool.base_url=http://pool.example/v1",
        opencode_provider_tokens="",
    )
    with use_config(config):
        with caplog.at_level(logging.WARNING, logger="agent_core.harness.process"):
            env = _build_env("/tmp", model_id="token-pool/gpt-5.5")

    assert "OPENAI_API_KEY" not in env
    assert any("token-pool" in r.message for r in caplog.records)


def test_a_provider_with_a_token_warns_about_nothing(monkeypatch, caplog):
    import logging

    from agent_core.config import HarnessConfig, use_config
    from agent_core.harness.process import _build_env

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = HarnessConfig(
        opencode_provider_chain="token-pool:gpt-5.5",
        opencode_provider_base_urls="token-pool.base_url=http://pool.example/v1",
        opencode_provider_tokens="token-pool.token=secret-value",
    )
    with use_config(config):
        with caplog.at_level(logging.WARNING, logger="agent_core.harness.process"):
            env = _build_env("/tmp", model_id="token-pool/gpt-5.5")

    assert env.get("OPENAI_API_KEY") == "secret-value"
    assert caplog.records == []
