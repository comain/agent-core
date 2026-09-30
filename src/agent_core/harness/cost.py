"""Counting what was actually submitted to a provider, not what was asked for.

One `run_turn` is not one charge. A provider chain that fails over twice
submitted three times and paid for three; a stalled turn nudged back to life
paid twice; a rejected answer retried paid again. Accounting once per outer
operation — which is what this package did — produces a number that is right
only in the case where nothing went wrong, and wrong in every case a cap
exists to catch.

So the unit here is the **paid attempt**: one actual submission to a provider.
Three rules make it usable.

**The ordinal is monotonic across everything.** Retry, fallback and in-session
recovery share one generator, so attempt 4 is the fourth submission this
operation paid for regardless of which mechanism produced it. Numbering per
mechanism would let three separate "attempt 1"s spend three times a cap that
was meant to allow one.

**The report happens before the next admission.** A cap that learns about
attempt 3 after attempt 4 has already been submitted is not a cap. The
submission context manager closes the loop: no later attempt can start until
the previous one's charge has been reported.

**An unknown charge is reported, not skipped.** A call that raised after the
request reached the provider probably cost money, and saying nothing lets the
next admission believe the budget is intact. `TurnCost.unavailable()` says "a
submission happened and its price is unknown", which a product with a cap
should treat as fail-closed and a product without one may aggregate into an
unavailable total. Silence cannot express either.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: The provider reported a charge for this attempt.
RECORDED = "recorded"
#: A submission happened; what it cost is not known.
UNAVAILABLE = "unavailable"


class AttemptNotAdmitted(RuntimeError):
    """Something refused to let another paid attempt start.

    The common supertype of every "do not spend again" refusal — a cap, a
    stopped task, a product observer that has decided the operation should not
    continue. The retry loop keys on this rather than on a list of specific
    types, so a new kind of refusal is not retried by default.
    """


class CostAdmissionError(AttemptNotAdmitted):
    """A paid attempt was refused, or the previous one could not be reported.

    Never retried. A cap that says no does not mean "try again"; retrying is
    how a refusal becomes a spend, and this type exists so the retry loop can
    tell that refusal apart from a provider failure worth another attempt.
    """


@dataclass(frozen=True)
class TurnCost:
    """Provider-reported cost for one paid attempt, never a price estimate."""

    provider_cost_usd: Optional[float]
    provenance: str = UNAVAILABLE

    @classmethod
    def from_provider(cls, value: Any) -> "TurnCost":
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return cls(provider_cost_usd=float(value), provenance=RECORDED)
        return cls.unavailable()

    @classmethod
    def unavailable(cls) -> "TurnCost":
        """A submission whose charge is not known — including zero-cost-looking
        failures, which are not the same thing as a reported zero."""
        return cls(provider_cost_usd=None)

    @property
    def known(self) -> bool:
        return self.provenance == RECORDED and self.provider_cost_usd is not None


def aggregate_turn_costs(costs: Iterable[TurnCost]) -> TurnCost:
    """Sum, unless anything is unknown — in which case the total is unknown.

    A partial total is the dangerous answer: it looks authoritative, it is
    always an underestimate, and a cap compared against it approves spend that
    already happened. A reported zero is a real number and sums normally; a
    missing one poisons the total, deliberately.
    """
    total = 0.0
    seen = False
    for cost in costs:
        if not cost.known:
            return TurnCost.unavailable()
        seen = True
        total += float(cost.provider_cost_usd or 0.0)
    if not seen:
        return TurnCost.unavailable()
    return TurnCost(provider_cost_usd=total, provenance=RECORDED)


@runtime_checkable
class TurnCostPort(Protocol):
    """Product cost policy, consulted around every provider submission.

    Every harness gets one. A product with no monetary cap supplies a no-op
    admission that still records — the recording is what makes an operation's
    real spend reconstructable later, cap or no cap.
    """

    def before_paid_attempt(self, *, operation_id: str, paid_attempt_ordinal: int) -> None:
        """Raise to prevent the submission."""

    def after_paid_attempt(
        self, *, operation_id: str, paid_attempt_ordinal: int, cost: TurnCost
    ) -> None:
        """Record what the submission cost, including when that is unknown."""


def accounts_for_paid_attempts(harness: Any) -> bool:
    """Whether this harness counts its own submissions.

    A harness whose single `run_turn` walks a provider chain submits more than
    once, and only it knows how many times. Such a harness sets
    ``accepts_paid_attempts`` and receives the ledger as a turn argument; every
    other harness is wrapped by its caller instead. Making that explicit is
    what keeps a fallback chain from being counted twice — once inside and once
    outside — which is a worse failure than not counting it at all, because the
    number still looks plausible.
    """
    return bool(getattr(harness, "accepts_paid_attempts", False))


class _Submission:
    """One in-flight paid attempt, waiting to be told what it cost."""

    def __init__(self, ordinal: int):
        self.ordinal = ordinal
        self.cost = TurnCost.unavailable()

    def record(self, cost: TurnCost) -> None:
        self.cost = cost

    def record_provider(self, value: Any) -> None:
        """Take the charge off a harness result, whatever shape it reports."""
        self.record(TurnCost.from_provider(value))


class PaidAttempts:
    """The shared ordinal generator and the port around each submission.

    One instance per operation, handed down through retry, fallback and
    recovery so all three draw from the same sequence.
    """

    def __init__(self, port: Optional[TurnCostPort] = None, *, operation_id: str = ""):
        self._port = port
        self._operation_id = operation_id
        self._lock = threading.Lock()
        self._ordinal = 0
        self._costs: list = []
        self._blocked: Optional[str] = None

    @property
    def count(self) -> int:
        """How many submissions this operation has paid for."""
        return self._ordinal

    @property
    def costs(self) -> tuple:
        return tuple(self._costs)

    def aggregate(self) -> TurnCost:
        return aggregate_turn_costs(self._costs)

    @contextmanager
    def submission(self) -> Iterator[_Submission]:
        """Admit one provider submission, then report what it cost.

        The report is in a `finally`: a call that raised after the request left
        for the provider still spent money, and the next admission has to be
        told so.
        """
        if self._blocked is not None:
            raise CostAdmissionError(
                f"a previous paid attempt could not be recorded ({self._blocked}); "
                "no further provider submissions are admitted"
            )
        with self._lock:
            self._ordinal += 1
            ordinal = self._ordinal
        attempt = _Submission(ordinal)

        if self._port is not None:
            try:
                self._port.before_paid_attempt(
                    operation_id=self._operation_id, paid_attempt_ordinal=ordinal
                )
            except CostAdmissionError:
                raise
            except Exception as exc:  # noqa: BLE001 - the port decides, we classify
                raise CostAdmissionError(
                    f"paid attempt {ordinal} was not admitted: {exc}"
                ) from exc

        failed = False
        try:
            yield attempt
        except BaseException:
            failed = True
            raise
        finally:
            self._costs.append(attempt.cost)
            if self._port is not None:
                try:
                    self._port.after_paid_attempt(
                        operation_id=self._operation_id,
                        paid_attempt_ordinal=ordinal,
                        cost=attempt.cost,
                    )
                except Exception as exc:  # noqa: BLE001
                    if not failed:
                        raise CostAdmissionError(
                            f"paid attempt {ordinal} could not be recorded: {exc}"
                        ) from exc
                    # The turn's own failure is the real cause and must not be
                    # masked by a bookkeeping error raised while unwinding it.
                    # The ledger is closed instead, so the next submission --
                    # the one that would spend against an unrecorded charge --
                    # is the thing that fails.
                    logger.warning(
                        "could not record paid attempt %s while handling a failure: %s",
                        ordinal,
                        exc,
                    )
                    self._blocked = f"attempt {ordinal}"
