"""OpenCode's `APIError` envelope must be recognised as a rate limit.

Beta, 2026-08-26: a `fix_coverage` agent turn ran for twenty-one minutes --
fifty messages, 243 recorded parts, ~100k tokens -- and then died. UTA reported
`error: True` with no message and the graph skipped the node, so it looked like
a repair that simply did not help. Two rounds went by that way.

OpenCode had the reason all along:

    {"name": "APIError",
     "data": {"message": "You have reached the limit.",
              "statusCode": 400, "isRetryable": false}}

`parse_rate_limit_payload` returned None for it, so no model fallback fired and
the chain's remaining models were never tried. Two separate reasons:

1. The message lives at `data.message`; the parser only read `data.error.
   message`, `responseBody.error.message` and `error.message`.
2. The status lives at `data.statusCode`, not at the top level.

The provider quota itself is upstream and not ours. Failing to *recognise* it,
and so failing over, is.
"""

from __future__ import annotations

import pytest

from agent_core.harness.rate_limit import parse_rate_limit_payload


def test_shared_logs_cannot_attribute_rate_limit_without_native_session(monkeypatch):
    from agent_core.harness import rate_limit

    def unexpected_scan(**kwargs):
        raise AssertionError("unowned global log scan")

    monkeypatch.setattr(rate_limit, "recent_log_files", unexpected_scan)
    assert rate_limit.detect_rate_limit_in_logs(
        session_id=None, provider_id="token-pool", model_id="gpt-6-astra"
    ) is None


OPENCODE_LIMIT = {
    "name": "APIError",
    "data": {
        "message": "You have reached the limit. (request id: 20260826021649...)",
        "statusCode": 400,
        "isRetryable": False,
        "responseHeaders": {},
    },
}


def test_the_opencode_api_error_envelope_is_recognised():
    parsed = parse_rate_limit_payload(OPENCODE_LIMIT)

    assert parsed is not None, "a quota exhaustion parsed as an ordinary error"
    assert parsed["status_code"] == 400
    assert "reached the limit" in parsed["message"]


def test_the_status_code_is_read_from_the_envelope():
    """It is on `data`, not at the top level, so a top-level-only read leaves
    it None and every status test below silently fails."""
    parsed = parse_rate_limit_payload(OPENCODE_LIMIT)

    assert parsed["status_code"] == 400


def test_response_headers_come_through_for_retry_timing():
    payload = {
        "name": "APIError",
        "data": {
            "message": "You have reached the limit.",
            "statusCode": 429,
            "responseHeaders": {"retry-after": "60"},
        },
    }

    parsed = parse_rate_limit_payload(payload)

    assert parsed["retry_after_seconds"] == 60


@pytest.mark.parametrize(
    "message",
    ["You have reached the limit.", "You have reached the usage limit for today"],
    ids=["exact", "variant"],
)
def test_the_provider_phrasings_match(message):
    assert parse_rate_limit_payload({"data": {"message": message, "statusCode": 400}})


def test_an_ordinary_error_is_still_not_a_rate_limit():
    """The envelope fix must not turn every 400 into a quota failure -- that
    would fail over on genuine bad requests and hide real bugs."""
    payload = {
        "name": "APIError",
        "data": {"message": "Invalid request: missing field 'model'", "statusCode": 400},
    }

    assert parse_rate_limit_payload(payload) is None
