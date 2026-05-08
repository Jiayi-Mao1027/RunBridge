# Bridge Companion

Bridge Companion 是一个系统外、只读、事实驱动的远程运行观演层。它用于观察 Claude Code Bridge Runtime 的实时 SDK stream、SDK hooks stream，以及用于审计/恢复/补洞的 runstate JSON，把后台执行状态翻译成用户可读的自然语言进度。

它不控制任务，不参与决策，不修改 runtime，不污染系统内 agent 的上下文。视觉可以采用游戏化、像素风、暗黑奇幻任务板等主题，但主信息必须严格来自已观测事实。实时 UI 优先来自 SDK stream / hooks stream；runtime_snapshot、event_log、ledger、observer JSONL 等 runstate 文件只作为 hydration、backfill、审计确认和断线恢复来源。

核心原则：

```text
视觉可以游戏化，信息不能游戏化。
```

更具体地说：

```text
像巫师三任务板一样好看，但像运行日志一样诚实。
```

## 目录

```text
bridge-companion/
  README.md
  package.json
  docs/
    architecture.md
    copy-principles.md
    runtime-status-model.md
    remote-gateway.md
    startup.md
    visual-direction.md
    interaction-plan.md
    interaction-rules.md
    brief-api.md
  gateway/
    server.mjs
  prototype/
    index.html
```

## 边界

Bridge Companion 的事实源分层如下：实时主源是 bridge SDK stream 与 SDK hooks stream；补充/审计源是 runtime_snapshot、event_log、main_leader_inbox、bridge result、artifact/report 引用和 observer JSONL。UI 不应把 runstate JSON 当成实时主源。

Bridge Companion 不做这些事：发起 bridge 调用、创建 team/task、向 teammate 发消息、修改 workflow ledger、修改 agent prompt、改写 frozen semantics、把运行事实写回系统上下文。

如果需要智能解释能力，应作为 UI 外挂 API 引入，并且只在 Companion 的解释层工作。该 API 只能基于已提供的 runtime facts 生成解释，不得影响 Bridge Runtime，也不得向系统内 agent 注入上下文。

## 启动

本地查看原型和只读网关：

```powershell
cd C:\Users\admin\Desktop\Structure-config-1\bridge-companion
node gateway\server.mjs
```

打开：

```text
http://127.0.0.1:8787/
```

如果要读取真实 runtime，把 `BRIDGE_RUNTIME_ROOT` 指向 runs 目录：

```powershell
$env:BRIDGE_RUNTIME_ROOT="C:\path\to\.claude\runtime_state\projects\<repo-key>\runs"
node gateway\server.mjs
```

远程部署时，通过 Cursor 端口映射或隧道把本地 `127.0.0.1:8787` 暴露给远程 UI。这个 gateway 只有只读接口，不提供控制接口。