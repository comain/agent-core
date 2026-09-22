"""Bounded source ingestion and exact, fail-closed benchmark bindings.

AA schema: https://artificialanalysis.ai/api-reference . Only the documented
Coding Index is evidence; inventory presence never proves tool capability.
No credential or arbitrary upstream metadata is retained in normalized records.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import zlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .policy import Candidate

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10_000
TIMEOUT = 10.0
_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_SUFFIX = re.compile(r"^(.+)-(" + "|".join(sorted(_EFFORTS)) + r")$")
_NAME_SUFFIX = re.compile(r"^([^()]+) \(([^()]+)\)$")


class SourceError(ValueError):
    """Safe diagnostic: fixed code and optional status, never response/request data."""

    def __init__(self, code: str, *, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(code if status_code is None else f"{code} (HTTP {status_code})")


def _text(value: object, *, empty: bool = False, identifier: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > 512
            or (not value and not empty) or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or (identifier and any(char.isspace() for char in value))):
        raise ValueError("invalid text")
    return value


def _display_name(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("invalid display name")
    # AA names can have surrounding spaces; retain them as source evidence.
    _text(value.strip(" "))
    return value


def _effort(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("invalid effort")
    value = value.strip().lower()
    if value and value not in _EFFORTS:
        raise ValueError("invalid effort")
    return value


class BenchmarkRecord(BaseModel):
    """Frozen whitelist with stable AA identity and raw name/slug as evidence.

    ``effort=None`` means an unknown/conflicting suffix; ``""`` means no suffix.
    Uniqueness of benchmark_id is enforced across the complete source, not slug.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    benchmark_id: str
    creator_id: str | None = None
    name: str
    slug: str
    score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    effort: str | None = ""
    source: Literal["https://artificialanalysis.ai/"] = "https://artificialanalysis.ai/"
    metric: Literal["artificial_analysis_coding_index"] = "artificial_analysis_coding_index"

    @field_validator("benchmark_id", "slug", "creator_id")
    @classmethod
    def validate_id(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, identifier=True)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _display_name(value)

    @field_validator("effort")
    @classmethod
    def validate_effort(cls, value: str | None) -> str | None:
        return None if value is None else _effort(value)


class PriceRecord(BaseModel):
    """One published OpenRouter listing: uncached input and output token prices.

    Both are USD per 1M tokens; ``None`` means the listing published no usable
    price (variable/negative), which is unknown, not free. Cache, image, audio,
    web-search, reasoning and long-context override rates are deliberately not
    retained: they depend on traffic shape this catalog cannot observe. The
    ranking blend and provider discounts are applied at selection time and are
    never baked into a cached record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    pricing_id: str
    slug: str
    prompt: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    completion: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    source: Literal["https://openrouter.ai/"] = "https://openrouter.ai/"
    metric: Literal["usd_per_million_tokens"] = "usd_per_million_tokens"

    @field_validator("pricing_id", "slug")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _text(value, identifier=True)


class PriceWeights(BaseModel):
    """How much each token kind counts when one price must rank two models.

    Agent traffic is input-dominated, so uncached input is weighted above
    output by default. Weights are an application's own traffic shape, not
    source evidence: they are applied when ranking, so changing them needs no
    catalog refresh. They scale every model alike, so only their ratio matters.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    prompt: float = Field(default=10.0, ge=0, le=1000, allow_inf_nan=False)
    completion: float = Field(default=1.0, ge=0, le=1000, allow_inf_nan=False)

    @model_validator(mode="after")
    def nonzero(self):
        if self.prompt + self.completion <= 0:
            raise ValueError("price weights must not be all zero")
        return self

    def blend(self, record: "PriceRecord | None") -> float | None:
        """One comparable price, or unknown when either published rate is."""
        if record is None or record.prompt is None or record.completion is None:
            return None
        return record.prompt * self.prompt + record.completion * self.completion


class ModelBinding(BaseModel):
    """Trusted metadata at an exact runtime identity, not an alias pattern.

    Omit benchmark_id for unique exact slug/effort discovery. A nonempty effort
    requires an explicit adapter variant. capability_approved attests that the
    operator tested this runtime configuration; neither source can grant it.
    Variant translation/support must also be validated by the invoking adapter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    benchmark_id: str | None = None
    pricing_id: str | None = None
    effort: str = ""
    variant: str = ""
    capability_approved: bool = False

    @field_validator("benchmark_id", "pricing_id")
    @classmethod
    def validate_id(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, identifier=True)

    @field_validator("effort")
    @classmethod
    def validate_effort(cls, value: str) -> str:
        return _effort(value)

    @field_validator("variant")
    @classmethod
    def validate_variant(cls, value: str) -> str:
        return _text(value, empty=True, identifier=True)


def _rows(payload: object) -> list:
    if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
            or ("status" in payload and payload["status"] != 200)):
        raise SourceError("invalid_source")
    rows = payload["data"]
    if len(rows) > MAX_ROWS:
        raise SourceError("too_many_rows")
    return rows


def _slug_effort(slug: str) -> tuple[str, str]:
    # Case only: preserve dots, version separators, namespaces, dates and aliases.
    normalized = slug.lower()
    match = _SUFFIX.fullmatch(normalized)
    return (match[1], match[2]) if match else (normalized, "")


def _record_effort(name: str, slug: str) -> str | None:
    _, slug_effort = _slug_effort(slug)
    match = _NAME_SUFFIX.fullmatch(name.strip(" "))
    if match:
        name_effort = match[2].lower()
        if name_effort not in _EFFORTS or (slug_effort and slug_effort != name_effort):
            return None
        return name_effort
    if "(" in name or ")" in name:
        return None
    return slug_effort


def parse_benchmarks(payload: object) -> tuple[BenchmarkRecord, ...]:
    """Parse AA's ``{data: [...]}`` envelope; reject any invalid/duplicate row.

    Missing/null coding scores remain unknown; no alternate evaluation is used.
    Stable IDs are opaque and case-sensitive. Returned order is stable-ID order.
    """
    result: dict[str, BenchmarkRecord] = {}
    for row in _rows(payload):
        try:
            if not isinstance(row, dict):
                raise TypeError
            evaluations = row.get("evaluations", {})
            creator = row.get("model_creator", {})
            if not isinstance(evaluations, dict) or not isinstance(creator, dict):
                raise TypeError
            name, slug = _display_name(row["name"]), _text(row["slug"], identifier=True)
            record = BenchmarkRecord(
                benchmark_id=row["id"], creator_id=creator.get("id"), name=name, slug=slug,
                score=evaluations.get("artificial_analysis_coding_index"),
                effort=_record_effort(name, slug),
            )
        except (KeyError, ValueError, TypeError):
            raise SourceError("invalid_benchmark") from None
        if record.benchmark_id in result:
            raise SourceError("duplicate_benchmark")
        result[record.benchmark_id] = record
    return tuple(result[key] for key in sorted(result))


def _token_price(value: object) -> float | None:
    """One OpenRouter per-token price string; unusable listings stay unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("invalid price")
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid price") from None
    if not math.isfinite(price):
        raise ValueError("invalid price")
    # OpenRouter publishes -1 for prices it cannot state; that is not free.
    return None if price < 0 else price


def parse_pricing(payload: object) -> tuple[PriceRecord, ...]:
    """Parse OpenRouter's ``{data: [{id, pricing: {...}}]}`` public catalog.

    Only the uncached ``prompt`` and ``completion`` rates are retained, scaled
    to USD per 1M tokens and left unblended. Variant listings
    (``id`` with a ``:`` suffix, such as ``:free``) are skipped rather than
    collapsed onto the base model. Returned order is pricing-ID order.
    """
    result: dict[str, PriceRecord] = {}
    for row in _rows(payload):
        try:
            if not isinstance(row, dict):
                raise TypeError
            pricing_id = _text(row["id"], identifier=True).lower()
            pricing = row.get("pricing", {})
            if not isinstance(pricing, dict):
                raise TypeError
            local = pricing_id.rpartition("/")[2]
            if not local or ":" in pricing_id:
                continue
            prompt = _token_price(pricing.get("prompt"))
            completion = _token_price(pricing.get("completion"))
            record = PriceRecord(
                pricing_id=pricing_id, slug=local,
                prompt=None if prompt is None else prompt * 1_000_000,
                completion=None if completion is None else completion * 1_000_000)
        except (KeyError, ValueError, TypeError):
            raise SourceError("invalid_pricing") from None
        if record.pricing_id in result:
            raise SourceError("duplicate_pricing")
        result[record.pricing_id] = record
    return tuple(result[key] for key in sorted(result))


def price_candidates(
    candidates: Sequence[Candidate],
    records: Sequence[PriceRecord],
    bindings: Mapping[str, object],
    discounts: Mapping[str, float],
    weights: PriceWeights | None = None,
) -> tuple[Candidate, ...]:
    """Attach list and discounted prices; ambiguous or absent evidence stays unknown.

    A binding's exact ``pricing_id`` wins and never falls back to slug matching.
    Otherwise the runtime model ID, minus any effort suffix, must match exactly
    one published slug price. Input and output rates are blended by ``weights``.
    A provider discount of 0 makes its models free regardless of the listing, so
    ranking falls back to efficiency alone.
    """
    weights = weights or PriceWeights()
    if len(records) > MAX_ROWS:
        raise SourceError("too_many_rows")
    by_id: dict[str, PriceRecord] = {}
    by_slug: dict[str, list[PriceRecord]] = defaultdict(list)
    for record in records:
        if not isinstance(record, PriceRecord):
            raise SourceError("invalid_pricing")
        if record.pricing_id in by_id:
            raise SourceError("duplicate_pricing")
        by_id[record.pricing_id] = record
        by_slug[record.slug].append(record)
    pricing_ids = {}
    for identity, binding in bindings.items():
        options = binding if isinstance(binding, list) else [binding]
        stated = {o.get("pricing_id") for o in options
                  if isinstance(o, Mapping) and o.get("pricing_id")}
        if len(stated) > 1:
            raise SourceError("invalid_binding")
        if stated:
            pricing_ids[identity] = _text(stated.pop(), identifier=True).lower()
    result = []
    for candidate in candidates:
        provider, _, model_id = candidate.identity.partition("/")
        discount = discounts.get(provider, 1.0)
        if pricing_ids.get(candidate.identity):
            record = by_id.get(pricing_ids[candidate.identity])
        else:
            # Several listings of one slug are usable only if they agree.
            matches = by_slug.get(_slug_effort(model_id)[0], [])
            rates = {(m.prompt, m.completion) for m in matches}
            record = matches[0] if len(rates) == 1 else None
        list_price = weights.blend(record)
        if discount == 0:
            # A free provider is free even where no listing was matched.
            price = 0.0
        else:
            price = None if list_price is None else list_price * discount
        result.append(candidate.model_copy(update={"list_price": list_price, "price": price}))
    return tuple(result)


def parse_inventory(payload: object) -> list[str]:
    """Parse OpenAI-compatible ``{data: [{id: ...}]}`` into sorted unique IDs.

    The caller supplies the provider name; never infer it from owned_by/creator.
    """
    result: set[str] = set()
    for row in _rows(payload):
        try:
            if not isinstance(row, dict):
                raise TypeError
            result.add(_text(row["id"], identifier=True))
        except (KeyError, ValueError, TypeError):
            raise SourceError("invalid_inventory") from None
    return sorted(result)


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError


async def _read_body(response: httpx.Response, max_bytes: int) -> bytearray:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise SourceError("invalid_encoding")
    if response.is_stream_consumed:
        # Preloaded responses occur with injected transports, not streamed HTTP.
        if len(response.content) > max_bytes:
            raise SourceError("response_too_large")
        return bytearray(response.content)
    decoder = None
    if encoding != "identity":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    body = bytearray()
    wire_bytes = 0
    try:
        async for raw in response.aiter_raw():
            wire_bytes += len(raw)
            if wire_bytes > MAX_BYTES:
                raise SourceError("response_too_large")
            # Bound decompression itself, not just accumulation of decoded output.
            chunk = decoder.decompress(raw, max_bytes - len(body) + 1) if decoder else raw
            if len(body) + len(chunk) > max_bytes:
                raise SourceError("response_too_large")
            body.extend(chunk)
            if decoder and decoder.unused_data:
                raise SourceError("invalid_encoding")
        if decoder and not decoder.eof:
            raise SourceError("invalid_encoding")
    except zlib.error:
        raise SourceError("invalid_encoding") from None
    return body


async def fetch_json(
    url: str,
    headers: Mapping[str, str] | None = None,
    *,
    timeout: float = TIMEOUT,
    max_bytes: int = MAX_BYTES,
    max_rows: int = MAX_ROWS,
    transport: httpx.AsyncBaseTransport | None = None,
    allow_http: bool = False,
) -> object:
    """GET authenticated JSON without redirects/retries; credentials stay in headers.

    Async total deadline plus HTTPX per-operation timeouts. Limits may only be
    lowered. Byte budget counts decoded bytes (wire also capped at 10 MiB).
    Identity, gzip and zlib-wrapped deflate are supported; other encodings fail.
    Row budget checks a top-level
    array or data array. Parsers subsequently enforce the source schema.
    ``transport`` is an injectable HTTPX async transport for deterministic tests.
    HTTPS by default; allow_http=True opts into a trusted configured HTTP provider.
    AA callers must leave this disabled. No URL credentials/query/fragment;
    errors contain fixed codes. Redirects remain disabled for either scheme.
    """
    if type(allow_http) is not bool:
        raise SourceError("invalid_request")
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= TIMEOUT
            or type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES
            or type(max_rows) is not int or not 0 < max_rows <= MAX_ROWS):
        raise SourceError("invalid_limits")
    try:
        target = httpx.URL(url)
        allowed_schemes = {"https", "http"} if allow_http else {"https"}
        if (target.scheme not in allowed_schemes or not target.host or target.userinfo
                or target.query or target.fragment):
            raise ValueError
    except (ValueError, TypeError, httpx.InvalidURL):
        raise SourceError("invalid_url") from None
    try:
        request_headers = httpx.Headers(headers)
        request_headers["Accept-Encoding"] = "identity"
        deadline = asyncio.get_running_loop().time() + timeout
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(transport=transport, timeout=timeout,
                                        follow_redirects=False, trust_env=False) as client:
                async with client.stream("GET", target, headers=request_headers) as response:
                    if response.status_code != 200:
                        code = "auth_error" if response.status_code in (401, 403) else "http_error"
                        raise SourceError(code, status_code=response.status_code)
                    body = await _read_body(response, max_bytes)
                    try:
                        payload = json.loads(body, object_pairs_hook=_json_object,
                                             parse_constant=_invalid_constant)
                    except (ValueError, UnicodeError, RecursionError):
                        raise SourceError("invalid_json") from None
                    rows = payload.get("data") if isinstance(payload, dict) else payload
                    if isinstance(rows, list) and len(rows) > max_rows:
                        raise SourceError("too_many_rows")
                    if asyncio.get_running_loop().time() >= deadline:
                        raise SourceError("timeout")
                    return payload
    except (TimeoutError, httpx.TimeoutException):
        raise SourceError("timeout") from None
    except httpx.HTTPError:
        raise SourceError("transport_error") from None
    except (ValueError, TypeError) as error:
        if isinstance(error, SourceError):
            raise
        raise SourceError("invalid_request") from None


def bind_candidates(
    inventory: dict[str, list[str]],
    records: Sequence[BenchmarkRecord],
    bindings: Mapping[str, ModelBinding | Mapping[str, object] | list[Mapping[str, object]]],
    *,
    default_effort: str = "",
) -> tuple[Candidate, ...]:
    """Expand approved alternatives without borrowing scores across effort levels.

    The single-binding form remains supported. Lists expose exact alternatives
    to policy selection; provider inventory remains authoritative for every one.
    """
    primary = {}
    alternatives = []
    for identity, binding in bindings.items():
        if isinstance(binding, list):
            if not binding or len(binding) > len(_EFFORTS) + 1:
                raise SourceError("invalid_binding")
            primary[identity] = binding[0]
            alternatives.extend((identity, option) for option in binding[1:])
        else:
            primary[identity] = binding
    result = list(_bind_single_candidates(inventory, records, primary, default_effort=default_effort))
    for identity, option in alternatives:
        provider, _, local = identity.partition("/")
        # Validate even non-inventoried bindings, as the single-binding path does.
        subset = {provider: [local]} if local in inventory.get(provider, []) else {}
        result.extend(_bind_single_candidates(subset, records, {identity: option}, default_effort=default_effort))
    # Multiple unmapped alternatives can yield the same pending placeholder.
    bound = {c.identity for c in result if c != Candidate(identity=c.identity)}
    pending = set()
    unique = []
    for candidate in result:
        if candidate == Candidate(identity=candidate.identity):
            if candidate.identity in bound or candidate.identity in pending:
                continue
            pending.add(candidate.identity)
        unique.append(candidate)
    return tuple(unique)


def _bind_single_candidates(
    inventory: dict[str, list[str]],
    records: Sequence[BenchmarkRecord],
    bindings: Mapping[str, ModelBinding | Mapping[str, object]],
    *,
    default_effort: str = "",
) -> tuple[Candidate, ...]:
    """One identity-sorted Candidate per inventoried runtime ID, including pending.

    Explicit stable-ID bindings take precedence and never fall back on failure.
    Otherwise match only a unique lowercased AA slug + exact canonical effort.
    No fuzzy matching, cross-version/namespace stripping or maximum-score choice.
    Ambiguous evidence or missing effort variant yields an unapproved, unscored
    Candidate. Truly unmatched models retain explicit capability approval but
    still require a separate policy-level unscored approval for admission.
    Successful auto matches still need capability approval.
    Desired effort is an explicitly supplied binding effort, otherwise the
    configured default_effort. Suffixes never choose the desired effort.
    An explicit empty effort overrides the default and matches unsuffixed rows.
    """
    try:
        default_effort = _effort(default_effort)
        metadata = {}
        for identity, value in bindings.items():
            _text(identity, identifier=True)
            Candidate(identity=identity)
            metadata[identity] = ModelBinding.model_validate(value)
    except (ValueError, TypeError, AttributeError):
        raise SourceError("invalid_binding") from None
    by_id: dict[str, BenchmarkRecord] = {}
    by_slug: dict[tuple[str, str], list[BenchmarkRecord]] = defaultdict(list)
    if len(records) > MAX_ROWS:
        raise SourceError("too_many_rows")
    for record in records:
        if not isinstance(record, BenchmarkRecord):
            raise SourceError("invalid_benchmark")
        if record.benchmark_id in by_id:
            raise SourceError("duplicate_benchmark")
        by_id[record.benchmark_id] = record
        if record.effort is not None:
            by_slug[(_slug_effort(record.slug)[0], record.effort)].append(record)
    known_slugs = {slug for slug, _ in by_slug}
    candidates: dict[str, Candidate] = {}
    try:
        for provider, ids in inventory.items():
            _text(provider, identifier=True)
            if "/" in provider or not isinstance(ids, list):
                raise ValueError
            if len(ids) > MAX_ROWS:
                raise SourceError("too_many_rows")
            for model_id in ids:
                _text(model_id, identifier=True)
                identity = f"{provider}/{model_id}"
                pending = Candidate(identity=identity)
                candidates[identity] = pending
                binding = metadata.get(identity)
                slug, runtime_effort = _slug_effort(model_id)
                effort = (binding.effort if binding and "effort" in binding.model_fields_set
                          else default_effort)
                variant = binding.variant if binding else ""
                if runtime_effort and runtime_effort != effort:
                    continue
                if binding and binding.benchmark_id is not None:
                    record = by_id.get(binding.benchmark_id)
                else:
                    matches = by_slug.get((slug, effort), [])
                    record = matches[0] if len(matches) == 1 else None
                    if (binding and binding.capability_approved
                            and slug not in known_slugs
                            and bool(effort) == bool(variant)):
                        # Preserve only the independent capability approval.
                        # Policy still requires explicit unscored approval; a
                        # missing/ambiguous explicit benchmark never takes this path.
                        candidates[identity] = Candidate(
                            identity=identity, effort=effort, variant=variant,
                            capability_approved=True,
                        )
                if (record is None or record.effort is None or record.effort != effort
                        or (effort and not variant) or (variant and not effort)):
                    continue
                candidates[identity] = Candidate(
                    identity=identity, score=record.score, benchmark_id=record.benchmark_id,
                    effort=effort, variant=variant,
                    capability_approved=binding.capability_approved if binding else False,
                )
    except (ValueError, TypeError, AttributeError) as error:
        if isinstance(error, SourceError):
            raise
        raise SourceError("invalid_inventory") from None
    return tuple(candidates[key] for key in sorted(candidates))
