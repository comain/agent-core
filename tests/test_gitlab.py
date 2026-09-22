"""Generic GitLab merge-request delivery."""

import httpx
import pytest

from agent_core.integrations import GitLabClient, GitLabError


def _client(handler, *, tokens=("token-1",)):
    http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    return GitLabClient("https://gitlab.example.com", tokens=tokens, client=http), http


def test_create_merge_request_posts_the_generic_gitlab_contract():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"web_url": "https://gitlab.example.com/mr/7"})

    client, http = _client(handler)
    try:
        url = client.create_merge_request(
            project="group/repo",
            source_branch="feature/a",
            target_branch="main",
            title="Review generated patterns",
        )
    finally:
        http.close()

    assert url == "https://gitlab.example.com/mr/7"
    assert requests[0].url.raw_path == b"/api/v4/projects/group%2Frepo/merge_requests"
    assert requests[0].headers["PRIVATE-TOKEN"] == "token-1"
    assert dict(httpx.QueryParams(requests[0].content.decode())) == {
        "source_branch": "feature/a",
        "target_branch": "main",
        "title": "Review generated patterns",
    }


def test_an_unauthorized_token_falls_through_to_the_next_token():
    tokens = []

    def handler(request):
        token = request.headers["PRIVATE-TOKEN"]
        tokens.append(token)
        if token == "expired":
            return httpx.Response(401, text="expired")
        return httpx.Response(201, json={"web_url": "https://gitlab.example.com/mr/8"})

    client, http = _client(handler, tokens=("expired", "working"))
    try:
        assert client.create_merge_request(
            project="group/repo",
            source_branch="feature/a",
            target_branch="main",
            title="Title",
        ) == "https://gitlab.example.com/mr/8"
    finally:
        http.close()

    assert tokens == ["expired", "working"]


def test_missing_credentials_skip_delivery_without_an_http_request():
    def handler(request):
        raise AssertionError("no token means no request")

    client, http = _client(handler, tokens=())
    try:
        assert client.create_merge_request(
            project="group/repo",
            source_branch="feature/a",
            target_branch="main",
            title="Title",
        ) is None
    finally:
        http.close()


def test_gitlab_failures_use_one_stable_error_type():
    client, http = _client(lambda request: httpx.Response(409, text="already exists"))
    try:
        with pytest.raises(GitLabError, match="HTTP 409.*already exists"):
            client.create_merge_request(
                project="group/repo",
                source_branch="feature/a",
                target_branch="main",
                title="Title",
            )
    finally:
        http.close()


def test_all_unauthorized_tokens_report_the_last_rejection():
    responses = iter(
        [
            httpx.Response(401, text="first expired"),
            httpx.Response(401, text="second expired"),
        ]
    )
    client, http = _client(lambda request: next(responses), tokens=("one", "two"))
    try:
        with pytest.raises(GitLabError, match="HTTP 401 second expired"):
            client.create_merge_request(
                project="group/repo",
                source_branch="feature/a",
                target_branch="main",
                title="Title",
            )
    finally:
        http.close()


def test_transport_failures_use_the_stable_error_type():
    def handler(request):
        raise httpx.ConnectError("network unavailable", request=request)

    client, http = _client(handler)
    try:
        with pytest.raises(GitLabError, match="network unavailable"):
            client.create_merge_request(
                project="group/repo",
                source_branch="feature/a",
                target_branch="main",
                title="Title",
            )
    finally:
        http.close()
