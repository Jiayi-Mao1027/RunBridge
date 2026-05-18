# Runtime Status Model

## Purpose

The status model is the safety layer between observed Bridge Runtime facts and the Bridge Companion UI. It is now derived from a live event stream first, then supplemented by runtime JSON for hydration, backfill, audit confirmation, and recovery. The UI should not freely interpret event logs as a realtime source.

The goal is to prevent two failures:

```text
1. turning visual fantasy into factual fantasy
2. guessing internal agent progress that runtime has not reported
```

## CompanionStatus

```ts
type CompanionStatus = {
  runId: string
  taskTitle: string
  phase: string
  lifecycleState: string
  latestEvent: RuntimeEventSummary | null
  lastUpdatedAt: string | null
  bridgeWindowId?: string
  teamId?: string
  taskId?: string
  waiting: boolean
  waitReason?: string
  hasCompletionReport: boolean
  hasArtifacts: boolean
  resultState: "not_started" | "running" | "waiting" | "partial" | "failed" | "succeeded" | "unknown"
  authority: "runtime_fact" | "derived_from_events" | "unknown"
  facts: string[]
  unknowns: string[]
  possibleNextEvents: string[]
  display: CompanionDisplayCopy
}

type RuntimeEventSummary = {
  eventType: string
  timestamp: string
  payloadSummary?: string
}

type CompanionDisplayCopy = {
  title: string
  factText: string
  explanationText: string
  nextStepText: string
  companionNote: string
}
```

## Input Sources

The model should consume live sources first:

```text
bridge SDK stream events
SDK hooks stream events
```

It may then consume secondary sources for hydration, backfill, audit confirmation, and recovery:

```text
runtime_snapshot.json
event_log.jsonl
transitions.jsonl
main_leader_inbox.jsonl
bridge result payloads
artifact/report references
observer JSONL files
```

The model must not consume free-floating agent chat context unless that content appears in SDK stream, hooks stream, runtime reports, artifacts, or audit records.

## Status Mapping

### leader_freeze / semantics frozen

Known facts:

```text
frozen semantics exists
frozen scope exists
no bridge window yet
```

Display:

```text
任务语义已冻结
主控已锁定本轮任务目标、约束和执行范围。下一步将根据当前阶段构建桥接任务包。
```

Unknowns should include:

```text
尚未创建桥接窗口
尚未创建执行团队
尚未收到执行报告或 artifact
```

### bridge_packet_built

Known facts:

```text
BridgePacket generated
phase route selected
target phase selected
completion contract exists
```

Display:

```text
桥接任务包已生成
runtime 已根据当前快照生成本轮 BridgePacket。该任务包包含目标阶段、队员配置、可用工具、完成条件和报告要求。
```

### bridge_window_opened

Known facts:

```text
bridge window opened
bridge leader accepted or is processing packet
```

Display:

```text
桥接窗口已开启
Bridge leader 已接收任务包，并为本轮执行创建独立的桥接窗口。
```

### team_create_completed

Known facts:

```text
team exists
team is bound to the bridge window
```

Display:

```text
执行团队已创建
本轮任务所需的执行团队已经创建完成，后续消息将发送给对应队员。
```

### message_dispatch_completed

Known facts:

```text
task instructions dispatched
team has received task requirements
```

Display:

```text
任务说明已下发
执行团队已收到任务目标、完成条件和报告要求。runtime 正在等待后续执行结果。
```

### team_waiting / TeamIdle

Known facts:

```text
runtime is waiting
completion evidence has not been accepted yet
```

Display:

```text
等待执行结果
当前处于 bridge window 内部等待阶段。runtime 暂未收到新的结构化报告、artifact 或完成信号。
```

Unknowns must include file-level and internal progress if no report provides them:

```text
当前无法确认具体正在修改哪些文件
当前无法确认队员内部执行进度
当前无法确认 completion contract 是否即将满足
```

### artifacts_ready

Known facts:

```text
one or more artifact references exist
```

Display:

```text
已收到阶段性产物
runtime 已记录本轮任务返回的 artifact。是否满足完成条件仍需根据 completion contract 检查。
```

### task_completion_rejected

Known facts:

```text
completion attempt exists
runtime rejected completion evidence
```

Display:

```text
完成条件未满足
队员已有返回，但 runtime 检查发现结果尚未满足本轮 completion contract。
```

### bridge_window_partial_returned

Known facts:

```text
bridge returned partial result
some completion evidence may exist
some required items remain missing or unconfirmed
```

Display:

```text
部分结果返回
本轮 bridge window 返回了部分结果。任务没有完整失败，但仍有未满足或未确认的项目。
```

### task_failed / bridge_call_failed

Known facts:

```text
failure event exists
failure payload may include reason and stage
```

Display:

```text
执行失败
本轮执行已返回失败结果。失败阶段、错误信息和可用证据已记录在 runtime 中。
```

### bridge_window_returned / task_completion_completed

Known facts:

```text
completion accepted
bridge result returned
```

Display:

```text
任务完成
本轮 bridge window 已返回成功结果，并满足当前 completion contract。
```

## Unknowns Policy

Every CompanionStatus should include unknowns. If the UI cannot prove something from runtime data, it should state the absence instead of hiding it.

Examples:

```text
尚未收到 completion report
尚未收到 artifact
当前无法确认具体文件级进展
当前无法确认队员内部执行细节
```

## Possible Next Events

Possible next events must come from lifecycle policy, not optimistic prediction. For prototype purposes, static mappings are acceptable. Production should read the lifecycle transition table through a read-only adapter or a compiled copy.

Example for waiting:

```text
artifacts_ready
task_completion_completed
task_completion_rejected
team_wait_timeout
task_failed
bridge_window_partial_returned
```

## Remote Deployment and Local Gateway Mapping

Bridge Companion may be deployed on a remote server while the Bridge Runtime remains local. In that case, do not expose the runtime control plane directly to the remote UI.

Recommended shape:

```text
local Bridge Runtime files
  -> local read-only gateway
  -> Cursor/port mapping or tunnel
  -> remote Bridge Companion server
  -> browser UI
```

The local gateway should be read-only and narrowly scoped. It should expose normalized status endpoints rather than raw control operations.

Suggested endpoints:

```text
GET /health
GET /runs
GET /runs/:runId/status
GET /runs/:runId/events
GET /runs/:runId/inbox
GET /runs/:runId/artifacts
```

Forbidden endpoints:

```text
POST /bridge/call
POST /tasks
POST /teams
POST /events/dispatch
POST /runtime/update
```

If using Cursor port forwarding or a similar mapping, the remote server should treat the mapped local gateway as a data source only. Authentication should be added before exposing anything outside a trusted development tunnel.

The UI should display gateway state separately from runtime state:

```text
网关状态：已连接 / 断开 / 延迟过高
runtime 状态：等待执行结果 / 执行失败 / 任务完成
```

Gateway failure must not be interpreted as task failure. If the gateway is disconnected, display:

```text
当前无法读取本地 runtime 网关。任务真实状态未知；这不代表远端任务失败或停止。
```