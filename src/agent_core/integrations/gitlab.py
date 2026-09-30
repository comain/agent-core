"""Minimal GitLab API client for publishing merge requests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional
from urllib.parse import quote, urlencode

import httpx


class GitLabError(RuntimeError):
    """GitLab rejected or could not receive a request."""


class GitLabClient:
    """Create merge requests without embedding product policy.

    Products choose the project, branches, title, and credentials. This client
    owns only GitLab's HTTP contract and authentication failover.
    """

    def __init__(
        self,
        base_url: str,
        *,
        tokens: Sequence[str] = (),
        timeout: float = 20.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = tuple(token for token in tokens if token)
        self.timeout = timeout
        self.client = client

    def find_merge_request(self, spec) -> Optional[str]:
        """An open merge request for this source branch, if there is one.

        Asked before every create, because a retry that skipped this opens a
        second merge request for a branch that already has one — and reviewers
        then have two places to comment and one of them is wrong.
        """
        if not self.tokens:
            return None
        url = (
            f"{self.base_url}/api/v4/projects/{quote(spec.project, safe='')}"
            "/merge_requests"
        )
        params = {
            "source_branch": spec.source_branch,
            "target_branch": spec.target_branch,
            "state": "opened",
        }
        for token in self.tokens:
            response = self._request(
                "GET", url, params=params, headers={"PRIVATE-TOKEN": token}
            )
            if response.status_code == 401:
                continue
            if not 200 <= response.status_code < 300:
                raise GitLabError(self._error_message(response))
            try:
                payload = response.json()
            except ValueError:
                return None
            if isinstance(payload, list) and payload:
                web_url = payload[0].get("web_url") if isinstance(payload[0], dict) else None
                return str(web_url) if web_url else None
            return None
        return None

    def manual_url(self, spec) -> str:
        """Where a human finishes what the API could not.

        The branch is already pushed; withholding the link because the API call
        failed leaves the work stranded on the remote with nothing pointing at
        it.
        """
        query = urlencode(
            {
                "merge_request[source_branch]": spec.source_branch,
                "merge_request[target_branch]": spec.target_branch,
            }
        )
        return f"{self.base_url}/{spec.project}/-/merge_requests/new?{query}"

    def create_merge_request(self, spec=None, **fields) -> Optional[str]:
        """Create a merge request, or return ``None`` without credentials.

        Takes a `MergeRequestSpec` so it satisfies the neutral publisher
        protocol; the keyword form is what existing callers pass.
        """
        if spec is not None:
            project = spec.project
            source_branch = spec.source_branch
            target_branch = spec.target_branch
            title = spec.title
        else:
            project = fields["project"]
            source_branch = fields["source_branch"]
            target_branch = fields["target_branch"]
            title = fields["title"]
        if not self.tokens:
            return None
        url = (
            f"{self.base_url}/api/v4/projects/{quote(project, safe='')}/merge_requests"
        )
        form = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        }
        last_unauthorized = ""
        for token in self.tokens:
            response = self._post(
                url,
                data=form,
                headers={"PRIVATE-TOKEN": token},
            )
            if response.status_code == 401:
                last_unauthorized = self._error_message(response)
                continue
            if not 200 <= response.status_code < 300:
                raise GitLabError(self._error_message(response))
            try:
                payload = response.json()
            except ValueError:
                return None
            web_url = payload.get("web_url") if isinstance(payload, dict) else None
            return str(web_url) if web_url else None
        raise GitLabError(last_unauthorized or "GitLab rejected every configured token")

    def _post(self, url: str, **kwargs) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            if self.client is not None:
                return self.client.request(method, url, timeout=self.timeout, **kwargs)
            with httpx.Client(trust_env=False) as client:
                return client.request(method, url, timeout=self.timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise GitLabError(f"GitLab MR creation failed: {exc}") from exc

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        body = response.text[:1000]
        return f"GitLab MR creation failed: HTTP {response.status_code} {body}".rstrip()
