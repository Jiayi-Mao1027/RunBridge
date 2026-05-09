import http from "node:http";
import https from "node:https";
import { readFile, readdir, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const companionRoot = path.resolve(__dirname, "..");
const workspaceRoot = path.resolve(companionRoot, "..");
const prototypeRoot = path.join(companionRoot, "prototype");

const PORT = Number(process.env.BRIDGE_COMPANION_PORT || 8787);
const HOST = process.env.BRIDGE_COMPANION_HOST || "127.0.0.1";
const STREAM_INTERVAL_MS = Number(process.env.BRIDGE_COMPANION_STREAM_INTERVAL_MS || 750);
const MAX_EVENT_LIMIT = 1000;
const DEFAULT_EVENT_LIMIT = 500;

const PROJECTS_ROOT = resolveProjectsRoot();
const SESSION_OBSERVER_ROOT = process.env.BRIDGE_SESSION_OBSERVER_ROOT
  ? path.resolve(process.env.BRIDGE_SESSION_OBSERVER_ROOT)
  : path.join(path.dirname(PROJECTS_ROOT), "session_observer");

const DEFAULT_BRIEF_BASE_URL = "https://api.deepseek.com";
const DEFAULT_BRIEF_MODEL = "deepseek-v4-pro";
const BRIEF_SECRET_PATH =
  process.env.BRIDGE_BRIEF_SECRET_PATH ||
  path.join(os.homedir(), ".bridge-companion", "brief-secret.json");

const runSourceFiles = [
  "sdk_stream_events.jsonl",
  "tool_events.jsonl",
  "agent_messages.jsonl",
  "teammate_reports.jsonl",
  "process_events.jsonl",
  "artifacts.jsonl",
  "completion_checks.jsonl",
  "transitions.jsonl",
  "trajectory.jsonl"
];

const sessionObserverFiles = [
  "sdk_stream_events.jsonl",
  "tool_events.jsonl",
  "session_events.jsonl",
  "session_bindings.jsonl"
];

function resolveProjectsRoot() {
  const explicit =
    process.env.BRIDGE_RUNTIME_PROJECTS_ROOT ||
    process.env.BRIDGE_RUNTIME_ROOT_PROJECTS;
  if (explicit) return path.resolve(explicit);

  const compatRoot =
    process.env.BRIDGE_RUNTIME_ROOT ||
    process.env.BRIDGE_RUNTIME_RUNS_ROOT ||
    "";
  if (compatRoot) {
    const resolved = path.resolve(compatRoot);
    if (path.basename(resolved).toLowerCase() === "runs") {
      return path.dirname(path.dirname(resolved));
    }
    return resolved;
  }

  return path.join(workspaceRoot, ".claude", "runtime_state", "projects");
}

function sendJson(res, statusCode, body) {
  res.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, HEAD, OPTIONS, POST",
    "access-control-allow-headers": "content-type"
  });
  res.end(JSON.stringify(body, null, 2));
}

function sendText(res, statusCode, body, contentType = "text/plain; charset=utf-8") {
  res.writeHead(statusCode, {
    "content-type": contentType,
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, HEAD, OPTIONS, POST",
    "access-control-allow-headers": "content-type"
  });
  res.end(body);
}

function safeSegment(value) {
  const text = String(value || "").trim();
  if (!text || text.includes("/") || text.includes("\\") || text === "." || text === "..") {
    return null;
  }
  if (!/^[A-Za-z0-9_.-]+$/.test(text)) return null;
  return text;
}

function safeChild(root, ...segments) {
  const resolvedRoot = path.resolve(root);
  const resolvedPath = path.resolve(resolvedRoot, ...segments);
  if (resolvedPath !== resolvedRoot && !resolvedPath.startsWith(resolvedRoot + path.sep)) {
    return null;
  }
  return resolvedPath;
}

function repoDir(repoKey) {
  const safe = safeSegment(repoKey);
  if (!safe) return null;
  return safeChild(PROJECTS_ROOT, safe);
}

function runDir(repoKey, runId) {
  const repo = repoDir(repoKey);
  const safeRun = safeSegment(runId);
  if (!repo || !safeRun) return null;
  return safeChild(path.join(repo, "runs"), safeRun);
}

async function exists(filePath) {
  try {
    await stat(filePath);
    return true;
  } catch {
    return false;
  }
}

async function readJsonIfExists(filePath, fallback = null) {
  try {
    return JSON.parse(await readFile(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

async function readJsonlWithMeta(filePath, sourceFile) {
  let text = "";
  try {
    text = await readFile(filePath, "utf8");
  } catch {
    return [];
  }
  const records = [];
  const lines = text.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim()) continue;
    let record;
    try {
      record = JSON.parse(line);
    } catch {
      record = { raw: line };
    }
    const sourceOffset = index + 1;
    const sourceSequence = Number(record?.sequence || record?.monotonic_index || sourceOffset);
    records.push({ record, sourceFile, sourceOffset, sourceSequence });
  }
  return records;
}

async function listRepos() {
  if (!(await exists(PROJECTS_ROOT))) return [];
  const entries = await readdir(PROJECTS_ROOT, { withFileTypes: true });
  const repos = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const repoKey = entry.name;
    const repoPath = path.join(PROJECTS_ROOT, repoKey);
    const runs = await listRuns(repoKey);
    const latestRun = runs[0] || null;
    const stats = await stat(repoPath).catch(() => null);
    repos.push({
      repoKey,
      runCount: runs.length,
      latestRun,
      updatedAt: latestRun?.updatedAt || stats?.mtime?.toISOString() || null
    });
  }
  return repos.sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
}

async function repoInfo(repoKey) {
  const repo = repoDir(repoKey);
  if (!repo || !(await exists(repo))) return null;
  const runs = await listRuns(repoKey);
  const stats = await stat(repo).catch(() => null);
  return {
    repoKey,
    runtimePath: repo,
    runsPath: path.join(repo, "runs"),
    runCount: runs.length,
    latestRun: runs[0] || null,
    updatedAt: runs[0]?.updatedAt || stats?.mtime?.toISOString() || null
  };
}

async function listRuns(repoKey) {
  const repo = repoDir(repoKey);
  if (!repo) return [];
  const runsRoot = path.join(repo, "runs");
  if (!(await exists(runsRoot))) return [];
  const entries = await readdir(runsRoot, { withFileTypes: true });
  const runs = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const runPath = path.join(runsRoot, entry.name);
    const snapshot = await readJsonIfExists(path.join(runPath, "runtime_snapshot.json"), null);
    const ledger = snapshot ? null : await readJsonIfExists(path.join(runPath, "run_ledger.json"), null);
    const stats = await stat(runPath).catch(() => null);
    runs.push({
      repoKey,
      runId: entry.name,
      phase: snapshot?.current_phase || ledger?.current_phase || null,
      lifecycleState: latestLifecycleState(snapshot || ledger),
      updatedAt:
        snapshot?.updated_at ||
        ledger?.updated_at ||
        stats?.mtime?.toISOString() ||
        null,
      hasSnapshot: Boolean(snapshot),
      hasTrajectory: await exists(path.join(runPath, "trajectory.jsonl"))
    });
  }
  return runs.sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
}

function latestLifecycleState(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return "unknown";
  const lifecycle = snapshot.lifecycle || {};
  const open = Array.isArray(lifecycle.open_bridge_window_ids)
    ? lifecycle.open_bridge_window_ids
    : [];
  const statusIndex =
    lifecycle.status_index && typeof lifecycle.status_index === "object"
      ? lifecycle.status_index
      : {};
  for (const windowId of open) {
    if (statusIndex[windowId]) return statusIndex[windowId];
  }
  const entries = Object.entries(statusIndex);
  if (entries.length) return entries.at(-1)[1] || "unknown";
  return snapshot.last_bridge_result?.status || snapshot.run_status || "unknown";
}

function normalizeStatus(value) {
  const raw = String(value || "").toLowerCase();
  if (["started", "running", "streaming", "team_waiting"].includes(raw)) return "running";
  if (["completed", "complete", "succeeded", "success", "done"].includes(raw)) return "completed";
  if (["failed", "failure", "error", "timeout"].includes(raw)) return "failed";
  if (["blocked", "denied", "rejected"].includes(raw)) return "blocked";
  return raw || undefined;
}

function actorFrom(record) {
  const nestedActor =
    record.actor && typeof record.actor === "object" && !Array.isArray(record.actor)
      ? record.actor
      : {};
  return {
    role:
      nestedActor.agent_type ||
      nestedActor.role ||
      record.agent_type ||
      record.agent_id ||
      record.source_kind ||
      record.event_type ||
      "runtime",
    teammateId:
      nestedActor.teammate_id ||
      nestedActor.teammateId ||
      record.teammate_id ||
      record.teammateId ||
      undefined,
    displayName:
      nestedActor.display_name ||
      nestedActor.teammate_id ||
      nestedActor.agent_type ||
      nestedActor.role ||
      record.display_name ||
      record.teammate_id ||
      record.agent_id ||
      record.agent_type ||
      undefined
  };
}

function compactText(value, fallback = "") {
  const source =
    value && typeof value === "object"
      ? JSON.stringify(value)
      : value ?? fallback ?? "";
  const text = String(source).replace(/\s+/g, " ").trim();
  return text.length > 700 ? `${text.slice(0, 697)}...` : text;
}

function fileRefs(record) {
  const refs = [];
  for (const ref of Array.isArray(record.file_refs) ? record.file_refs : []) {
    if (typeof ref === "string") refs.push(ref);
    else if (ref && typeof ref === "object" && ref.path) refs.push(String(ref.path));
  }
  for (const value of [
    record.target,
    record.path,
    record.file_path,
    record.normalized_input?.path,
    record.normalized_input?.file_path,
    record.safe_input_preview?.path,
    record.safe_input_preview?.file_path,
    record.action?.path,
    record.action?.file_path,
    record.action?.file_refs
  ]) {
    if (typeof value === "string" && value.trim()) refs.push(value.trim());
    else if (Array.isArray(value)) refs.push(...value.map(item => String(item)));
  }
  return [...new Set(refs)].slice(0, 12);
}

function evidenceRefs(record) {
  const refs = [];
  for (const key of ["evidence_refs", "artifact_refs", "artifact_ref", "report_ref", "raw_ref"]) {
    const value = record[key];
    if (Array.isArray(value)) refs.push(...value.map(item => String(item)));
    else if (value && typeof value === "object") {
      refs.push(...Object.values(value).filter(Boolean).map(item => String(item)));
    }
    else if (value) refs.push(String(value));
  }
  return [...new Set(refs)].slice(0, 20);
}

function sourceAndKind(sourceFile, record) {
  const eventType = String(record.event_type || record.eventType || record.type || "");
  if (sourceFile === "tool_events.jsonl") {
    const status = normalizeStatus(record.status);
    return {
      source: "hook_tool_event",
      kind:
        status === "completed"
          ? "tool_completed"
          : status === "failed" || status === "blocked"
            ? "tool_failed"
            : "tool_started"
    };
  }
  if (sourceFile === "sdk_stream_events.jsonl") {
    if (eventType.includes("assistant_text") || record.message_preview) {
      return { source: "sdk_stream", kind: "text_delta" };
    }
    return { source: "sdk_stream", kind: "assistant_message" };
  }
  if (sourceFile === "agent_messages.jsonl") {
    return { source: "agent_message", kind: "assignment_sent" };
  }
  if (sourceFile === "teammate_reports.jsonl") {
    return { source: "teammate_report", kind: "report_received" };
  }
  if (sourceFile === "process_events.jsonl") {
    const state = String(record.state || record.status || eventType).toLowerCase();
    if (state.includes("complete") || state.includes("exit") || state.includes("fail") || state.includes("terminal")) {
      return { source: "process_event", kind: "process_completed" };
    }
    if (state.includes("heartbeat") || state.includes("running") || state.includes("poll")) {
      return { source: "process_event", kind: "process_heartbeat" };
    }
    return { source: "process_event", kind: "process_started" };
  }
  if (sourceFile === "artifacts.jsonl") {
    return { source: "artifact", kind: "report_received" };
  }
  if (sourceFile === "completion_checks.jsonl") {
    const text = `${record.status || ""} ${eventType}`.toLowerCase();
    return {
      source: "completion_check",
      kind: text.includes("reject") || text.includes("fail") ? "completion_rejected" : "completion_passed"
    };
  }
  if (sourceFile === "trajectory.jsonl") {
    return { source: "runtime_snapshot", kind: "lifecycle_transition" };
  }
  return { source: "runtime_snapshot", kind: "lifecycle_transition" };
}

function messageFor(sourceFile, record) {
  if (sourceFile === "tool_events.jsonl") {
    return compactText(
      [
        record.status || "tool",
        record.tool_name,
        record.summary || record.target || record.command_preview
      ].filter(Boolean).join(" ")
    );
  }
  if (record.message_preview) return compactText(record.message_preview);
  if (record.summary) return compactText(record.summary);
  if (record.title) return compactText(record.title);
  if (record.event_type) return compactText(record.event_type);
  if (record.event_kind) return compactText(record.event_kind);
  if (record.action) return compactText(record.action);
  if (record.observation) return compactText(record.observation);
  return compactText(JSON.stringify(record).slice(0, 700));
}

function normalizeRunRecord(repoKey, runId, item) {
  const record = item.record || {};
  const rawRef = {
    sourceFile: item.sourceFile,
    sourceOffset: item.sourceOffset,
    sourceSequence: item.sourceSequence
  };
  const { source, kind } = sourceAndKind(item.sourceFile, record);
  const status = normalizeStatus(record.status || record.state || record.lifecycle_status);
  const event = {
    seq: 0,
    ts: record.timestamp || record.created_at || record.started_at || record.completed_at || null,
    repoKey,
    runId,
    bridgeWindowId: record.bridge_window_id || record.bridgeWindowId || undefined,
    teamId: record.team_id || record.teamId || undefined,
    taskId: record.task_id || record.taskId || undefined,
    sessionId: record.session_id || record.sessionId || undefined,
    source,
    kind,
    actor: actorFrom(record),
    textDelta: source === "sdk_stream" && kind === "text_delta" ? messageFor(item.sourceFile, record) : undefined,
    messagePreview: messageFor(item.sourceFile, record),
    toolName: source === "hook_tool_event" ? record.tool_name : undefined,
    status,
    target:
      record.target ||
      record.command_preview ||
      record.normalized_input?.path ||
      record.normalized_input?.command ||
      record.action?.safe_input_preview ||
      record.action?.tool_name ||
      undefined,
    fileRefs: fileRefs(record),
    evidenceRefs: evidenceRefs(record),
    rawRef,
    raw: record
  };
  if (item.sourceFile === "trajectory.jsonl") {
    event.trajectoryStep = record.step_index ?? record.step ?? item.sourceOffset;
    event.messagePreview = compactText(record.action || record.observation || event.messagePreview);
  }
  return event;
}

function sourceFileOrder(sourceFile) {
  const sourceFiles = [...runSourceFiles, ...sessionObserverFiles];
  const index = sourceFiles.indexOf(sourceFile);
  return index === -1 ? sourceFiles.length : index;
}

function sortEvents(events) {
  return events.sort((a, b) => {
    const at = Date.parse(a.ts || "") || 0;
    const bt = Date.parse(b.ts || "") || 0;
    if (at !== bt) return at - bt;
    const sourceOrder = sourceFileOrder(a.rawRef?.sourceFile) - sourceFileOrder(b.rawRef?.sourceFile);
    if (sourceOrder !== 0) return sourceOrder;
    return Number(a.rawRef?.sourceOffset || 0) - Number(b.rawRef?.sourceOffset || 0);
  });
}

function assignSequences(events) {
  return sortEvents(events).map((event, index) => ({
    ...event,
    seq: index + 1
  }));
}

async function loadRunEvents(repoKey, runId) {
  const dir = runDir(repoKey, runId);
  if (!dir) return [];
  const all = [];
  for (const sourceFile of runSourceFiles) {
    const records = await readJsonlWithMeta(path.join(dir, sourceFile), sourceFile);
    for (const item of records) {
      all.push(normalizeRunRecord(repoKey, runId, item));
    }
  }
  return assignSequences(all);
}

async function loadSessionObserverEvents() {
  const all = [];
  for (const sourceFile of sessionObserverFiles) {
    const records = await readJsonlWithMeta(path.join(SESSION_OBSERVER_ROOT, sourceFile), sourceFile);
    for (const item of records) {
      const repoKey = item.record?.repoKey || "session_observer";
      const runId = item.record?.run_id || "unbound";
      all.push(normalizeRunRecord(repoKey, runId, item));
    }
  }
  return assignSequences(all);
}

function filterEvents(events, query) {
  const after = Number(query.get("after") || 0);
  const limit = Math.min(
    MAX_EVENT_LIMIT,
    Math.max(1, Number(query.get("limit") || DEFAULT_EVENT_LIMIT))
  );
  const filtered = events.filter(event => event.seq > after).slice(0, limit);
  return {
    events: filtered,
    latestSeq: events.reduce((max, event) => Math.max(max, event.seq), after),
    count: filtered.length
  };
}

async function loadCompanionData(repoKey, runId) {
  const dir = runDir(repoKey, runId);
  if (!dir) return null;
  const snapshot = await readJsonIfExists(path.join(dir, "runtime_snapshot.json"), null);
  const runLedger = await readJsonIfExists(path.join(dir, "run_ledger.json"), null);
  const activeOperations = await readJsonIfExists(path.join(dir, "active_operations.json"), null);
  const sessionBindings = await readJsonlWithMeta(path.join(dir, "session_bindings.jsonl"), "session_bindings.jsonl");
  const trajectory = (await readJsonlWithMeta(path.join(dir, "trajectory.jsonl"), "trajectory.jsonl")).map(item => ({
    ...item.record,
    rawRef: {
      sourceFile: item.sourceFile,
      sourceOffset: item.sourceOffset,
      sourceSequence: item.sourceSequence
    }
  }));
  const events = await loadRunEvents(repoKey, runId);
  return {
    repoKey,
    runId,
    snapshot,
    runLedger,
    activeOperations,
    sessionBindings: sessionBindings.map(item => item.record),
    trajectory,
    events
  };
}

function packetSummaryFrom(snapshot, runLedger, events) {
  const packetEvent = [...events].reverse().find(event => event.raw?.packet || event.raw?.payload?.packet);
  const packet = packetEvent?.raw?.packet || packetEvent?.raw?.payload?.packet || null;
  const task = packet?.task_spec || {};
  const completion = packet?.completion_contract || task?.completion_contract || {};
  const report = packet?.report_contract || task?.report_contract || {};
  const semantic = snapshot?.semantic?.frozen || runLedger?.semantic?.frozen || {};
  return {
    objective:
      task.task_subject ||
      task.task_description ||
      semantic.task_subject ||
      semantic.user_instruction ||
      "No task packet recorded",
    targetPhase: packet?.target_phase || snapshot?.current_phase || runLedger?.current_phase || "unknown",
    completionSummary: completion.required_outputs || completion.required_artifacts
      ? JSON.stringify({
          required_outputs: completion.required_outputs || [],
          required_artifacts: completion.required_artifacts || [],
          validation_requirements: completion.validation_requirements || []
        })
      : "No completion contract recorded",
    reportSummary: report.required_sections
      ? JSON.stringify({
          required_sections: report.required_sections || [],
          required_evidence: report.required_evidence || []
        })
      : "No report contract recorded",
    packet
  };
}

function memberKey(value) {
  return String(value || "unknown");
}

function compactToolForMember(event) {
  return {
    seq: event.seq,
    toolName: event.toolName,
    status: event.status,
    target: event.target,
    messagePreview: event.messagePreview,
    exitCode: event.raw?.exit_code,
    fileRefs: event.fileRefs || [],
    rawRef: event.rawRef
  };
}

function teamFrom(data) {
  const members = new Map();
  const ensure = (id, seed = {}) => {
    const key = memberKey(id || seed.displayName || seed.role);
    if (!members.has(key)) {
      members.set(key, {
        id: key,
        role: seed.role || key,
        teammateId: seed.teammateId || undefined,
        displayName: seed.displayName || seed.role || key,
        sessionId: seed.sessionId || undefined,
        bridgeWindowId: seed.bridgeWindowId || undefined,
        teamId: seed.teamId || undefined,
        taskId: seed.taskId || undefined,
        status: "idle",
        activeTool: null,
        lastCompletedTool: null,
        reports: 0,
        rawRefs: []
      });
    }
    const member = members.get(key);
    Object.assign(member, Object.fromEntries(Object.entries(seed).filter(([, value]) => value !== undefined && value !== null && value !== "")));
    return member;
  };

  for (const binding of data.sessionBindings || []) {
    const id = binding.teammate_id || binding.agent_type || binding.agent_id || binding.session_id;
    const member = ensure(id, {
      role: binding.agent_type || binding.teammate_id || "session",
      teammateId: binding.teammate_id,
      displayName: binding.display_name || binding.teammate_id || binding.agent_type,
      sessionId: binding.session_id,
      bridgeWindowId: binding.bridge_window_id,
      teamId: binding.team_id,
      taskId: binding.task_id
    });
    member.rawRefs.push({ sourceFile: "session_bindings.jsonl" });
  }

  const activeTeam = Array.isArray(data.activeOperations?.teammates)
    ? data.activeOperations.teammates
    : [];
  for (const item of activeTeam) {
    const id = item.teammate_id || item.agent_id || item.agent_type || item.session_id;
    const member = ensure(id, {
      role: item.agent_type || item.teammate_id || "session",
      teammateId: item.teammate_id,
      displayName: item.display_name || item.agent_type,
      sessionId: item.session_id,
      bridgeWindowId: item.bridge_window_id,
      teamId: item.team_id,
      taskId: item.task_id
    });
    if (item.active_tool) {
      member.status = "running";
      member.activeTool = item.active_tool;
    }
    if (item.last_completed_tool) {
      member.lastCompletedTool = item.last_completed_tool;
      if (!member.activeTool) member.status = "idle";
    }
  }

  for (const event of data.events || []) {
    const id =
      event.actor?.teammateId ||
      event.actor?.displayName ||
      event.actor?.role ||
      event.sessionId ||
      "runtime";
    const member = ensure(id, {
      role: event.actor?.role || id,
      teammateId: event.actor?.teammateId,
      displayName: event.actor?.displayName,
      sessionId: event.sessionId,
      bridgeWindowId: event.bridgeWindowId,
      teamId: event.teamId,
      taskId: event.taskId
    });
    if (event.source === "hook_tool_event") {
      const tool = compactToolForMember(event);
      if (event.kind === "tool_started") {
        member.status = "running";
        member.activeTool = tool;
      } else {
        if (member.activeTool?.toolName === tool.toolName || member.activeTool?.seq === tool.seq) {
          member.activeTool = null;
        }
        member.status = event.kind === "tool_failed" ? "failed" : "idle";
        member.lastCompletedTool = tool;
      }
    }
    if (event.source === "teammate_report") member.reports += 1;
    member.rawRefs.push(event.rawRef);
  }

  if (!members.size) {
    ensure("runtime", {
      role: "runtime",
      displayName: "No bound sessions yet"
    });
  }
  return [...members.values()].sort((a, b) => String(a.displayName).localeCompare(String(b.displayName)));
}

function unknownsFor(data) {
  const events = data.events || [];
  const hasTool = events.some(event => event.source === "hook_tool_event");
  const hasDiscussion = events.some(event =>
    ["sdk_stream", "agent_message", "teammate_report"].includes(event.source)
  );
  const hasReport = events.some(event => event.source === "teammate_report");
  const hasCompletion = events.some(event => event.source === "completion_check");
  const unknowns = [];
  if (!hasDiscussion && hasTool) {
    unknowns.push("No captured discussion text; only tool events are available.");
  }
  if (hasReport && !hasTool) {
    unknowns.push("Reports exist without matching tool events; the UI must not present them as Read/Edit/Bash actions.");
  }
  if (!hasReport) unknowns.push("No teammate report has been captured for this run yet.");
  if (!hasCompletion) unknowns.push("No completion check has been captured for this run yet.");
  if (!data.snapshot) unknowns.push("runtime_snapshot.json is missing; state is reconstructed from observer files only.");
  if (!events.length) unknowns.push("No observer events are available for this run.");
  return [...new Set(unknowns)];
}

async function buildStatus(repoKey, runId) {
  const data = await loadCompanionData(repoKey, runId);
  if (!data) return null;
  const packetSummary = packetSummaryFrom(data.snapshot, data.runLedger, data.events);
  const lifecycleState = latestLifecycleState(data.snapshot || data.runLedger);
  const latestEvent = data.events.at(-1) || null;
  const team = teamFrom(data);
  return {
    repoKey,
    runId,
    taskTitle: packetSummary.objective,
    phase: data.snapshot?.current_phase || data.runLedger?.current_phase || "unknown",
    lifecycleState,
    latestEvent,
    gateway: {
      state: "connected",
      label: "read-only",
      projectsRoot: PROJECTS_ROOT,
      sessionObserverRoot: SESSION_OBSERVER_ROOT
    },
    packetSummary,
    teammates: team,
    activityFeed: data.events.slice(-200),
    trajectory: data.trajectory,
    unknowns: unknownsFor(data),
    detail: {
      snapshot: data.snapshot,
      runLedger: data.runLedger,
      activeOperations: data.activeOperations,
      sessionBindings: data.sessionBindings,
      events: data.events.slice(-500),
      trajectory: data.trajectory
    },
    streamContract: {
      transport: "sse",
      primarySources: ["sdk_stream_events.jsonl", "tool_events.jsonl"],
      fallbackSources: runSourceFiles.filter(file => !["sdk_stream_events.jsonl", "tool_events.jsonl"].includes(file)),
      readOnly: true
    }
  };
}

async function rawRecord(repoKey, runId, sourceFile, sourceOffset) {
  const safeFile = runSourceFiles.includes(sourceFile) || sourceFile === "session_bindings.jsonl"
    ? sourceFile
    : null;
  if (!safeFile) return null;
  const dir = runDir(repoKey, runId);
  if (!dir) return null;
  const records = await readJsonlWithMeta(path.join(dir, safeFile), safeFile);
  return records.find(item => Number(item.sourceOffset) === Number(sourceOffset)) || null;
}

async function loadBriefSecret() {
  const fileConfig = await readJsonIfExists(BRIEF_SECRET_PATH, null);
  const localProjectConfig = await readJsonIfExists(path.join(companionRoot, "key.json"), null);
  const config = fileConfig || localProjectConfig || {};
  return {
    baseUrl: config.baseUrl || config.base_url || process.env.BRIDGE_BRIEF_BASE_URL || DEFAULT_BRIEF_BASE_URL,
    apiKey: config.apiKey || config.api_key || config.key || process.env.BRIDGE_BRIEF_API_KEY || "",
    model: config.model || process.env.BRIDGE_BRIEF_MODEL || DEFAULT_BRIEF_MODEL
  };
}

function httpsJson(url, payload, headers = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const body = JSON.stringify(payload);
    const req = https.request({
      method: "POST",
      hostname: target.hostname,
      port: target.port || 443,
      path: `${target.pathname}${target.search}`,
      headers: {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(body),
        ...headers
      }
    }, res => {
      let data = "";
      res.setEncoding("utf8");
      res.on("data", chunk => { data += chunk; });
      res.on("end", () => {
        try {
          const parsed = data ? JSON.parse(data) : {};
          if (res.statusCode >= 200 && res.statusCode < 300) resolve(parsed);
          else reject(new Error(`brief api http ${res.statusCode}: ${data.slice(0, 500)}`));
        } catch (error) {
          reject(error);
        }
      });
    });
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

function buildBriefPrompt(input) {
  const unknowns = Array.isArray(input?.unknowns)
    ? input.unknowns
    : Array.isArray(input?.status?.unknowns)
      ? input.status.unknowns
      : [];
  return [
    "You are the read-only explanation layer for Bridge Companion.",
    "Input is normalized runtime facts plus unknowns. Output display copy only.",
    "Forbidden: status decisions, retry decisions, route decisions, completion decisions, workflow control, runtime writes, or agent instructions.",
    "Your answer must include an Unknowns section, even if it says none recorded.",
    "",
    `Unknowns: ${JSON.stringify(unknowns)}`,
    "",
    "Runtime facts:",
    JSON.stringify(input, null, 2).slice(0, 16000)
  ].join("\n");
}

function ensureUnknownsSection(text, unknowns) {
  const body = String(text || "Model returned no readable brief.").trim();
  if (/\bunknowns\b/i.test(body)) return body;
  return [
    body,
    "",
    "Unknowns:",
    ...(unknowns.length ? unknowns.map(item => `- ${item}`) : ["- none recorded"])
  ].join("\n");
}

async function generateBrief(input) {
  const unknowns = Array.isArray(input?.unknowns)
    ? input.unknowns
    : Array.isArray(input?.status?.unknowns)
      ? input.status.unknowns
      : [];
  const secret = await loadBriefSecret();
  if (!secret.apiKey) {
    return {
      configured: false,
      text: [
        "Model brief is not configured.",
        "",
        "Unknowns:",
        ...(unknowns.length ? unknowns.map(item => `- ${item}`) : ["- none recorded"])
      ].join("\n"),
      unknowns
    };
  }
  const base = secret.baseUrl.replace(/\/+$/, "");
  const response = await httpsJson(`${base}/chat/completions`, {
    model: secret.model,
    messages: [
      {
        role: "system",
        content: "Summarize runtime facts for a read-only UI. Never invent progress or control execution."
      },
      { role: "user", content: buildBriefPrompt(input) }
    ],
    temperature: 0.2
  }, { authorization: `Bearer ${secret.apiKey}` });
  return {
    configured: true,
    model: secret.model,
    text: ensureUnknownsSection(response?.choices?.[0]?.message?.content, unknowns),
    unknowns
  };
}

async function parseBody(req) {
  return new Promise(resolve => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", chunk => { body += chunk; });
    req.on("end", () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch {
        resolve({});
      }
    });
  });
}

function writeSseEvent(res, eventName, data, id = null) {
  if (id !== null && id !== undefined) res.write(`id: ${id}\n`);
  res.write(`event: ${eventName}\n`);
  const payload = JSON.stringify(data);
  for (const line of payload.split(/\r?\n/)) {
    res.write(`data: ${line}\n`);
  }
  res.write("\n");
}

async function streamRun(req, res, repoKey, runId, query) {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-store, no-transform",
    "connection": "keep-alive",
    "access-control-allow-origin": "*"
  });
  let lastSeq = Number(query.get("after") || req.headers["last-event-id"] || 0);
  const writeLive = async () => {
    const events = await loadRunEvents(repoKey, runId);
    const next = events.filter(event => event.seq > lastSeq).slice(0, DEFAULT_EVENT_LIMIT);
    for (const event of next) {
      lastSeq = Math.max(lastSeq, event.seq);
      writeSseEvent(res, "companion_event", event, event.seq);
    }
    if (!next.length) res.write(`: heartbeat ${Date.now()}\n\n`);
  };
  await writeLive();
  const timer = setInterval(() => {
    writeLive().catch(error => {
      writeSseEvent(res, "gateway_error", { message: error.message }, null);
    });
  }, Math.max(250, STREAM_INTERVAL_MS));
  req.on("close", () => clearInterval(timer));
}

async function streamSessionObserver(req, res, query) {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-store, no-transform",
    "connection": "keep-alive",
    "access-control-allow-origin": "*"
  });
  let lastSeq = Number(query.get("after") || req.headers["last-event-id"] || 0);
  const writeLive = async () => {
    const events = await loadSessionObserverEvents();
    const next = events.filter(event => event.seq > lastSeq).slice(0, DEFAULT_EVENT_LIMIT);
    for (const event of next) {
      lastSeq = Math.max(lastSeq, event.seq);
      writeSseEvent(res, "companion_event", event, event.seq);
    }
    if (!next.length) res.write(`: heartbeat ${Date.now()}\n\n`);
  };
  await writeLive();
  const timer = setInterval(() => {
    writeLive().catch(error => {
      writeSseEvent(res, "gateway_error", { message: error.message }, null);
    });
  }, Math.max(250, STREAM_INTERVAL_MS));
  req.on("close", () => clearInterval(timer));
}

async function serveStatic(req, res, pathname) {
  let target = pathname === "/" ? "/index.html" : pathname;
  target = decodeURIComponent(target);
  const filePath = safeChild(prototypeRoot, target.replace(/^\/+/, ""));
  if (!filePath || !(await exists(filePath))) {
    sendText(res, 404, "not found");
    return;
  }
  const ext = path.extname(filePath).toLowerCase();
  const contentType =
    ext === ".html" ? "text/html; charset=utf-8" :
    ext === ".js" ? "text/javascript; charset=utf-8" :
    ext === ".css" ? "text/css; charset=utf-8" :
    ext === ".png" ? "image/png" :
    "application/octet-stream";
  sendText(res, 200, await readFile(filePath), contentType);
}

async function handleApi(req, res, url) {
  const pathname = url.pathname;
  const parts = pathname.split("/").filter(Boolean);

  if (pathname === "/api/health") {
    sendJson(res, 200, {
      ok: true,
      projectsRoot: PROJECTS_ROOT,
      projectsRootExists: await exists(PROJECTS_ROOT),
      sessionObserverRoot: SESSION_OBSERVER_ROOT,
      streamIntervalMs: STREAM_INTERVAL_MS,
      readOnly: true
    });
    return;
  }

  if (pathname === "/api/repos") {
    sendJson(res, 200, { projectsRoot: PROJECTS_ROOT, repos: await listRepos() });
    return;
  }

  if (parts[0] === "api" && parts[1] === "repos" && parts[2]) {
    const repoKey = safeSegment(parts[2]);
    if (!repoKey) {
      sendJson(res, 400, { error: "invalid repoKey" });
      return;
    }
    if (parts.length === 3) {
      const info = await repoInfo(repoKey);
      if (!info) sendJson(res, 404, { error: "repo not found", repoKey });
      else sendJson(res, 200, info);
      return;
    }
    if (parts[3] === "runs" && parts.length === 4) {
      sendJson(res, 200, { repoKey, runs: await listRuns(repoKey) });
      return;
    }
    if (parts[3] === "runs" && parts[4] === "latest") {
      const runs = await listRuns(repoKey);
      if (!runs.length) sendJson(res, 404, { error: "no runs", repoKey });
      else sendJson(res, 200, runs[0]);
      return;
    }
    if (parts[3] === "runs" && parts[4]) {
      const runId = safeSegment(parts[4]);
      if (!runId) {
        sendJson(res, 400, { error: "invalid runId" });
        return;
      }
      const dir = runDir(repoKey, runId);
      if (!dir || !(await exists(dir))) {
        sendJson(res, 404, { error: "run not found", repoKey, runId });
        return;
      }
      const action = parts[5] || "status";
      if (action === "snapshot") {
        const snapshot = await readJsonIfExists(path.join(dir, "runtime_snapshot.json"), null);
        sendJson(res, snapshot ? 200 : 404, { repoKey, runId, snapshot });
        return;
      }
      if (action === "events") {
        const events = await loadRunEvents(repoKey, runId);
        sendJson(res, 200, { repoKey, runId, ...filterEvents(events, url.searchParams) });
        return;
      }
      if (action === "stream") {
        await streamRun(req, res, repoKey, runId, url.searchParams);
        return;
      }
      if (action === "status") {
        const status = await buildStatus(repoKey, runId);
        sendJson(res, status ? 200 : 404, status || { error: "status unavailable", repoKey, runId });
        return;
      }
      if (action === "raw") {
        const sourceFile = String(url.searchParams.get("file") || "");
        const sourceOffset = Number(url.searchParams.get("offset") || url.searchParams.get("sourceOffset") || 0);
        const raw = await rawRecord(repoKey, runId, sourceFile, sourceOffset);
        if (!raw) sendJson(res, 404, { error: "raw record not found" });
        else sendJson(res, 200, { repoKey, runId, rawRef: raw, record: raw.record });
        return;
      }
    }
  }

  if (pathname === "/api/session-observer/events") {
    const events = await loadSessionObserverEvents();
    sendJson(res, 200, { ...filterEvents(events, url.searchParams), source: "session_observer" });
    return;
  }

  if (pathname === "/api/session-observer/stream") {
    await streamSessionObserver(req, res, url.searchParams);
    return;
  }

  if (pathname === "/api/brief" || pathname === "/brief") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "POST required" });
      return;
    }
    const input = await parseBody(req);
    try {
      sendJson(res, 200, await generateBrief(input));
    } catch (error) {
      sendJson(res, 502, {
        error: "brief_failed",
        message: error.message,
        unknowns: input?.unknowns || input?.status?.unknowns || []
      });
    }
    return;
  }

  sendJson(res, 404, { error: "api route not found", path: pathname });
}

async function requestHandler(req, res) {
  if (req.method === "OPTIONS") {
    sendText(res, 204, "");
    return;
  }
  if (!["GET", "HEAD", "POST"].includes(req.method)) {
    sendJson(res, 405, { error: "read-only gateway" });
    return;
  }
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  try {
    if (url.pathname.startsWith("/api/") || url.pathname === "/brief") {
      await handleApi(req, res, url);
      return;
    }

    if (url.pathname === "/favicon.ico") {
      sendText(res, 204, "");
      return;
    }

    // Backward-compatible read-only aliases for the old prototype.
    if (url.pathname === "/runs") {
      const repos = await listRepos();
      const latestRepo = repos[0];
      const runs = latestRepo ? await listRuns(latestRepo.repoKey) : [];
      sendJson(res, 200, { repoKey: latestRepo?.repoKey || null, runs });
      return;
    }
    const legacy = url.pathname.match(/^\/runs\/([^/]+)\/(status|events|stream)$/);
    if (legacy) {
      const repos = await listRepos();
      const repoKey = repos[0]?.repoKey;
      if (!repoKey) {
        sendJson(res, 404, { error: "no repos" });
        return;
      }
      const runId = decodeURIComponent(legacy[1]);
      if (legacy[2] === "stream") await streamRun(req, res, repoKey, runId, url.searchParams);
      else if (legacy[2] === "events") {
        const events = await loadRunEvents(repoKey, runId);
        sendJson(res, 200, { repoKey, runId, ...filterEvents(events, url.searchParams) });
      } else {
        sendJson(res, 200, await buildStatus(repoKey, runId));
      }
      return;
    }

    await serveStatic(req, res, url.pathname);
  } catch (error) {
    sendJson(res, 500, { error: "gateway_error", message: error.message });
  }
}

http.createServer(requestHandler).listen(PORT, HOST, () => {
  console.log(`Bridge Companion gateway listening on http://${HOST}:${PORT}`);
  console.log(`projects root: ${PROJECTS_ROOT}`);
  console.log(`session observer root: ${SESSION_OBSERVER_ROOT}`);
});
