"""Human-gate adapters for workflow engines.

The LangGraph adapter lives in :mod:`agent_core.gates.langgraph` and is imported
lazily, so agent-core does not force a workflow engine on consumers that only
want the harness.
"""

__all__ = ["langgraph"]
