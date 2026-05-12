# Bridge Companion

Bridge Companion is a read-only observation UI for the Bridge Runtime. It observes, attributes, presents, recovers after disconnects, and provides audit jumps into raw runtime facts.

It does not create bridge windows, create teams or tasks, modify ledgers, modify frozen semantics, control retries, decide completion, or inject context into agents.

## Runtime Sources

Primary live sources:

- `sdk_stream_events.jsonl`
- `bridge_packets.jsonl`
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

Current SSH development startup uses two remote terminals. Start the outer host from the target repo:

```bash
cd /data03/liang/mjy/safe_opd
python3 ../.claude/control/runtime/outer_sdk_host.py \
  --control-root ../.claude/control \
  --repo-root . \
  --main-session-id outer-main \
  --adapter auto
```

Then start the Companion gateway:

```bash
cd /data03/liang/mjy/bridge-companion
export BRIDGE_OUTER_HOST_URL="http://127.0.0.1:8791"
node gateway/server.mjs
```

From the local machine, forward both ports through SSH:

```bash
ssh -L 8787:127.0.0.1:8787 -L 8791:127.0.0.1:8791 root@10.26.128.46
```

If `8791` is already in use, check the live host before restarting it:

```bash
curl -s http://127.0.0.1:8791/v1/status | python3 -m json.tool | grep -E '"adapter"|"run_id"|"started_at"'
```

For the current custom-provider path the expected adapter is `claude-tmux-repl`. The gateway debug page and terminal runner can confirm the same status through `http://127.0.0.1:8787/api/debug`.

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
- `BRIDGE_COMPANION_TOKEN`: optional bearer/query/header token required for `/api/*`
- `BRIDGE_COMPANION_ORIGIN`: optional CORS allow origin; default `http://127.0.0.1:<port>`
- `BRIDGE_OUTER_HOST_URL`: optional long-lived outer host URL, for example `http://127.0.0.1:8791`; enables `/api/leader/input` forwarding
- `BRIDGE_OUTER_HOST_TOKEN`: optional token forwarded to the outer host as `x-bridge-outer-host-token`
- `BRIDGE_COMPANION_ALLOW_PROJECT_SECRET=1`: allow loading `bridge-companion/key.json` for `/api/brief`. By default the gateway only uses `BRIDGE_BRIEF_API_KEY`.

The gateway is a read-only projection layer. It redacts common token/password/secret patterns before responses and only serves files from the configured runtime roots and the Companion static directory.
When `/api/leader/input` is enabled, the gateway only forwards the user message to the separate outer host. The outer host records runtime facts; Companion still does not own policy, routing, completion, or recovery truth.

Projection fixture check:

```powershell
npm run test:projection
npm run test:leader-input
```

## Read-Only API

```text
GET /api/repos
GET /api/repos/:repoKey
GET /api/repos/:repoKey/runs
GET /api/repos/:repoKey/runs/latest
GET /api/repos/:repoKey/runs/:runId/status
GET /api/repos/:repoKey/runs/:runId/snapshot
GET /api/repos/:repoKey/runs/:runId/events?after=<seq>&afterId=<eventId>&afterCursor=<json>&limit=500
GET /api/repos/:repoKey/runs/:runId/projection
GET /api/repos/:repoKey/runs/:runId/stream?after=<seq>&afterId=<eventId>&afterCursor=<json>
GET /api/repos/:repoKey/runs/:runId/raw?file=<jsonl>&offset=<line>
GET /api/session-observer/events?after=<seq>&afterId=<eventId>&afterCursor=<json>&limit=500
GET /api/session-observer/stream?after=<seq>&afterId=<eventId>&afterCursor=<json>
POST /api/leader/input
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

When source records carry `runtime_event`, the UI treats that as the canonical normalized envelope. Companion records must keep `authority=projection` or `authority=observed`; they are never authoritative runtime state.

`/projection` returns `companion_projection.v1`: active task, timeline, live tool cards, agent message cards, artifact cards, completion checklist, failure/retry lane, semantic coverage matrix, and raw JSON refs. It is derived display data and must not be used for workflow recovery.

The SSE path uses a per-source JSONL tailer after the initial backfill. It tracks byte and line cursors per source file, so new observer lines can reach the UI without repeatedly re-reading whole long logs.

If a tailed source file is truncated or rewritten, the gateway resets that file cursor and emits a `gateway_warning` SSE event. The UI displays the warning as a fact and does not infer task failure.

SDK stream and outer-host boundary events are displayed in the discussion lane. SDK `tool_use` / `tool_result` / `input_json_delta` events are never rendered as real Read/Edit/Bash cards; those cards still require hook `tool_events.jsonl`.

The activity lanes are filters, not exclusive state buckets:

- Tools: `hook_tool_event`
- Discussion: `sdk_stream`, `outer_host`, or `agent_message`
- Reports: `teammate_report` or `artifact`
- Processes: `process_event`
- Completion: `completion_check`
- Failures: failed/blocked status or failed/rejected kind

The Detail Inspector shows Raw JSON, Normalized Event, Related Trajectory, and Related Source Cursor for the selected item.
