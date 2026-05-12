import assert from "node:assert/strict";
import http from "node:http";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const tmpRoot = await mkdtemp(path.join(os.tmpdir(), "bridge-companion-projection-"));
const projectsRoot = path.join(tmpRoot, "projects");
const repoKey = "repo_fixture";
const runId = "run_projection";
const runRoot = path.join(projectsRoot, repoKey, "runs", runId);

process.env.BRIDGE_RUNTIME_PROJECTS_ROOT = projectsRoot;
process.env.BRIDGE_SESSION_OBSERVER_ROOT = path.join(tmpRoot, "session_observer");
process.env.BRIDGE_RUNTIME_REGISTRY_ROOT = path.join(tmpRoot, "registry");

const { buildProjection, closeSseClients, filterEvents, redactForResponse, requestHandler, submitLeaderInput } = await import("./server.mjs");

try {
  await mkdir(runRoot, { recursive: true });
  await mkdir(process.env.BRIDGE_SESSION_OBSERVER_ROOT, { recursive: true });
  await writeJson(path.join(runRoot, "runtime_snapshot.json"), {
    run_id: runId,
    current_phase: "l4_execute",
    run_status: "in_progress",
    lifecycle: {
      open_bridge_window_ids: ["bw_projection"],
      status_index: { bw_projection: "team_waiting" }
    },
    snapshot_refs: {
      canonical_event_log: "event_log.jsonl",
      transitions: "transitions.jsonl"
    }
  });
  await writeJson(path.join(runRoot, "run_ledger.json"), {
    run_id: runId,
    current_phase: "l4_execute",
    run_status: "in_progress"
  });
  await appendJsonl(path.join(runRoot, "bridge_packets.jsonl"), {
    timestamp: "2026-05-11T00:00:00.000Z",
    event_type: "bridge_packets",
    run_id: runId,
    bridge_window_id: "bw_projection",
    team_id: "team_projection",
    task_id: "task_projection",
    target_phase: "l4_execute",
    task_title: "fixture execute task",
    objective: "run fixture and report",
    completion_contract: { required_outputs: ["report"], required_artifacts: ["artifact"] },
    report_contract: { required_sections: ["summary", "evidence"] },
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "session_bindings.jsonl"), {
    timestamp: "2026-05-11T00:00:00.500Z",
    event_type: "session_bindings",
    run_id: runId,
    bridge_window_id: "bw_projection",
    team_id: "team_projection",
    task_id: "task_projection",
    session_id: "sess_executor",
    teammate_id: "executor",
    agent_type: "executor",
    display_name: "executor",
    run_binding_state: "bound_to_run",
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "agent_messages.jsonl"), {
    timestamp: "2026-05-11T00:00:00.750Z",
    event_type: "agent_messages",
    run_id: runId,
    bridge_window_id: "bw_projection",
    team_id: "team_projection",
    task_id: "task_projection",
    from: "bridge-leader",
    to: "executor",
    message_type: "assignment",
    summary: "run fixture and report",
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "tool_events.jsonl"), {
    timestamp: "2026-05-11T00:00:01.000Z",
    event_type: "tool_events",
    run_id: runId,
    bridge_window_id: "bw_projection",
    team_id: "team_projection",
    task_id: "task_projection",
    agent_id: "executor",
    teammate_id: "executor",
    agent_type: "executor",
    session_id: "sess_executor",
    tool_use_id: "tool_read_fixture",
    tool_name: "Read",
    status: "started",
    target: "README.md",
    runtime_event: { authority: "observed", event_id: "evt_tool_start" },
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "tool_events.jsonl"), {
    timestamp: "2026-05-11T00:00:01.500Z",
    event_type: "tool_events",
    run_id: runId,
    bridge_window_id: "bw_projection",
    team_id: "team_projection",
    task_id: "task_projection",
    agent_id: "executor",
    teammate_id: "executor",
    agent_type: "executor",
    session_id: "sess_executor",
    tool_use_id: "tool_read_fixture",
    tool_name: "Read",
    status: "completed",
    target: "README.md",
    file_refs: [{ path: "README.md", role: "read" }],
    duration_ms: 500,
    runtime_event: { authority: "observed", event_id: "evt_tool" },
    sequence: 2
  });
  await writeJson(path.join(runRoot, "active_operations.json"), {
    run_id: runId,
    updated_at: "2026-05-11T00:00:01.500Z",
    teammates: [{
      teammate_id: "executor",
      agent_type: "executor",
      display_name: "executor",
      session_id: "sess_executor",
      bridge_window_id: "bw_projection",
      team_id: "team_projection",
      task_id: "task_projection",
      active_tool: null,
      last_completed_tool: {
        tool_use_id: "tool_read_fixture",
        tool_name: "Read",
        status: "completed",
        target: "README.md"
      }
    }]
  });
  await appendJsonl(path.join(runRoot, "teammate_reports.jsonl"), {
    timestamp: "2026-05-11T00:00:02.000Z",
    event_type: "teammate_reports",
    run_id: runId,
    bridge_window_id: "bw_projection",
    team_id: "team_projection",
    task_id: "task_projection",
    teammate_id: "executor",
    report: {
      summary: "done",
      instruction_coverage: { "run fixture and report": "completed" },
      evidence_refs: ["event:evt_tool"]
    },
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "transitions.jsonl"), {
    timestamp: "2026-05-11T00:00:03.500Z",
    event_kind: "bridge_window_opened",
    run_id: runId,
    bridge_window_id: "bw_projection",
    from_status: "bridge_call_started",
    to_status: "bridge_window_opened",
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "event_log.jsonl"), {
    timestamp: "2026-05-11T00:00:03.600Z",
    event_kind: "bridge_window_opened",
    event_type: "bridge_window_opened",
    run_id: runId,
    bridge_window_id: "bw_projection",
    from_status: "bridge_call_started",
    to_status: "bridge_window_opened",
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "completion_checks.jsonl"), {
    timestamp: "2026-05-11T00:00:03.000Z",
    event_type: "completion_checks",
    run_id: runId,
    bridge_window_id: "bw_projection",
    team_id: "team_projection",
    task_id: "task_projection",
    status: "satisfied",
    completion_checks: {
      validated_by: "completion_validator.v1",
      final_disposition: "succeeded",
      checks: [
        { name: "schema_validation", status: "pass", subject: "bridge result", evidence_ref: "event:evt_tool" }
      ]
    },
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "outer_host_events.jsonl"), {
    schema_version: "outer_sdk_host_event.v1",
    timestamp: "2026-05-11T00:00:04.000Z",
    event_kind: "outer_leader_result",
    source: "outer_sdk_host",
    authority: "source",
    run_id: runId,
    repo_key: repoKey,
    payload: {
      request: { run_id: runId, repo_key: repoKey, main_session_id: "outer-main" },
      leader_result: {
        status: "succeeded",
        handled_by: "claude-agent-sdk",
        reports: [{ summary: "leader reported runtime status" }],
        artifact_refs: [],
        evidence: { sdk_message_count: 2 },
        error_or_null: null,
        cleanup_required: false
      }
    },
    runtime_event: { authority: "source", event_id: "evt_outer_result" },
    sequence: 1
  });
  await appendJsonl(path.join(runRoot, "sdk_stream_events.jsonl"), {
    timestamp: "2026-05-11T00:00:04.250Z",
    event_type: "sdk_stream_started",
    session_id: "outer-main",
    status: "running",
    outer_leader_options: {
      model: "gpt-main",
      cli_path: "/tmp/bin/claude_mjy",
      cli_source: "BRIDGE_CLAUDE_COMMAND",
      cli_mcp_config: "/tmp/.claude/mcp.json"
    },
    settings_diagnostics: {
      settings_path: "/tmp/.claude/runtime_state/generated/outer_leader_settings.json",
      inferred_source_path: "/tmp/.claude/settings.json",
      settings_has_anthropic_base_url: true,
      settings_anthropic_base_url: "https://provider.example/v1",
      settings_has_anthropic_auth_token: true,
      settings_has_http_proxy: false,
      settings_has_https_proxy: false,
      subprocess_env_has_anthropic_base_url: true,
      subprocess_anthropic_base_url: "https://provider.example/v1",
      subprocess_env_has_anthropic_auth_token: true,
      subprocess_anthropic_model: "gpt-main",
      subprocess_env_has_http_proxy: false,
      subprocess_env_has_https_proxy: false
    }
  });
  await appendJsonl(path.join(runRoot, "sdk_stream_events.jsonl"), {
    timestamp: "2026-05-11T00:00:04.500Z",
    event_type: "ResultMessage",
    sdk_message_type: "ResultMessage",
    session_id: "outer-main",
    status: "succeeded",
    result: "leader reported runtime status"
  });
  await appendJsonl(path.join(runRoot, "sdk_stream_events.jsonl"), {
    timestamp: "2026-05-11T00:00:05.000Z",
    event_type: "ResultMessage",
    sdk_message_type: "ResultMessage",
    session_id: "outer-main",
    status: "succeeded",
    result: "SafeDPO and SafeOPD latest project status summary"
  });
  const longLeaderPrefix = "long duplicate leader report ".repeat(12).slice(0, 260);
  const longLeaderReport = `${longLeaderPrefix} full-tail-marker`;
  await appendJsonl(path.join(runRoot, "outer_host_events.jsonl"), {
    schema_version: "outer_sdk_host_event.v1",
    timestamp: "2026-05-11T00:00:06.000Z",
    event_kind: "outer_leader_result",
    source: "outer_sdk_host",
    authority: "source",
    run_id: runId,
    repo_key: repoKey,
    payload: {
      leader_result: {
        status: "succeeded",
        handled_by: "claude-agent-sdk",
        reports: [{ summary: longLeaderPrefix }],
        artifact_refs: [],
        evidence: {},
        error_or_null: null,
        cleanup_required: false
      }
    },
    sequence: 2
  });
  await appendJsonl(path.join(runRoot, "sdk_stream_events.jsonl"), {
    timestamp: "2026-05-11T00:00:06.500Z",
    event_type: "ResultMessage",
    sdk_message_type: "ResultMessage",
    session_id: "outer-main",
    status: "succeeded",
    result: longLeaderReport
  });

  const projection = await buildProjection(repoKey, runId);
  assert.equal(projection.schemaVersion, "companion_projection.v1");
  assert.equal(projection.authority, "projection");
  assert.equal(projection.activeTask.title, "fixture execute task");
  assert.equal(projection.activeTask.targetPhase, "l4_execute");
  assert.ok(projection.timeline.length >= 3);
  assert.ok(projection.timeline.some(event => event.messagePreview.includes("model=gpt-main") && event.messagePreview.includes("cli=/tmp/bin/claude_mjy") && event.messagePreview.includes("cli_source=BRIDGE_CLAUDE_COMMAND") && event.messagePreview.includes("mcp_config=/tmp/.claude/mcp.json") && event.messagePreview.includes("base_url=https://provider.example/v1") && event.messagePreview.includes("settings_proxy_https=false")));
  assert.ok(projection.liveToolCards.some(card => card.toolName === "Read"));
  assert.equal(projection.completionChecklist.validatedBy, "completion_validator.v1");
  assert.ok(projection.leaderReportCards.some(card => card.reportStatus === "succeeded" && card.handledBy === "claude-agent-sdk"));
  assert.equal(projection.leaderReportCards.filter(card => card.summary === "leader reported runtime status").length, 1);
  assert.ok(projection.leaderReportCards.some(card => card.summary.includes("SafeDPO and SafeOPD")));
  assert.ok(projection.leaderReportCards.some(card => card.summary.includes("full-tail-marker")));
  const longReportText = `${"report-body ".repeat(900)}report-tail-marker`;
  const redactedProjection = redactForResponse({
    leaderReportCards: [{ summary: longReportText }],
    rawLine: longReportText
  });
  assert.ok(redactedProjection.leaderReportCards[0].summary.includes("report-tail-marker"));
  assert.ok(redactedProjection.rawLine.endsWith("...<truncated>"));
  const redactedInputResponse = redactForResponse({
    leader_result: { reports: [{ summary: longReportText }] }
  });
  assert.ok(redactedInputResponse.leader_result.reports[0].summary.includes("report-tail-marker"));
  assert.ok(projection.semanticCoverageMatrix.some(row => row.disposition === "completed"));
  assert.ok(projection.rawJsonRefs.every(ref => ref.sourceAuthority !== "authoritative"));
  assert.equal(projection.tuiView.schemaVersion, "companion_tui_view.v1");
  assert.equal(projection.tuiView.header.lifecycleState, "team_waiting");
  assert.equal(projection.tuiView.mainReport.status, "succeeded");
  const tuiReadCards = projection.tuiView.activityItems.filter(item => item.kind === "tool" && item.toolName === "Read");
  assert.equal(tuiReadCards.length, 1);
  assert.equal(tuiReadCards[0].status, "completed");
  assert.equal(tuiReadCards[0].rawRefs.length, 2);
  assert.ok(!("source" in tuiReadCards[0]));
  assert.ok(projection.tuiView.activityItems.some(item => item.kind === "waiting" && item.title === "Waiting for teammate evidence"));
  assert.equal(projection.tuiView.activityItems.filter(item => item.kind === "lifecycle" && item.status === "bridge_window_opened").length, 0);
  const executorCard = projection.tuiView.teamTree.find(item => item.teammateId === "executor");
  assert.equal(executorCard.sourceQuality, "tool_activity");
  assert.equal(executorCard.lastCompletedTool.toolName, "Read");
  assert.ok(projection.tuiView.unknowns.includes("teammate tool activity captured; teammate live text not captured."));
  assert.ok(projection.tuiView.inspectorIndex[tuiReadCards[0].id].rawRefs.length >= 2);
  const syntheticEvents = Array.from({ length: 605 }, (_, index) => ({ seq: index + 1, eventId: `evt_${index + 1}` }));
  const initialTail = filterEvents(syntheticEvents, new URLSearchParams("limit=500&tail=1"));
  assert.equal(initialTail.events[0].seq, 106);
  assert.equal(initialTail.events.at(-1).seq, 605);
  const cursorPage = filterEvents(syntheticEvents, new URLSearchParams("after=600&limit=10&tail=1"));
  assert.equal(cursorPage.events[0].seq, 601);
  const emptyCursorTail = filterEvents(syntheticEvents, new URLSearchParams("afterCursor={}&limit=5&tail=1"));
  assert.equal(emptyCursorTail.events[0].seq, 601);
  const disabledInput = await submitLeaderInput({ text: "hello" });
  assert.equal(disabledInput.accepted, false);
  assert.equal(disabledInput.error, "outer_host_not_configured");
  await assertSseShutdown(repoKey, runId);
  console.log(JSON.stringify({ ok: true, projection: "passed" }, null, 2));
} finally {
  await rm(tmpRoot, { recursive: true, force: true });
}

async function writeJson(filePath, payload) {
  await writeFile(filePath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

async function appendJsonl(filePath, payload) {
  await writeFile(filePath, `${JSON.stringify(payload)}\n`, { encoding: "utf8", flag: "a" });
}

async function assertSseShutdown(repoKey, runId) {
  const server = http.createServer(requestHandler);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  try {
    const streamText = await new Promise((resolve, reject) => {
      let body = "";
      const timeout = setTimeout(() => reject(new Error("gateway_shutdown SSE event not received")), 3000);
      const req = http.get(
        {
          hostname: "127.0.0.1",
          port,
          path: `/api/repos/${encodeURIComponent(repoKey)}/runs/${encodeURIComponent(runId)}/stream?after=999999`,
          headers: { accept: "text/event-stream" }
        },
        res => {
          res.setEncoding("utf8");
          res.on("data", chunk => {
            body += chunk;
          });
          res.on("end", () => {
            clearTimeout(timeout);
            resolve(body);
          });
        }
      );
      req.on("error", error => {
        clearTimeout(timeout);
        reject(error);
      });
      setTimeout(() => closeSseClients("fixture_shutdown"), 50);
    });
    assert.match(streamText, /retry: 86400000/);
    assert.match(streamText, /event: gateway_shutdown/);
    assert.match(streamText, /fixture_shutdown/);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
}
