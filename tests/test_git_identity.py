"""Tests for git credentials.

Credential handling is where a mistake leaks a token or authenticates as the
wrong account, so the negative cases get as much attention as the happy path.
"""

from __future__ import annotations

import base64

import pytest

from agent_core.git import (
    env_with_identity,
    env_with_ssh_key,
    has_access_token,
    repo_name_from_url,
    ssh_command_for_key,
    url_for_access_token,
)
from agent_core.git.identity import append_git_config


# -- ssh -------------------------------------------------------------------------


def test_ssh_command_pins_a_single_key():
    cmd = ssh_command_for_key("~/.ssh/deploy_key")
    assert "-F /dev/null" in cmd, "must not read the user's ssh config"
    assert "IdentitiesOnly=yes" in cmd, "must not offer other agent-loaded keys"
    assert "PreferredAuthentications=publickey" in cmd
    assert "~" not in cmd, "path should be expanded"


def test_ssh_command_quotes_a_path_with_spaces():
    assert "'" in ssh_command_for_key("/keys/my key")


def test_no_key_means_no_command():
    assert ssh_command_for_key("") is None
    assert ssh_command_for_key("   ") is None
    assert env_with_ssh_key("") is None


def test_ssh_env_sets_git_ssh_command():
    env = env_with_ssh_key("/keys/id_ed25519")
    assert "GIT_SSH_COMMAND" in env and "/keys/id_ed25519" in env["GIT_SSH_COMMAND"]


# -- token -----------------------------------------------------------------------


def test_token_becomes_a_scoped_auth_header():
    env = env_with_identity(access_token="glpat-secret", token_host="git.example.com")
    idx = int(env["GIT_CONFIG_COUNT"]) - 1
    assert env[f"GIT_CONFIG_KEY_{idx}"] == "http.https://git.example.com/.extraheader"
    expected = base64.b64encode(b"oauth2:glpat-secret").decode()
    assert expected in env[f"GIT_CONFIG_VALUE_{idx}"]


def test_token_disables_interactive_prompting():
    """A service has no terminal; without this a bad credential hangs the task."""
    env = env_with_identity(access_token="t", token_host="git.example.com")
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_token_requires_an_explicit_host():
    """An auth header sent to the wrong host is a credential leak.

    The original defaulted to one deployment's hostname, which a shared package
    cannot do.
    """
    with pytest.raises(ValueError, match="token_host"):
        env_with_identity(access_token="secret")


def test_token_wins_over_a_key():
    env = env_with_identity(ssh_key_path="/keys/k", access_token="t", token_host="h")
    assert "GIT_SSH_COMMAND" not in env


def test_whitespace_token_is_not_a_token():
    env = env_with_identity(ssh_key_path="/keys/k", access_token="   ")
    assert "GIT_SSH_COMMAND" in env
    assert has_access_token("   ") is False


def test_no_credentials_at_all_yields_nothing():
    assert env_with_identity() is None


# -- url rewriting ---------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("git@git.example.com:group/repo.git", "https://git.example.com/group/repo.git"),
        ("ssh://git@git.example.com/group/repo.git", "https://git.example.com/group/repo.git"),
        ("https://git.example.com/group/repo.git", "https://git.example.com/group/repo.git"),
    ],
)
def test_url_rewriting(given, expected):
    assert url_for_access_token(given) == expected


def test_existing_userinfo_is_dropped():
    """Otherwise an old credential rides along in the rewritten URL."""
    out = url_for_access_token("https://olduser:oldpass@git.example.com/g/r.git")
    assert "olduser" not in out and "oldpass" not in out
    assert out == "https://git.example.com/g/r.git"


def test_port_is_preserved():
    assert url_for_access_token("ssh://git@git.example.com:2222/g/r.git").startswith(
        "https://git.example.com:2222/"
    )


def test_unrecognised_url_is_returned_unchanged():
    assert url_for_access_token("file:///tmp/repo") == "file:///tmp/repo"
    assert url_for_access_token("not a url") == "not a url"


# -- misc ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,name",
    [
        ("git@h:group/repo.git", "repo"),
        ("https://h/group/repo", "repo"),
        ("https://h/group/repo/", "repo"),
    ],
)
def test_repo_name_from_url(url, name):
    assert repo_name_from_url(url) == name


def test_git_config_entries_accumulate():
    env: dict = {}
    append_git_config(env, "a.b", "1")
    append_git_config(env, "c.d", "2")
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "a.b" and env["GIT_CONFIG_KEY_1"] == "c.d"


def test_corrupt_config_count_does_not_crash():
    env = {"GIT_CONFIG_COUNT": "not-a-number"}
    append_git_config(env, "a.b", "1")
    assert env["GIT_CONFIG_COUNT"] == "1"
