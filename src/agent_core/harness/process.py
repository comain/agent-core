"""Per-turn OpenCode process runner using `opencode run --format json`."""

import json
import logging
import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from agent_core.config import current_config, settings
from agent_core.harness.environment import sanitize_agent_env
from agent_core.harness.lifecycle import sanitize_detail
from agent_core.harness.opencode_runtime import (
    keep_print_logs,
    keep_project_dir_flag,
    keep_pure,
    model_cli_values,
    run_isolation_args,
    skip_permission_args,
)
from agent_core.harness.shutdown import track as _track_process
from agent_core.harness.tiered_router import (
    parse_provider_base_urls,
    parse_provider_tokens,
    provider_candidates,
)
from agent_core.harness.rate_limit import detect_rate_limit_in_logs, parse_rate_limit_payload
from agent_core.harness.sessions import AgentSessionRef, SessionLocatorScope
from agent_core.harness.stream import OpenCodeStreamParser

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    type: str  # "completed" | "incomplete" | "error" | "rate_limited" | "timeout" | "cancelled"
    result: str = ""
    session_id: Optional[str] = None
    model_id: Optional[str] = None
    cost_usd: Optional[float] = None
    tokens: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None
    patch_count: int = 0
    fallback_eligible: bool = False
    fallback_reason: Optional[str] = None
    raw_log_path: Optional[str] = None
    #: Every conversation behind this result. Plural because provider fallback
    #: makes one outcome out of several conversations, and the singular
    #: `session_id` can only name the last one tried.
    session_refs: tuple = ()
    #: Ordered provider-chain attempts behind this result. Each entry contains
    #: the model id, outcome, classified fallback reason, and optionally bounded
    #: sanitized HTTP evidence; raw provider errors stay out of telemetry.
    model_attempts: tuple = ()

    def __post_init__(self):
        """Derive the ref from the session id this runner already parsed.

        Every `TurnResult` built in this module comes from `opencode run`,
        whose session id is written into OpenCode's own storage and is still
        resolvable after this process exits — so `DURABLE` is the correct scope
        here and nowhere else. The client-backed session mints its own wrapper
        UUID and passes a `PROCESS`-scoped ref explicitly, which this leaves
        alone.
        """
        if self.session_id and not self.session_refs:
            self.session_refs = (
                AgentSessionRef(
                    harness="opencode",
                    locator=str(self.session_id),
                    scope=SessionLocatorScope.DURABLE,
                ),
            )


#: stderr markers meaning OpenCode could not reach the provider at all.
PROVIDER_TRANSPORT_FAILURES = (
    "ConnectionRefused",
    "ECONNREFUSED",
    "ENOTFOUND",
    "EAI_AGAIN",
    "ETIMEDOUT",
    "ECONNRESET",
    "fetch failed",
    "socket hang up",
)


#: Where the AI SDK echoes the request into an API error. It carries the prompt
#: and the reviewed diff, so a marker found there is the code under review
#: talking, not the provider.
_REQUEST_ECHO_KEY = "requestBodyValues"


def _without_request_echo(value):
    if isinstance(value, Mapping):
        return {
            key: _without_request_echo(item)
            for key, item in value.items()
            if key != _REQUEST_ECHO_KEY
        }
    if isinstance(value, (list, tuple)):
        return [_without_request_echo(item) for item in value]
    return value


def provider_http_error_evidence(error_obj: Any) -> Dict[str, Any]:
    """Return bounded, nonsecret HTTP evidence from an OpenCode API error.

    OpenCode's envelope may contain the entire request, including the prompt
    and credentials.  Product diagnostics need the provider's answer, not that
    echoed request, so this deliberately keeps only status, error code and a
    scrubbed human-readable detail.
    """
    if not isinstance(error_obj, Mapping):
        return {}

    mappings: List[Mapping[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, Mapping) and value not in mappings:
            mappings.append(value)

    add(error_obj)
    add(error_obj.get("error"))
    add(error_obj.get("data"))
    for item in tuple(mappings):
        add(item.get("error"))
        add(item.get("data"))
        response = item.get("responseBody") or item.get("response_body")
        if isinstance(response, str):
            try:
                parsed = json.loads(response)
            except (TypeError, ValueError):
                parsed = None
            add(parsed)
            if isinstance(parsed, Mapping):
                add(parsed.get("error"))

    status = None
    for item in mappings:
        candidate = (
            item.get("httpStatus")
            or item.get("statusCode")
            or item.get("status_code")
        )
        try:
            candidate = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= candidate <= 599:
            status = candidate
            break

    code = ""
    for item in reversed(mappings):
        candidate = (
            item.get("errorCode")
            or item.get("error_code")
            or item.get("code")
            or item.get("type")
        )
        if candidate and re.fullmatch(r"[A-Za-z0-9_.+-]{1,96}", str(candidate)):
            code = str(candidate)
            break

    detail = ""
    for item in reversed(mappings):
        candidate = item.get("errorDetail") or item.get("detail") or item.get("message")
        if candidate:
            detail = sanitize_detail(candidate)
            if detail:
                break

    evidence: Dict[str, Any] = {}
    if status is not None:
        evidence["httpStatus"] = status
    if code:
        evidence["errorCode"] = code
    if detail:
        evidence["errorDetail"] = detail
    return evidence


def has_authoritative_auth_evidence(error_obj: Any) -> bool:
    """Whether an auth classification is strong enough for permanent scope quarantine."""
    evidence = provider_http_error_evidence(error_obj)
    if evidence.get("httpStatus") == 401:
        return True
    code = str(evidence.get("errorCode") or "").lower()
    if code in {
        "authentication_error",
        "invalid_api_key",
        "invalid_authentication_error",
        "unauthorized",
    }:
        return True
    return False


def contains_transport_failure(value) -> bool:
    """Whether a transport marker appears anywhere in a nested error payload.

    Recursive because OpenCode nests the real cause under `errors[].cause.code`,
    and inspecting only the top level misses every retry-wrapped failure.
    """
    if isinstance(value, dict):
        return any(
            contains_transport_failure(item)
            for key, item in value.items()
            if key != _REQUEST_ECHO_KEY
        )
    if isinstance(value, (list, tuple)):
        return any(contains_transport_failure(item) for item in value)
    if isinstance(value, str):
        return any(marker in value for marker in PROVIDER_TRANSPORT_FAILURES)
    return False


#: Server-side statuses that mean "the gateway or model is unwell", as opposed
#: to a 4xx, which means the request was wrong. Retrying a 4xx elsewhere spends
#: money and hides a real defect, so only 5xx counts.
_PROVIDER_SERVER_ERROR_STATUSES = (500, 502, 503, 504)


def provider_failure_from_stderr(line: str):
    """A provider-transport failure reported on stderr, or None.

    Requires an OpenCode API-error marker plus evidence the fault was the
    provider's: either a socket-level transport marker, or a 5xx status. The
    two-marker rule exists because either signal alone produces false positives
    on ordinary provider noise, and it is kept.

    5xx is here because a gateway answering 503 is the most ordinary provider
    fault there is, and without it the turn is not failed fast: it sits until
    the stream idle timeout, holding the task slot and reporting nothing an
    operator can act on.
    """
    if "AI_APICallError" not in line and "AI_RetryError" not in line:
        return None
    evidence = line
    start = line.find("error=")
    if start >= 0:
        try:
            payload, _ = json.JSONDecoder().raw_decode(line[start + len("error="):])
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            # Judge the error, not the echoed request: a diff that says
            # "fetch failed" or `"statusCode":503` is not a provider fault.
            evidence = json.dumps(_without_request_echo(payload), ensure_ascii=False)
    compact = evidence.replace(" ", "")
    if not any(marker in evidence for marker in PROVIDER_TRANSPORT_FAILURES) and not any(
        f'"statusCode":{status}' in compact
        for status in _PROVIDER_SERVER_ERROR_STATUSES
    ):
        return None
    return {"message": "opencode provider transport failure", "stderr": line[:4000]}


def provider_fallback_from_stderr(line: str):
    """Return a classified fail-fast provider failure from OpenCode stderr.

    OpenCode can keep retrying a 429 internally without emitting JSONL. Waiting
    for process timeout in that case hides the real reason and stalls the whole
    fallback chain, so classify the structured API envelope as it arrives.
    """
    transport = provider_failure_from_stderr(line)
    if transport is not None:
        return "provider_transport_error", transport
    if "AI_APICallError" not in line and "AI_RetryError" not in line:
        return None
    marker = line.find("error=")
    if marker < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(line[marker + len("error="):])
    except (TypeError, ValueError):
        return None
    reason = classify_provider_model_error(payload)
    if reason is None:
        return None
    evidence = provider_http_error_evidence(payload)
    return reason, {
        "name": "OpenCodeProviderFailure",
        "data": {"message": reason, **evidence},
    }


def provider_failure_is_fail_fast(failure: Any, *, saw_meaningful_output: bool) -> bool:
    """Fail over only before a model has begun useful, potentially billed work."""
    return failure is not None and not saw_meaningful_output


def classify_provider_model_error(error_obj: Any) -> Optional[str]:
    """Return a narrow fallback reason for provider/model availability errors."""
    if not error_obj:
        return None
    if isinstance(error_obj, Mapping):
        data = error_obj.get("data")
        data = data if isinstance(data, Mapping) else {}
        # OpenCode's stderr envelope nests the API error, and that error echoes
        # the request under `requestBodyValues`. Read only what the provider
        # said: OpenCode's own system prompt mentions "authentication", so
        # matching over the stringified echo turned a 429 into an auth failure.
        nested = error_obj.get("error")
        nested = nested if isinstance(nested, Mapping) else {}
        direct_messages = {
            str(message).strip().lower()
            for message in (
                error_obj.get("message"),
                nested.get("message"),
                data.get("message"),
            )
            if message
        }
        status_code = (
            data.get("statusCode")
            or data.get("status_code")
            or error_obj.get("statusCode")
            or nested.get("statusCode")
        )
        message_parts = [
            error_obj.get("message"),
            error_obj.get("name"),
            error_obj.get("responseBody"),
            None if nested else error_obj.get("error"),
            nested.get("message"),
            nested.get("name"),
            nested.get("responseBody"),
            data.get("message"),
            data.get("error"),
            data.get("errorCode"),
            data.get("type"),
            data.get("responseBody"),
        ]
    else:
        status_code = None
        message_parts = [error_obj]
        direct_messages = {str(error_obj).strip().lower()}
    text = " ".join(str(part) for part in message_parts if part).lower()
    quota_signatures = (
        "预扣费额度失败",
        "用户剩余额度",
        "需要预扣费额度",
        "insufficient balance",
        "insufficient credit",
        "insufficient quota",
        "credit balance is too low",
        "usage limit",
        "usage_limit_reached",
        "rate limit",
        "too many request",
        "quota exceeded",
    )
    if any(signature in text for signature in quota_signatures):
        return "rate_limit"
    # Some gateways mislabel model routing failures as authentication errors.
    # Require an explicit model subject so missing credentials stay auth failures.
    if (
        re.search(r"\bmodel(?:[ _-]id)?\s+(?:does not exist|not found|is not found)\b", text)
        or "unknown model" in text
        or "model_not_found" in text
    ):
        return "model_not_found"
    if (
        status_code == 401
        or "authentication" in text
        or "invalid api key" in text
        or "invalid_api_key" in text
        or "unauthorized" in text
    ):
        return "provider_auth_failed"
    if status_code == 404:
        return "model_not_found"
    if status_code == 429:
        return "rate_limit"
    if "disabled" in text:
        return "model_disabled"
    if "not found" in text or "does not exist" in text or "unknown model" in text:
        return "model_not_found"
    if "unavailable" in text or "not available" in text:
        return "model_unavailable"
    transient_transport_signatures = (
        "stream error: stream id",
        "connection reset by peer",
        "connection closed before message completed",
        "upstream connect error or disconnect/reset before headers",
        "unexpected eof",
        "unexpected end of json input",
        "unterminated json",
    )
    if direct_messages & {"transport", "transport error"}:
        # OpenCode 2 can collapse an exhausted provider stream to a bare
        # unknown error without retaining the HTTP or socket-level cause.
        return "provider_transport_error"
    if any(signature in text for signature in transient_transport_signatures):
        return "provider_transport_error"
    try:
        if int(status_code) in _PROVIDER_SERVER_ERROR_STATUSES:
            # The gateway or the model behind it is unwell. Transient by
            # nature, and worth trying the next provider in the chain.
            return "provider_transport_error"
    except (TypeError, ValueError):
        pass
    return None


def _configured_providers() -> set:
    chain = provider_candidates(fallback_enabled=True)
    if chain:
        return {candidate.provider for candidate in chain}
    providers = set()
    if settings.opencode_provider:
        providers.add(settings.opencode_provider)
    return providers


def _provider_from_model(model_id: Optional[str] = None) -> Optional[str]:
    if model_id and "/" in model_id:
        return model_id.split("/", 1)[0]
    chain = provider_candidates(fallback_enabled=False)
    if chain:
        return chain[0].provider
    if settings.opencode_provider:
        return settings.opencode_provider
    return None


def _provider_token_env_var(provider_id: str) -> str:
    mapping = {
        "deepseek": "DEEPSEEK_API_KEY",
        "google": "GOOGLE_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "tencent": "TENCENT_API_KEY",
        "token-pool": "OPENAI_API_KEY",
    }
    if provider_id in mapping:
        return mapping[provider_id]
    normalized = "".join(ch if ch.isalnum() else "_" for ch in provider_id.upper())
    return f"{normalized}_API_KEY"


def _build_env(
    repo_path: Optional[str] = None,
    *,
    model_id: Optional[str] = None,
) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(settings.opencode_credential_env)
    if repo_path:
        env["PWD"] = repo_path
    if settings.opencode_data_home:
        env["XDG_DATA_HOME"] = str(Path(settings.opencode_data_home).expanduser().resolve())

    # OpenCode tool calls inherit this environment. The deployed service is
    # launched by a venv interpreter, but process PATH may not include that venv.
    # Prepending it keeps Python self-checks aligned with the consumer's verifier runtime.
    service_python_bin = Path(sys.executable).resolve().parent
    if service_python_bin.exists():
        path_parts = [part for part in (env.get("PATH") or "").split(os.pathsep) if part]
        service_bin_text = str(service_python_bin)
        if service_bin_text not in path_parts:
            env["PATH"] = os.pathsep.join([service_bin_text, *path_parts])
        resolved_python = str(Path(sys.executable).resolve())
        env.setdefault("AGENT_SERVICE_PYTHON_BIN", resolved_python)
        # Compatibility window (D5): the source repo's Python verifier reads the
        # legacy name. Emit both until that consumer migrates, then drop this.
        env.setdefault("UTA_SERVICE_PYTHON_BIN", resolved_python)

    provider_id = _provider_from_model(model_id)
    provider_tokens = parse_provider_tokens(settings.opencode_provider_tokens)
    provider_base_urls = parse_provider_base_urls(settings.opencode_provider_base_urls)
    provider_token = provider_tokens.get(provider_id or "")
    referenced_credentials = {}
    for token in provider_tokens.values():
        if token.startswith("{env:") and token.endswith("}"):
            name = token[5:-1]
            referenced_credentials[name] = env.get(name, "")
    if provider_token and provider_token.startswith("{env:") and provider_token.endswith("}"):
        provider_token = referenced_credentials[provider_token[5:-1]]
    provider_base_url = provider_base_urls.get(provider_id or "")

    gemini_key = provider_token if provider_id == "google" else ""
    gemini_key = gemini_key or (settings.gemini_api_key if provider_id == "google" else None) or ""
    if provider_id == "google" and gemini_key:
        env["GOOGLE_GENERATIVE_AI_API_KEY"] = gemini_key
        env["GOOGLE_API_KEY"] = gemini_key

    openrouter_key = provider_token if provider_id == "openrouter" else ""
    openrouter_key = openrouter_key or (settings.openrouter_api_key if provider_id == "openrouter" else None) or ""
    if provider_id == "openrouter" and openrouter_key:
        env["OPENROUTER_API_KEY"] = openrouter_key

    deepseek_key = provider_token if provider_id == "deepseek" else ""
    deepseek_key = deepseek_key or (settings.deepseek_api_key if provider_id == "deepseek" else None) or ""
    if provider_id == "deepseek" and deepseek_key:
        env["DEEPSEEK_API_KEY"] = deepseek_key

    tencent_key = provider_token if provider_id == "tencent" else ""
    tencent_key = tencent_key or (settings.tencent_api_key if provider_id == "tencent" else None) or ""
    if provider_id == "tencent" and tencent_key:
        env["TENCENT_API_KEY"] = tencent_key

    tencent_base = settings.tencent_base_url or os.environ.get("TENCENT_BASE_URL", "")
    if provider_id == "tencent" and tencent_base:
        env["TENCENT_BASE_URL"] = tencent_base

    ollama_host = settings.ollama_host or os.environ.get("OLLAMA_HOST", "")
    if provider_id == "ollama" and ollama_host:
        env["OLLAMA_HOST"] = ollama_host

    if provider_id and not provider_token:
        # Otherwise the only symptom is a 401 from the provider, arriving as
        # `fallback_reason: provider_auth_failed` several layers away with the
        # provider's own words ("Missing API key") and nothing naming the
        # setting that was empty. Every candidate in a chain then fails the
        # same way, which reads like a provider fault rather than a configuration
        # gap. Warn once per submission, naming the provider and the setting.
        logger.warning(
            "no token resolved for provider %r; the turn will call it "
            "unauthenticated. Set '%s.token=...' in opencode_provider_tokens "
            "(currently %s).",
            provider_id,
            provider_id,
            "empty" if not settings.opencode_provider_tokens else "set but without this provider",
        )

    if provider_token and provider_id not in {"deepseek", "google", "openrouter", "tencent"}:
        env[_provider_token_env_var(provider_id)] = provider_token
    if provider_base_url and provider_id != "openai":
        env["OPENAI_BASE_URL"] = provider_base_url
        if provider_token:
            env["OPENAI_API_KEY"] = provider_token

    # Native OpenAI (ChatGPT subscription) uses OAuth stored in OpenCode config.
    # OpenCode also treats OPENAI_* specially for the reserved openai provider;
    # keep proxy baseURL/apiKey in opencode.json instead of the process env.
    if provider_id == "openai":
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENAI_BASE_URL", None)

    # Explicit config references must survive legacy provider env cleanup.
    env.update(referenced_credentials)
    return env


def _effective_variant(variant: Optional[str] = None) -> Optional[str]:
    value = variant if variant is not None else settings.opencode_variant
    value = (value or "").strip()
    return value or None


def _message_args(message: str, *, repo_path: Optional[str]) -> List[str]:
    threshold = int(getattr(settings, "opencode_prompt_file_threshold_chars", 0) or 60000)
    if len(message or "") <= threshold or not repo_path:
        return [message]
    prompt_path = _write_prompt_file(repo_path, message)
    # OpenCode's --file option is an array option. If the short message is
    # placed after --file, the CLI treats it as another file path.
    return [
        f"Read and follow the attached prompt file exactly: {prompt_path.name}",
        "--file",
        str(prompt_path),
    ]


def _prompt_file_args(prompt_path: Path) -> List[str]:
    """Reference a caller-supplied prompt file.

    Same shape as the oversized-message branch: a short instruction, then the
    file. `--file` is an array option, so the instruction must come first or the
    CLI consumes it as another path.

    The path is resolved to an absolute one because the agent does not run in
    the caller's working directory -- it runs in a per-turn workspace. A
    caller whose paths are configured relatively (`runtime/audit/...`, which
    is what a deployment sourcing a relative BASE_DIR produces) would
    otherwise hand over a path that resolves to nothing, and the agent exits
    with "File not found" before emitting a single event. That reads exactly
    like a model that produced no output, so it burns the whole fallback chain
    and reports every model unhealthy.
    """
    # `absolute`, not `resolve`: the aim is to anchor a relative path, and
    # resolving would also rewrite symlinks a deployment chose deliberately.
    prompt_path = Path(os.path.abspath(prompt_path))
    return [
        f"Read and follow the attached prompt file exactly: {prompt_path.name}",
        "--file",
        str(prompt_path),
    ]


def _write_prompt_file(repo_path: str, message: str) -> Path:
    root = Path(repo_path) / current_config().agent_cache_dir / "opencode" / "prompts"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    path = root / f"prompt-{digest}.md"
    path.write_text(message, encoding="utf-8")
    return path


def _start_stdin_writer(
    proc: subprocess.Popen,
    *,
    message: Optional[str],
    prompt_file: Optional[Path],
) -> threading.Thread:
    """Write stdin on another thread so a chatty child cannot deadlock us.

    Filling stdin before reading stdout is a classic pipe stall: the child
    blocks on a full stdout pipe while we block on a full stdin pipe.
    """

    def write() -> None:
        handle = proc.stdin
        if handle is None:
            return
        try:
            text = message
            if text is None and prompt_file is not None:
                text = Path(prompt_file).read_text(encoding="utf-8")
            handle.write((text or "").encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass

    thread = threading.Thread(target=write, name="opencode-stdin", daemon=True)
    thread.start()
    return thread


def _safe_log_component(value: Optional[str], default: str) -> str:
    raw = str(value or default)
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw)
    return cleaned[:120] or default


def _opencode_turn_log_path(repo_path: str, *, model_id: Optional[str]) -> Optional[Path]:
    if not getattr(settings, "opencode_turn_log_enabled", True):
        return None
    configured = Path(
        settings.opencode_turn_log_dir
        or f"{current_config().agent_cache_dir}/opencode_turns"
    )
    root = configured if configured.is_absolute() else Path(repo_path) / configured
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    model = _safe_log_component(model_id, "model")
    return root / f"{timestamp}_{model}.jsonl"


def _json_safe_process_id(proc: subprocess.Popen) -> Optional[Any]:
    pid = getattr(proc, "pid", None)
    if pid is None:
        return None
    if isinstance(pid, (str, int, float, bool)):
        return pid
    return str(pid)


def _opencode_child_preexec() -> None:
    """Create an isolated child group and ask Linux to stop it if the parent dies."""
    os.setsid()
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        pr_set_pdeathsig = 1
        libc.prctl(pr_set_pdeathsig, signal.SIGTERM)
        if os.getppid() == 1:
            os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        return


def _opencode_popen_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {}
    return {"preexec_fn": _opencode_child_preexec}


def _terminate_opencode_process(proc: subprocess.Popen, *, grace_seconds: float = 2.0) -> None:
    if os.name != "nt":
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            proc.wait(timeout=grace_seconds)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class OpenCodeProcess:
    """Runs `opencode run --format json` for one turn and parses the JSONL stream."""

    def __init__(self):
        self._parser = OpenCodeStreamParser()

    def run_turn(
        self,
        message: Optional[str] = None,
        *,
        prompt_file: Optional[Path] = None,
        session_id: Optional[str] = None,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
        repo_path: str,
        attachments: Optional[Sequence[str]] = None,
        timeout: int = 3600,
        is_cancelled: Optional[Callable[[], bool]] = None,
        on_update: Optional[Callable[[str], None]] = None,
        delivery: str = "argv",
        title: Optional[str] = None,
        pure: Optional[bool] = None,
        env: Optional[Mapping[str, str]] = None,
        project_dir: Optional[str] = None,
    ) -> TurnResult:
        if (message is None) == (prompt_file is None):
            raise ValueError("pass exactly one of message= or prompt_file=")
        if delivery not in {"argv", "file", "stdin"}:
            raise ValueError(f"delivery must be argv, file, or stdin, got {delivery!r}")
        cmd = self._build_cmd(
            message,
            prompt_file=prompt_file,
            attachments=attachments,
            session_id=session_id,
            model_id=model_id,
            variant=variant,
            repo_path=repo_path,
            delivery=delivery,
            title=title,
            pure=pure,
            project_dir=project_dir,
        )
        process_env = _build_env(repo_path, model_id=model_id)
        if env:
            process_env.update(dict(env))
        process_env.update(settings.opencode_credential_env)
        use_stdin = delivery == "stdin"
        if on_update:
            # Emit from the invocation boundary so retries and session turns
            # report the effective variant, not an earlier discovery snapshot.
            on_update(
                f"model-selected: {model_id or settings.opencode_model or 'default'} "
                f"effort={_effective_variant(variant) or 'default'}"
            )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=repo_path,
            env=sanitize_agent_env(process_env),
            **_opencode_popen_kwargs(),
        )
        stdin_thread = (
            _start_stdin_writer(proc, message=message, prompt_file=prompt_file)
            if use_stdin
            else None
        )
        with _track_process(proc):
            try:
                return self._read_stream(
                    proc,
                    timeout=timeout,
                    on_update=on_update,
                    model_id=model_id,
                    started_at=time.time(),
                    raw_log_path=_opencode_turn_log_path(repo_path, model_id=model_id),
                    is_cancelled=is_cancelled,
                )
            finally:
                if stdin_thread is not None:
                    stdin_thread.join(timeout=1)

    def _build_cmd(
        self,
        message: str,
        *,
        session_id: Optional[str],
        model_id: Optional[str],
        variant: Optional[str] = None,
        repo_path: Optional[str] = None,
        attachments: Optional[Sequence[str]] = None,
        prompt_file: Optional[Path] = None,
        delivery: str = "argv",
        title: Optional[str] = None,
        pure: Optional[bool] = None,
        project_dir: Optional[str] = None,
    ) -> List[str]:
        spawn_cmd_raw = settings.opencode_spawn_cmd
        if spawn_cmd_raw:
            try:
                base = json.loads(spawn_cmd_raw)
                if not isinstance(base, list):
                    base = [str(spawn_cmd_raw)]
            except (json.JSONDecodeError, TypeError):
                base = [spawn_cmd_raw]
        else:
            bin_path = settings.opencode_bin or "opencode"
            base = [bin_path, "run"]

        cmd = list(base)
        if keep_print_logs():
            cmd.append("--print-logs")
        cmd += ["--format", "json"]
        if settings.skip_permission_prompts:
            cmd.extend(skip_permission_args())
        use_pure = getattr(settings, "opencode_pure", True) if pure is None else bool(pure)
        if use_pure and keep_pure():
            cmd.append("--pure")

        cmd.extend(run_isolation_args(attach_url=settings.opencode_attach_url))
        # repo_path is already set as cwd on the subprocess; --dir overrides config
        # resolution in opencode 1.14.50+ and prevents local opencode.json from loading.
        if project_dir and keep_project_dir_flag():
            cmd += ["--dir", str(project_dir)]
        if title:
            cmd += ["--title", str(title)]

        effective_variant = _effective_variant(variant)
        model_flag, variant_flag = model_cli_values(model_id, effective_variant)
        if model_flag and settings.opencode_pass_model_flag:
            cmd += ["--model", model_flag]
        if variant_flag:
            cmd += ["--variant", variant_flag]

        if session_id:
            cmd += ["--session", session_id, "--continue"]

        if delivery == "stdin":
            pass
        elif prompt_file is not None or delivery == "file":
            path = Path(prompt_file) if prompt_file is not None else _write_prompt_file(
                repo_path or ".", message or ""
            )
            cmd.extend(_prompt_file_args(path))
        else:
            cmd.extend(_message_args(message or "", repo_path=repo_path))
        for attachment in attachments or ():
            cmd += ["-f", str(attachment)]
        return cmd

    def _read_stream(
        self,
        proc: subprocess.Popen,
        timeout: int,
        on_update: Optional[Callable[[str], None]],
        *,
        model_id: Optional[str] = None,
        started_at: Optional[float] = None,
        raw_log_path: Optional[Path] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> TurnResult:
        events: List[Dict[str, Any]] = []
        cancelled = False
        provider_failure: List[Any] = [None]
        collected_session_id: Optional[str] = None
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        progress_lock = threading.Lock()
        stream_started_at = time.time()
        last_event_at = stream_started_at
        saw_event = False
        saw_meaningful_output = False
        raw_log_lock = threading.Lock()
        raw_log_file = None
        raw_log_path_text: Optional[str] = None
        if raw_log_path is not None:
            try:
                raw_log_path.parent.mkdir(parents=True, exist_ok=True)
                raw_log_file = raw_log_path.open("a", encoding="utf-8")
                raw_log_path_text = str(raw_log_path)
                raw_log_file.write(
                    json.dumps(
                        {
                            "kind": "turn_start",
                            "timestamp": time.time(),
                            "model_id": model_id,
                            "timeout": timeout,
                            "pid": _json_safe_process_id(proc),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                raw_log_file.flush()
            except (OSError, TypeError):
                raw_log_file = None
                raw_log_path_text = None

        stdout_stream = getattr(proc, "stdout", None)
        stderr_stream = getattr(proc, "stderr", None)

        def record_raw_line(stream: str, line: str) -> None:
            if raw_log_file is None:
                return
            event = self._parser.parse_line(line)
            payload = {
                "kind": "stream_line",
                "timestamp": time.time(),
                "stream": stream,
                "raw": line,
                "event_type": event.get("type") if isinstance(event, dict) else None,
                "session_id": event.get("sessionID") if isinstance(event, dict) else None,
            }
            with raw_log_lock:
                raw_log_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
                raw_log_file.flush()

        def finish_raw_log(result: TurnResult) -> TurnResult:
            if raw_log_file is None:
                return result
            result.raw_log_path = raw_log_path_text
            summary = {
                "kind": "turn_finish",
                "timestamp": time.time(),
                "result_type": result.type,
                "session_id": result.session_id,
                "model_id": result.model_id,
                "cost_usd": result.cost_usd,
                "tokens": result.tokens,
                "patch_count": result.patch_count,
                "fallback_eligible": result.fallback_eligible,
                "fallback_reason": result.fallback_reason,
            }
            with raw_log_lock:
                raw_log_file.write(json.dumps(summary, ensure_ascii=False) + "\n")
                raw_log_file.close()
            return result

        def mark_progress(event: Mapping[str, Any]) -> None:
            nonlocal last_event_at, saw_event, saw_meaningful_output
            with progress_lock:
                last_event_at = time.time()
                saw_event = True
                if event.get("type") not in {"step_start", "step-start", "connection"}:
                    saw_meaningful_output = True

        def stdout_reader():
            nonlocal collected_session_id
            if stdout_stream is None:
                return
            try:
                iterator = iter(stdout_stream)
            except TypeError:
                return
            while True:
                try:
                    raw = next(iterator)
                except (StopIteration, TypeError):
                    return
                line = raw.decode(errors="replace").rstrip()
                stdout_lines.append(line)
                record_raw_line("stdout", line)
                event = self._parser.parse_line(line)
                if event is None:
                    continue
                events.append(event)
                mark_progress(event)
                if collected_session_id is None:
                    collected_session_id = event.get("sessionID")
                if on_update:
                    progress = self._parser.progress_line(event)
                    if progress:
                        on_update(progress)

        def stderr_reader():
            if stderr_stream is None:
                return
            try:
                iterator = iter(stderr_stream)
            except TypeError:
                return
            while True:
                try:
                    raw = next(iterator)
                except (StopIteration, TypeError):
                    return
                line = raw.decode(errors="replace").rstrip()
                stderr_lines.append(line)
                record_raw_line("stderr", line)
                failure = provider_fallback_from_stderr(line)
                with progress_lock:
                    can_fail_fast = provider_failure_is_fail_fast(
                        failure,
                        saw_meaningful_output=saw_meaningful_output,
                    )
                if can_fail_fast and provider_failure[0] is None:
                    # Do not wait for the idle timeout: the provider is
                    # unreachable and no output is coming.
                    provider_failure[0] = failure
                event = self._parser.parse_line(line)
                if event is None:
                    continue
                events.append(event)
                mark_progress(event)

        stdout_thread = threading.Thread(target=stdout_reader, daemon=True)
        stderr_thread = threading.Thread(target=stderr_reader, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        def timeout_result() -> TurnResult:
            _terminate_opencode_process(proc)
            stdout_thread.join(timeout=0.2)
            stderr_thread.join(timeout=0.2)
            inferred = self._build_result(
                events,
                collected_session_id,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                model_id=model_id,
                started_at=started_at,
            )
            if inferred.type in {"rate_limited", "error"}:
                return finish_raw_log(inferred)
            # Token accounting is not a completed artifact. Preserve usage while
            # allowing recovery unless recorded edits make replay unsafe.
            no_output = not inferred.tokens and not inferred.patch_count
            return finish_raw_log(TurnResult(
                type="timeout",
                session_id=collected_session_id,
                model_id=inferred.model_id or model_id,
                tokens=inferred.tokens,
                cost_usd=inferred.cost_usd,
                result=inferred.result,
                patch_count=inferred.patch_count,
                fallback_eligible=not inferred.patch_count,
                fallback_reason="no_output" if no_output else "timeout",
                error={
                    "name": "OpenCodeNoOutputTimeout",
                    "data": {
                        "message": "OpenCode produced no JSONL output before timeout",
                        "model_id": model_id,
                    },
                } if no_output else None,
            ))

        base_timeout = max(0.001, float(timeout or 0))
        active_multiplier = max(1.0, float(settings.opencode_active_timeout_multiplier or 1.0))
        active_timeout = max(base_timeout, base_timeout * active_multiplier)
        idle_timeout = max(1.0, float(settings.opencode_stream_idle_timeout_seconds or base_timeout))
        initial_output_timeout = min(
            base_timeout,
            max(0.1, float(settings.opencode_initial_output_timeout_seconds or base_timeout)),
        )

        while stdout_thread.is_alive() or stderr_thread.is_alive():
            with progress_lock:
                current_last_event_at = last_event_at
                current_saw_event = saw_event
                current_saw_meaningful_output = saw_meaningful_output
            if provider_failure[0] is not None and not current_saw_meaningful_output:
                _terminate_opencode_process(proc)
                fallback_reason, error = provider_failure[0]
                return finish_raw_log(TurnResult(
                    type="rate_limited" if fallback_reason == "rate_limit" else "error",
                    model_id=model_id,
                    session_id=collected_session_id,
                    error=error,
                    fallback_eligible=True,
                    fallback_reason=fallback_reason,
                ))
            if is_cancelled is not None and is_cancelled():
                _terminate_opencode_process(proc)
                cancelled = True
                break
            now = time.time()
            elapsed = now - stream_started_at

            if not current_saw_meaningful_output and elapsed >= initial_output_timeout:
                return timeout_result()

            # Before OpenCode emits any JSONL event, keep the stage timeout as
            # the hard TTFT cap. Once events arrive, recent reasoning/tool/text
            # updates count as liveness and the broader active cap applies.
            if not current_saw_event and elapsed >= base_timeout:
                return timeout_result()
            if current_saw_event and elapsed >= active_timeout:
                return timeout_result()
            # After real progress, a silent next response is not evidence of
            # provider failure. Keep the active wall cap and cancellation, but
            # do not discard paid work merely because JSONL pauses mid-turn.
            if current_saw_event and not current_saw_meaningful_output and now - current_last_event_at >= idle_timeout:
                return timeout_result()

            time.sleep(0.1)

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_opencode_process(proc)

        if cancelled:
            return finish_raw_log(TurnResult(
                type="cancelled",
                model_id=model_id,
                session_id=collected_session_id,
                error={"message": "task cancelled by operator"},
                fallback_eligible=False,
            ))

        return finish_raw_log(self._build_result(
            events,
            collected_session_id,
            stdout_lines=stdout_lines,
            stderr_lines=stderr_lines,
            model_id=model_id,
            started_at=started_at,
        ))

    def _build_result(
        self,
        events: List[Dict[str, Any]],
        session_id: Optional[str],
        *,
        stdout_lines: Optional[List[str]] = None,
        stderr_lines: Optional[List[str]] = None,
        model_id: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> TurnResult:
        # OpenCode 2 may emit an aborted/shutdown error immediately before the
        # rejected tool event that caused it (for example, a headless
        # ``question`` prompt). Attribute that turn to the tool contract so the
        # caller can recover the same session instead of blaming the provider.
        tool_error = _terminal_tool_contract_error(events)
        if tool_error is not None:
            return TurnResult(
                type="error",
                session_id=session_id,
                model_id=model_id,
                error=tool_error,
                result=self._parser.extract_text(events),
                tokens=self._parser.extract_tokens(events),
                patch_count=self._parser.count_patches(events),
                fallback_eligible=False,
                fallback_reason="tool_contract_error",
            )

        for ev in events:
            if ev.get("type") == "error":
                error_obj = ev.get("error") or {}
                if self._parser.detect_rate_limit(ev):
                    data = error_obj.get("data") or {}
                    return TurnResult(
                        type="rate_limited",
                        session_id=session_id,
                        error=error_obj,
                        result="",
                        fallback_eligible=True,
                        fallback_reason="rate_limit",
                    )
                fallback_reason = classify_provider_model_error(error_obj)
                if fallback_reason is None and contains_transport_failure(error_obj):
                    # A retry-wrapped transport failure: the provider is
                    # unreachable, so the next candidate is worth trying.
                    fallback_reason = "provider_error"
                return TurnResult(
                    type="error",
                    session_id=session_id,
                    error=error_obj,
                    result=self._parser.extract_text(events),
                    fallback_eligible=fallback_reason is not None,
                    fallback_reason=fallback_reason,
                )

        result_text = self._parser.extract_text(events)
        tokens = self._parser.extract_tokens(events)
        patch_count = self._parser.count_patches(events)
        completion = self._parser.detect_completion(events)

        # If the JSONL stream reached an explicit terminal stop, trust that
        # completed turn over broader fallback heuristics that inspect raw
        # stderr/logs. Those fallbacks are intended for missing/partial streams
        # and can pick up unrelated provider noise.
        if completion == "stop":
            return TurnResult(
                type="completed",
                result=result_text,
                session_id=session_id,
                model_id=model_id,
                cost_usd=self._parser.extract_cost(events),
                tokens=tokens,
                patch_count=patch_count,
            )

        # Some OpenCode versions exit after a completed client-side tool call
        # instead of continuing the agent loop. Text before that tool is only
        # progress, not a terminal answer. Preserve the session so the harness
        # can resume it until OpenCode emits an explicit `stop`.
        if self._parser.finish_reason(events) == "tool-calls":
            return TurnResult(
                type="incomplete",
                result=result_text,
                session_id=session_id,
                model_id=model_id,
                cost_usd=self._parser.extract_cost(events),
                tokens=tokens,
                patch_count=patch_count,
                fallback_eligible=False,
            )

        stderr_output = "\n".join(line for line in (stderr_lines or []) if line).strip()
        text_rate_limit = self._infer_rate_limit_from_text(stderr_output)
        if text_rate_limit:
            return TurnResult(
                type="rate_limited",
                session_id=session_id,
                error=text_rate_limit,
                result=self._parser.extract_text(events),
                fallback_eligible=True,
                fallback_reason="rate_limit",
            )

        log_rate_limit = self._infer_rate_limit_from_logs(
            session_id=session_id,
            model_id=model_id,
            started_at=started_at,
        )
        if log_rate_limit:
            return TurnResult(
                type="rate_limited",
                session_id=session_id,
                error=log_rate_limit,
                result=self._parser.extract_text(events),
                fallback_eligible=True,
                fallback_reason="rate_limit",
            )

        if not completion and not result_text:
            no_output = not tokens and not patch_count
            return TurnResult(
                type="stalled",
                session_id=session_id,
                tokens=tokens,
                patch_count=patch_count,
                fallback_eligible=no_output,
                fallback_reason="no_output" if no_output else None,
                error={
                    "name": "OpenCodeNoOutputStall",
                    "data": {
                        "message": "OpenCode stream ended without assistant output",
                        "model_id": model_id,
                    },
                } if no_output else None,
            )

        return TurnResult(
            type="completed",
            result=result_text,
            session_id=session_id,
            model_id=model_id,
            cost_usd=self._parser.extract_cost(events),
            tokens=tokens,
        )

    def _infer_rate_limit_from_text(self, raw_output: str) -> Optional[Dict[str, Any]]:
        if not raw_output:
            return None
        parsed = self._infer_structured_rate_limit(raw_output)
        if parsed:
            return parsed
        plain_lines = []
        for line in raw_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("{") or " error={" in stripped:
                continue
            plain_lines.append(stripped)
        plain_text = "\n".join(plain_lines)
        if not self._parser.detect_rate_limit_text(plain_text):
            return None
        retry_after = self._extract_int(raw_output, "resets_in_seconds")
        reset_at = self._extract_int(raw_output, "resets_at")
        return {
            "raw_type": "rate_limit",
            "message": plain_text.splitlines()[0][:500] if plain_text else "Provider/model rate limit reached",
            "retry_after_seconds": retry_after,
            "reset_at": reset_at,
            "data": {
                "statusCode": 429,
                "message": plain_text.splitlines()[0][:500] if plain_text else "Provider/model rate limit reached",
                **({"resets_in_seconds": retry_after} if retry_after is not None else {}),
                **({"resets_at": reset_at} if reset_at is not None else {}),
            },
        }

    def _infer_structured_rate_limit(self, raw_output: str) -> Optional[Dict[str, Any]]:
        decoder = json.JSONDecoder()
        for line in raw_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            candidates = [stripped]
            if " error=" in stripped:
                candidates.insert(0, stripped.partition(" error=")[2])
            for candidate in candidates:
                candidate = candidate.strip()
                if not candidate.startswith("{"):
                    continue
                try:
                    payload, _ = decoder.raw_decode(candidate)
                except Exception:
                    continue
                parsed = parse_rate_limit_payload(payload)
                if not parsed:
                    continue
                parsed["retry_after_seconds"] = parsed.get("retry_after_seconds") or self._extract_int(candidate, "resets_in_seconds")
                parsed["reset_at"] = parsed.get("reset_at") or self._extract_int(candidate, "resets_at")
                parsed.setdefault("data", {})
                parsed["data"].setdefault("statusCode", parsed.get("status_code") or 429)
                parsed["data"].setdefault("message", parsed.get("message") or "Provider/model rate limit reached")
                return parsed
        return None

    def _infer_rate_limit_from_logs(
        self,
        *,
        session_id: Optional[str],
        model_id: Optional[str],
        started_at: Optional[float],
    ) -> Optional[Dict[str, Any]]:
        provider_id = model_id.split("/", 1)[0] if model_id and "/" in model_id else None
        bare_model_id = model_id.split("/", 1)[1] if model_id and "/" in model_id else model_id
        return detect_rate_limit_in_logs(
            session_id=session_id,
            provider_id=provider_id,
            model_id=bare_model_id,
            since_time=started_at,
        )

    @staticmethod
    def _extract_int(raw_text: Any, field: str) -> Optional[int]:
        if not isinstance(raw_text, str):
            return None
        marker = f'"{field}":'
        idx = raw_text.find(marker)
        if idx < 0:
            return None
        idx += len(marker)
        digits = []
        while idx < len(raw_text) and raw_text[idx] in " \t":
            idx += 1
        while idx < len(raw_text) and raw_text[idx].isdigit():
            digits.append(raw_text[idx])
            idx += 1
        if not digits:
            return None
        try:
            return int("".join(digits))
        except Exception:
            return None


def _terminal_tool_contract_error(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Describe a terminal tool failure without blaming the model provider."""

    for event in reversed(events):
        if event.get("type") != "tool_use":
            continue
        part = event.get("part") or {}
        state = part.get("state") or {}
        if state.get("status") != "error":
            return None
        output = str(state.get("output") or state.get("error") or "tool failed")
        return {
            "name": "OpenCodeToolContractError",
            "data": {
                "tool": str(part.get("tool") or "unknown"),
                "message": output[:1000],
            },
        }
    return None
