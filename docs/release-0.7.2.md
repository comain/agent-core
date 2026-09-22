# agent-core 0.7.2 — consumer composition corrections

This patch completes the public composition surface needed by the first 0.7
consumer:

- exports `SinkProgressPort` from `agent_core.runtime`, so products can adapt
  their existing progress sink without importing an implementation module;
- passes the typed cancellation source into active harness calls as
  `is_cancelled`, preserving cancellation during a long provider turn as well
  as the pre-turn cancellation check.

No persisted or result shape changes from 0.7.1.
