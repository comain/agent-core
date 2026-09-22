# Strict Git Comparisons

Code review and other branch-wide checks should call
`ChangeCollector.collect(path, base_refs=["origin/master"], strict_base=True)`.
The default remains the legacy best-effort mode for existing consumers.

Strict mode refreshes the configured base, resolves its merge-base with HEAD,
and fetches missing shallow history using `--unshallow` over advertised branch
refs when necessary (compatible with servers rejecting historical SHA fetches).
The comparison uses pinned commit IDs even if fetched refs advance. It never
checks out a different commit. Fetches retain the
GitWorkspace credentials, command timeout, cancellation and configured transient
retry policy. Callers must hold the repository lock while collecting context.

Missing bases, unrelated histories, failed fetches and failed diff queries raise
instead of falling back to HEAD or a recent-commit window. An actual merged
branch still returns an empty diff. `fallback_refs` is ignored in strict mode.

The regression was a branch with production changes followed by six test-only
repair commits: shallow history prevented merge-base resolution, and the legacy
HEAD~5 fallback excluded all production changes. Strict mode recovers the full
branch comparison or fails explicitly.
