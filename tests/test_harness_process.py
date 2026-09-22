

def test_a_relative_prompt_path_is_made_absolute():
    """The agent runs in a per-turn workspace, not the caller's directory.

    A deployment configuring `BASE_DIR=runtime` produces prompt paths like
    `runtime/audit/<task>/prompt.md`. Passed through unchanged, the agent
    resolves that against its own workspace, finds nothing, and exits before
    emitting an event -- indistinguishable from a model returning no output,
    so the whole provider chain gets walked and every model marked unhealthy.
    """
    import os
    from pathlib import Path

    from agent_core.harness.process import _prompt_file_args

    args = _prompt_file_args(Path("runtime/audit/task1/prompt.md"))

    assert Path(args[-1]).is_absolute()
    assert args[-1].endswith("runtime/audit/task1/prompt.md")
    assert args[-2] == "--file"
    # The instruction names the file, not the path, so it stays readable.
    assert args[0].endswith("prompt.md")


def test_an_absolute_prompt_path_is_left_alone():
    from pathlib import Path

    from agent_core.harness.process import _prompt_file_args

    args = _prompt_file_args(Path("/tmp/somewhere/prompt.md"))
    # Unchanged, and in particular not symlink-resolved: on macOS /tmp is a
    # symlink, and rewriting it would change a path the caller chose.
    assert args[-1] == "/tmp/somewhere/prompt.md"


# -- gateway 5xx -------------------------------------------------------------
#
# Beta, replaying a production task: the token-pool gateway answered
# `POST /v1/chat/completions` with 503 and `isRetryable: true`. Neither
# classifier recognised it, so the fast provider-failure path never fired and
# the turn sat idle until the 15-minute stream timeout -- holding the single
# task slot and reporting nothing useful. A 5xx from a gateway is the most
# ordinary provider fault there is.


def test_a_gateway_503_is_a_provider_failure():
    from agent_core.harness.process import provider_failure_from_stderr

    line = (
        'ERROR service=llm providerID=token-pool modelID=claude-opus-5 '
        'error={"name":"AI_APICallError","statusCode":503,"isRetryable":true,'
        '"url":"http://token-pool.example/v1/chat/completions"}'
    )

    assert provider_failure_from_stderr(line) is not None


def test_gateway_502_and_504_are_provider_failures():
    from agent_core.harness.process import provider_failure_from_stderr

    for status in (500, 502, 504):
        line = (
            'ERROR service=llm error={"name":"AI_APICallError",'
            f'"statusCode":{status}' + "}"
        )
        assert provider_failure_from_stderr(line) is not None, status


def test_a_client_error_is_not_a_transport_failure():
    """4xx is the caller's problem; retrying it on another provider wastes
    money and hides a real defect."""
    from agent_core.harness.process import provider_failure_from_stderr

    line = 'ERROR service=llm error={"name":"AI_APICallError","statusCode":400}'

    assert provider_failure_from_stderr(line) is None


def test_ordinary_provider_noise_is_still_not_a_failure():
    """The two-marker rule exists to avoid false positives; keep it."""
    from agent_core.harness.process import provider_failure_from_stderr

    assert provider_failure_from_stderr("INFO service=llm streaming response") is None
    assert provider_failure_from_stderr('{"statusCode":503}') is None, (
        "a status code alone, with no API-error marker, is not enough"
    )


def test_usage_limit_stderr_is_classified_without_waiting_for_timeout():
    from agent_core.harness.process import provider_fallback_from_stderr

    line = (
        'ERROR service=llm error={"error":{"name":"AI_APICallError",'
        '"statusCode":429,"responseBody":"{\\"error\\":{'
        '\\"type\\":\\"usage_limit_reached\\"}}"}} stream error'
    )

    failure = provider_fallback_from_stderr(line)

    assert failure is not None
    assert failure[0] == "rate_limit"


def test_provider_failure_only_fails_fast_before_meaningful_output():
    from agent_core.harness.process import provider_failure_is_fail_fast

    failure = ("rate_limit", {"name": "OpenCodeProviderFailure"})

    assert provider_failure_is_fail_fast(failure, saw_meaningful_output=False) is True
    assert provider_failure_is_fail_fast(failure, saw_meaningful_output=True) is False


def test_a_503_classifies_as_a_transient_transport_error():
    """So the fallback reason names something an operator can act on."""
    from agent_core.harness.process import classify_provider_model_error

    reason = classify_provider_model_error(
        {"name": "AI_APICallError", "data": {"statusCode": 503}}
    )

    assert reason == "provider_transport_error"


def test_a_404_still_classifies_as_a_missing_model():
    from agent_core.harness.process import classify_provider_model_error

    assert classify_provider_model_error({"data": {"statusCode": 404}}) == "model_not_found"


def test_plain_string_authentication_error_uses_provider_fallback():
    from agent_core.harness.process import classify_provider_model_error

    reason = classify_provider_model_error(
        "Authentication Fails, Your api key: ****d353 is invalid"
    )

    assert reason == "provider_auth_failed"


def test_model_rejection_takes_precedence_over_gateway_auth_status():
    from agent_core.harness.process import classify_provider_model_error

    assert classify_provider_model_error({"data": {
        "statusCode": 401,
        "message": "Your model id does not exist, recognized as k3. Please set model id as `k3`.",
        "type": "invalid_authentication_error",
    }}) == "model_not_found"
    assert classify_provider_model_error({"data": {
        "statusCode": 401, "message": "API key does not exist for this model",
    }}) == "provider_auth_failed"


def test_insufficient_balance_403_uses_quota_fallback():
    from agent_core.harness.process import classify_provider_model_error

    reason = classify_provider_model_error(
        {
            "name": "AI_APICallError",
            "data": {
                "statusCode": 403,
                "message": (
                    "预扣费额度失败, 用户剩余额度: ¥0.12, "
                    "需要预扣费额度: ¥0.14"
                ),
            },
        }
    )

    assert reason == "rate_limit"


def test_weekly_usage_limit_403_is_rate_limited_not_auth_quarantined():
    from agent_core.harness.process import (
        classify_provider_model_error,
        has_authoritative_auth_evidence,
    )

    payload = {
        "error": {
            "name": "AI_APICallError",
            "statusCode": 403,
            "responseBody": (
                '{"error":{"type":"usage_limit_reached",'
                '"message":"You have reached your weekly usage limit"}}'
            ),
        }
    }

    assert classify_provider_model_error(payload) == "rate_limit"
    assert has_authoritative_auth_evidence(payload) is False


def test_bare_403_is_not_authoritative_auth_evidence():
    from agent_core.harness.process import (
        classify_provider_model_error,
        has_authoritative_auth_evidence,
    )

    payload = {"data": {"statusCode": 403, "message": "Forbidden"}}

    assert classify_provider_model_error(payload) is None
    assert has_authoritative_auth_evidence(payload) is False


def test_403_with_structured_invalid_key_is_authoritative_auth_evidence():
    from agent_core.harness.process import (
        classify_provider_model_error,
        has_authoritative_auth_evidence,
    )

    payload = {
        "data": {
            "statusCode": 403,
            "errorCode": "invalid_api_key",
            "message": "The supplied credential is invalid",
        }
    }

    assert classify_provider_model_error(payload) == "provider_auth_failed"
    assert has_authoritative_auth_evidence(payload) is True


def test_a_429_whose_echoed_prompt_mentions_authentication_is_a_rate_limit():
    """OpenCode's stderr error echoes the whole request under `requestBodyValues`.

    OpenCode's own system prompt says "integrate with your existing authentication
    system", so matching over the echo classified a production 429 as
    provider_auth_failed -- an auth quarantine with no expiry, for every task.
    """
    import json

    from agent_core.harness.process import (
        classify_provider_model_error,
        provider_fallback_from_stderr,
    )

    payload = {"error": {
        "name": "AI_APICallError",
        "url": "http://token-pool.example/v1/chat/completions",
        "requestBodyValues": {"model": "gpt-6-astra", "messages": [{
            "role": "system",
            "content": "... frontend forms that integrate with your existing authentication system.",
        }]},
        "statusCode": 429,
        "responseHeaders": {"content-type": "application/json"},
        "responseBody": '{"detail":"Rate limit exceeded"}',
        "isRetryable": True,
    }}
    line = (
        "ERROR 2026-09-15T07:53:41 +1556ms service=llm providerID=token-pool "
        "modelID=gpt-6-astra error=" + json.dumps(payload)
    )

    assert classify_provider_model_error(payload) == "rate_limit"
    failure = provider_fallback_from_stderr(line)
    assert failure is not None
    assert failure[0] == "rate_limit"
    assert failure[1]["data"]["httpStatus"] == 429


def test_a_nested_401_is_still_an_auth_failure():
    import json

    from agent_core.harness.process import (
        classify_provider_model_error,
        provider_fallback_from_stderr,
    )

    payload = {"error": {
        "name": "AI_APICallError",
        "requestBodyValues": {
            "messages": [{"content": "private reviewed source"}],
            "apiKey": "sk-request-secret",
        },
        "responseHeaders": {"authorization": "Bearer response-secret"},
        "statusCode": 401,
        "responseBody": '{"error":{"type":"invalid_api_key",'
        '"message":"Missing API key sk-provider-secret"}}',
    }}

    assert classify_provider_model_error(payload) == "provider_auth_failed"
    failure = provider_fallback_from_stderr(
        "ERROR service=llm error=" + json.dumps(payload)
    )

    assert failure is not None
    assert failure[0] == "provider_auth_failed"
    assert failure[1]["data"] == {
        "message": "provider_auth_failed",
        "httpStatus": 401,
        "errorCode": "invalid_api_key",
        "errorDetail": "Missing API key [redacted]",
    }
    serialized = json.dumps(failure[1])
    assert "private reviewed source" not in serialized
    assert "request-secret" not in serialized
    assert "response-secret" not in serialized
    assert "provider-secret" not in serialized


def test_transport_markers_in_the_echoed_request_are_not_a_transport_failure():
    """The reviewed diff rides along in `requestBodyValues`; code that says
    "fetch failed" or `"statusCode":503` must not fail over a provider."""
    import json

    from agent_core.harness.process import (
        contains_transport_failure,
        provider_failure_from_stderr,
        provider_fallback_from_stderr,
    )

    payload = {"error": {
        "name": "AI_APICallError",
        "requestBodyValues": {"messages": [{
            "role": "user",
            "content": 'catch (e) { log("fetch failed", e.code === "ECONNRESET", {"statusCode":503}) }',
        }]},
        "statusCode": 429,
        "responseBody": '{"detail":"Rate limit exceeded"}',
    }}
    line = "ERROR service=llm providerID=token-pool error=" + json.dumps(payload)

    assert provider_failure_from_stderr(line) is None
    assert provider_fallback_from_stderr(line)[0] == "rate_limit"
    assert contains_transport_failure(payload) is False


def test_a_real_nested_transport_cause_is_still_found():
    from agent_core.harness.process import contains_transport_failure

    assert contains_transport_failure({"error": {
        "name": "AI_RetryError",
        "requestBodyValues": {"messages": [{"content": "hello"}]},
        "errors": [{"cause": {"code": "ECONNRESET"}}],
    }}) is True
