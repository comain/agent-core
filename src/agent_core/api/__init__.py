"""Shared HTTP surface for task services.

FastAPI is an optional extra, so the names below are resolved on first access
rather than at import time: a consumer that serves its UI by other means can
still import `agent_core` without FastAPI installed.

The lazy step was previously only described, not implemented -- `__all__`
named `router`, which is a submodule, and neither `create_task_router` nor
`TaskServicePorts` could be imported from here at all. A consumer had to
reach into `agent_core.api.router`, which is the private path.
"""

from typing import TYPE_CHECKING, Any

__all__ = ["TaskServicePorts", "create_task_router"]

if TYPE_CHECKING:  # import for type checkers only; no runtime FastAPI dependency
    from agent_core.api.router import TaskServicePorts, create_task_router


def __getattr__(name: str) -> Any:
    if name in __all__:
        from agent_core.api import router as _router

        return getattr(_router, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(__all__)
