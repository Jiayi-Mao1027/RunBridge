# Bridge Companion SDK Stream Handoff

这份交接给系统/runtime 侧负责人。Bridge Companion UI 侧已经完成第一轮方向纠偏：实时 UI 现在优先消费 SDK stream / SDK hooks stream 形态的 live event envelope；runstate JSON、ledger、snapshot、observer JSONL 只作为 hydration、backfill、审计确认和断线恢复来源。

当前阻塞点是 runtime 侧还没有真正写出 `sdk_stream_events.jsonl`。Companion gateway 已经准备好优先读取这个文件，所以系统侧只需要把 bridge 执行过程中的 SDK/CLI stream tap 写出来。

## 目标

把 bridge executor 从“只在最后拿到 structured JSON result”升级为“执行过程中持续写 UI-safe SDK stream 事件，同时保留最终 result 解析”。

当前关键文件是：

```text
.claude/control/runtime/claude_cli_executor.py
```

当前关键调用点在 `claude_cli_team_executor()` 中，大约是这段：

```python
proc = subprocess.run(
    cmd,
    cwd=str(project_root),
    capture_output=True,
    text=True,
    encoding="utf-8",
    timeout=_timeout_seconds(packet),
    env=env,
)
```

这段同步 `subprocess.run()` 会等 Claude CLI 完整结束后才拿 stdout/stderr，因此 UI 无法实时看到 SDK/CLI stream。请把它改成可流式采集的执行路径。

## 要写出的文件

系统侧应在 bridge 执行期间写出：

```text
.claude/runtime_state/projects/<repo-key>/runs/<run_id>/sdk_stream_events.jsonl
.claude/runtime_state/session_observer/sdk_stream_events.jsonl
```

run-scoped 文件用于当前 run 的 Companion 展示；session observer 文件用于 unbound/direct fallback。Bridge Companion gateway 已经读取这两个位置。

## 事件格式

每行是一个 UI-safe JSON object。建议字段如下：

```json
{
  "timestamp": "2026-05-09T00:00:00.000000+00:00",
  "event_type": "sdk_stream_delta",
  "stream_source": "sdk",
  "run_id": "run_xxx",
  "main_session_id": "...",
  "sub_session_id": "...",
  "bridge_window_id": "...",
  "team_id": "...",
  "task_id": "...",
  "session_id": "...",
  "agent_id": "bridge-leader",
  "agent_type": "bridge-leader",
  "status": "streaming",
  "message_preview": "bounded redacted text",
  "payload_keys": ["type", "subtype", "stop_reason"],
  "sequence": 1,
  "monotonic_index": 1
}
```

`event_type` 建议至少支持：

```text
sdk_stream_started
sdk_stream_delta
sdk_stream_tool_use
sdk_stream_tool_result
sdk_stream_assistant_text
sdk_stream_stderr
sdk_stream_final
sdk_stream_error
sdk_stream_timeout
```

如果 CLI 输出不是逐行 JSON，也可以先把每行作为 bounded text preview 写入 `sdk_stream_delta`。后续再细分工具、消息、delta 类型。

## 安全要求

这个 stream 是给 UI 看的，不是审计原文仓库。必须 UI-safe：

```text
不要写完整 prompt
不要写完整大 stdout/stderr
不要写 secrets
不要写完整文件内容
不要写完整 tool input/output 中的大块内容
message_preview 建议限制在 1000 字符以内
```

至少要 redaction：

```text
api_key=...
token=...
password=...
secret=...
sk-...
```

可以复用 hooks 侧已有的 redaction 思路：`.claude/hooks/common.py` 里的 `redact_observer_text()`。

## 建议实现形态

在 `claude_cli_executor.py` 增加 helper：

```python
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sdk_stream_event_paths(project_root: Path, execution_input: dict[str, Any]) -> list[Path]:
    run_id = _safe_path_component(str(execution_input.get("run_id") or "run"))
    return [
        _control_claude_dir()
        / "runtime_state"
        / "projects"
        / _project_state_key(project_root)
        / "runs"
        / run_id
        / "sdk_stream_events.jsonl",
        _control_claude_dir()
        / "runtime_state"
        / "session_observer"
        / "sdk_stream_events.jsonl",
    ]


def _emit_sdk_stream_event(
    project_root: Path,
    execution_input: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
    *,
    status: str = "streaming",
) -> None:
    # build UI-safe record, append_jsonl to both paths
```

需要 import：

```python
import threading
from datetime import datetime, timezone
from persist import append_jsonl, sanitize_json_value
```

然后用类似下面的函数替代 `subprocess.run()`：

```python
def _run_claude_streaming(cmd, project_root, env, timeout, execution_input):
    proc = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    stdout_parts = []
    stderr_parts = []

    _emit_sdk_stream_event(project_root, execution_input, "sdk_stream_started", {"cmd_preview": _redact_cmd(cmd)})

    def read_stdout():
        for line in proc.stdout or []:
            stdout_parts.append(line)
            parsed = _parse_json_object_text(line) or {}
            payload = parsed if parsed else {"text": line}
            _emit_sdk_stream_event(project_root, execution_input, "sdk_stream_delta", payload)

    def read_stderr():
        for line in proc.stderr or []:
            stderr_parts.append(line)
            _emit_sdk_stream_event(project_root, execution_input, "sdk_stream_stderr", {"text": line})

    t_out = threading.Thread(target=read_stdout, daemon=True)
    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        _emit_sdk_stream_event(project_root, execution_input, "sdk_stream_timeout", {"timeout_seconds": timeout}, status="failed")
        raise

    t_out.join(timeout=2)
    t_err.join(timeout=2)

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    _emit_sdk_stream_event(project_root, execution_input, "sdk_stream_final", {"returncode": returncode}, status="completed" if returncode == 0 else "failed")

    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
```

然后原来的：

```python
proc = subprocess.run(...)
```

替换为：

```python
proc = _run_claude_streaming(cmd, project_root, env, _timeout_seconds(packet), execution_input)
```

后续 `proc.returncode`、`proc.stdout`、`proc.stderr`、`_parse_claude_payload(proc.stdout, proc.stderr)` 仍然保持兼容。

## payload 提取建议

`_emit_sdk_stream_event()` 不应该直接把完整 payload 写出去。建议只写：

```python
payload_keys = sorted(str(k) for k in payload.keys())[:20]
message_preview = _sdk_message_preview(payload)
```

`_sdk_message_preview()` 可以从这些字段取 bounded text：

```text
text
result
message
content[].text
summary
stop_reason
subtype
type
```

如果是 tool_use block，只写 tool name / id / input keys，不写完整 input。

## Companion 侧已完成的适配

`bridge-companion/gateway/server.mjs` 已经：

```text
1. 优先读取 run/<run_id>/sdk_stream_events.jsonl
2. 优先读取 session_observer/sdk_stream_events.jsonl
3. 把这类事件标记为 streamSource: "sdk"
4. 在 /runs/:runId/stream 中先发 event: companion_event，再发兼容 event: status
5. 保留 tool_events/session_events/event_log/runtime_snapshot 作为 fallback/backfill/audit
```

前端 prototype 已经监听：

```text
event: companion_event
event: status
```

所以 runtime 侧只要开始写 `sdk_stream_events.jsonl`，UI 就会优先显示 SDK stream。

## 验收标准

系统侧完成后，运行一次真实 bridge，应该能看到：

```text
.claude/runtime_state/projects/<repo-key>/runs/<run_id>/sdk_stream_events.jsonl 存在
session_observer/sdk_stream_events.jsonl 存在或至少在 unbound 场景存在
文件中有 sdk_stream_started / sdk_stream_delta / sdk_stream_final
Bridge Companion 的活动流优先出现 source=sdk_stream_events 或 global_sdk_stream_events 的事件
最终 bridge result 解析仍然成功，不破坏 reports/artifact_refs/error_or_null/cleanup_required 结构
```

如果 streaming parse 出现噪声，先保证 bounded `sdk_stream_delta` 可用；精细分类可以后续迭代。
