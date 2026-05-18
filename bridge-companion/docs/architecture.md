# Bridge Companion Architecture

Bridge Companion is outside the Bridge Runtime control loop. It is a read-only observation layer with five responsibilities:

- observe runtime streams
- attribute activity to repo, run, bridge window, team, task, teammate, and session
- present current activity and history
- recover after disconnects by backfilling from JSONL files
- jump to raw runtime facts for audit

It is forbidden to:

- create bridge windows
- create teams or tasks
- write ledgers or snapshots
- modify frozen semantics
- control retry or routing
- decide task completion
- inject context into system agents

## Data Flow

```text
.claude/runtime_state/registry/*.json
  -> repo/run discovery
.claude/runtime_state/projects/<repo-key>/runs/<run-id>/*.jsonl
  -> gateway source readers and source-file tailers
  -> normalized CompanionEvent
  -> REST backfill and SSE live stream
  -> UI reducer
  -> team tree, activity stream, detail inspector, trajectory tab
```

Global unbound or direct-session activity is read from:

```text
.claude/runtime_state/session_observer/*.jsonl
```

The gateway never calls Bridge MCP tools and never writes runtime files.

## Multi-Repo Layout

The gateway first reads:

```text
.claude/runtime_state/registry/repos.json
.claude/runtime_state/registry/active_runs.json
```

If registry files are missing, it falls back to scanning:

```text
.claude/runtime_state/projects/
```

Each child directory is treated as one `repoKey`; each `repoKey/runs/<run-id>` directory is one run.

`latest` is repo-local. Registry `latest_run_id` and `active_run_ids` take priority; snapshot, ledger, and directory mtime are fallback ordering signals. It is never global across all repos.

## Normalized Event Sources

| Source file | Companion source | Display rule |
| --- | --- | --- |
| `sdk_stream_events.jsonl` | `sdk_stream` | discussion lane; assistant text, StreamEvent deltas, SDK tool declarations/results, and input JSON deltas |
| `outer_host_events.jsonl` | `outer_host` | discussion lane; host input/result boundary events, source/projection only |
| `tool_events.jsonl` | `hook_tool_event` | only source of tool started/completed/failed cards |
| `agent_messages.jsonl` | `agent_message` | assignment cards |
| `teammate_reports.jsonl` | `teammate_report` | report cards |
| `process_events.jsonl` | `process_event` | process state cards |
| `artifacts.jsonl` | `artifact` | artifact cards |
| `completion_checks.jsonl` | `completion_check` | completion status cards |
| `transitions.jsonl` | `runtime_snapshot` | lifecycle transitions |
| `trajectory.jsonl` | `runtime_snapshot` | trajectory steps and audit context |

The UI does not invent discussion text. If a run has tool events but no SDK text, outer-host event, agent message, or report text, the UI shows that only tool activity was captured.

SDK stream classification is explicit:

- `sdk_stream_assistant_text`, `content_block_delta`, or text delta fields -> discussion text
- `sdk_stream_tool_use` -> SDK tool declaration in discussion lane
- `sdk_stream_tool_result` -> SDK tool result in discussion lane
- `input_json_delta` / partial JSON fields -> accumulated SDK tool input preview in discussion lane

Real Read/Edit/Bash cards still only come from hook `tool_events.jsonl`.

## Transport

REST endpoints provide discovery, hydration, backfill, and raw-record lookup.

SSE endpoints provide live read-only updates:

```text
GET /api/repos/:repoKey/runs/:runId/stream?afterCursor=<json>
GET /api/session-observer/stream?afterCursor=<json>
```

The browser reconnects with `afterCursor`, a map of `sourceFile -> lineOffset`. `afterId` and `Last-Event-ID` are accepted as fallbacks. Events are deduplicated by stable `eventId`, not display `seq`.

Each SSE connection performs one backfill pass and then switches to per-source JSONL tailers. Tailers track byte offset and line offset for every source file.

Partial JSONL writes are buffered until a newline completes the record. If a file size moves backwards, the gateway treats it as truncate/rewrite, resets that source cursor, and emits `gateway_warning`. The UI displays this as a gateway warning only; it does not infer runtime failure.

## UI Structure

The prototype uses three panes:

- Repo / run / team tree
- Live activity stream with lane filters: All, Tools, Discussion, Reports, Processes, Completion, Failures
- Detail inspector with raw, trajectory, and model brief tabs

The team tree combines `session_bindings.jsonl`, `active_operations.json`, and hook tool events. A teammate running Bash is shown as running only when hook tool events or active operations say so. A report alone is displayed as a report, not as a fabricated tool action.

Each teammate card may show current tool, last completed tool, last discussion text, last report, and current blocker. Missing discussion is displayed as missing; it is not inferred from reports or tool names.

Lane filters are multi-membership predicates:

- Tools: `source == hook_tool_event`
- Discussion: `source == sdk_stream || source == outer_host || source == agent_message`
- Reports: `source == teammate_report || source == artifact`
- Processes: `source == process_event`
- Completion: `source == completion_check`
- Failures: `status in {failed, blocked}` or `kind` contains `failed` / `rejected`

The Detail Inspector exposes four audit blocks for every selected event: Raw JSON, Normalized Event, Related Trajectory, and Related Source Cursor.

## Trajectory

When `trajectory.jsonl` exists, the trajectory tab shows:

- step index
- actor
- action
- observation
- state delta
- evidence refs
- raw refs

Selecting a completion, process, artifact, or tool event filters related trajectory steps by run/window/task IDs and evidence refs.

## Model Brief

The optional model brief endpoint is display-only.

Input:

- normalized events
- snapshot/status data
- unknowns

Output:

- explanatory copy
- unknowns

Forbidden:

- status decisions
- retry decisions
- route decisions
- completion decisions
- runtime writes
- agent instructions

If the brief fails, the main UI state remains unchanged.
