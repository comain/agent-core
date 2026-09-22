"""Test doubles for the harness.

Without these, every consumer stubs :class:`subprocess.Popen` to test a workflow
that happens to call a model — which couples each product's tests to the
harness's internals, and re-implements the same fixture four times.

:class:`FakeOpenCodeProcess` is a drop-in for :class:`OpenCodeProcess`: same
``run_turn`` signature, scripted results, and a record of what it was asked.

    process = FakeOpenCodeProcess(["first answer", "second answer"])
    workflow.run(process)

    assert process.call_count == 2
    assert "design" in process.calls[0]["message"]

Adapted from the one consumer that shipped a fake rather than stubbing Popen.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Union

from agent_core.harness.process import TurnResult

#: What a script entry may be: a plain string (becomes a completed turn), a
#: ready-made TurnResult, or an exception instance to raise.
ScriptEntry = Union[str, TurnResult, BaseException]


class FakeOpenCodeProcess:
    """A deterministic stand-in for :class:`OpenCodeProcess`.

    ``script`` is consumed one entry per turn. When it runs out the last entry
    repeats, so a test that only cares about the first turn need not enumerate
    the rest. An empty script yields empty completed turns.
    """

    def __init__(
        self,
        script: Optional[Sequence[ScriptEntry]] = None,
        *,
        session_id: str = "fake-session",
        model_id: str = "fake/model",
    ):
        self.script: List[ScriptEntry] = list(script or [])
        self.session_id = session_id
        self.model_id = model_id
        self.calls: List[Dict[str, Any]] = []

    # -- inspection --------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_call(self) -> Optional[Dict[str, Any]]:
        return self.calls[-1] if self.calls else None

    @property
    def prompts(self) -> List[Optional[str]]:
        """Every message passed, in order. ``None`` where a prompt file was used."""
        return [call.get("message") for call in self.calls]

    # -- the interface under test -------------------------------------------

    def run_turn(self, message: Optional[str] = None, **kwargs: Any) -> TurnResult:
        call = {"message": message, **kwargs}
        self.calls.append(call)

        if not self.script:
            entry: ScriptEntry = ""
        elif len(self.script) == 1:
            entry = self.script[0]  # last entry repeats
        else:
            entry = self.script.pop(0)

        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, TurnResult):
            return entry
        return TurnResult(
            type="completed",
            result=entry,
            # Reflect the caller's session when it supplied one, so affinity
            # behaviour is exercised rather than masked by a constant.
            session_id=kwargs.get("session_id") or self.session_id,
            model_id=kwargs.get("model_id") or self.model_id,
        )
