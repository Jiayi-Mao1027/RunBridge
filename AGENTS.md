# Agent Working Notes

This repository is the workflow control plane, not a normal target application repo. Read `README.md` first, then inspect the relevant files under `.claude/` before changing behavior.

## Live SSH RunBridge Stewardship

- Treat the local checkout as the only source-of-truth system version. The SSH side is the live test/deployment surface, not an independent source of truth.
- The local forwarded Companion/debug gateway normally enters through `http://127.0.0.1:8787`; the forwarded outer host status endpoint is `http://127.0.0.1:8791/v1/status`.
- Use `8787` and `8791` first to inspect live system health, active run IDs, bridge windows, runtime snapshots, observer streams, tmux/outer-host status, and Companion diagnostics.
- Judge live system progress in roughly 5-minute intervals while work is actively moving; once the system reaches a steady state or an owned long-running phase, stretch the check interval toward 30 minutes unless evidence suggests trouble.
- Your job is to advance, debug, repair, and update the RunBridge system so it can advance the target project. Do not take over the target project directly just because you can see files on the SSH side.
- Project completion belongs to the RunBridge system and its `leader-orchestrator`/bridge workflow. Push project work by giving instructions to `leader-orchestrator` through the outer host; direct lower-layer shell, tmux, or project-file operations are debugging tools, not the normal project-execution path.
- Live test operations should simulate how a human drives the system. Outside explicit debugging or repair, default to sending messages to `leader-orchestrator` through the `8787` gateway/outer-host path instead of operating lower-level system or project internals yourself.
- You do not need to understand the target project's science or product deeply unless that understanding is needed to diagnose a system failure. Keep attention on system health, routing, lifecycle, observer evidence, and bridge execution correctness.
- When system files must change, patch the local source-of-truth files first, verify locally, then move only the changed file or files to the SSH side. Do not fetch, push, pull, rsync, or otherwise sync the whole repository between local and SSH sides.
- Single-file transfer to the SSH side is allowed when needed; fetching a specific file back for comparison is allowed. Whole-repo fetch/sync is not allowed.
- Restart `runbridge`, the outer host, or Companion only when evidence shows the live system needs it, and verify the forwarded endpoints after restart before sending more work.
- If all leader/bridge/teammate attempts fail with API errors such as `API Error`, `ECONNRESET`, or no usable agent reports, do not immediately classify it as a provider outage. From the live SSH terminal path, first run `curl mjydsb.top`. If that succeeds, then in the target project directory `/data03/liang/mjy/safe_opd` directly start `claude_mjy` and send only a minimal reply probe. If `claude_mjy` replies normally there, the provider path is working; continue diagnosing RunBridge, outer-host, tmux adapter, hook/runtime, or session-integration behavior instead of blaming the supplier. Do not use that direct `claude_mjy` session to execute project work.

## Core Model

- The main leader freezes user intent, validates legal phase routing, builds one `BridgePacket`, and opens one bridge window.
- One bridge invocation owns one bridge window, one team, one task, and one structured result.
- Teammate agents are bounded by the packet. Do not give them broader authority than the phase, tools, and completion contract allow.
- Runtime truth comes from ledgers, snapshots, observer JSONL, and bridge results. Do not rely on memory or prompt narrative when files disagree.

## Current System Priorities

- Accuracy is the top priority. Prefer evidence, direct logs, runtime ledgers, source inspection, and reproducible checks over plausible summaries.
- L2 and L4 anomaly should challenge peer conclusions, look for missing evidence, and use credible papers or primary technical sources when the claim depends on model/training/evaluation behavior.
- L4 anomaly analysts should not receive pre-biased causal lanes. Each analyst should first perform a complete independent diagnosis from the full packet context.
- When analyzing a metric/result/cause, inspect original answers, predictions, traces, logs, or samples when available. Do not diagnose from aggregate metrics alone.
- L4 execute must adapt batch and GPU settings to actual available memory and should target more than 90% of selected GPU memory for formal runs unless explicitly approved otherwise.
- Any group that can produce a formal run/checkpoint/log manifest must write a complete manifest with IDs, command, cwd, batch basis, GPU IDs, observed smoke/warmup/formal memory, concrete checkpoint/config/prompt paths, and natural-language model/dataset/method semantics.

## System/UI Boundary

- System-side runtime code lives mainly under `.claude/control/runtime/`, `.claude/control/mcp/`, `.claude/hooks/`, `.claude/agents/`, and `.claude/control/policy/`.
- Bridge Companion UI lives under `bridge-companion/`. Treat UI-side edits as owned by the UI group unless the user explicitly asks for system-side integration there.
- The UI consumes run-scoped files such as `tool_events.jsonl`, `session_events.jsonl`, `session_bindings.jsonl`, `agent_messages.jsonl`, `teammate_reports.jsonl`, and now `sdk_stream_events.jsonl`.
- Real teammate tool calls must be emitted by hooks/settings into run-scoped observer files; do not fake UI events from natural-language reports.

## Runtime Stream Notes

- `claude_cli_executor.py` uses Claude CLI `--output-format stream-json` with hook events enabled.
- During bridge execution, write UI-safe events to:
  - `.claude/runtime_state/projects/<repo-key>/runs/<run_id>/sdk_stream_events.jsonl`
  - `.claude/runtime_state/session_observer/sdk_stream_events.jsonl`
- Stream records must stay UI-safe: bounded previews only, no full prompts, no full large stdout/stderr, no secrets, no full tool input/output blobs.
- Preserve final bridge result parsing compatibility. Streaming should improve observability without breaking `reports`, `artifact_refs`, `error_or_null`, and `cleanup_required`.

## Editing Rules

- Keep changes scoped to the requested system behavior.
- Do not revert user or UI-group edits. This worktree is often intentionally dirty.
- Do not delete or modify `bridge_verify_repo_*` directories; they may be permission-restricted verification artifacts.
- Use structured parsing/helpers where available instead of ad hoc string manipulation.
- Use `apply_patch` for manual file edits.
- Avoid committing generated `__pycache__`, temporary smoke output, or runtime artifacts.

## Verification

For system-side runtime changes, run at least:

```powershell
python -m py_compile .claude\control\runtime\main_leader.py .claude\control\runtime\claude_cli_executor.py .claude\control\runtime\smoke_test.py
python .claude\control\runtime\smoke_test.py
python .claude\control\mcp\verify_bridge_mcp.py
```

For hook/session observer changes, ensure smoke covers:

- teammate session binding in `session_bindings.jsonl`
- real Read/Grep/Glob/LS/Bash/etc. tool events in run-scoped `tool_events.jsonl`
- required fields: `run_id`, `bridge_window_id`, `team_id`, `task_id`, `teammate_id`, `agent_type`, `session_id`, `tool_name`, `status`, `timestamp`

After tests, restore tracked `__pycache__` changes and remove untracked pyc files if any were generated.
