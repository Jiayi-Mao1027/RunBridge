# SDK Stream Tap Patch Plan

Bridge Companion has been switched to a stream-first gateway/API shape, but the runtime executor file under `.claude/control/runtime/claude_cli_executor.py` is protected from this editing session. The following patch is the intended next runtime-side change.

## Target

`claude_cli_executor.py` currently uses `subprocess.run(..., capture_output=True)` and only receives the final JSON envelope. Replace that with a streaming subprocess path that writes UI-safe SDK stream records as lines arrive, while preserving the final stdout/stderr behavior used by `_parse_claude_payload`.

## Event files

Write events to both locations when possible:

```text
.claude/runtime_state/projects/<repo-key>/runs/<run_id>/sdk_stream_events.jsonl
.claude/runtime_state/session_observer/sdk_stream_events.jsonl
```

The run-scoped file is for the active run; the session observer file is for unbound/direct fallback. Bridge Companion already looks for both.

## Event envelope

Each line should be UI-safe and append-only:

```json
{
  "timestamp": "2026-05-09T...Z",
  "event_type": "sdk_stream_delta | sdk_stream_final | sdk_stream_stderr | sdk_stream_error",
  "stream_source": "sdk",
  "run_id": "...",
  "main_session_id": "...",
  "sub_session_id": "...",
  "bridge_window_id": "...",
  "team_id": "...",
  "task_id": "...",
  "session_id": "...",
  "agent_type": "bridge-leader",
  "status": "streaming | completed | failed",
  "message_preview": "redacted bounded text",
  "payload_keys": ["type", "subtype", "stop_reason"],
  "sequence": 1
}
```

Do not write full prompts, full stdout, secrets, or large tool outputs. Redact `api_key`, `token`, `password`, `secret`, and `sk-*` tokens. Bound `message_preview` to roughly 1000 characters.

## Minimal code shape

Add helpers near the executor utilities:

```python
def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _sdk_stream_path(project_root, execution_input):
    run_id = _safe_path_component(str(execution_input.get("run_id") or "run"))
    return _control_claude_dir() / "runtime_state" / "projects" / _project_state_key(project_root) / "runs" / run_id / "sdk_stream_events.jsonl"

def _session_sdk_stream_path():
    return _control_claude_dir() / "runtime_state" / "session_observer" / "sdk_stream_events.jsonl"

def _emit_sdk_stream_event(project_root, execution_input, event_type, payload, status="streaming"):
    # build UI-safe record, append to both paths with append_jsonl
```

Then replace the current `subprocess.run` block with a function like:

```python
def _run_claude_streaming(cmd, project_root, env, timeout, execution_input):
    proc = subprocess.Popen(cmd, cwd=str(project_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    stdout_parts, stderr_parts = [], []

    def read_stdout():
        for line in proc.stdout or []:
            stdout_parts.append(line)
            parsed = _parse_json_object_text(line) or {}
            _emit_sdk_stream_event(project_root, execution_input, "sdk_stream_delta", parsed or {"text": line})

    def read_stderr():
        for line in proc.stderr or []:
            stderr_parts.append(line)
            _emit_sdk_stream_event(project_root, execution_input, "sdk_stream_stderr", {"text": line}, status="streaming")

    # join with timeout, kill on timeout, return CompletedProcess-like object
```

If streaming parse of the CLI output is noisy, still emit bounded `sdk_stream_delta` previews. The final result must remain compatible with `_parse_claude_payload(proc.stdout, proc.stderr)`.

## Gateway status

`bridge-companion/gateway/server.mjs` now prioritizes `sdk_stream_events.jsonl` and labels the current adapter as `sdk_stream_tap_with_observer_fallback` because the runtime-side stream tap has landed. Keep observer JSONL as the fallback/backfill layer.
