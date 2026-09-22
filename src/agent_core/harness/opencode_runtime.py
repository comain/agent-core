"""OpenCode 1.x versus 2.x runtime differences.

The JSONL stream from ``opencode run --format json`` is shared. Isolation
flags and the HTTP API are not. 2.x starts a user-level background service
unless ``--standalone`` is set, moved HTTP routes under ``/api``, wraps
bodies in ``{data: ...}``, and requires basic auth on the server.
"""

from __future__ import annotations

import base64
import re
import secrets
import subprocess
from typing import Any, Mapping, Optional

from agent_core.config import current_config

# OpenCode 2 prints `opencode v2.0.6`; `\b2` does not match inside `v2`.
_VERSION_RE = re.compile(r"\bv?([12])\.\d+\.\d+\b")
_detect_cache: dict[str, int] = {}
_generated_password: Optional[str] = None


def reset_runtime_cache() -> None:
    """Drop cached version and generated server password. Tests only."""
    _detect_cache.clear()
    global _generated_password
    _generated_password = None


def parse_major(text: str) -> Optional[int]:
    """Return 1 or 2 from an ``opencode --version`` string, else None."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1))


def configured_bin() -> str:
    """Binary the process actually spawns, not whatever ``opencode`` is on PATH."""
    raw = getattr(current_config(), "opencode_bin", None)
    text = str(raw).strip() if raw else ""
    return text or "opencode"


def detect_major(bin_path: Optional[str] = None) -> int:
    """Probe the binary once. Fail closed to 1.x, which is what is deployed."""
    binary = bin_path or configured_bin()
    cached = _detect_cache.get(binary)
    if cached is not None:
        return cached
    major = 1
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        major = parse_major(f"{result.stdout or ''}\n{result.stderr or ''}") or 1
    except (OSError, subprocess.SubprocessError):
        major = 1
    _detect_cache[binary] = major
    return major


def configured_major() -> Optional[int]:
    raw = getattr(current_config(), "opencode_major", None)
    if raw in (1, 2):
        return int(raw)
    return None


def major(*, bin_path: Optional[str] = None) -> int:
    pinned = configured_major()
    if pinned is not None:
        return pinned
    return detect_major(bin_path)


def is_v2(*, bin_path: Optional[str] = None) -> bool:
    return major(bin_path=bin_path) >= 2


def api_prefix(*, bin_path: Optional[str] = None) -> str:
    return "/api" if is_v2(bin_path=bin_path) else ""


def health_path(*, bin_path: Optional[str] = None) -> str:
    return "/api/info" if is_v2(bin_path=bin_path) else "/session"


def session_path(
    session_id: Optional[str] = None,
    *,
    suffix: str = "",
    bin_path: Optional[str] = None,
) -> str:
    base = f"{api_prefix(bin_path=bin_path)}/session"
    if not session_id:
        return base
    path = f"{base}/{session_id}"
    return f"{path}/{suffix.lstrip('/')}" if suffix else path


def prompt_path(session_id: str, *, bin_path: Optional[str] = None) -> str:
    if is_v2(bin_path=bin_path):
        return session_path(session_id, suffix="prompt", bin_path=bin_path)
    return session_path(session_id, suffix="message", bin_path=bin_path)


def init_path(session_id: str, *, bin_path: Optional[str] = None) -> str:
    if is_v2(bin_path=bin_path):
        return session_path(session_id, suffix="command", bin_path=bin_path)
    return session_path(session_id, suffix="init", bin_path=bin_path)


def model_switch_path(session_id: str, *, bin_path: Optional[str] = None) -> str:
    return session_path(session_id, suffix="model", bin_path=bin_path)


def provider_path(*, bin_path: Optional[str] = None) -> str:
    return f"{api_prefix(bin_path=bin_path)}/provider"


def provider_auth_path(*, bin_path: Optional[str] = None) -> str:
    if is_v2(bin_path=bin_path):
        return f"{api_prefix(bin_path=bin_path)}/integration"
    return "/provider/auth"


def oauth_authorize_path(provider_id: str, *, bin_path: Optional[str] = None) -> str:
    if is_v2(bin_path=bin_path):
        return f"{api_prefix(bin_path=bin_path)}/integration/{provider_id}/connect/oauth"
    return f"/provider/{provider_id}/oauth/authorize"


def unwrap(payload: Any, *, bin_path: Optional[str] = None) -> Any:
    if not is_v2(bin_path=bin_path):
        return payload
    if isinstance(payload, Mapping) and "data" in payload:
        return payload["data"]
    return payload


def session_id_of(payload: Any, *, bin_path: Optional[str] = None) -> str:
    body = unwrap(payload, bin_path=bin_path)
    if isinstance(body, Mapping) and body.get("id"):
        return str(body["id"])
    raise KeyError("session id")


def server_password() -> str:
    configured = (getattr(current_config(), "opencode_server_password", None) or "").strip()
    if configured:
        return configured
    if not is_v2():
        return ""
    global _generated_password
    if not _generated_password:
        _generated_password = secrets.token_urlsafe(16)
    return _generated_password


def server_username() -> str:
    value = (getattr(current_config(), "opencode_server_username", None) or "opencode").strip()
    return value or "opencode"


def auth_headers() -> dict[str, str]:
    password = server_password()
    if not password:
        return {}
    token = base64.b64encode(f"{server_username()}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def model_ref(
    provider_id: Optional[str],
    model_id: str,
    *,
    variant: Optional[str] = None,
) -> dict[str, str]:
    if is_v2():
        ref: dict[str, str] = {}
        if provider_id:
            ref["providerID"] = provider_id
        ref["id"] = model_id
        if variant:
            ref["variant"] = variant
        return ref
    ref = {}
    if provider_id:
        ref["providerID"] = provider_id
    ref["modelID"] = model_id
    return ref


def run_isolation_args(*, attach_url: Optional[str] = None) -> list[str]:
    """Flags that keep a turn off the user's shared OpenCode service."""
    url = (attach_url or "").strip()
    if is_v2():
        if url:
            return ["--server", url]
        return ["--standalone"]
    if url:
        return ["--attach", url]
    return []


def skip_permission_args() -> list[str]:
    return ["--auto"] if is_v2() else ["--dangerously-skip-permissions"]


def keep_pure() -> bool:
    return not is_v2()


def keep_print_logs() -> bool:
    """V1 and V2 both accept ``--print-logs``; V2 treats it as a global flag."""
    return True


def keep_project_dir_flag() -> bool:
    """V2 dropped ``--dir``; the process cwd is the working directory."""
    return not is_v2()


def model_cli_values(
    model_id: Optional[str],
    variant: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(--model value, --variant value)``. V2 folds variant into the model."""
    if not is_v2():
        return model_id, variant
    if model_id and variant and "#" not in model_id:
        return f"{model_id}#{variant}", None
    return model_id, None


_ACTION_RENAMES = {
    "bash": "shell",
    "task": "subagent",
    "write": "edit",
    "patch": "edit",
}


def permission_rules_from_v1(permission: Mapping[str, Any]) -> list[dict[str, str]]:
    """Translate a V1 ``permission`` map into V2's ordered ``permissions`` array."""
    rules: list[dict[str, str]] = []
    for action, spec in (permission or {}).items():
        v2_action = _ACTION_RENAMES.get(str(action), str(action))
        if isinstance(spec, str):
            rules.append({"action": v2_action, "resource": "*", "effect": spec})
            continue
        if not isinstance(spec, Mapping):
            continue
        for resource, effect in spec.items():
            resource_text = str(resource)
            if v2_action == "external_directory" and resource_text.endswith("/**"):
                resource_text = resource_text[:-3] + "/*"
            rules.append(
                {
                    "action": v2_action,
                    "resource": resource_text,
                    "effect": str(effect),
                }
            )
    return rules


def providers_from_v1(provider: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a V1 ``provider`` map into V2 ``providers``."""
    native_packages = {
        "@ai-sdk/anthropic": "@opencode/ai/providers/anthropic",
        "@ai-sdk/google": "@opencode/ai/providers/google",
        "@ai-sdk/openai": "@opencode/ai/providers/openai",
        "@ai-sdk/openai-compatible": "@opencode/ai/providers/openai-compatible",
    }
    out: dict[str, Any] = {}
    for provider_id, spec in (provider or {}).items():
        if not isinstance(spec, Mapping):
            continue
        entry: dict[str, Any] = {}
        if spec.get("name"):
            entry["name"] = spec["name"]
        npm = spec.get("npm")
        if npm:
            npm_text = str(npm)
            entry["package"] = native_packages.get(npm_text, npm_text)
        options = spec.get("options")
        if isinstance(options, Mapping):
            entry["settings"] = dict(options)
        models = spec.get("models")
        if isinstance(models, Mapping):
            converted: dict[str, Any] = {}
            for model_id, model in models.items():
                if not isinstance(model, Mapping):
                    continue
                item = dict(model)
                if "options" in item:
                    item["settings"] = item.pop("options")
                variants = item.get("variants")
                if isinstance(variants, Mapping):
                    item["variants"] = [
                        {
                            "id": str(variant_id),
                            "settings": dict(variant_options),
                        }
                        for variant_id, variant_options in variants.items()
                        if isinstance(variant_options, Mapping)
                    ]
                item.setdefault("modelID", model_id)
                converted[str(model_id)] = item
            entry["models"] = converted
        out[str(provider_id)] = entry
    return out


def session_permission_payload(permission: Optional[list]) -> dict[str, Any]:
    """Session-create permission field for the active runtime."""
    if not permission:
        return {}
    if not is_v2():
        return {"permission": permission}
    rules: list[dict[str, Any]] = []
    for item in permission:
        if not isinstance(item, Mapping):
            continue
        if "effect" in item and "action" in item:
            rules.append(dict(item))
            continue
        action = str(item.get("permission") or "")
        rules.append(
            {
                "action": _ACTION_RENAMES.get(action, action) or "*",
                "resource": str(item.get("pattern") or "*"),
                "effect": str(item.get("action") or "deny"),
            }
        )
    return {"permissions": rules}


def wait_path(session_id: str) -> str:
    return f"/api/experimental/session/{session_id}/wait"


def v2_workspace_defaults() -> dict[str, Any]:
    """Headless defaults that use V2 features without paying for TUI conveniences.

    Snapshots and warming are TUI/session keep-alives. This harness owns Git
    state and cost accounting, so both stay off. Compaction checkpoints are
    the V2 replacement for long-review context overflow.
    """
    return {
        "snapshots": False,
        "warming": False,
        "update": "disable",
        "share": "disabled",
        "compaction": {
            "auto": True,
            "keep": {"tokens": 15000},
            "buffer": 20000,
        },
    }
