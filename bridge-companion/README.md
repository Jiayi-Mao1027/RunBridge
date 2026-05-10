# Bridge Companion

Bridge Companion is a read-only observation UI for the Bridge Runtime. It observes, attributes, presents, recovers after disconnects, and provides audit jumps into raw runtime facts.

It does not create bridge windows, create teams or tasks, modify ledgers, modify frozen semantics, control retries, decide completion, or inject context into agents.

## Runtime Sources

Primary live sources:

- `sdk_stream_events.jsonl`
- `tool_events.jsonl`
- `agent_messages.jsonl`
- `teammate_reports.jsonl`
- `process_events.jsonl`
- `artifacts.jsonl`
- `completion_checks.jsonl`
- `trajectory.jsonl`

Hydration and audit sources:

- `runtime_snapshot.json`
- `run_ledger.json`
- `transitions.jsonl`
- `session_bindings.jsonl`
- `active_operations.json`
- `.claude/runtime_state/session_observer/*.jsonl`

The UI must not synthesize low-level tool actions from reports. `Read`, `Edit`, `Write`, `MultiEdit`, `Bash`, `Grep`, `Glob`, and `LS` cards only come from hook `tool_events.jsonl`.

## Gateway

Start locally:

```powershell
cd C:\Users\admin\Desktop\Structure-config-1\bridge-companion
node gateway\server.mjs
```

Open:

```text
http://127.0.0.1:8787/
```

Default runtime discovery reads:

```text
../.claude/runtime_state/projects/<repo-key>/runs/<run-id>/
```

Optional overrides:

- `BRIDGE_RUNTIME_PROJECTS_ROOT`: path to `.claude/runtime_state/projects`
- `BRIDGE_RUNTIME_REGISTRY_ROOT`: path to `.claude/runtime_state/registry`
- `BRIDGE_RUNTIME_ROOT` or `BRIDGE_RUNTIME_RUNS_ROOT`: compatibility path; if it points at a single repo `runs` directory, the gateway derives the projects root from it
- `BRIDGE_SESSION_OBSERVER_ROOT`: path to `.claude/runtime_state/session_observer`
- `BRIDGE_COMPANION_PORT`: default `8787`
- `BRIDGE_COMPANION_STREAM_INTERVAL_MS`: default `750`

## Read-Only API

```text
GET /api/repos
GET /api/repos/:repoKey
GET /api/repos/:repoKey/runs
GET /api/repos/:repoKey/runs/latest
GET /api/repos/:repoKey/runs/:runId/status
GET /api/repos/:repoKey/runs/:runId/snapshot
GET /api/repos/:repoKey/runs/:runId/events?after=<seq>&afterId=<eventId>&afterCursor=<json>&limit=500
GET /api/repos/:repoKey/runs/:runId/stream?after=<seq>&afterId=<eventId>&afterCursor=<json>
GET /api/repos/:repoKey/runs/:runId/raw?file=<jsonl>&offset=<line>
GET /api/session-observer/events?after=<seq>&afterId=<eventId>&afterCursor=<json>&limit=500
GET /api/session-observer/stream?after=<seq>&afterId=<eventId>&afterCursor=<json>
POST /api/brief
```

`/api/brief` is an optional explanation layer. It accepts normalized facts and unknowns, returns display copy, and never writes runtime state.

## Event Contract

Run event streams emit normalized events with:

- `seq`
- `eventId`
- `cursor`
- `ts`
- `repoKey`
- `runId`
- `bridgeWindowId`
- `teamId`
- `taskId`
- `sessionId`
- `source`
- `kind`
- `lane`
- `actor`
- `textDelta`
- `toolInputDelta`
- `messagePreview`
- `toolName`
- `sdkToolName`
- `status`
- `target`
- `fileRefs`
- `evidenceRefs`
- `rawRef`

`eventId` is stable for a source JSONL line and is used for UI dedupe. `seq` is only a display/back-compat ordinal. Reconnect should prefer `afterCursor`, which is a map of `sourceFile -> lineOffset`; `Last-Event-ID` / `afterId` are also accepted.

The SSE path uses a per-source JSONL tailer after the initial backfill. It tracks byte and line cursors per source file, so new observer lines can reach the UI without repeatedly re-reading whole long logs.

SDK stream events are displayed in the discussion lane. SDK `tool_use` / `tool_result` / `input_json_delta` events are never rendered as real Read/Edit/Bash cards; those cards still require hook `tool_events.jsonl`.
