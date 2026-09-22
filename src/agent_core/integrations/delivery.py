"""Delivering a result to a system that may not answer the first time.

Both products wrote this loop, with the same arithmetic -- `retry_times + 2`
-- the same per-attempt record, and the same httpx call. It was left out of
the protocol interface on the reasoning that each product keeps its own retry
policy. That reasoning did not survive reading the two implementations: what
differs between them is a timeout, a header and a log line, all of which are
arguments.

What a product still owns is what to *do* about a failed delivery: one raises
and lets a daemon retry the task later, another records the failure and moves
on. So this reports what happened and never decides.

The per-attempt history is the point of returning anything at all. It is
persisted by at least one consumer and read back when someone asks why a
pipeline never heard, so the shape is part of the contract: a status code and
elapsed time for an answered attempt, an error for one that never landed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Mapping, Optional

try:
    import httpx
except ImportError as exc:  # pragma: no cover - httpx is a hard dependency
    raise ImportError("agent_core.integrations.delivery requires httpx") from exc

from agent_core.integrations.protocol import ReportResult

logger = logging.getLogger(__name__)

#: Longest pause between attempts. Backoff climbs to this and stays: a caller
#: waiting on an ack should hear within seconds, not minutes.
MAX_BACKOFF_SECONDS = 3


def deliver_json(
    url: str,
    body: Any,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 10.0,
    attempts: int = 1,
    label: str = "",
    sleep: Callable[[float], None] = time.sleep,
    client_factory: Optional[Callable[..., Any]] = None,
) -> ReportResult:
    """POST a JSON body, retrying while it does not land.

    ``attempts`` is the total number of tries, not the number of retries.

    An attempt counts as delivered on any 2xx. A 4xx is recorded and retried
    like anything else: the systems on the other end of this have been known
    to answer 502 from a proxy and 400 from a half-warm instance, and telling
    those apart from the outside is guesswork.

    Never raises for a failed delivery -- the result says what happened, and
    what that means is the caller's.
    """
    total = max(1, int(attempts))
    request_headers = {"Content-Type": "application/json", **dict(headers or {})}
    history: list = []
    prefix = f"{label} " if label else ""

    for attempt in range(1, total + 1):
        started = time.time()
        try:
            logger.info("%scallback attempt=%s/%s target=%s", prefix, attempt, total, url)
            client = (client_factory or httpx.Client)(timeout=timeout)
            with client as session:
                response = session.post(url, headers=request_headers, json=body)
            elapsed_ms = int((time.time() - started) * 1000)
            entry = {
                "attempt": attempt,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
                # Bounded: a failing endpoint that returns a page of HTML
                # should not put a page of HTML in every task row.
                "body": response.text[:1000],
            }
            history.append(entry)
            logger.info(
                "%scallback attempt=%s/%s finished status_code=%s elapsed_ms=%s",
                prefix, attempt, total, response.status_code, elapsed_ms,
            )
            if 200 <= response.status_code < 300:
                return ReportResult(delivered=True, attempts=history)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.warning("%scallback attempt=%s/%s failed error=%s", prefix, attempt, total, exc)
            history.append(
                {
                    "attempt": attempt,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "error": str(exc),
                }
            )

        # No pause after the last attempt: there is nothing left to wait for,
        # and the caller is blocked until this returns.
        if attempt < total:
            sleep(min(attempt, MAX_BACKOFF_SECONDS))

    return ReportResult(
        delivered=False,
        attempts=history,
        error=f"delivery to {url} failed after {total} attempt(s)",
    )
