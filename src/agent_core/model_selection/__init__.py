"""Application-independent model admission over validated cached evidence."""

from .policy import Candidate, ModelPolicy, ResolvedSelection, resolve_selection

__all__ = ["Candidate", "ModelPolicy", "ResolvedSelection", "resolve_selection"]
