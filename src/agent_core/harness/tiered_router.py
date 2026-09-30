"""Provider-chain routing for OpenCode sessions.

The provider chain is the single source of model selection. Compile-fix,
coverage, mutation, planning, and generation phases use the same selected
provider/model candidate.

Usage:
    model = effective_model("compile_fix")
    session_id = client.create_session(model_id=model)
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import httpx

from agent_core.config import settings

_DEFAULT_COOLDOWN_SECONDS = 60
_MODEL_UNAVAILABLE_COOLDOWN_SECONDS = 15 * 60
_TIMEOUT_COOLDOWN_SECONDS = 5 * 60
_NO_OUTPUT_COOLDOWN_SECONDS = 10 * 60
_model_api_cache: Dict[str, Tuple[float, Optional[Set[str]]]] = {}


@dataclass(frozen=True)
class ProviderCandidate:
    """One ordered OpenCode provider/model candidate."""

    provider: str
    model: str
    index: int


def provider_local_model_id(provider_id: str, model_id: str) -> str:
    """Return the model id as registered inside one OpenCode provider."""
    if model_id.startswith(f"{provider_id}/"):
        return model_id.split("/", 1)[1]
    return model_id


def opencode_model_id(candidate: ProviderCandidate) -> str:
    """Return the executable OpenCode model id for a provider candidate."""
    if not candidate.provider or "/" in candidate.model:
        return candidate.model
    return f"{candidate.provider}/{candidate.model}"


def parse_provider_chain(raw: str) -> List[ProviderCandidate]:
    """Parse ``provider:model,model;provider:model`` preserving valid order."""
    candidates: List[ProviderCandidate] = []
    for provider_group in (raw or "").split(";"):
        group = provider_group.strip()
        if not group or ":" not in group:
            continue
        provider, raw_models = group.split(":", 1)
        provider = provider.strip()
        if not provider:
            continue
        for raw_model in raw_models.split(","):
            model = raw_model.strip()
            if not model:
                continue
            candidates.append(
                ProviderCandidate(
                    provider=provider,
                    model=model,
                    index=len(candidates),
                )
            )
    return candidates


def parse_provider_tokens(raw: str) -> Dict[str, str]:
    """Parse semicolon-separated ``provider.token=value`` entries."""
    tokens: Dict[str, str] = {}
    for raw_entry in (raw or "").split(";"):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        raw_key, raw_value = entry.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if not key.endswith(".token") or not value:
            continue
        provider = key[: -len(".token")].strip()
        if provider:
            tokens[provider] = value
    return tokens


def parse_provider_base_urls(raw: str) -> Dict[str, str]:
    """Parse semicolon-separated provider base URL entries."""
    urls: Dict[str, str] = {}
    if not isinstance(raw, str):
        return urls
    accepted_suffixes = (".base_url", ".baseURL", ".base-url", ".baseurl")
    for raw_entry in (raw or "").split(";"):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        raw_key, raw_value = entry.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip().rstrip("/")
        if not value:
            continue
        provider = ""
        for suffix in accepted_suffixes:
            if key.endswith(suffix):
                provider = key[: -len(suffix)].strip()
                break
        if provider:
            urls[provider] = value
    return urls


def _setting_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def provider_token_statuses(
    chain: Iterable[ProviderCandidate],
    tokens: Dict[str, str],
) -> Dict[str, str]:
    """Return token presence by provider without exposing token values."""
    statuses: Dict[str, str] = {}
    for candidate in chain:
        if candidate.provider in statuses:
            continue
        statuses[candidate.provider] = (
            "configured" if tokens.get(candidate.provider) else "missing"
        )
    return statuses


def provider_candidates(
    *,
    fallback_enabled: Optional[bool] = None,
) -> List[ProviderCandidate]:
    """Return active provider/model candidates for the current config."""
    candidates = parse_provider_chain(settings.opencode_provider_chain)
    if not candidates:
        return []
    enabled = (
        settings.opencode_provider_fallback_enabled
        if fallback_enabled is None
        else fallback_enabled
    )
    if not enabled:
        return candidates[:1]
    return candidates


def parse_model_list_response(payload: Any) -> Set[str]:
    """Extract model ids from common OpenAI-compatible model-list shapes."""
    raw_items: Any
    if isinstance(payload, dict):
        raw_items = payload.get("data")
        if raw_items is None:
            raw_items = payload.get("models")
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        return set()

    model_ids: Set[str] = set()
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            model_ids.add(item.strip())
        elif isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                model_ids.add(model_id.strip())
    return model_ids


def reset_model_availability_cache() -> None:
    _model_api_cache.clear()


def _provider_api_key(provider_id: str) -> str:
    tokens = parse_provider_tokens(settings.opencode_provider_tokens)
    token = tokens.get(provider_id, "")
    if token:
        return token
    if provider_id == "deepseek":
        return _setting_text(settings.deepseek_api_key)
    if provider_id == "openai":
        return _setting_text(settings.openai_api_key)
    if provider_id == "openrouter":
        return _setting_text(settings.openrouter_api_key)
    if provider_id == "tencent":
        return _setting_text(settings.tencent_api_key)
    if provider_id == "google":
        return _setting_text(settings.gemini_api_key)
    return ""


def _model_api_headers(provider_id: str) -> Dict[str, str]:
    token = _provider_api_key(provider_id)
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _model_api_url(provider_id: str) -> Optional[str]:
    configured_urls = parse_provider_base_urls(settings.opencode_provider_base_urls)
    base_url = configured_urls.get(provider_id, "")
    if not base_url and provider_id in {"openai", "token-pool"}:
        base_url = _setting_text(settings.openai_base_url)
    elif not base_url and provider_id == "tencent":
        base_url = _setting_text(settings.tencent_base_url)
    elif not base_url and provider_id == "ollama":
        ollama_host = _setting_text(settings.ollama_host)
        base_url = f"{ollama_host.rstrip('/')}/v1" if ollama_host else ""
    elif not base_url and provider_id not in {
        "cursor",
        "deepseek",
        "google",
        "openrouter",
    }:
        base_url = _setting_text(settings.openai_base_url)
    if not base_url:
        return None
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


def _provider_available_models(
    provider_id: str,
    *,
    http_get: Optional[Callable[..., Any]] = None,
) -> Optional[Set[str]]:
    http_get = http_get or httpx.get
    if float(settings.opencode_model_api_timeout_seconds or 0) <= 0:
        return None
    url = _model_api_url(provider_id)
    if not url:
        return None

    ttl = max(0, int(settings.opencode_model_api_cache_seconds or 0))
    cached = _model_api_cache.get(url)
    now = time.time()
    if cached and ttl > 0 and now - cached[0] < ttl:
        return cached[1]

    try:
        headers = _model_api_headers(provider_id)
        kwargs: Dict[str, Any] = {"timeout": settings.opencode_model_api_timeout_seconds}
        if headers:
            kwargs["headers"] = headers
        response = http_get(url, **kwargs)
        status_code = getattr(response, "status_code", 200)
        if int(status_code) in {401, 403}:
            for candidate in provider_candidates(fallback_enabled=True):
                if candidate.provider == provider_id:
                    _tracker.mark_unhealthy(
                        opencode_model_id(candidate),
                        reason="provider_auth_failed",
                    )
            available: Set[str] = set()
            _model_api_cache[url] = (now, available)
            return available
        if int(status_code) >= 400:
            raise RuntimeError(f"model API returned HTTP {status_code}")
        available = parse_model_list_response(response.json())
    except Exception:
        _model_api_cache[url] = (now, None)
        return None
    _model_api_cache[url] = (now, available)
    return available


def _candidate_model_ids(candidate: ProviderCandidate) -> Set[str]:
    ids = {candidate.model, opencode_model_id(candidate)}
    local_id = provider_local_model_id(candidate.provider, candidate.model)
    if local_id:
        ids.add(local_id)
    return ids


def available_provider_candidates(
    *,
    fallback_enabled: Optional[bool] = None,
    http_get: Optional[Callable[..., Any]] = None,
) -> List[ProviderCandidate]:
    """Return candidates after non-fatal model API availability filtering."""
    candidates = provider_candidates(fallback_enabled=fallback_enabled)
    filtered: List[ProviderCandidate] = []
    by_provider: Dict[str, Optional[Set[str]]] = {}
    for candidate in candidates:
        if candidate.provider not in by_provider:
            by_provider[candidate.provider] = _provider_available_models(
                candidate.provider,
                http_get=http_get,
            )
        available = by_provider[candidate.provider]
        if not _tracker.is_healthy(opencode_model_id(candidate)):
            continue
        if available is None or _candidate_model_ids(candidate) & available:
            filtered.append(candidate)
    return filtered


class ModelHealthTracker:
    """Tracks per-model rate-limit cooldowns.

    Thread-safety: reads/writes to a dict are GIL-protected in CPython;
    sufficient for our use case (one writer per rate-limit event, many readers).
    """

    def __init__(self) -> None:
        self._unhealthy_until: Dict[str, float] = {}
        self._reasons: Dict[str, str] = {}
        self._permanently_unhealthy: Set[str] = set()
        self._last_success: Optional[str] = None

    def mark_rate_limited(
        self,
        model_id: str,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        self.mark_unhealthy(model_id, reason="rate_limit", retry_after_seconds=retry_after_seconds)

    def mark_unhealthy(
        self,
        model_id: str,
        *,
        reason: str = "model_unavailable",
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        if reason == "provider_auth_failed":
            self._permanently_unhealthy.add(model_id)
            self._unhealthy_until.pop(model_id, None)
            self._reasons[model_id] = reason
            return
        cooldown = self._cooldown_seconds(reason, retry_after_seconds)
        self._unhealthy_until[model_id] = time.time() + cooldown
        self._reasons[model_id] = reason or "model_unavailable"

    def is_healthy(self, model_id: str) -> bool:
        if model_id in self._permanently_unhealthy:
            return False
        until = self._unhealthy_until.get(model_id)
        if until is None:
            return True
        if time.time() >= until:
            del self._unhealthy_until[model_id]
            self._reasons.pop(model_id, None)
            return True
        return False

    def status(self, model_id: str) -> Optional[Dict[str, Any]]:
        if self.is_healthy(model_id):
            return None
        return {
            "model": model_id,
            "reason": self._reasons.get(model_id) or "model_unavailable",
            "unhealthy_until": self._unhealthy_until.get(model_id),
        }

    def is_permanently_unhealthy(self, model_id: str) -> bool:
        return model_id in self._permanently_unhealthy

    def reset(self) -> None:
        self._unhealthy_until.clear()
        self._reasons.clear()
        self._permanently_unhealthy.clear()
        self._last_success = None

    def mark_success(self, model_id: str) -> None:
        self._last_success = model_id
        if model_id in self._permanently_unhealthy:
            return
        self._unhealthy_until.pop(model_id, None)
        self._reasons.pop(model_id, None)

    def last_success(self) -> Optional[str]:
        return self._last_success

    @staticmethod
    def _cooldown_seconds(reason: str, retry_after_seconds: Optional[int]) -> int:
        if retry_after_seconds and retry_after_seconds > 0:
            return int(retry_after_seconds)
        if reason == "no_output":
            return _NO_OUTPUT_COOLDOWN_SECONDS
        if reason in {"timeout", "Timeout"}:
            return _TIMEOUT_COOLDOWN_SECONDS
        if reason and reason != "rate_limit":
            return _MODEL_UNAVAILABLE_COOLDOWN_SECONDS
        return _DEFAULT_COOLDOWN_SECONDS


_tracker = ModelHealthTracker()


def is_model_healthy(model_id: str) -> bool:
    """Whether a model is outside its rate-limit cooldown."""
    return _tracker.is_healthy(model_id)


def is_model_permanently_unhealthy(model_id: str) -> bool:
    """Whether a model is quarantined until provider configuration reloads."""
    return _tracker.is_permanently_unhealthy(model_id)


def reset_model_health() -> None:
    """Clear all cooldowns. Primarily for tests and operator recovery."""
    _tracker.reset()


def mark_model_unhealthy(
    model_id: str,
    *,
    reason: str,
    retry_after_seconds: Optional[int] = None,
) -> None:
    _tracker.mark_unhealthy(model_id, reason=reason, retry_after_seconds=retry_after_seconds)


def mark_model_success(model_id: str) -> None:
    _tracker.mark_success(model_id)


def last_successful_model() -> Optional[str]:
    return _tracker.last_success()


def model_health_for_candidates(candidates: Iterable[ProviderCandidate]) -> Dict[str, Any]:
    skipped: List[Dict[str, Any]] = []
    for candidate in candidates:
        model_id = opencode_model_id(candidate)
        status = _tracker.status(model_id)
        if status:
            skipped.append(
                {
                    "provider": candidate.provider,
                    "model": model_id,
                    "candidate_index": candidate.index,
                    "reason": status["reason"],
                    "unhealthy_until": status["unhealthy_until"],
                }
            )
    return {"skipped": skipped}


def cheap_model_for_phase(phase: str) -> Optional[str]:
    """Return None because cheap-tier overrides are no longer model inputs."""
    return None


def effective_model(phase: str, *, fallback: Optional[str] = None) -> str:
    """Return the model ID to use for ``phase``.

    The fallback argument is retained for caller compatibility. Provider
    fallback is handled by task stop/resume, not by switching models inside the
    same running turn.
    """
    configured_model = settings.opencode_model
    all_candidates = provider_candidates(fallback_enabled=True)
    for candidate in all_candidates:
        executable_model = opencode_model_id(candidate)
        if configured_model in {candidate.model, executable_model}:
            return executable_model
    candidates = available_provider_candidates(fallback_enabled=False)
    if not candidates:
        candidates = provider_candidates(fallback_enabled=False)
    if candidates:
        return opencode_model_id(candidates[0])
    return configured_model
