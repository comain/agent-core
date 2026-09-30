"""Git credentials for non-interactive access.

Two mechanisms, both producing an environment for a `git` subprocess rather than
writing anything to disk: an SSH key, or a GitLab access token injected as an
HTTP auth header.

This module was byte-identical across two products — 90 lines duplicated
exactly, differing only by a trailing blank line — and partially forked into a
third. It is the clearest duplication in the codebase.

Nothing here touches the user's `~/.ssh/config` or global git config. Every
credential travels in the subprocess environment, so concurrent tasks using
different identities cannot interfere with each other.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse

_SSH_REMOTE = re.compile(r"^[^@\s]+@([^:\s]+):(.+)$")


def ssh_command_for_key(key_path: str) -> Optional[str]:
    """A ``GIT_SSH_COMMAND`` pinned to one key.

    ``-F /dev/null`` and ``IdentitiesOnly=yes`` together stop ssh consulting the
    user's config or offering other agent-loaded keys — which matters when a
    service host has several deploy keys and the wrong one would authenticate as
    the wrong account.
    """
    if not key_path.strip():
        return None
    resolved = str(Path(key_path).expanduser())
    return (
        f"ssh -F /dev/null -i {shlex.quote(resolved)} "
        "-o IdentitiesOnly=yes -o PreferredAuthentications=publickey "
        "-o StrictHostKeyChecking=accept-new"
    )


def env_with_ssh_key(key_path: str) -> Optional[Dict[str, str]]:
    ssh_command = ssh_command_for_key(key_path)
    if not ssh_command:
        return None
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = ssh_command
    return env


def env_with_identity(
    *,
    ssh_key_path: str = "",
    access_token: str = "",
    token_host: str = "",
) -> Optional[Dict[str, str]]:
    """Environment carrying whichever credential is configured.

    A token wins over a key when both are present: it is the more specific
    choice, and a deployment that sets one usually means it.

    ``token_host`` has no default. The original hard-coded one deployment's
    GitLab hostname, which a shared package cannot do — and an auth header sent
    to the wrong host is a credential leak, not a misconfiguration.
    """
    token = access_token.strip()
    if token:
        if not token_host.strip():
            raise ValueError("token_host is required when using an access token")
        env = os.environ.copy()
        # Never let git prompt: a service has no terminal, and without this a
        # bad credential hangs the task instead of failing it.
        env["GIT_TERMINAL_PROMPT"] = "0"
        append_git_config(
            env,
            f"http.https://{token_host}/.extraheader",
            _access_token_header(token),
        )
        return env
    return env_with_ssh_key(ssh_key_path)


def url_for_access_token(git_url: str) -> str:
    """Rewrite a git URL to the HTTPS form a token can authenticate.

    Any userinfo already in the URL is dropped rather than carried into the
    rewritten one.
    """
    if "://" in git_url:
        parsed = urlparse(git_url)
        if parsed.scheme == "ssh" and parsed.hostname:
            path = parsed.path.lstrip("/")
            return urlunparse(("https", _netloc_without_userinfo(parsed), f"/{path}", "", "", ""))
        if parsed.scheme == "https" and parsed.hostname:
            return urlunparse(("https", _netloc_without_userinfo(parsed), parsed.path, "", "", ""))
        return git_url
    match = _SSH_REMOTE.match(git_url)
    if not match:
        return git_url
    host, path = match.groups()
    return f"https://{host}/{path.lstrip('/')}"


def has_access_token(access_token: str) -> bool:
    return bool(access_token.strip())


def repo_name_from_url(git_url: str) -> str:
    """Last path segment, without the ``.git`` suffix."""
    return git_url.rstrip("/").split("/")[-1].removesuffix(".git")


def append_git_config(env: Dict[str, str], key: str, value: str) -> None:
    """Add a git config override to an environment.

    Uses ``GIT_CONFIG_KEY_n``/``VALUE_n`` rather than writing a file, so the
    setting is scoped to one subprocess and leaves no trace afterwards.
    """
    try:
        index = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        index = 0
    env[f"GIT_CONFIG_KEY_{index}"] = key
    env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(index + 1)


def _access_token_header(access_token: str) -> str:
    encoded = base64.b64encode(f"oauth2:{access_token}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {encoded}"


def _netloc_without_userinfo(parsed) -> str:
    host = parsed.hostname or ""
    if parsed.port is not None:
        return f"{host}:{parsed.port}"
    return host
