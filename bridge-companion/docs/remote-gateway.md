# Remote Deployment and Local Gateway Plan

## Problem

Bridge Companion may run on a remote server, while the Bridge Runtime and runtime ledgers remain on the local machine. The UI still needs live status, but it must not expose or control the original agent system.

The required shape is:

```text
remote Bridge Companion UI
  -> mapped connection through Cursor / local tunnel
  -> local read-only gateway
  -> local SDK/hook live streams, plus runtime snapshot and event files for backfill
```

This keeps Bridge Companion outside the agent system while still allowing a remote deployment to observe local runtime state.

## Principle

The gateway is a read-only observation gateway. It must never become a control gateway.

Allowed:

```text
subscribe to bridge SDK stream events
subscribe to SDK hooks stream events
stream UI-safe live event envelopes
read runtime snapshots for hydration/recovery
read event logs and inbox notifications for backfill/audit
read bridge results for final confirmation
normalize derived status for UI fallback
```

Forbidden:

```text
dispatch workflow events
call bridge SDK
build bridge packets
create teams
create tasks
write runtime ledgers
modify agent prompts
send instructions into system agents
```

## Recommended Topology

```text
[Local Machine]
  Bridge Runtime files
      |
      v
  Local Companion Gateway :8787
      |
      | Cursor port mapping / SSH tunnel / dev tunnel
      v
[Remote Server]
  Bridge Companion web app
      |
      v
[Browser]
```

The remote server calls the mapped gateway URL as a data source. If the mapping is unavailable, the UI should report gateway disconnection, not task failure.

## Gateway API

Suggested read-only endpoints:

```text
GET /health
GET /runs
GET /runs/:runId/status
GET /runs/:runId/events
GET /runs/:runId/inbox
GET /runs/:runId/artifacts
GET /runs/:runId/stream
```

`/status` should return the normalized CompanionStatus model rather than raw internal control objects where possible.

`/stream` should use SSE for stream-first live updates. It should emit append-only event envelopes first, and may also emit derived status patches for compatibility:

```text
event: companion_event
id: <stream-sequence>
data: { source: "sdk" | "hook" | "runstate", kind, runId, bridgeWindowId, actorId, toolUseId, timestamp, payloadPreview, refs }

event: status
id: status-<stream-sequence>
data: { ...CompanionStatus }
```

`status` is a derived view and fallback surface. It is not the primary realtime feed.

## Gateway State vs Runtime State

The UI must separate gateway connectivity from task runtime status.

Example:

```text
网关状态：已连接
runtime 状态：等待执行结果
```

If the gateway is down:

```text
网关状态：断开
runtime 状态：未知
当前无法读取本地 runtime 网关。任务真实状态未知；这不代表远端任务失败或停止。
```

If runtime says failed while gateway is healthy:

```text
网关状态：已连接
runtime 状态：执行失败
本轮 bridge window 已返回失败结果。失败阶段、错误信息和可用证据已记录在 runtime 中。
```

## Security Notes

Do not expose the gateway publicly without authentication. During early development, prefer Cursor mapping or a trusted tunnel bound to localhost.

The gateway should bind locally by default:

```text
127.0.0.1:8787
```

It should reject non-GET methods for runtime routes. If future write routes are ever needed, they should live in a separate tool or admin service, not in Bridge Companion.

## Realtime Observation

The UI should prefer `GET /runs/:runId/stream` for live updates. The stream is event-first: SDK stream events and hooks stream events should be forwarded as UI-safe envelopes as soon as they are observed. If direct SDK/hook streaming is unavailable during transition, the gateway may tail observer files such as `tool_events.jsonl`, `session_events.jsonl`, `agent_messages.jsonl`, and `companion_events.jsonl` to emulate the same event envelope shape. If `EventSource` or the tunnel does not support SSE, the UI may fall back to `GET /runs/:runId/status` polling.

The realtime feed is still read-only. `runtime_snapshot.json`, `event_log.jsonl`, `main_leader_inbox.jsonl`, bridge results, and artifact/report refs are secondary sources for hydration, missed-event backfill, lifecycle confirmation, and audit details. They should not replace SDK/hook streams as the main live UI source.

This allows the UI to show direct session operations when hooks have recorded them, including tool names, actors, and file references. It does not rely solely on final reports. If these side-channel files do not exist or lack enough detail, the honest UI state is "unknown". Capturing additional internal operations would require adding or widening read-only hook/observer instrumentation in the Bridge Runtime; Companion itself must not make that system change without explicit approval.

## Optional Intelligence API

If the remote UI wants smarter explanations, add a separate explanation API on the remote side:

```text
CompanionStatus
  -> explanation API
  -> constrained explanation text
```

The explanation API must only receive normalized facts and unknowns. It must not connect to the local runtime gateway with write access, and it must not communicate with system agents.

Recommended prompt boundary:

```text
You are explaining runtime facts for display. Use only the provided facts. Do not infer file-level progress unless present. Do not rename agent roles in authoritative status copy. Always preserve unknowns.
```