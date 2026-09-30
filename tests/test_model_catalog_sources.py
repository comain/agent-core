"""Sanitized source fixtures; no live credentials or network access."""

import asyncio
import gzip
import json
import traceback
import zlib

import httpx
import pytest

from agent_core.model_selection.policy import Candidate
from agent_core.model_selection.sources import (
    BenchmarkRecord, ModelBinding, PriceWeights, SourceError, bind_candidates,
    fetch_json, parse_benchmarks, parse_inventory, parse_pricing, price_candidates,
)


def aa(identifier="aa-1", slug="model-1", name="Model 1", score=70.001):
    return {
        "id": identifier, "slug": slug, "name": name,
        "model_creator": {"id": "creator-1", "name": "Creator"},
        "evaluations": {"artificial_analysis_coding_index": score,
                        "artificial_analysis_intelligence_index": 99,
                        "livecodebench": 100},
        "api_key": "synthetic-secret", "pricing": {"secret": "synthetic-secret"},
    }


def records(*rows):
    return parse_benchmarks({"status": 200, "data": list(rows)})


def test_missing_alternative_does_not_collide_with_valid_default_binding():
    from agent_core.model_selection.policy import ModelPolicy, resolve_selection

    bindings = {"pool/model-1": [
        {"benchmark_id": "missing", "effort": "low", "variant": "low", "capability_approved": True},
        {"benchmark_id": "aa-1", "capability_approved": True},
    ]}
    candidates = bind_candidates({"pool": ["model-1"]}, records(aa()), bindings)
    selected = resolve_selection(candidates, ModelPolicy(application_id="test"))
    assert len(candidates) == 1
    assert candidates[0].effort == ""
    assert candidates[0].score == 70.001
    assert selected.candidates == ()
    assert selected.decisions[0].reason == "prohibited_effort"


@pytest.mark.parametrize("alternatives", [[], [{}] * 9])
def test_variant_lists_are_nonempty_and_bounded(alternatives):
    with pytest.raises(SourceError, match="invalid_binding"):
        bind_candidates({"pool": ["model-1"]}, records(aa()), {"pool/model-1": alternatives})


def test_variant_lists_cannot_add_models_absent_from_inventory():
    assert bind_candidates({"pool": []}, records(aa()), {"pool/model-1": [
        {"benchmark_id": "aa-1", "capability_approved": True},
        {"benchmark_id": "aa-1", "capability_approved": True},
    ]}) == ()


def test_benchmark_stable_identity_coding_only_and_whitelisted_provenance():
    row = records(aa())[0]
    assert isinstance(row, BenchmarkRecord)
    assert (row.benchmark_id, row.creator_id, row.slug, row.name, row.score) == (
        "aa-1", "creator-1", "model-1", "Model 1", 70.001)
    assert row.source == "https://artificialanalysis.ai/"
    assert row.metric == "artificial_analysis_coding_index"
    assert "synthetic-secret" not in row.model_dump_json()
    with pytest.raises(ValueError):
        row.score = 80
    missing = aa()
    del missing["evaluations"]["artificial_analysis_coding_index"]
    assert records(missing)[0].score is None
    assert records(aa(score=None))[0].score is None


def test_documented_aa_shape_and_stable_ids_survive_slug_changes():
    row = aa("2dad8957-4c16-4e74-bf2d-8b21514e0ae9", "o3-mini", "o3-mini", 55.8)
    row["model_creator"] = {"id": "e67e56e3-15cd-43db-b679-da4660a69f41",
                            "name": "OpenAI", "slug": "openai"}
    payload = {"status": 200, "prompt_options": {"parallel_queries": 1}, "data": [row]}
    original = parse_benchmarks(payload)[0]
    row["slug"] = "renamed-slug"
    candidate, = bind_candidates({"pool": ["o3-mini"]}, parse_benchmarks(payload),
        {"pool/o3-mini": {"benchmark_id": original.benchmark_id, "capability_approved": True}})
    assert candidate.score == 55.8 and candidate.benchmark_id == original.benchmark_id


def test_live_padded_display_name_does_not_reject_whole_source():
    raw_name = "Qwen2.5 Coder Instruct 7B "
    result = records(aa(), aa(
        "e410e854-104d-4b35-a171-899ff9d974bb",
        "qwen2-5-coder-7b-instruct", raw_name, None,
    ))
    assert len(result) == 2
    record = next(row for row in result if row.slug == "qwen2-5-coder-7b-instruct")
    assert record.name == raw_name
    assert record.score is None and record.effort == ""
    assert BenchmarkRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize("name,effort", [
    (" Model 1 (high) ", "high"),
    ("Model 1 (unknown) ", None),
    ("  Model 1  ", ""),
])
def test_name_padding_preserves_raw_evidence_and_effort_parsing(name, effort):
    record, = records(aa(name=name))
    assert record.name == name and record.effort == effort


@pytest.mark.parametrize("name", [" ", "", "Model 1\n", "\tModel 1", " " * 512 + "x"])
def test_name_padding_does_not_allow_blank_controls_or_oversized_text(name):
    with pytest.raises(SourceError, match="invalid_benchmark"):
        records(aa(name=name))


@pytest.mark.parametrize("field", ["id", "slug", "creator_id"])
@pytest.mark.parametrize("padding", [" ", "\t"])
def test_display_name_fix_does_not_relax_identifier_whitespace(field, padding):
    row = aa()
    target = row["model_creator"] if field == "creator_id" else row
    key = "id" if field == "creator_id" else field
    target[key] = padding + target[key] + padding
    with pytest.raises(SourceError, match="invalid_benchmark"):
        records(row)


def test_dot_hyphen_aliases_remain_explicit_stable_id_bindings():
    rows = records(aa("sol-high", "gpt-5-6-sol-high", "GPT-5.6 Sol (high)", 77.2))
    inventory = {"pool": ["gpt5.6-sol"]}
    metadata = {"effort": "high", "variant": "high", "capability_approved": True}
    auto, = bind_candidates(inventory, rows, {"pool/gpt5.6-sol": metadata})
    assert auto.score is None and auto.capability_approved
    assert auto.benchmark_id is None
    explicit, = bind_candidates(inventory, rows,
        {"pool/gpt5.6-sol": {**metadata, "benchmark_id": "sol-high"}})
    assert explicit.score == 77.2 and explicit.capability_approved


@pytest.mark.parametrize("change", [{"id": None}, {"name": "x" * 513},
    {"model_creator": None}, {"evaluations": []}, {"slug": "bad slug"}])
def test_invalid_benchmark_metadata_is_sanitized(change):
    with pytest.raises(SourceError, match="invalid_benchmark"):
        records({**aa(), **change})


@pytest.mark.parametrize("score", [True, "70", -1, 101, float("nan"), float("inf")])
def test_invalid_coding_score_rejects_whole_source(score):
    with pytest.raises(SourceError, match="invalid_benchmark"):
        records(aa(score=score))


@pytest.mark.parametrize("payload", [None, [], {}, {"data": {}}, {"data": [None]},
                                      {"status": 401, "data": []}])
def test_malformed_sources(payload):
    for parser in (parse_benchmarks, parse_inventory):
        with pytest.raises(SourceError):
            parser(payload)


def test_duplicate_stable_ids_rejected_even_if_identical():
    with pytest.raises(SourceError, match="duplicate_benchmark"):
        records(aa(), aa())


def test_inventory_preserves_provider_local_ids_and_deduplicates():
    assert parse_inventory({"data": [{"id": "Z"}, {"id": "vendor/model"},
                                      {"id": "Z"}, {"id": "a"}]}) == ["Z", "a", "vendor/model"]


@pytest.mark.parametrize("identifier", ["", " padded ", "bad\nname", 12, "x" * 513])
def test_invalid_inventory_ids(identifier):
    with pytest.raises(SourceError):
        parse_inventory({"data": [{"id": identifier}]})


def test_row_budgets_apply_before_deduplication():
    for parser, row in ((parse_benchmarks, aa()), (parse_inventory, {"id": "x"})):
        with pytest.raises(SourceError, match="too_many_rows"):
            parser({"data": [row] * 10001})


@pytest.mark.parametrize("name,slug,effort", [
    ("Model 1", "model-1", ""),
    ("Model 1 (high)", "model-1", "high"),
    ("Model 1 (xhigh)", "model-1-xhigh", "xhigh"),
    ("Model 1 (unknown)", "model-1", None),
    ("Model 1 (high)", "model-1-low", None),
    ("Model 1 (high) preview", "model-1", None),
])
def test_conservative_effort_suffixes(name, slug, effort):
    assert records(aa(name=name, slug=slug))[0].effort == effort


def test_exact_auto_match_is_not_capability_approval_or_fuzzy_matching():
    result = bind_candidates({"pool": ["MODEL-1", "model1", "model-2"]}, records(aa()), {})
    assert [c.identity for c in result] == ["pool/MODEL-1", "pool/model-2", "pool/model1"]
    assert result[0].benchmark_id == "aa-1"
    assert result[0].score == 70.001
    assert not any(c.capability_approved for c in result)
    assert all(c.score is None for c in result[1:])


def test_binding_metadata_can_approve_auto_mapping_without_exhaustive_ids():
    candidate, = bind_candidates({"pool": ["model-1"]}, records(aa()), {
        "pool/model-1": {"capability_approved": True}})
    assert candidate.capability_approved and candidate.benchmark_id == "aa-1"


def test_unmatched_model_preserves_explicit_capability_for_unscored_policy():
    from agent_core.model_selection.policy import ModelPolicy, resolve_selection

    candidate, = bind_candidates({"pool": ["new-model"]}, records(aa()), {
        "pool/new-model": {"capability_approved": True}})
    assert candidate.score is None and candidate.capability_approved
    assert not resolve_selection([candidate], ModelPolicy(application_id="test")).model_ids
    policy = ModelPolicy(application_id="test", unscored_approvals=("pool/new-model",))
    assert resolve_selection([candidate], policy).model_ids == ("pool/new-model",)


def test_explicit_stable_id_effort_variant_binding_never_takes_max_score():
    rows = records(aa("low", "model-1-low", "Model 1 (low)", 69.999),
                   aa("high", "model-1-high", "Model 1 (high)", 99))
    candidate, = bind_candidates({"pool": ["runtime-alias"]}, rows, {
        "pool/runtime-alias": {"benchmark_id": "low", "effort": " LOW ",
                               "variant": "reasoning-low", "capability_approved": True}})
    assert (candidate.score, candidate.effort, candidate.variant) == (69.999, "low", "reasoning-low")
    assert candidate.capability_approved


@pytest.mark.parametrize("metadata", [
    {"benchmark_id": "missing"},
    {"benchmark_id": "high", "effort": "low", "variant": "low"},
    {"benchmark_id": "high", "effort": "high"},
])
def test_missing_binding_or_effort_variant_mismatch_stays_pending(metadata):
    candidate, = bind_candidates({"pool": ["model-1"]},
        records(aa("high", "model-1", "Model 1 (high)")),
        {"pool/model-1": {**metadata, "capability_approved": True}})
    assert candidate.score is None and not candidate.capability_approved


def test_ambiguous_normalized_aliases_do_not_select_highest_score():
    rows = records(aa("one"), aa("two", "MODEL-1", score=99))
    candidate, = bind_candidates({"pool": ["model-1"]}, rows,
                                {"pool/model-1": {"capability_approved": True}})
    assert candidate.benchmark_id is None and not candidate.capability_approved
    explicit, = bind_candidates({"pool": ["model-1"]}, rows,
        {"pool/model-1": {"benchmark_id": "one", "capability_approved": True}})
    assert explicit.score == 70.001


def test_bindings_are_strict_and_inventory_is_authoritative():
    binding = ModelBinding(benchmark_id="aa-1", capability_approved=True)
    assert bind_candidates({}, records(aa()), {"pool/model-1": binding}) == ()
    for value in ({"capability_approved": "true"}, {"api_key": "synthetic-secret"}):
        with pytest.raises(SourceError, match="invalid_binding"):
            bind_candidates({"pool": ["model-1"]}, records(aa()), {"pool/model-1": value})


def test_default_effort_and_explicit_override_are_distinct_from_omission():
    rows = records(aa("high", "model-1-high", "Model 1 (high)", 90),
                   aa("plain", "model-1", "Model 1", 70))
    default, = bind_candidates({"pool": ["model-1"]}, rows,
        {"pool/model-1": {"variant": "reasoning-high", "capability_approved": True}},
        default_effort=" HIGH ")
    assert (default.benchmark_id, default.effort, default.variant) == (
        "high", "high", "reasoning-high")
    override, = bind_candidates({"pool": ["model-1"]}, rows,
        {"pool/model-1": {"effort": "", "capability_approved": True}}, default_effort="high")
    assert override.benchmark_id == "plain"
    with pytest.raises(SourceError, match="invalid_binding"):
        bind_candidates({}, rows, {}, default_effort="unknown")


def test_unknown_effort_and_runtime_conflicts_cannot_be_approved():
    rows = records(aa("unknown", "model-1", "Model 1 (unknown)"),
                   aa("high", "model-1-high", "Model 1 (high)"))
    for identity, benchmark_id in (("pool/model-1", "unknown"), ("pool/model-1-low", "high")):
        candidate, = bind_candidates({"pool": [identity.split("/", 1)[1]]}, rows,
            {identity: {"benchmark_id": benchmark_id, "effort": "high",
                        "variant": "high", "capability_approved": True}})
        assert not candidate.capability_approved and candidate.benchmark_id is None


def test_suffix_does_not_choose_desired_effort():
    rows = records(aa("high", "model-1-high", "Model 1 (high)"))
    metadata = {"pool/model-1-high": {"variant": "high", "capability_approved": True}}
    unspecified, = bind_candidates({"pool": ["model-1-high"]}, rows, metadata)
    assert unspecified.score is None and not unspecified.capability_approved
    specified, = bind_candidates({"pool": ["model-1-high"]}, rows, metadata, default_effort="high")
    assert specified.score == 70.001 and specified.capability_approved


def test_explicit_id_is_case_sensitive_and_cannot_fall_back_to_slug():
    candidate, = bind_candidates({"pool": ["model-1"]}, records(aa("AA-ID")),
        {"pool/model-1": {"benchmark_id": "aa-id", "capability_approved": True}})
    assert candidate.benchmark_id is None and not candidate.capability_approved


def test_unscored_evidence_can_be_explicitly_capability_approved():
    candidate, = bind_candidates({"pool": ["model-1"]}, records(aa(score=None)),
                                {"pool/model-1": {"capability_approved": True}})
    assert candidate.score is None and candidate.capability_approved
    assert candidate.benchmark_id == "aa-1"


def test_binding_is_deterministic_and_does_not_mutate_inputs():
    inventory = {"z": ["model-1", "model-1"], "a": ["model-1"]}
    rows = records(aa())
    bindings = {"z/model-1": {"capability_approved": True}}
    original = json.dumps(inventory), json.dumps(bindings)
    first = bind_candidates(inventory, rows, bindings)
    assert first == bind_candidates(dict(reversed(list(inventory.items()))), rows, bindings)
    assert (json.dumps(inventory), json.dumps(bindings)) == original
    assert [c.capability_approved for c in first] == [False, True]
    with pytest.raises(SourceError, match="duplicate_benchmark"):
        bind_candidates(inventory, rows + rows, bindings)


def fetch(handler, **kwargs):
    return asyncio.run(fetch_json("https://fixture.test/models",
        {"x-api-key": "synthetic-secret"}, transport=httpx.MockTransport(handler), **kwargs))


def test_authenticated_fetch_and_default_timeout():
    def handler(request):
        assert request.headers["x-api-key"] == "synthetic-secret"
        assert set(request.extensions["timeout"].values()) == {10.0}
        return httpx.Response(200, json={"data": []})
    assert fetch(handler) == {"data": []}


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 429, 500])
def test_status_errors_are_sanitized_and_redirects_not_followed(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="synthetic-secret",
            headers={"Location": "https://other.test/?key=synthetic-secret"})
    with pytest.raises(SourceError) as error:
        fetch(handler)
    assert error.value.status_code == status
    assert error.value.code == ("auth_error" if status in (401, 403) else "http_error")
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"{synthetic-secret", b"{\"data\": NaN}",
                                 b'{"data": [], "data": []}', b"[" * 2000])
def test_malformed_json_is_sanitized(body):
    with pytest.raises(SourceError, match="invalid_json") as error:
        fetch(lambda _: httpx.Response(200, content=body))
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))


def test_decoded_byte_limit_including_compressed_responses():
    body = json.dumps({"data": "x" * 1000}).encode()
    for content, headers in ((body, {}), (gzip.compress(body), {"Content-Encoding": "gzip"})):
        with pytest.raises(SourceError, match="response_too_large"):
            fetch(lambda _: httpx.Response(200, stream=httpx.ByteStream(content), headers=headers),
                  max_bytes=100)
    assert fetch(lambda _: httpx.Response(200, content=b'{}'), max_bytes=2) == {}


def test_valid_compression_and_truncated_or_unsupported_encoding():
    body = gzip.compress(b'{"data": []}')
    assert fetch(lambda _: httpx.Response(200, stream=httpx.ByteStream(body),
        headers={"Content-Encoding": "gzip"})) == {"data": []}
    for content, encoding in ((body[:-4], "gzip"), (b"invalid", "gzip"), (b"x", "unknown")):
        with pytest.raises(SourceError, match="invalid_encoding"):
            fetch(lambda _: httpx.Response(200, stream=httpx.ByteStream(content),
                  headers={"Content-Encoding": encoding}))


def test_streamed_deflate_and_concatenated_gzip_rejection():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            content = zlib.compress(b'{"data": []}')
            for index in range(0, len(content), 2):
                yield content[index:index + 2]
    assert fetch(lambda _: httpx.Response(200, stream=Stream(),
        headers={"Content-Encoding": "deflate"})) == {"data": []}
    with pytest.raises(SourceError, match="invalid_encoding"):
        fetch(lambda _: httpx.Response(200, stream=httpx.ByteStream(gzip.compress(b'{}') * 2),
              headers={"Content-Encoding": "gzip"}))


def test_oversized_stream_stops_consuming_and_closes():
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b"x" * 101
            pytest.fail("must stop consuming after budget exceeded")
        async def aclose(self):
            self.closed = True
    stream = Stream()
    with pytest.raises(SourceError, match="response_too_large"):
        fetch(lambda _: httpx.Response(200, stream=stream), max_bytes=100)
    assert stream.closed


@pytest.mark.parametrize("url", ["http://fixture.test", "https://user:synthetic-secret@fixture.test",
    "https://fixture.test?key=synthetic-secret", "https://fixture.test/#synthetic-secret"])
def test_credentials_must_not_be_in_urls(url):
    with pytest.raises(SourceError, match="invalid_url") as error:
        asyncio.run(fetch_json(url))
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))


def test_http_requires_explicit_per_call_opt_in():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer synthetic-secret"
        return httpx.Response(200, json={"data": [{"id": "model-1"}]})
    transport = httpx.MockTransport(handler)
    url = "http://fixture.test/v1/models"
    payload = asyncio.run(fetch_json(url, {"Authorization": "Bearer synthetic-secret"},
                                    allow_http=True, transport=transport))
    assert parse_inventory(payload) == ["model-1"]
    for options in ({}, {"allow_http": False}):
        with pytest.raises(SourceError, match="invalid_url"):
            asyncio.run(fetch_json(url, transport=transport, **options))
    assert len(calls) == 1


@pytest.mark.parametrize("opt_in", ["true", "false", 1, None])
def test_http_opt_in_requires_boolean(opt_in):
    with pytest.raises(SourceError, match="invalid_request"):
        asyncio.run(fetch_json("http://fixture.test", allow_http=opt_in,
            transport=httpx.MockTransport(lambda _: pytest.fail("must not send request"))))


@pytest.mark.parametrize("url", ["ftp://fixture.test/models",
    "http://user:synthetic-secret@fixture.test/models",
    "http://fixture.test/models?key=synthetic-secret",
    "http://fixture.test/models#synthetic-secret"])
def test_http_opt_in_preserves_url_restrictions(url):
    with pytest.raises(SourceError, match="invalid_url") as error:
        asyncio.run(fetch_json(url, allow_http=True,
            transport=httpx.MockTransport(lambda _: pytest.fail("must not send request"))))
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("scheme,allow_http", [("http", True), ("https", False)])
def test_http_opt_in_does_not_allow_redirects(scheme, allow_http):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "http://other.test/models"})
    with pytest.raises(SourceError, match="http_error") as error:
        asyncio.run(fetch_json(f"{scheme}://fixture.test/models", allow_http=allow_http,
                               transport=httpx.MockTransport(handler)))
    assert error.value.status_code == 307 and len(calls) == 1


def test_http_opt_in_keeps_decoded_byte_budget():
    with pytest.raises(SourceError, match="response_too_large"):
        asyncio.run(fetch_json("http://fixture.test/models", allow_http=True, max_bytes=2,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"{} "))))


def test_fetch_row_limit():
    with pytest.raises(SourceError, match="too_many_rows"):
        fetch(lambda _: httpx.Response(200, json={"data": [0] * 10001}))


def test_transport_timeout_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout("synthetic-secret", request=request)
    with pytest.raises(SourceError, match="timeout") as error:
        fetch(handler)
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))


def test_transport_error_is_sanitized():
    def handler(request):
        raise httpx.ConnectError("synthetic-secret", request=request)
    with pytest.raises(SourceError, match="transport_error") as error:
        fetch(handler)
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))


def test_total_deadline_not_only_socket_timeout():
    async def handler(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})
    with pytest.raises(SourceError, match="timeout"):
        fetch(handler, timeout=0.001)


@pytest.mark.parametrize("kwargs", [{"timeout": 11}, {"timeout": 0},
    {"max_bytes": 10485761}, {"max_rows": 10001}, {"max_rows": 0}])
def test_callers_cannot_expand_hard_limits(kwargs):
    with pytest.raises(SourceError, match="invalid_limits"):
        fetch(lambda _: pytest.fail("request should not run"), **kwargs)


def test_exact_row_and_identifier_limits_are_inclusive():
    row = aa(name="n" * 512, slug="s" * 512)
    parsed, = records(row)
    assert parsed.name == row["name"] and parsed.slug == row["slug"]
    assert parse_inventory({"data": [{"id": "x"}] * 10000}) == ["x"]
    rows = records(*(aa(str(i)) for i in range(10000)))
    assert len(rows) == 10000
    assert fetch(lambda _: httpx.Response(200, json={"data": [1, 2]}), max_rows=2) == {"data": [1, 2]}


@pytest.mark.parametrize("name", ["Model (high", "Model high)"])
def test_partial_effort_suffix_is_unknown(name):
    assert records(aa(name=name))[0].effort is None


@pytest.mark.parametrize("inventory", [{"pool": ("model-1",)}, {"pool/nested": ["model-1"]}])
def test_binding_inventory_rejects_invalid_provider_or_collection(inventory):
    with pytest.raises(SourceError, match="invalid_inventory"):
        bind_candidates(inventory, records(aa()), {})


def test_unmatched_or_conflicting_model_does_not_stop_later_admission():
    result = bind_candidates({"pool": ["missing", "model-1-high", "model-1"]}, records(aa()),
                             {"pool/model-1": {"capability_approved": True}})
    by_id = {c.identity: c for c in result}
    assert set(by_id) == {"pool/missing", "pool/model-1-high", "pool/model-1"}
    assert by_id["pool/model-1"].score == 70.001
    assert by_id["pool/model-1"].capability_approved
    assert by_id["pool/missing"].score is None
    assert by_id["pool/model-1-high"].score is None


def test_cumulative_wire_budget_is_distinct_from_decoded_budget(monkeypatch):
    from agent_core.model_selection import sources

    monkeypatch.setattr(sources, "MAX_BYTES", 8)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"12345"
            yield b"6789"
    response = httpx.Response(200, stream=Stream())
    with pytest.raises(SourceError, match="response_too_large"):
        asyncio.run(sources._read_body(response, max_bytes=100))


def test_exact_streamed_byte_budget_is_accepted():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            yield b"}"
    assert fetch(lambda _: httpx.Response(200, stream=Stream()), max_bytes=2) == {}


def test_completed_response_at_total_deadline_is_rejected(monkeypatch):
    from agent_core.model_selection import sources

    class Clock:
        times = iter((10.0, 11.0))
        def time(self):
            return next(self.times)
    monkeypatch.setattr(sources.asyncio, "get_running_loop", lambda: Clock())
    with pytest.raises(SourceError, match="timeout"):
        fetch(lambda _: httpx.Response(200, json={}), timeout=1)


def prices(*rows):
    return parse_pricing({"status": 200, "data": list(rows)})


def listing(identifier="vendor/model-1", prompt="0.000001", completion="0.000003", **extra):
    return {"id": identifier, "pricing": {"prompt": prompt, "completion": completion, **extra},
            "api_key": "synthetic-secret"}


def test_pricing_keeps_input_and_output_rates_per_million_tokens():
    record = prices(listing())[0]
    assert (record.pricing_id, record.slug) == ("vendor/model-1", "model-1")
    assert (record.prompt, record.completion) == (1.0, 3.0)
    assert record.source == "https://openrouter.ai/"
    assert "secret" not in json.dumps(record.model_dump(mode="json"))


def test_weights_blend_input_heavy_by_default_and_only_the_ratio_matters():
    record = prices(listing())[0]
    assert PriceWeights().blend(record) == 13.0
    assert PriceWeights(prompt=1, completion=1).blend(record) == 4.0
    assert PriceWeights(prompt=20, completion=2).blend(record) == 2 * PriceWeights().blend(record)
    assert PriceWeights().blend(None) is None


def test_a_missing_rate_leaves_the_blend_unknown():
    assert PriceWeights().blend(prices(listing(prompt="-1"))[0]) is None
    assert PriceWeights().blend(prices(listing(completion=None))[0]) is None


@pytest.mark.parametrize("weights", [{"prompt": 0, "completion": 0}, {"prompt": -1},
                                     {"prompt": float("inf")}, {"completion": 1001}])
def test_unusable_weights_are_rejected(weights):
    with pytest.raises(ValueError):
        PriceWeights(**weights)


@pytest.mark.parametrize("row,field", [
    (listing(prompt="-1"), "prompt"), (listing(completion=None), "completion"),
    (listing(completion=""), "completion"),
])
def test_unstated_rate_is_unknown_not_free(row, field):
    assert getattr(prices(row)[0], field) is None


@pytest.mark.parametrize("row", [
    listing(prompt="free"), listing(prompt="NaN"), listing(prompt=True),
    listing(prompt=["0.1"]), {"id": "vendor/model-1", "pricing": "cheap"}, {"pricing": {}},
])
def test_invalid_pricing_rows_are_rejected(row):
    with pytest.raises(SourceError, match="invalid_pricing"):
        prices(row)


def test_variant_listings_never_collapse_onto_the_base_model():
    assert [r.pricing_id for r in prices(listing("vendor/model-1:free", prompt="0", completion="0"),
                                         listing())] == ["vendor/model-1"]


def test_duplicate_pricing_ids_are_rejected():
    with pytest.raises(SourceError, match="duplicate_pricing"):
        prices(listing(), listing())


def priced(identity="pool/model-1", **update):
    return Candidate(identity=identity, score=80, benchmark_id="aa-1",
                     effort="low", variant="low", capability_approved=True).model_copy(update=update)


def test_price_matches_unique_slug_and_applies_provider_discount():
    result = price_candidates([priced()], prices(listing()), {}, {"pool": 0.5})
    assert (result[0].list_price, result[0].price) == (13.0, 6.5)


def test_configured_weights_reach_the_candidate_price():
    result = price_candidates([priced()], prices(listing()), {}, {},
                              PriceWeights(prompt=1, completion=1))
    assert (result[0].list_price, result[0].price) == (4.0, 4.0)


def test_zero_discount_is_free_even_without_a_published_price():
    result = price_candidates([priced("free-pool/unlisted")], prices(listing()), {},
                              {"free-pool": 0})
    assert (result[0].list_price, result[0].price) == (None, 0.0)


def test_effort_suffixed_runtime_id_uses_its_base_model_price():
    result = price_candidates([priced("pool/model-1-low")], prices(listing()), {}, {})
    assert result[0].price == 13.0


def test_ambiguous_slug_price_is_unknown_and_matching_prices_are_not():
    rows = prices(listing("a/model-1"), listing("b/model-1", prompt="0.000009"))
    assert price_candidates([priced()], rows, {}, {})[0].price is None
    same = prices(listing("a/model-1"), listing("b/model-1"))
    assert price_candidates([priced()], same, {}, {})[0].price == 13.0


def test_explicit_pricing_id_wins_and_never_falls_back_to_slug():
    rows = prices(listing("vendor/other", prompt="0", completion="0.000002"), listing())
    bindings = {"pool/model-1": {"pricing_id": "vendor/other", "capability_approved": True}}
    assert price_candidates([priced()], rows, bindings, {})[0].price == 2.0
    absent = {"pool/model-1": [{"pricing_id": "vendor/absent"}, {"pricing_id": "vendor/absent"}]}
    assert price_candidates([priced()], rows, absent, {})[0].price is None


def test_conflicting_pricing_ids_for_one_model_are_rejected():
    bindings = {"pool/model-1": [{"pricing_id": "vendor/model-1"}, {"pricing_id": "vendor/other"}]}
    with pytest.raises(SourceError, match="invalid_binding"):
        price_candidates([priced()], prices(listing()), bindings, {})
