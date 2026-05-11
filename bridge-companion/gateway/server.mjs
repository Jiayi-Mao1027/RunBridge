import http from "node:http";
import https from "node:https";
import { open, readFile, readdir, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
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
const RESPONSE_TEXT_LIMIT = 8000;
const REPORT_RESPONSE_TEXT_LIMIT = Number(process.env.BRIDGE_COMPANION_REPORT_TEXT_LIMIT || 50000);
const COMPANION_TOKEN = process.env.BRIDGE_COMPANION_TOKEN || "";
const OUTER_HOST_URL = process.env.BRIDGE_OUTER_HOST_URL || process.env.OUTER_SDK_HOST_URL || "";
const OUTER_HOST_TOKEN = process.env.BRIDGE_OUTER_HOST_TOKEN || process.env.OUTER_SDK_HOST_TOKEN || "";
const ACCESS_CONTROL_ALLOW_ORIGIN =
  process.env.BRIDGE_COMPANION_ORIGIN ||
  process.env.BRIDGE_COMPANION_ALLOWED_ORIGIN ||
  process.env.BRIDGE_COMPANION_ALLOWED_ORIGINS?.split(",").map(item => item.trim()).filter(Boolean)[0] ||
  `http://${HOST}:${PORT}`;

const PROJECTS_ROOT = resolveProjectsRoot();
const SESSION_OBSERVER_ROOT = process.env.BRIDGE_SESSION_OBSERVER_ROOT
  ? path.resolve(process.env.BRIDGE_SESSION_OBSERVER_ROOT)
  : path.join(path.dirname(PROJECTS_ROOT), "session_observer");
const REGISTRY_ROOT = process.env.BRIDGE_RUNTIME_REGISTRY_ROOT
  ? path.resolve(process.env.BRIDGE_RUNTIME_REGISTRY_ROOT)
  : path.join(path.dirname(PROJECTS_ROOT), "registry");

const DEFAULT_BRIEF_BASE_URL = "https://api.deepseek.com";
const DEFAULT_BRIEF_MODEL = "deepseek-v4-pro";
const BRIEF_SECRET_PATH =
  process.env.BRIDGE_BRIEF_SECRET_PATH ||
  path.join(os.homedir(), ".bridge-companion", "brief-secret.json");

const runSourceFiles = [
  "sdk_stream_events.jsonl",
  "outer_host_events.jsonl",
  "bridge_packets.jsonl",
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
    "access-control-allow-origin": ACCESS_CONTROL_ALLOW_ORIGIN,
    "access-control-allow-methods": "GET, HEAD, OPTIONS, POST",
    "access-control-allow-headers": "content-type, authorization, x-bridge-companion-token"
  });
  res.end(JSON.stringify(redactForResponse(body), null, 2));
}

function sendText(res, statusCode, body, contentType = "text/plain; charset=utf-8") {
  res.writeHead(statusCode, {
    "content-type": contentType,
    "cache-control": "no-store",
    "access-control-allow-origin": ACCESS_CONTROL_ALLOW_ORIGIN,
    "access-control-allow-methods": "GET, HEAD, OPTIONS, POST",
    "access-control-allow-headers": "content-type, authorization, x-bridge-companion-token"
  });
  res.end(body);
}

function authorizeRequest(req, url) {
  if (!COMPANION_TOKEN) return true;
  const auth = String(req.headers.authorization || "");
  const bearer = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
  const headerToken = String(req.headers["x-bridge-companion-token"] || "");
  const queryToken = String(url.searchParams.get("token") || "");
  return [bearer, headerToken, queryToken].some(value => value && value === COMPANION_TOKEN);
}

function redactForResponse(value, depth = 0, pathParts = []) {
  if (depth > 8) return "<max-depth>";
  if (typeof value === "string") return redactText(value, responseTextLimitFor(pathParts));
  if (Array.isArray(value)) return value.map((item, index) => redactForResponse(item, depth + 1, [...pathParts, String(index)]));
  if (!value || typeof value !== "object") return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (/api[_-]?key|authorization|token|password|secret/i.test(key)) {
      result[key] = item ? "<redacted>" : item;
      continue;
    }
    result[key] = redactForResponse(item, depth + 1, [...pathParts, key]);
  }
  return result;
}

function responseTextLimitFor(pathParts) {
  const pathText = pathParts.join(".");
  if (
    /(^|\.)leaderReportCards\.\d+\.summary$/.test(pathText) ||
    /(^|\.)leader_result\.reports\.\d+\.summary$/.test(pathText) ||
    /(^|\.)leaderResult\.reports\.\d+\.summary$/.test(pathText)
  ) {
    return Math.max(RESPONSE_TEXT_LIMIT, Math.min(REPORT_RESPONSE_TEXT_LIMIT || 50000, 200000));
  }
  return RESPONSE_TEXT_LIMIT;
}

function redactText(value, limit = RESPONSE_TEXT_LIMIT) {
  let text = String(value || "");
  text = text.replace(/(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)/gi, "$1$2<redacted>");
  text = text.replace(/bearer\s+[A-Za-z0-9._~+/=-]{12,}/gi, "Bearer <redacted>");
  text = text.replace(/sk-[A-Za-z0-9_-]{12,}/g, "sk-<redacted>");
  if (text.length > limit) return `${text.slice(0, limit)}...<truncated>`;
  return text;
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
  let sourceByteOffset = 0;
  let charOffset = 0;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const eol =
      text.slice(charOffset + line.length, charOffset + line.length + 2) === "\r\n"
        ? "\r\n"
        : text[charOffset + line.length] === "\n"
          ? "\n"
          : "";
    const lineByteLength = Buffer.byteLength(line);
    if (!line.trim()) {
      sourceByteOffset += lineByteLength + Buffer.byteLength(eol);
      charOffset += line.length + eol.length;
      continue;
    }
    let record;
    let parseError = null;
    try {
      record = JSON.parse(line);
    } catch (error) {
      parseError = error.message;
      record = { raw: line };
    }
    const sourceOffset = index + 1;
    const sourceSequence = Number(record?.sequence || record?.monotonic_index || sourceOffset);
    records.push({ record, sourceFile, sourceOffset, sourceSequence, sourceByteOffset, rawLine: line, parseError });
    sourceByteOffset += lineByteLength + Buffer.byteLength(eol);
    charOffset += line.length + eol.length;
  }
  return records;
}

async function lineCountAndSize(filePath) {
  try {
    const text = await readFile(filePath, "utf8");
    const lines = text.split(/\r?\n/).filter(line => line.trim());
    return { byteOffset: Buffer.byteLength(text), lineOffset: lines.length };
  } catch {
    return { byteOffset: 0, lineOffset: 0 };
  }
}

async function readJsonlTail(cursor) {
  const warnings = [];
  const stats = await stat(cursor.filePath).catch(() => null);
  if (!stats) return { records: [], warnings };
  if (stats.size < cursor.byteOffset) {
    warnings.push({
      kind: "source_truncated",
      sourceFile: cursor.sourceFile,
      previousByteOffset: cursor.byteOffset,
      currentByteSize: stats.size,
      message: `${cursor.sourceFile} was truncated or rewritten; cursor reset.`
    });
    cursor.byteOffset = 0;
    cursor.lineOffset = 0;
    cursor.partial = "";
  }
  if (stats.size === cursor.byteOffset) return { records: [], warnings };

  const byteLength = stats.size - cursor.byteOffset;
  const handle = await open(cursor.filePath, "r");
  try {
    const buffer = Buffer.alloc(byteLength);
    await handle.read(buffer, 0, byteLength, cursor.byteOffset);
    cursor.byteOffset = stats.size;
    const text = cursor.partial + buffer.toString("utf8");
    const complete = text.endsWith("\n") || text.endsWith("\r");
    const lines = text.split(/\r?\n/);
    cursor.partial = complete ? "" : lines.pop() || "";
    const records = [];
    for (const line of lines) {
      if (!line.trim()) {
        cursor.lineOffset += 1;
        continue;
      }
      cursor.lineOffset += 1;
      let record;
      let parseError = null;
      try {
        record = JSON.parse(line);
      } catch (error) {
        parseError = error.message;
        record = { raw: line };
      }
      records.push({
        record,
        sourceFile: cursor.sourceFile,
        sourceOffset: cursor.lineOffset,
        sourceSequence: Number(record?.sequence || record?.monotonic_index || cursor.lineOffset),
        sourceByteOffset: null,
        rawLine: line,
        parseError
      });
    }
    return { records, warnings };
  } finally {
    await handle.close();
  }
}

async function listRepos() {
  const registry = await loadRegistry();
  if (!(await exists(PROJECTS_ROOT)) && !registry.repos.length) return [];
  const byKey = new Map();

  for (const item of registry.repos) {
    const repoKey = item.repoKey;
    const runs = await listRuns(repoKey);
    const active = registry.activeRuns.get(repoKey) || {};
    const latestRun =
      runs.find(run => run.runId === active.latestRunId) ||
      runs[0] ||
      null;
    byKey.set(repoKey, {
      repoKey,
      displayName: item.displayName || repoKey,
      repoRoot: item.repoRoot || "",
      git: item.git || {},
      isActive: Boolean(item.isActive || active.status === "running" || active.activeRunIds?.length),
      activeRunIds: active.activeRunIds || [],
      activeRunStatus: active.status || null,
      runCount: runs.length,
      latestRun,
      updatedAt: active.lastSeenAt || item.lastSeenAt || latestRun?.updatedAt || item.updatedAt || null,
      registrySource: "registry"
    });
  }

  if (!(await exists(PROJECTS_ROOT))) {
    return [...byKey.values()].sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
  }
  const entries = await readdir(PROJECTS_ROOT, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const repoKey = entry.name;
    const repoPath = path.join(PROJECTS_ROOT, repoKey);
    const runs = await listRuns(repoKey);
    const latestRun = runs[0] || null;
    const stats = await stat(repoPath).catch(() => null);
    const existing = byKey.get(repoKey) || {};
    byKey.set(repoKey, {
      ...existing,
      repoKey,
      displayName: existing.displayName || repoKey,
      runCount: runs.length,
      latestRun: existing.latestRun || latestRun,
      updatedAt: existing.updatedAt || latestRun?.updatedAt || stats?.mtime?.toISOString() || null,
      registrySource: existing.registrySource || "scan"
    });
  }
  return [...byKey.values()].sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
}

async function repoInfo(repoKey) {
  const repo = repoDir(repoKey);
  const registry = await loadRegistry();
  const registryRepo = registry.repos.find(item => item.repoKey === repoKey) || {};
  const active = registry.activeRuns.get(repoKey) || {};
  const repoExists = repo ? await exists(repo) : false;
  if (!repoExists && !registryRepo.repoKey) return null;
  const runs = repoExists ? await listRuns(repoKey) : [];
  const stats = repoExists ? await stat(repo).catch(() => null) : null;
  return {
    repoKey,
    displayName: registryRepo.displayName || repoKey,
    repoRoot: registryRepo.repoRoot || "",
    git: registryRepo.git || {},
    isActive: Boolean(registryRepo.isActive || active.status === "running" || active.activeRunIds?.length),
    activeRunIds: active.activeRunIds || [],
    activeRunStatus: active.status || null,
    runtimePath: repo,
    runsPath: repo ? path.join(repo, "runs") : null,
    runCount: runs.length,
    latestRun: runs.find(run => run.runId === active.latestRunId) || runs[0] || null,
    updatedAt: active.lastSeenAt || registryRepo.lastSeenAt || runs[0]?.updatedAt || stats?.mtime?.toISOString() || null,
    registrySource: registryRepo.repoKey ? "registry" : "scan"
  };
}

async function loadRegistry() {
  const reposPayload = await readJsonIfExists(path.join(REGISTRY_ROOT, "repos.json"), null);
  const activePayload = await readJsonIfExists(path.join(REGISTRY_ROOT, "active_runs.json"), null);
  const repos = Object.entries(reposPayload?.repos || {}).map(([key, value]) => ({
    repoKey: value.repo_key || key,
    repoRoot: value.repo_root || "",
    displayName: value.display_name || value.repo_key || key,
    git: value.git || {},
    isActive: value.is_active,
    createdAt: value.created_at || null,
    lastSeenAt: value.last_seen_at || null,
    updatedAt: reposPayload?.updated_at || null
  }));
  const activeRuns = new Map(Object.entries(activePayload?.repos || {}).map(([key, value]) => [key, {
    latestRunId: value.latest_run_id || null,
    activeRunIds: Array.isArray(value.active_run_ids) ? value.active_run_ids : [],
    status: value.status || null,
    lastSeenAt: value.last_seen_at || activePayload?.updated_at || null
  }]));
  return { repos, activeRuns, updatedAt: reposPayload?.updated_at || activePayload?.updated_at || null };
}

async function listRuns(repoKey) {
  const repo = repoDir(repoKey);
  if (!repo) return [];
  const runsRoot = path.join(repo, "runs");
  if (!(await exists(runsRoot))) return [];
  const registry = await loadRegistry();
  const active = registry.activeRuns.get(repoKey) || {};
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
  return runs.sort((a, b) => {
    if (active.latestRunId) {
      if (a.runId === active.latestRunId) return -1;
      if (b.runId === active.latestRunId) return 1;
    }
    const aActive = active.activeRunIds?.includes(a.runId) ? 1 : 0;
    const bActive = active.activeRunIds?.includes(b.runId) ? 1 : 0;
    if (aActive !== bActive) return bActive - aActive;
    return String(b.updatedAt || "").localeCompare(String(a.updatedAt || ""));
  });
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
  const rawStreamEventType = String(record.raw_stream_event_type || record.rawStreamEventType || "");
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
    if (eventType === "sdk_stream_tool_use") {
      return { source: "sdk_stream", kind: "sdk_tool_declared" };
    }
    if (eventType === "sdk_stream_tool_result") {
      return { source: "sdk_stream", kind: "sdk_tool_result" };
    }
    if (rawStreamEventType === "content_block_delta" || eventType.includes("content_block_delta") || eventType === "sdk_stream_delta") {
      return { source: "sdk_stream", kind: sdkRecordHasTextDelta(record) ? "text_delta" : "sdk_delta" };
    }
    if (eventType.includes("assistant_text")) {
      return { source: "sdk_stream", kind: "text_delta" };
    }
    return { source: "sdk_stream", kind: "assistant_message" };
  }
  if (sourceFile === "outer_host_events.jsonl") {
    return { source: "outer_host", kind: record.event_kind || record.event_type || "outer_host_event" };
  }
  if (sourceFile === "bridge_packets.jsonl") {
    return { source: "bridge_packet", kind: "bridge_packet" };
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

function sdkRecordHasTextDelta(record) {
  return Boolean(
    record.text_delta ||
    record.delta_text ||
    record.delta?.text ||
    record.delta?.type === "text_delta" ||
    record.raw_stream_event_type === "content_block_delta" && record.text_delta ||
    record.content_block_delta?.text ||
    record.message_preview && String(record.event_type || "").includes("assistant_text")
  );
}

function boundedDeltaText(value) {
  if (value === undefined || value === null) return "";
  const text = String(value);
  return text.length > 700 ? text.slice(0, 700) : text;
}

function sdkTextDelta(record, sourceFile) {
  if (sourceFile !== "sdk_stream_events.jsonl") return undefined;
  return boundedDeltaText(
    record.text_delta ||
    record.delta_text ||
    record.delta?.text ||
    record.content_block_delta?.text ||
    (String(record.event_type || "").includes("assistant_text") ? record.message_preview : "")
  );
}

function sdkToolInputDelta(record, sourceFile) {
  if (sourceFile !== "sdk_stream_events.jsonl") return undefined;
  const value =
    record.input_json_delta ||
    record.tool_input_json_delta ||
    record.delta?.partial_json ||
    record.content_block_delta?.partial_json;
  return value === undefined ? undefined : boundedDeltaText(value);
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
  if (sourceFile === "outer_host_events.jsonl") {
    const payload = record.payload && typeof record.payload === "object" ? record.payload : {};
    const leaderResult = payload.leader_result && typeof payload.leader_result === "object" ? payload.leader_result : {};
    const request = payload.request && typeof payload.request === "object" ? payload.request : payload;
    if (record.event_kind === "outer_leader_result" || leaderResult.status) {
      const report = Array.isArray(leaderResult.reports) ? leaderResult.reports[0] : null;
      return compactText([
        "leader",
        leaderResult.status,
        report?.summary,
        leaderResult.error_or_null?.type || leaderResult.error_or_null?.message
      ].filter(Boolean).join(" "));
    }
    if (request.safe_preview) return compactText(request.safe_preview);
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

function stableEventId(repoKey, runId, sourceFile, sourceOffset, sourceSequence) {
  const digest = createHash("sha1")
    .update([repoKey, runId, sourceFile, sourceOffset, sourceSequence || ""].join("\u001f"))
    .digest("hex")
    .slice(0, 20);
  return `ev_${digest}`;
}

function normalizeRunRecord(repoKey, runId, item) {
  const record = item.record || {};
  const actualRepoKey = record.repoKey || record.repo_key || repoKey;
  const actualRunId = record.runId || record.run_id || runId;
  const rawRef = {
    sourceFile: item.sourceFile,
    sourceOffset: item.sourceOffset,
    sourceSequence: item.sourceSequence,
    sourceByteOffset: item.sourceByteOffset ?? undefined
  };
  const { source, kind } = sourceAndKind(item.sourceFile, record);
  const status = normalizeStatus(record.status || record.state || record.lifecycle_status || record.payload?.leader_result?.status);
  const eventId = stableEventId(actualRepoKey, actualRunId, item.sourceFile, item.sourceOffset, item.sourceSequence);
  const textDelta = sdkTextDelta(record, item.sourceFile);
  const event = {
    seq: 0,
    eventId,
    cursor: {
      sourceFile: item.sourceFile,
      sourceOffset: item.sourceOffset,
      sourceSequence: item.sourceSequence,
      sourceByteOffset: item.sourceByteOffset ?? undefined
    },
    ts: record.timestamp || record.created_at || record.started_at || record.completed_at || null,
    repoKey: actualRepoKey,
    runId: actualRunId,
    bridgeWindowId: record.bridge_window_id || record.bridgeWindowId || undefined,
    teamId: record.team_id || record.teamId || undefined,
    taskId: record.task_id || record.taskId || undefined,
    sessionId: record.session_id || record.sessionId || undefined,
    source,
    kind,
    lane: laneFor(source, kind, status),
    streamEventType: record.event_type || record.eventType || record.type || undefined,
    actor: actorFrom(record),
    textDelta: textDelta || undefined,
    toolInputDelta: sdkToolInputDelta(record, item.sourceFile),
    messagePreview: messageFor(item.sourceFile, record),
    toolName: source === "hook_tool_event" ? record.tool_name : record.tool_name || undefined,
    sdkToolName: source === "sdk_stream" ? record.tool_name || undefined : undefined,
    toolId: record.tool_id || record.tool_use_id || undefined,
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
    rawLine: item.rawLine,
    parseError: item.parseError || undefined,
    runtimeEvent: record.runtime_event || undefined,
    raw: record
  };
  if (item.sourceFile === "trajectory.jsonl") {
    event.trajectoryStep = record.step_index ?? record.step ?? item.sourceOffset;
    event.messagePreview = compactText(record.action || record.observation || event.messagePreview);
  }
  return event;
}

function laneFor(source, kind, status) {
  const normalizedStatus = String(status || "").toLowerCase();
  const normalizedKind = String(kind || "").toLowerCase();
  if (["failed", "blocked"].includes(normalizedStatus) || normalizedKind.includes("failed") || normalizedKind.includes("rejected")) return "failures";
  if (source === "hook_tool_event") return "tools";
  if (source === "sdk_stream" || source === "agent_message" || source === "outer_host") return "discussion";
  if (source === "teammate_report" || source === "artifact") return "reports";
  if (source === "process_event") return "processes";
  if (source === "completion_check") return "completion";
  return "all";
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

function sourceCursorsFor(events) {
  const cursors = {};
  for (const event of events || []) {
    const sourceFile = event.rawRef?.sourceFile;
    const sourceOffset = Number(event.rawRef?.sourceOffset || 0);
    if (!sourceFile || !sourceOffset) continue;
    cursors[sourceFile] = Math.max(Number(cursors[sourceFile] || 0), sourceOffset);
  }
  return cursors;
}

function parseSourceCursor(value) {
  if (!value) return null;
  const normalize = parsed =>
    parsed && typeof parsed === "object" && !Array.isArray(parsed) && Object.keys(parsed).length
      ? parsed
      : null;
  try {
    const parsed = JSON.parse(value);
    return normalize(parsed);
  } catch {
    try {
      const decoded = Buffer.from(String(value), "base64url").toString("utf8");
      const parsed = JSON.parse(decoded);
      return normalize(parsed);
    } catch {
      return null;
    }
  }
}

function eventAfterSourceCursor(event, cursor) {
  if (!cursor) return true;
  const sourceFile = event.rawRef?.sourceFile;
  if (!sourceFile) return true;
  const seenOffset = Number(cursor[sourceFile] || 0);
  return Number(event.rawRef?.sourceOffset || 0) > seenOffset;
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
  const afterId = query.get("afterId") || query.get("after_id") || "";
  const sourceCursor = parseSourceCursor(query.get("afterCursor") || query.get("after_cursor") || "");
  const tail = ["1", "true", "yes", "latest"].includes(String(query.get("tail") || "").toLowerCase());
  const limit = Math.min(
    MAX_EVENT_LIMIT,
    Math.max(1, Number(query.get("limit") || DEFAULT_EVENT_LIMIT))
  );
  let filtered;
  if (sourceCursor) {
    filtered = events.filter(event => eventAfterSourceCursor(event, sourceCursor)).slice(0, limit);
  } else if (afterId) {
    const index = events.findIndex(event => event.eventId === afterId);
    filtered = (index >= 0 ? events.slice(index + 1) : events).slice(0, limit);
  } else if (tail && !after) {
    filtered = events.slice(Math.max(0, events.length - limit));
  } else {
    filtered = events.filter(event => event.seq > after).slice(0, limit);
  }
  return {
    events: filtered,
    latestSeq: events.reduce((max, event) => Math.max(max, event.seq), after),
    latestEventId: events.at(-1)?.eventId || null,
    sourceCursors: sourceCursorsFor(events),
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
      sourceSequence: item.sourceSequence,
      sourceByteOffset: item.sourceByteOffset ?? undefined
    },
    rawLine: item.rawLine,
    parseError: item.parseError || undefined
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
  const packetSummaryEvent = [...events].reverse().find(event => event.source === "bridge_packet");
  const packet = packetEvent?.raw?.packet || packetEvent?.raw?.payload?.packet || null;
  const packetSummary = packetSummaryEvent?.raw || {};
  const task = packet?.task_spec || {};
  const completion = packet?.completion_contract || task?.completion_contract || packetSummary.completion_contract || {};
  const report = packet?.report_contract || task?.report_contract || packetSummary.report_contract || {};
  const semantic = snapshot?.semantic?.frozen || runLedger?.semantic?.frozen || {};
  return {
    objective:
      task.task_subject ||
      task.task_description ||
      packetSummary.task_title ||
      packetSummary.objective ||
      semantic.task_subject ||
      semantic.user_instruction ||
      "No task packet recorded",
    targetPhase: packet?.target_phase || packetSummary.target_phase || snapshot?.current_phase || runLedger?.current_phase || "unknown",
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
    packet,
    packetSummary
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
    ["sdk_stream", "agent_message", "outer_host"].includes(event.source)
  );
  const hasReport = events.some(event => event.source === "teammate_report");
  const hasCompletion = events.some(event => event.source === "completion_check");
  const unknowns = [];
  if (!hasDiscussion) unknowns.push("No discussion text captured.");
  if (!hasTool) unknowns.push("No hook tool event captured.");
  if (hasReport && !hasTool) {
    unknowns.push("Report exists without tool event.");
  }
  if (!hasReport) unknowns.push("No teammate report captured.");
  if (!hasCompletion) unknowns.push("No completion check captured.");
  if (!data.snapshot) unknowns.push("runtime_snapshot missing.");
  if (!events.length) unknowns.push("observer events missing.");
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

async function buildProjection(repoKey, runId) {
  const data = await loadCompanionData(repoKey, runId);
  if (!data) return null;
  const packetSummary = packetSummaryFrom(data.snapshot, data.runLedger, data.events);
  const events = data.events || [];
  return {
    schemaVersion: "companion_projection.v1",
    authority: "projection",
    repoKey,
    runId,
    generatedAt: new Date().toISOString(),
    derivedFrom: {
      authoritativeRefs: {
        snapshot: "runtime_snapshot.json",
        runLedger: "run_ledger.json",
        canonicalEventLog: data.snapshot?.snapshot_refs?.canonical_event_log || data.snapshot?.snapshot_refs?.event_log || "event_log.jsonl",
        transitions: data.snapshot?.snapshot_refs?.transitions || "transitions.jsonl"
      },
      observerRefs: runSourceFiles,
      projectionRule: "Companion projection is derived display data and must not be used for workflow recovery."
    },
    activeTask: {
      title: packetSummary.objective,
      targetPhase: packetSummary.targetPhase,
      lifecycleState: latestLifecycleState(data.snapshot || data.runLedger),
      packetRef: _projectionRawRef(events.find(event => event.source === "bridge_packet" || event.raw?.packet || event.raw?.payload?.packet)),
      completionSummary: packetSummary.completionSummary,
      reportSummary: packetSummary.reportSummary
    },
    timeline: events.slice(-300).map(projectTimelineEvent),
    liveToolCards: events.filter(event => event.source === "hook_tool_event").slice(-80).map(projectToolCard),
    agentMessageCards: events.filter(event => event.source === "agent_message" || event.source === "sdk_stream" || event.source === "outer_host").slice(-120).map(projectMessageCard),
    artifactCards: events.filter(event => event.source === "artifact" || event.evidenceRefs?.length).slice(-120).map(projectArtifactCard),
    completionChecklist: projectCompletionChecklist(events),
    leaderReportCards: projectLeaderReportCards(events),
    failureRetryLane: events.filter(event => event.lane === "failures" || ["failed", "blocked", "rejected"].includes(String(event.status || ""))).slice(-80).map(projectTimelineEvent),
    semanticCoverageMatrix: projectSemanticCoverage(events),
    rawJsonRefs: events.slice(-300).map(event => ({ eventId: event.eventId, rawRef: event.rawRef, sourceAuthority: event.runtimeEvent?.authority || "observed" })),
    unknowns: unknownsFor(data)
  };
}

function projectTimelineEvent(event) {
  return {
    seq: event.seq,
    eventId: event.eventId,
    ts: event.ts,
    lane: event.lane,
    kind: event.kind,
    source: event.source,
    sourceAuthority: event.runtimeEvent?.authority || "observed",
    sourceEnvelopeId: event.runtimeEvent?.event_id || undefined,
    bridgeWindowId: event.bridgeWindowId,
    teamId: event.teamId,
    taskId: event.taskId,
    actor: event.actor,
    status: event.status,
    messagePreview: event.messagePreview,
    rawRef: event.rawRef
  };
}

function projectToolCard(event) {
  return {
    ...projectTimelineEvent(event),
    toolName: event.toolName,
    target: event.target,
    fileRefs: event.fileRefs || [],
    evidenceRefs: event.evidenceRefs || []
  };
}

function projectMessageCard(event) {
  return {
    ...projectTimelineEvent(event),
    textDelta: event.textDelta || undefined,
    toolInputDelta: event.toolInputDelta || undefined,
    sdkToolName: event.sdkToolName || undefined
  };
}

function projectLeaderReportCards(events) {
  const outerResults = events.filter(event => event.source === "outer_host" && event.raw?.event_kind === "outer_leader_result");
  const sdkResults = events.filter(event => {
    const sdkType = String(event.raw?.sdk_message_type || event.raw?.event_type || "");
    return event.source === "sdk_stream" && ["ResultMessage", "sdk_stream_final_result"].includes(sdkType);
  });
  return dedupeLeaderReportCards([...outerResults, ...sdkResults])
    .slice(-40)
    .map(projectLeaderReportCard);
}

function dedupeLeaderReportCards(events) {
  const byKey = new Map();
  for (const event of sortEvents([...events])) {
    const card = projectLeaderReportCard(event);
    const key = [
      card.reportStatus,
      compactText(card.summary || "").slice(0, 260).toLowerCase()
    ].join("|");
    const existing = byKey.get(key);
    if (!existing || String(card.summary || "").length > String(existing.card.summary || "").length) {
      byKey.set(key, { event, card });
    }
  }
  return sortEvents([...byKey.values()].map(item => item.event));
}

function projectLeaderReportCard(event) {
  const raw = event.raw || {};
  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  const leaderResult = payload.leader_result && typeof payload.leader_result === "object" ? payload.leader_result : null;
  const report = Array.isArray(leaderResult?.reports) ? leaderResult.reports[0] : null;
  return {
    ...projectTimelineEvent(event),
    handledBy: leaderResult?.handled_by || raw.handled_by || raw.source || undefined,
    reportStatus: leaderResult?.status || raw.status || event.status || "unknown",
    summary: report?.summary || raw.result || event.messagePreview || "",
    error: leaderResult?.error_or_null || raw.error_or_null || null,
    evidence: leaderResult?.evidence || raw.evidence || null
  };
}

function projectArtifactCard(event) {
  const raw = event.raw || {};
  const artifactRef = raw.artifact_ref || raw.artifact_refs || raw.raw_ref || null;
  return {
    ...projectTimelineEvent(event),
    artifactType: raw.artifact_type || raw.ref_type || "artifact",
    artifactId: raw.artifact_id || raw.id || undefined,
    artifactRef,
    safePreview: raw.safe_preview || raw.summary || event.messagePreview,
    evidenceRefs: event.evidenceRefs || []
  };
}

function projectCompletionChecklist(events) {
  const latest = [...events].reverse().find(event => event.source === "completion_check");
  if (!latest) return { status: "unknown", items: [], rawRef: null };
  const raw = latest.raw || {};
  const checks = raw.completion_checks || {};
  const items = [];
  for (const item of Array.isArray(checks.checks) ? checks.checks : []) {
    if (!item || typeof item !== "object") continue;
    items.push({
      name: item.name || "check",
      status: item.status || "unknown",
      subject: item.subject || "",
      message: item.message || "",
      evidenceRef: item.evidence_ref || null
    });
  }
  if (!items.length && Array.isArray(raw.items)) {
    for (const item of raw.items) {
      if (!item || typeof item !== "object") continue;
      items.push({
        name: item.id || "contract_item",
        status: item.status || "unknown",
        subject: item.text || "",
        message: item.reason || "",
        evidenceRef: Array.isArray(item.evidence_refs) ? item.evidence_refs[0] || null : null
      });
    }
  }
  return {
    status: raw.status || latest.status || "unknown",
    finalDisposition: checks.final_disposition || undefined,
    validatedBy: checks.validated_by || undefined,
    items,
    rawRef: latest.rawRef
  };
}

function projectSemanticCoverage(events) {
  const rows = [];
  for (const event of events.filter(item => item.source === "teammate_report")) {
    const report = event.raw?.report && typeof event.raw.report === "object" ? event.raw.report : event.raw || {};
    const coverage = report.instruction_coverage && typeof report.instruction_coverage === "object"
      ? report.instruction_coverage
      : {};
    for (const [item, disposition] of Object.entries(coverage)) {
      rows.push({
        item,
        disposition: coverageDisposition(disposition),
        teammateId: event.actor?.teammateId || event.actor?.displayName || event.actor?.role || undefined,
        evidenceRefs: Array.isArray(report.evidence_refs) ? report.evidence_refs : [],
        rawRef: event.rawRef
      });
    }
  }
  return rows;
}

function coverageDisposition(value) {
  if (value && typeof value === "object") {
    return String(value.disposition || value.status || value.state || "unknown");
  }
  return String(value || "unknown");
}

function _projectionRawRef(event) {
  return event?.rawRef || null;
}

async function rawRecord(repoKey, runId, sourceFile, sourceOffset) {
  const safeFile = runSourceFiles.includes(sourceFile) || sourceFile === "session_bindings.jsonl"
    ? sourceFile
    : null;
  if (!safeFile) return null;
  if (!Number.isInteger(Number(sourceOffset)) || Number(sourceOffset) < 1) return null;
  const dir = runDir(repoKey, runId);
  if (!dir) return null;
  const records = await readJsonlWithMeta(path.join(dir, safeFile), safeFile);
  return records.find(item => Number(item.sourceOffset) === Number(sourceOffset)) || null;
}

async function loadBriefSecret() {
  const fileConfig = await readJsonIfExists(BRIEF_SECRET_PATH, null);
  const localProjectConfig = process.env.BRIDGE_COMPANION_ALLOW_PROJECT_SECRET === "1"
    ? await readJsonIfExists(path.join(companionRoot, "key.json"), null)
    : null;
  const config = fileConfig || localProjectConfig || {};
  return {
    baseUrl: config.baseUrl || config.base_url || process.env.BRIDGE_BRIEF_BASE_URL || DEFAULT_BRIEF_BASE_URL,
    apiKey: config.apiKey || config.api_key || config.key || process.env.BRIDGE_BRIEF_API_KEY || "",
    model: config.model || process.env.BRIDGE_BRIEF_MODEL || DEFAULT_BRIEF_MODEL
  };
}

function requestJson(url, payload = null, headers = {}, method = "POST") {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const body = payload === null || payload === undefined ? "" : JSON.stringify(payload);
    const transport = target.protocol === "http:" ? http : https;
    const req = transport.request({
      method,
      hostname: target.hostname,
      port: target.port || (target.protocol === "http:" ? 80 : 443),
      path: `${target.pathname}${target.search}`,
      headers: {
        ...(body ? { "content-type": "application/json", "content-length": Buffer.byteLength(body) } : {}),
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
    if (body) req.write(body);
    req.end();
  });
}

function httpsJson(url, payload, headers = {}) {
  return requestJson(url, payload, headers);
}

async function submitLeaderInput(input) {
  if (!OUTER_HOST_URL) {
    return {
      accepted: false,
      error: "outer_host_not_configured",
      message: "Set BRIDGE_OUTER_HOST_URL to enable UI-to-host user input forwarding."
    };
  }
  const endpoint = new URL("/v1/input", OUTER_HOST_URL).toString();
  const headers = OUTER_HOST_TOKEN ? { "x-bridge-outer-host-token": OUTER_HOST_TOKEN } : {};
  return requestJson(endpoint, { ...input, source: "bridge_companion_gateway" }, headers);
}

async function outerHostStatus() {
  if (!OUTER_HOST_URL) {
    return {
      ok: false,
      error: "outer_host_not_configured",
      message: "Set BRIDGE_OUTER_HOST_URL to enable outer host status discovery."
    };
  }
  const endpoint = new URL("/v1/status", OUTER_HOST_URL).toString();
  const headers = OUTER_HOST_TOKEN ? { "x-bridge-outer-host-token": OUTER_HOST_TOKEN } : {};
  return requestJson(endpoint, null, headers, "GET");
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
    const chunks = [];
    req.on("data", chunk => { chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)); });
    req.on("end", () => {
      const buffer = Buffer.concat(chunks);
      const candidates = [
        () => new TextDecoder("utf-8", { fatal: true }).decode(buffer),
        () => new TextDecoder("gb18030", { fatal: true }).decode(buffer),
        () => buffer.toString("utf8")
      ];
      let body = "";
      for (const decode of candidates) {
        try {
          body = decode();
          break;
        } catch {}
      }
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
  const payload = JSON.stringify(redactForResponse(data));
  for (const line of payload.split(/\r?\n/)) {
    res.write(`data: ${line}\n`);
  }
  res.write("\n");
}

async function createTailer(rootDir, files) {
  const cursors = [];
  for (const sourceFile of files) {
    const filePath = path.join(rootDir, sourceFile);
    const { byteOffset, lineOffset } = await lineCountAndSize(filePath);
    cursors.push({
      sourceFile,
      filePath,
      byteOffset,
      lineOffset,
      partial: ""
    });
  }
  return cursors;
}

async function readTailerEvents(tailer, repoKey, runId) {
  const events = [];
  const warnings = [];
  for (const cursor of tailer) {
    const result = await readJsonlTail(cursor);
    warnings.push(...result.warnings);
    const records = result.records;
    for (const item of records) {
      events.push(normalizeRunRecord(repoKey, runId, item));
    }
  }
  return { events: assignSequences(events), warnings };
}

async function streamRun(req, res, repoKey, runId, query) {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-store, no-transform",
    "connection": "keep-alive",
    "access-control-allow-origin": ACCESS_CONTROL_ALLOW_ORIGIN
  });
  const dir = runDir(repoKey, runId);
  let sourceCursor = parseSourceCursor(query.get("afterCursor") || query.get("after_cursor") || "");
  let lastSeq = Number(query.get("after") || 0);
  let lastEventId = query.get("afterId") || query.get("after_id") || req.headers["last-event-id"] || "";
  const emitted = new Set();
  const writeEvents = events => {
    const next = events.filter(event => {
      if (emitted.has(event.eventId)) return false;
      if (sourceCursor && !eventAfterSourceCursor(event, sourceCursor)) return false;
      if (!sourceCursor && lastEventId) {
        const afterIndex = events.findIndex(item => item.eventId === lastEventId);
        if (afterIndex >= 0 && event.seq <= events[afterIndex].seq) return false;
      }
      if (!sourceCursor && !lastEventId && event.seq <= lastSeq) return false;
      return true;
    }).slice(0, DEFAULT_EVENT_LIMIT);
    for (const event of next) {
      lastSeq = Math.max(lastSeq, event.seq);
      lastEventId = event.eventId;
      emitted.add(event.eventId);
      writeSseEvent(res, "companion_event", event, event.eventId);
    }
    sourceCursor = { ...(sourceCursor || {}), ...sourceCursorsFor(next) };
    return next.length;
  };

  const tailer = dir ? await createTailer(dir, runSourceFiles) : [];
  const initialEvents = await loadRunEvents(repoKey, runId);
  writeEvents(initialEvents);
  const writeLive = async () => {
    const { events: nextEvents, warnings } = await readTailerEvents(tailer, repoKey, runId);
    for (const warning of warnings) {
      if (sourceCursor && warning.sourceFile) sourceCursor[warning.sourceFile] = 0;
      if (warning.sourceFile) emitted.clear();
      writeSseEvent(res, "gateway_warning", { repoKey, runId, ...warning }, null);
    }
    const count = writeEvents(nextEvents);
    if (!count) res.write(`: heartbeat ${Date.now()}\n\n`);
  };
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
    "access-control-allow-origin": ACCESS_CONTROL_ALLOW_ORIGIN
  });
  let sourceCursor = parseSourceCursor(query.get("afterCursor") || query.get("after_cursor") || "");
  let lastSeq = Number(query.get("after") || 0);
  let lastEventId = query.get("afterId") || query.get("after_id") || req.headers["last-event-id"] || "";
  const emitted = new Set();
  const writeEvents = events => {
    const next = events.filter(event => {
      if (emitted.has(event.eventId)) return false;
      if (sourceCursor && !eventAfterSourceCursor(event, sourceCursor)) return false;
      if (!sourceCursor && lastEventId) {
        const afterIndex = events.findIndex(item => item.eventId === lastEventId);
        if (afterIndex >= 0 && event.seq <= events[afterIndex].seq) return false;
      }
      if (!sourceCursor && !lastEventId && event.seq <= lastSeq) return false;
      return true;
    }).slice(0, DEFAULT_EVENT_LIMIT);
    for (const event of next) {
      lastSeq = Math.max(lastSeq, event.seq);
      lastEventId = event.eventId;
      emitted.add(event.eventId);
      writeSseEvent(res, "companion_event", event, event.eventId);
    }
    sourceCursor = { ...(sourceCursor || {}), ...sourceCursorsFor(next) };
    return next.length;
  };
  const tailer = await createTailer(SESSION_OBSERVER_ROOT, sessionObserverFiles);
  const initialEvents = await loadSessionObserverEvents();
  writeEvents(initialEvents);
  const writeLive = async () => {
    const { events: nextEvents, warnings } = await readTailerEvents(tailer, "session_observer", "unbound");
    for (const warning of warnings) {
      if (sourceCursor && warning.sourceFile) sourceCursor[warning.sourceFile] = 0;
      if (warning.sourceFile) emitted.clear();
      writeSseEvent(res, "gateway_warning", { repoKey: "session_observer", runId: "unbound", ...warning }, null);
    }
    const count = writeEvents(nextEvents);
    if (!count) res.write(`: heartbeat ${Date.now()}\n\n`);
  };
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
  if (!authorizeRequest(req, url)) {
    sendJson(res, 401, { error: "unauthorized" });
    return;
  }

  if (pathname === "/api/health") {
    let outerHost = null;
    if (OUTER_HOST_URL) {
      try {
        const status = await outerHostStatus();
        outerHost = {
          ok: true,
          adapter: status.adapter,
          repoKey: status.repo_key || status.repoKey,
          runId: status.run_id || status.runId,
          defaultRunId: status.default_run_id || status.defaultRunId,
          hostInstanceId: status.host_instance_id || status.hostInstanceId,
          startedAt: status.started_at || status.startedAt
        };
      } catch (error) {
        outerHost = { ok: false, error: error.message };
      }
    }
    sendJson(res, 200, {
      ok: true,
      projectsRoot: PROJECTS_ROOT,
      projectsRootExists: await exists(PROJECTS_ROOT),
      registryRoot: REGISTRY_ROOT,
      registryRootExists: await exists(REGISTRY_ROOT),
      sessionObserverRoot: SESSION_OBSERVER_ROOT,
      streamIntervalMs: STREAM_INTERVAL_MS,
      readOnly: true,
      outerHostConfigured: Boolean(OUTER_HOST_URL),
      inputProxy: OUTER_HOST_URL ? "outer_sdk_host" : "disabled",
      outerHost
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
      if (action === "projection") {
        const projection = await buildProjection(repoKey, runId);
        sendJson(res, projection ? 200 : 404, projection || { error: "projection unavailable", repoKey, runId });
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
        else sendJson(res, 200, {
          repoKey,
          runId,
          rawRef: {
            sourceFile: raw.sourceFile,
            sourceOffset: raw.sourceOffset,
            sourceSequence: raw.sourceSequence,
            sourceByteOffset: raw.sourceByteOffset ?? undefined
          },
          record: raw.record,
          rawLine: raw.rawLine,
          parseError: raw.parseError || null
        });
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

  if (pathname === "/api/leader/input") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "POST required" });
      return;
    }
    const input = await parseBody(req);
    try {
      const response = await submitLeaderInput(input);
      sendJson(res, response?.accepted === false ? 503 : 200, response);
    } catch (error) {
      sendJson(res, 502, { error: "outer_host_forward_failed", message: error.message });
    }
    return;
  }

  if (pathname === "/api/leader/status") {
    try {
      const status = await outerHostStatus();
      sendJson(res, status?.ok === false ? 503 : 200, status);
    } catch (error) {
      sendJson(res, 502, { error: "outer_host_status_failed", message: error.message });
    }
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

function startServer() {
  return http.createServer(requestHandler).listen(PORT, HOST, () => {
    console.log(`Bridge Companion gateway listening on http://${HOST}:${PORT}`);
    console.log(`projects root: ${PROJECTS_ROOT}`);
    console.log(`session observer root: ${SESSION_OBSERVER_ROOT}`);
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  startServer();
}

export {
  buildProjection,
  buildStatus,
  filterEvents,
  loadRunEvents,
  normalizeRunRecord,
  outerHostStatus,
  projectCompletionChecklist,
  projectSemanticCoverage,
  redactForResponse,
  requestHandler,
  startServer,
  submitLeaderInput
};
