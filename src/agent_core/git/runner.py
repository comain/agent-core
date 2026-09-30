"""Running git against a checkout someone else owns.

`GitWorkspace` clones and manages repositories, and its write path --
`commit_all_and_push`, with its policy guard and rebase-before-push -- is the
right thing to use when it owns the clone. Consumers frequently do not want
that. They already have a checkout, made by a CI runner or a task workspace,
and they want to run git against it.

Those consumers reach for `subprocess.run(["git", ...], cwd=path)` and
consistently lose four things:

* **a timeout** -- `fetch`, `push` and `rebase` talk to a remote, where a
  stalled connection or a server that accepts and never answers hangs forever,
  and the caller hangs with it;
* **credentials** -- resolved separately at each site, or forgotten, and a
  forgotten one fails only on the hosts that need it;
* **cancellation** -- an operator's stop cannot reach a git command that has
  already started;
* **typed failures** -- a `CompletedProcess` with a non-zero code and a string,
  which no caller can classify, so none of them retry a transport blip.

This owns those four. The verbs stay with the caller: twenty-four call sites
in one consumer alone pass entirely different flags, and wrapping each in a
method would be a worse interface than `git`'s own.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Union

from agent_core.git.identity import env_with_identity
from agent_core.git.workspace import GitCancelled, GitCommandError, GitCredentials, GitTimeout

logger = logging.getLogger(__name__)


@dataclass
class GitRunner:
    """Invoke git in a directory, safely.

    ``timeout`` of None means no limit, for an operator with a clone that
    legitimately takes longer than any default anyone would pick. It is not the
    default, because unbounded is how a turn ends up wedged.
    """

    credentials: Optional[GitCredentials] = None
    timeout: Optional[float] = 600.0
    git_bin: str = "git"

    def __post_init__(self) -> None:
        credentials = self.credentials or GitCredentials()
        self._env = env_with_identity(
            ssh_key_path=credentials.ssh_key_path,
            access_token=credentials.access_token,
            token_host=credentials.token_host,
        )

    def run(
        self,
        repo_path: Union[str, Path],
        *args: str,
        check: bool = True,
        timeout: Optional[float] = -1.0,
        is_cancelled: Optional[Callable[[], bool]] = None,
        env: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        """Run one git command.

        ``check`` raises `GitCommandError` on a non-zero exit, so a caller that
        wants a typed failure gets one and a caller inspecting ``returncode``
        passes ``check=False``. A timeout always raises, whatever ``check``
        says -- a command that never returned produced no exit code to inspect,
        and treating that as an ordinary failure loses the distinction that
        matters for retrying.

        ``timeout`` defaults to the runner's; pass None for no limit.
        """
        if is_cancelled is not None and is_cancelled():
            raise GitCancelled(f"cancelled before running git {args[0] if args else ''}")

        effective = self.timeout if timeout == -1.0 else timeout
        command = [self.git_bin, *args]
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)

        try:
            completed = subprocess.run(
                command,
                cwd=str(repo_path),
                env=env if env is not None else self._env,
                timeout=effective,
                check=False,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitTimeout(
                f"git {' '.join(args)} timed out after {effective}s in {repo_path}"
            ) from exc
        except (FileNotFoundError, NotADirectoryError) as exc:
            # `cwd=` fails at the OS layer before git starts, where the
            # `git -C <path>` form these callers came from lets git report it:
            # exit 128 on stderr, no exception. Callers read `returncode`, so
            # surfacing an OSError here turns a failed command into a crash
            # somewhere that never expected one. Report it the way git does and
            # let `check` decide whether it raises.
            completed = subprocess.CompletedProcess(
                command,
                128,
                stdout="",
                stderr=f"fatal: cannot change to '{repo_path}': {exc.strerror}\n",
            )

        if check and completed.returncode != 0:
            raise GitCommandError(command, completed.returncode, completed.stderr or "")
        return completed

    def output(self, repo_path: Union[str, Path], *args: str, **kwargs) -> str:
        """Stdout of a git command, stripped. The common read."""
        return (self.run(repo_path, *args, **kwargs).stdout or "").strip()

    def succeeds(self, repo_path: Union[str, Path], *args: str, **kwargs) -> bool:
        """Whether a command exits zero, for the questions git answers by status.

        `rev-parse --verify` and `show-ref` are asked this way, and a caller
        writing `returncode == 0` by hand has to remember `check=False` -- which
        is easy to forget, and raises where the answer was simply "no".
        """
        kwargs["check"] = False
        return self.run(repo_path, *args, **kwargs).returncode == 0


def runner_for(
    *,
    ssh_key_path: str = "",
    access_token: str = "",
    token_host: str = "",
    timeout: Optional[float] = 600.0,
) -> GitRunner:
    """A runner from loose credential values, which is how consumers hold them."""
    return GitRunner(
        credentials=GitCredentials(
            ssh_key_path=ssh_key_path,
            access_token=access_token,
            token_host=token_host,
        ),
        timeout=timeout,
    )
