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

## SSH Development Startup

For the current remote development layout, open an SSH session that forwards both the UI gateway and the outer host:

```bash
ssh -L 8787:127.0.0.1:8787 -L 8791:127.0.0.1:8791 root@10.26.128.46
```

In one remote terminal, start the outer host from the target repo:

```bash
cd /data03/liang/mjy/safe_opd
python3 ../.claude/control/runtime/outer_sdk_host.py \
  --control-root ../.claude/control \
  --repo-root . \
  --adapter auto
```

In another remote terminal, start the Companion gateway:

```bash
cd /data03/liang/mjy/bridge-companion
export BRIDGE_OUTER_HOST_URL="http://127.0.0.1:8791"
node gateway/server.mjs
```

If a port is already occupied, inspect the live process before starting another copy:

```bash
curl -s http://127.0.0.1:8791/v1/status | python3 -m json.tool | grep -E '"adapter"|"run_id"|"started_at"'
```

The healthy custom-provider path reports `"adapter": "claude-tmux-repl"`. `Address already in use` on `8791` usually means the outer host is already running; it is only a problem if `/v1/status` reports the wrong adapter or stale configuration.

## Outer Host Input Path

Run the long-lived outer host separately from Companion:

```powershell
cd C:\Users\admin\Desktop\Structure-config-1\<target-repo>
python ..\.claude\control\runtime\outer_sdk_host.py --control-root ..\.claude\control --repo-root . --adapter auto
```

The host derives the Claude launch wrapper from the target repo structure: it first looks for `../.claude` relative to `--repo-root`, sets the Claude subprocess `HOME` to that parent directory, and loads `../.claude/mcp.json`. You do not need to export an interactive `claude_mjy` alias for the host.

Run recovery is keyed by the selected RunBridge `run_id`. The outer host resolves that run's native Claude Code session UUID from runtime truth and relaunches Claude with `--resume <session-id>` when it must recreate the outer leader. New runs use `--session-id <session-id>`. Avoid fixed placeholders such as `outer-main`; `--continue` is also not suitable because it follows the most recent cwd session rather than the selected run.

Before sending any model request, verify the effective startup plan:

```powershell
python ..\.claude\control\runtime\outer_sdk_host.py --control-root ..\.claude\control --repo-root . --diagnose-startup
```

The healthy structural path reports `verdict.status=ok`, `checks.settings_arg_mode=home`, `checks.process_env_provider_overrides=false`, `effective_options.cli_home=<repo parent>`, and `effective_options.cli_mcp_config=<repo parent>\.claude\mcp.json`.

For gateway-side diagnostics from the terminal, run:

```powershell
npm run debug
```

The command first queries the live gateway at `http://127.0.0.1:8787/api/debug`, so it can show the actual 8787 process environment and the proxied 8791 `startup_diagnostics`. If the gateway is not running, it falls back to a local read-only snapshot. Use `npm run debug -- --local` for local-only mode, or `npm run debug -- --gateway http://127.0.0.1:8787 --strict-live` to require a live gateway response.

To include the latest run's recent `sdk_stream_events.jsonl`, `outer_host_events.jsonl`, and `tool_events.jsonl` records in the same terminal output:

```powershell
npm run debug -- --strict-live --events --event-limit 80
```

The same gateway-side command runner is available in the UI under `Detail Inspector` -> `Terminal`. It posts to `/api/debug/terminal`, runs inside the 8787 gateway process environment, redacts known provider and bridge secrets, and is enabled by default only when the gateway binds to loopback. Set `BRIDGE_COMPANION_ENABLE_TERMINAL=0` to disable it or `BRIDGE_COMPANION_ENABLE_TERMINAL=1` to explicitly enable it for a non-loopback bind.

Treat the Detail Inspector terminal as a live diagnostics entrypoint, not a safe test sandbox. It inherits the active gateway process environment, including `BRIDGE_OUTER_HOST_URL`, and commands can interact with the same runtime session the user is watching. Use it for short read-only checks such as `pwd`, `ps`, `curl /api/debug`, `sha256sum`, or file inspection. Run projection fixtures and gateway lifecycle tests from a normal development shell or a throwaway Companion process; if a remote fixture must be run through the terminal, clear live forwarding variables first.

Then start Companion with a forwarding target:

```powershell
$env:BRIDGE_OUTER_HOST_URL="http://127.0.0.1:8791"
node gateway\server.mjs
```

`POST /api/leader/input` forwards user text to the outer host. Companion does not write ledgers directly; the host records the authoritative runtime input event and SDK-observed stream records.

`--adapter auto` uses the SDK adapter for first-party/default Claude API paths. For custom-provider `ANTHROPIC_BASE_URL` setups on Linux with `tmux` available, it uses the interactive TTY adapter so Companion follows the same Claude Code entrypoint as the working shell alias. Set `BRIDGE_OUTER_LEADER_AUTO_TMUX=0` or start with `--adapter sdk` to force SDK-first behavior. Use `--adapter unavailable` only for debug/smoke paths where the host boundary is being tested without model execution.

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
