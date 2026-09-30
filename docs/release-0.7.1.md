# agent-core 0.7.1 — result identity correction

This patch corrects the typed turn result-port contract before either product
adopts the 0.7 lifecycle. `TurnResultPort.commit` and `reject` now receive the
immutable `AgentTurnRequest` together with `AgentTurnExecutionResult`.

The request is the authoritative source of `operation_id` and turn name. A
result alone cannot identify the product operation it belongs to, and a
cancelled turn commits before a guard can expose that identity through any
other lifecycle callback. Consumers must implement:

```python
commit(request, execution)
reject(request, execution, *, reason)
```

No persisted format, workflow identity, or result payload changes from 0.7.0.
