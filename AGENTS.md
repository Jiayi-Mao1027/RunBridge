# Agent Working Notes

This repository is the workflow control plane, not a normal target application repo. Read `README.md` first, then inspect the relevant files under `.claude/` before changing behavior.

## Live SSH RunBridge Stewardship

- Treat the SSH side as the primary live repair and test surface during active RunBridge operations. For live failures, patch the SSH files directly and verify there first.
- The local checkout remains the canonical stable codebase, but update it only after a remote fix has proven stable enough to preserve as the standard version. Do not block live SSH recovery on local-first patching.
- The local forwarded Companion/debug gateway normally enters through `http://127.0.0.1:8787`; the forwarded outer host status endpoint is `http://127.0.0.1:8791/v1/status`.
- Use `8787` and `8791` first to inspect live system health, active run IDs, bridge windows, runtime snapshots, observer streams, tmux/outer-host status, and Companion diagnostics.
- Judge live system progress in roughly 5-minute intervals while work is actively moving; once the system reaches a steady state or an owned long-running phase, stretch the check interval toward 30 minutes unless evidence suggests trouble.
- Do not stop merely because one bridge succeeded. If the current run has no open bridge window, no live reminder/heartbeat currently taking ownership of the next check, and runtime shows `phase_exit_ready=true` or legal next actions, continue advancing the target project through the normal `8787`/leader-orchestrator path.
- Your job is to advance, debug, repair, and update the RunBridge system so it can advance the target project. Do not take over the target project directly just because you can see files on the SSH side.
- Project completion belongs to the RunBridge system and its `leader-orchestrator`/bridge workflow. Push project work by giving instructions to `leader-orchestrator` through the outer host; direct lower-layer shell, tmux, or project-file operations are debugging tools, not the normal project-execution path.
- Live test operations should simulate how a human drives the system. Outside explicit debugging or repair, default to sending messages to `leader-orchestrator` through the `8787` gateway/outer-host path instead of operating lower-level system or project internals yourself.
- The repair acceptance target is that a human can operate through `8787` without hitting the bug. Direct shell, tmux, or debug-path success is useful evidence, but it is not sufficient by itself.
- When advancing a run through `8787/api/leader/input`, use `input_kind=continue` or `input_kind=advance` with `dispatch_intent=advance_or_continue` and the intended `target_phase`. Reserve `input_kind=user_answer` for a real `paused_for_user_answer` state; using it as a generic continue command can be legally rejected before leader reasoning starts.
- Prefer ASCII operational instructions through the live gateway unless Unicode handling is explicitly under test. The SSH/live JSON path may preserve runtime semantics better when the user-facing natural-language instruction avoids avoidable encoding ambiguity.
- For live-system repairs, local work is only source review, stable-version preservation, and single-file copying. Do not run local smoke tests for this workflow.
- Full smoke testing is SSH-only and should be run at most once for a repair cycle. Use it only after static review and SSH-side targeted checks indicate the copied version is ready.
- The final acceptance check must run on the SSH live surface through `8787`/`8791` and show the SSH run reached the intended state.
- Do not try to fix RunBridge behavior by prompt steering. If behavior should be deterministic, encode it in runtime code, policy files, schemas, hooks, or durable docs; after the fix, continue through the original conversation/gateway path as needed to prove the human-facing route works.
- You do not need to understand the target project's science or product deeply unless that understanding is needed to diagnose a system failure. Keep attention on system health, routing, lifecycle, observer evidence, and bridge execution correctness.
- During active live SSH recovery, patch the SSH system file first and verify the fix on the SSH live surface. Update the local canonical checkout only after the remote fix is stable enough to preserve as the standard version. Do not fetch, push, pull, rsync, or otherwise sync the whole repository between local and SSH sides.
- Single-file transfer to the SSH side is allowed when needed; fetching a specific file back for comparison is allowed. Whole-repo fetch/sync is not allowed.
- Restart `runbridge`, the outer host, or Companion only when evidence shows the live system needs it, and verify the forwarded endpoints after restart before sending more work.
- After copying runtime Python files to SSH, restart the long-lived outer host and clear only the current-run outer/bridge tmux sessions before relying on the new code. Existing outer leader or MCP bridge-server processes keep imported runtime code and may continue running the old behavior.
- If leader/bridge/teammate attempts fail with API-shaped errors such as `API Error`, `ECONNRESET`, retries, or no usable agent reports, treat it as a RunBridge/system-program error by default. Do not run provider probes unless every live path is dead. If any minimal path is still alive, continue diagnosing RunBridge, outer-host, tmux adapter, hook/runtime, command shape, or session integration instead of blaming the supplier. Never use direct `claude_mjy` sessions to execute project work. The expected alias, only for emergency liveness checks, is `alias claude_mjy='HOME=/data03/liang/mjy claude --mcp-config /data03/liang/mjy/.claude/mcp.json'`.
- Do not count a bridge as healthy merely because the ledger says `succeeded`. A valid success needs structurally usable teammate reports, not runtime-wrapped TUI banners, spinner text, prompt echoes, or other non-JSON terminal noise. If teammate output cannot be parsed into the report contract, classify and fix it as a system/reporting failure.
- If `Agent(...)` is missing or unreliable in a Claude Code surface, fix the runtime dispatch path. For L3 read-only bridge work, deterministic runtime-owned teammate dispatch is acceptable only when it launches the contracted teammates with bounded tools and requires parseable report JSON.
- If an accepted `advance_or_continue` request fails only in the outer leader TUI path with API transport errors or a denied disallowed tool such as Bash, first verify the pinned provider probe. If the provider is healthy and runtime integrity allows `call_bridge_sdk`, keep bounded outer-leader permissions and let outer host deterministic auto-bridge continue; do not broaden outer leader tools just to work around the TUI failure.
- If the bridge leader itself fails before returning a structurally usable bridge result, use bounded runtime-owned retry for the same packet with a new bridge window. Record `retry_attempt_scheduled`, preserve the original packet semantics, and stop at the configured retry cap; do not rely on prompt steering or manual resend.
- If bridge-leader CLI repeatedly fails in a non-execute phase while the pinned provider probe succeeds, prefer a bounded runtime-owned teammate fallback over repeating the same large bridge-leader prompt. Keep the packet contract intact and strip mutating tools from runtime-owned fallback unless a later explicit execute/implementation contract safely authorizes them.
- If runtime-owned fallback collects one teammate but another teammate fails with a transport/API reset, retry the failed teammate directly through the same bounded runtime path before rerunning the whole bridge. Do not ask agents to invent missing mechanical report fields; normalize deterministic format fields in runtime code.
- Observer-only blocked teammate reports are diagnostic evidence, not success. If a teammate had completed tool activity but provider transport failed before a semantic JSON report, classify the bridge as `partial_or_failed` or failed with explicit blocked teammates; never promote that to `succeeded`.
- In runtime-owned print/stream-json fallback, an observed teammate with tool activity but no parseable JSON report is still a system/reporting gap, not a missing teammate. `NoReport`, `NoJsonReport`, timeout, provider transport, and provider gate failures should all pass through observer blocked-report salvage before the bridge result is finalized.
- When matching observer evidence for runtime-owned teammate fallback, remember that child print sessions may bind `task_id` as `<bridge-task-id>_<teammate>`. Scope matching must accept that suffix while still requiring the same run, bridge window, and team.
- Do not spend repeated long backoff retries on the same runtime-owned teammate after tool activity has already been observed. One bounded alternate-transport retry is appropriate for retryable runtime wrapper/no-json/timeout failures; after that, salvage that teammate as observer-blocked, record the transport/reporting failure, and continue to the next contracted teammate or final bridge result.
- Provider transport errors such as `ProviderTransportReset`, `ProviderTransportRateLimited`, and `ProviderGateTimeout` are provider/gate-level outcomes, not teammate CLI wrapper defects. Do not retry them through `_runtime_owned_teammate_error_retryable`; retry only runtime-owned CLI wrapper/no-json/timeout failures when policy allows.
- A non-tool provider probe is not enough evidence that the RunBridge tool-use path is healthy. If ordinary `/v1/messages` works but tool-use/tool-result traffic repeatedly resets, treat the issue as a runtime/provider request-shape compatibility bug and keep the bridge result explicit instead of relabeling it as rate limit.
- For outer TTY/HTTP completion, `partial_or_failed` is a terminal bridge result. Once runtime ledgers show `bridge_result_returned_with_partial`, stale TTY retry/API text must not keep the `8787` response open or override the bridge-backed result.

## Core Model

- The main leader freezes user intent, validates legal phase routing, builds one `BridgePacket`, and opens one bridge window.
- One bridge invocation owns one bridge window, one team, one task, and one structured result.
- Teammate agents are bounded by the packet. Do not give them broader authority than the phase, tools, and completion contract allow.
- The healthy bridge path is team/task based: bridge-leader/runtime opens the team and task, dispatches the teammates into that shared bridge window, and the teammate reports come back from that team context.
- Do not rely on a single bridge-leader prompt to decide mechanical teammate fan-out. If teammate launch order, retry policy, allowed tools, report schema, or packet binding can be determined mechanically, encode it in runtime code or policy. Let agents decide semantic content, evidence interpretation, and readiness only.
- Runtime-owned direct teammate execution is acceptable when the system selects it to avoid an unreliable Agent/transport surface, but it must still be bounded by the same bridge window, team, task, packet, tools, and completion contract. Independent teammates should be launched with bounded parallelism; serial teammate fallback is a degraded recovery mode, not the target architecture.
- A parallel teammate executor must return one normalized `BridgeResult` for the bridge window. Preserve report parsing compatibility, mark mixed teammate failures as `partial_or_failed` or failed, and never synthesize missing teammate reports from bridge-leader narration.
- Runtime truth comes from ledgers, snapshots, observer JSONL, and bridge results. Do not rely on memory or prompt narrative when files disagree.
- Precision is the central system policy: anything that can be made explicit should live in the system contract. Put deterministic routing, validation, required fields, tool arguments, lifecycle rules, observer bindings, and guardrails in runtime/policy/schema/hook code; leave agents only the semantic interpretation, evidence weighing, and judgment calls that cannot be written down mechanically.
- Packet construction should minimize agent-authored fields. The system should fill IDs, bindings, lifecycle fields, team/task mapping, schema shape, default retry policy, completion-contract scaffolding, and other format/mechanical fields; agents should supply only strong semantic content such as intent, scope, evidence interpretation, and decision rationale.

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
- Treat run-scoped `sdk_stream_events.jsonl` as a first-class recovery evidence stream for print/stream-json fallback. Use it to prove bounded teammate activity for salvage and diagnostics, but never fabricate semantic completion from assistant deltas or UI-only stream text.

## Editing Rules

- Keep changes scoped to the requested system behavior.
- Do not revert user or UI-group edits. This worktree is often intentionally dirty.
- Do not delete or modify `bridge_verify_repo_*` directories; they may be permission-restricted verification artifacts.
- Use structured parsing/helpers where available instead of ad hoc string manipulation.
- Use `apply_patch` for manual file edits.
- Avoid committing generated `__pycache__`, temporary smoke output, or runtime artifacts.

## Delegation

- When the user explicitly permits or requests subagents, use them freely for bounded context gathering, independent review, and parallel verification.
- The main agent remains responsible for direction, source-of-truth decisions, integration, and final reporting. Do not let delegated context searches replace runtime evidence or policy inspection.

## Verification

For substantial system-side runtime changes, run one SSH-side smoke test only after static review and targeted SSH checks pass:

```powershell
cd /data03/liang/mjy/safe_opd
python3 ../.claude/control/runtime/smoke_test.py
```

Small scoped repairs, documentation-only edits, and low-risk surgical changes do not require a full smoke run every time. Use targeted SSH verification that matches the blast radius, such as remote `py_compile`, focused tests, readback, or gateway-level confirmation. Local validation should not go beyond source inspection unless the user explicitly changes this rule.

For hook/session observer changes, ensure smoke covers:

- teammate session binding in `session_bindings.jsonl`
- real Read/Grep/Glob/LS/Bash/etc. tool events in run-scoped `tool_events.jsonl`
- required fields: `run_id`, `bridge_window_id`, `team_id`, `task_id`, `teammate_id`, `agent_type`, `session_id`, `tool_name`, `status`, `timestamp`

After tests, restore tracked `__pycache__` changes and remove untracked pyc files if any were generated.
