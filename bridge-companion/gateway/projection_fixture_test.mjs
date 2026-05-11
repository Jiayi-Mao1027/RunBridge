import assert from "node:assert/strict";
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

const { buildProjection, submitLeaderInput } = await import("./server.mjs");

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
  await appendJsonl(path.join(runRoot, "tool_events.jsonl"), {
    timestamp: "2026-05-11T00:00:01.000Z",
    event_type: "tool_events",
    run_id: runId,
    bridge_window_id: "bw_projection",
    team_id: "team_projection",
    task_id: "task_projection",
    agent_id: "executor",
    tool_name: "Read",
    status: "completed",
    target: "README.md",
    runtime_event: { authority: "projection", event_id: "evt_tool" },
    sequence: 1
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

  const projection = await buildProjection(repoKey, runId);
  assert.equal(projection.schemaVersion, "companion_projection.v1");
  assert.equal(projection.authority, "projection");
  assert.equal(projection.activeTask.title, "fixture execute task");
  assert.equal(projection.activeTask.targetPhase, "l4_execute");
  assert.ok(projection.timeline.length >= 3);
  assert.ok(projection.liveToolCards.some(card => card.toolName === "Read"));
  assert.equal(projection.completionChecklist.validatedBy, "completion_validator.v1");
  assert.ok(projection.leaderReportCards.some(card => card.reportStatus === "succeeded" && card.handledBy === "claude-agent-sdk"));
  assert.ok(projection.semanticCoverageMatrix.some(row => row.disposition === "completed"));
  assert.ok(projection.rawJsonRefs.every(ref => ref.sourceAuthority !== "authoritative"));
  const disabledInput = await submitLeaderInput({ text: "hello" });
  assert.equal(disabledInput.accepted, false);
  assert.equal(disabledInput.error, "outer_host_not_configured");
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
