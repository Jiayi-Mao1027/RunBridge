# Bridge Companion Startup

## Goal

Run Bridge Companion as a separate, read-only companion surface without changing the existing agent/runtime system.

## Local Development

From the `bridge-companion` folder:

```powershell
cd C:\Users\admin\Desktop\Structure-config-1\bridge-companion
node gateway\server.mjs
```

Then open:

```text
http://127.0.0.1:8787/
```

Without `BRIDGE_RUNTIME_ROOT`, the UI still opens with mock data. This is useful for UI/copy work.

## Outer Host Input Path

Run the long-lived outer host separately from Companion:

```powershell
cd C:\Users\admin\Desktop\Structure-config-1\<target-repo>
python ..\.claude\control\runtime\outer_sdk_host.py --control-root ..\.claude\control --repo-root . --main-session-id outer-main --adapter auto
```

Then start Companion with a forwarding target:

```powershell
$env:BRIDGE_OUTER_HOST_URL="http://127.0.0.1:8791"
node gateway\server.mjs
```

`POST /api/leader/input` forwards user text to the outer host. Companion does not write ledgers directly; the host records the authoritative runtime input event and SDK-observed stream records.

`--adapter auto` is SDK-first and keeps one process-owned outer leader SDK client alive. It requires the optional `claude-agent-sdk` Python package. If that package is missing, the host still records the input and returns `OuterLeaderSdkDependencyMissing` rather than falling back to a one-shot bridge-like call. Use `--adapter unavailable` only for debug/smoke paths where the host boundary is being tested without model execution.

## Reading Real Runtime Data

Point the gateway at a run root folder for hydration/backfill/audit, and when available connect it to the live SDK/hook stream source. During the transition, the gateway can tail observer JSONL files that emulate live event envelopes:

```text
runtime_snapshot.json
event_log.jsonl
main_leader_inbox.jsonl
tool_events.jsonl
session_events.jsonl
companion_events.jsonl
```

Example:

```powershell
$env:BRIDGE_RUNTIME_ROOT="C:\path\to\.claude\runtime_state\projects\<repo-key>\runs"
$env:BRIDGE_COMPANION_PORT="8787"
node gateway\server.mjs
```

The gateway remains read-only. It only serves `GET`, `HEAD`, and `OPTIONS` for runtime routes; `/brief` is the only POST endpoint and only summarizes already-provided UI facts. It must never call bridge tools, create teams/tasks, or mutate runtime ledgers.

## Remote Server + Cursor Mapping

If the UI is deployed remotely but runtime files are local, use this topology:

```text
local runtime files
  -> local gateway on 127.0.0.1:8787
  -> Cursor port mapping / tunnel
  -> remote Bridge Companion UI
```

The remote UI should call the mapped gateway URL as its data source. The mapped gateway must be treated as observation-only.

Recommended Cursor mapping intent:

```text
Forward local 127.0.0.1:8787 to the remote environment as the Bridge Companion data gateway.
```

If the mapping breaks, the UI must say:

```text
当前无法读取本地 runtime 网关。任务真实状态未知；这不代表远端任务失败或停止。
```

## API

Read-only endpoints:

```text
GET /health
GET /runs
GET /runs/:runId/status
GET /runs/:runId/events
GET /runs/:runId/inbox
GET /runs/:runId/artifacts
GET /runs/:runId/stream
```

No write/control endpoints exist. Requests using methods other than `GET`, `HEAD`, or `OPTIONS` return 405.

## UI Usage

Open the prototype page, enter a run id, then click:

```text
从网关读取状态
```

If the gateway or run id is unavailable, the UI falls back to an explicit gateway-disconnected state rather than guessing runtime status.

## Deployment Notes

For early development, keep the gateway bound to localhost:

```text
127.0.0.1:8787
```

Do not expose the gateway publicly without authentication. Even though it is read-only, runtime logs may contain sensitive implementation details.

## Boundary Reminder

Bridge Companion must not import or execute Bridge Runtime control code as a controller. It should consume SDK/hook stream taps, exported files, or read-only adapters only. Runtime JSON is a fallback/audit surface, not the realtime UI source.

The original agent system remains authoritative. Bridge Companion is a viewer.
