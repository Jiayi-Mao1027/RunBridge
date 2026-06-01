import http from "node:http";
import https from "node:https";
import { spawn } from "node:child_process";
import { open, readFile, readdir, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { reduceToTuiView } from "./tui_projection.mjs";

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
const REQUEST_JSON_TIMEOUT_MS = Math.max(
  1000,
  Number(process.env.BRIDGE_COMPANION_REQUEST_JSON_TIMEOUT_MS || 5000)
);
const OUTER_HOST_HEALTH_TIMEOUT_MS = Math.max(
  1000,
  Number(process.env.BRIDGE_COMPANION_OUTER_HOST_HEALTH_TIMEOUT_MS || 3000)
);
const LEADER_INPUT_TIMEOUT_MS = Math.max(
  10000,
  Number(process.env.BRIDGE_COMPANION_LEADER_INPUT_TIMEOUT_MS || 15 * 60 * 1000)
);
const LEADER_INPUT_ACK_TIMEOUT_MS = Math.max(
  10000,
  Number(process.env.BRIDGE_COMPANION_LEADER_INPUT_ACK_TIMEOUT_MS || 30000)
);
const BRIEF_REQUEST_TIMEOUT_MS = Math.max(
  5000,
  Number(process.env.BRIDGE_COMPANION_BRIEF_REQUEST_TIMEOUT_MS || 30000)
);
const JSONL_TAIL_MAX_BYTES = Math.max(
  64 * 1024,
  Number(process.env.BRIDGE_COMPANION_JSONL_TAIL_MAX_BYTES || 256 * 1024)
);
const JSONL_TAIL_MAX_LINES = Math.max(
  100,
  Number(process.env.BRIDGE_COMPANION_JSONL_TAIL_MAX_LINES || 1000)
);
const COMPANION_RUN_LEDGER_MAX_BYTES = Math.max(
  1024,
  Number(process.env.BRIDGE_COMPANION_RUN_LEDGER_MAX_BYTES || 2 * 1024 * 1024)
);
const PROJECTION_EVENT_WINDOW = Math.max(
  100,
  Number(process.env.BRIDGE_COMPANION_PROJECTION_EVENT_WINDOW || 1000)
);
const PROJECTION_JSONL_TAIL_MAX_BYTES = Math.max(
  64 * 1024,
  Number(process.env.BRIDGE_COMPANION_PROJECTION_JSONL_TAIL_MAX_BYTES || 256 * 1024)
);
const PROJECTION_JSONL_TAIL_MAX_LINES = Math.max(
  100,
  Number(process.env.BRIDGE_COMPANION_PROJECTION_JSONL_TAIL_MAX_LINES || 250)
);
const STATUS_ACTIVITY_EVENT_LIMIT = Math.max(
  20,
  Number(process.env.BRIDGE_COMPANION_STATUS_ACTIVITY_EVENT_LIMIT || 40)
);
const STATUS_TRAJECTORY_LIMIT = Math.max(
  20,
  Number(process.env.BRIDGE_COMPANION_STATUS_TRAJECTORY_LIMIT || 60)
);
const RUN_LIST_JSON_MAX_BYTES = Math.max(
  16 * 1024,
  Number(process.env.BRIDGE_COMPANION_RUN_LIST_JSON_MAX_BYTES || 512 * 1024)
);
const RESPONSE_TEXT_LIMIT = 8000;
const REPORT_RESPONSE_TEXT_LIMIT = Number(process.env.BRIDGE_COMPANION_REPORT_TEXT_LIMIT || 50000);
const TERMINAL_TEXT_LIMIT = Number(process.env.BRIDGE_COMPANION_TERMINAL_TEXT_LIMIT || 120000);
const TERMINAL_TIMEOUT_MS = Number(process.env.BRIDGE_COMPANION_TERMINAL_TIMEOUT_MS || 30000);
const COMPANION_TOKEN = process.env.BRIDGE_COMPANION_TOKEN || "";
const OUTER_HOST_URL = process.env.BRIDGE_OUTER_HOST_URL || process.env.OUTER_SDK_HOST_URL || "";
const OUTER_HOST_TOKEN = process.env.BRIDGE_OUTER_HOST_TOKEN || process.env.OUTER_SDK_HOST_TOKEN || "";
const ACCESS_CONTROL_ALLOW_ORIGIN =
  process.env.BRIDGE_COMPANION_ORIGIN ||
  process.env.BRIDGE_COMPANION_ALLOWED_ORIGIN ||
  process.env.BRIDGE_COMPANION_ALLOWED_ORIGINS?.split(",").map(item => item.trim()).filter(Boolean)[0] ||
  `http://${HOST}:${PORT}`;
const PRETTY_JSON_RESPONSES = ["1", "true", "yes"].includes(
  String(process.env.BRIDGE_COMPANION_PRETTY_JSON || "").toLowerCase()
);

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
const DEBUG_ENV_KEYS = [
  "HOME",
  "PWD",
  "USER",
  "USERNAME",
  "SHELL",
  "BRIDGE_COMPANION_HOST",
  "BRIDGE_COMPANION_PORT",
  "BRIDGE_COMPANION_ORIGIN",
  "BRIDGE_COMPANION_ALLOWED_ORIGIN",
  "BRIDGE_COMPANION_ALLOWED_ORIGINS",
  "BRIDGE_COMPANION_TOKEN",
  "BRIDGE_RUNTIME_PROJECTS_ROOT",
  "BRIDGE_RUNTIME_ROOT_PROJECTS",
  "BRIDGE_RUNTIME_ROOT",
  "BRIDGE_RUNTIME_RUNS_ROOT",
  "BRIDGE_SESSION_OBSERVER_ROOT",
  "BRIDGE_RUNTIME_REGISTRY_ROOT",
  "BRIDGE_OUTER_HOST_URL",
  "OUTER_SDK_HOST_URL",
  "BRIDGE_OUTER_HOST_TOKEN",
  "OUTER_SDK_HOST_TOKEN",
  "OUTER_SDK_HOST",
  "OUTER_SDK_HOST_PORT",
  "BRIDGE_CLAUDE_COMMAND",
  "BRIDGE_CLAUDE_CLI",
  "BRIDGE_CLAUDE_SETTINGS",
  "OUTER_LEADER_CLAUDE_CLI",
  "OUTER_LEADER_CLAUDE_SETTINGS",
  "BRIDGE_DISABLE_CLAUDE_STARTUP_DEFAULTS",
  "BRIDGE_DISABLE_CLAUDE_MJY_AUTO",
  "OUTER_LEADER_MODEL",
  "ANTHROPIC_MODEL",
  "ANTHROPIC_BASE_URL",
  "ANTHROPIC_AUTH_TOKEN",
  "HTTPS_PROXY",
  "HTTP_PROXY",
  "NO_PROXY"
];

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
  "event_log.jsonl",
  "trajectory.jsonl"
];

const sessionObserverFiles = [
  "sdk_stream_events.jsonl",
  "tool_events.jsonl",
  "session_events.jsonl",
  "session_bindings.jsonl"
];

const runJsonFiles = [
  "runtime_snapshot.json",
  "run_ledger.json",
  "active_operations.json"
];

const activeSseClients = new Set();
let gatewayShuttingDown = false;

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
  res.end(JSON.stringify(redactForResponse(body), null, PRETTY_JSON_RESPONSES ? 2 : 0));
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
    if (shouldRedactResponseKey(key, item)) {
      result[key] = item ? "<redacted>" : item;
      continue;
    }
    result[key] = redactForResponse(item, depth + 1, [...pathParts, key]);
  }
  return result;
}

function shouldRedactResponseKey(key, value) {
  if (typeof value === "boolean" || typeof value === "number" || value === null || value === undefined) {
    return false;
  }
  return /api[_-]?key|authorization|auth[_-]?token|token|password|secret/i.test(key);
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
  if (/(^|\.)(stdout|stderr)$/.test(pathText)) {
    return Math.max(RESPONSE_TEXT_LIMIT, Math.min(TERMINAL_TEXT_LIMIT || 120000, 200000));
  }
  return RESPONSE_TEXT_LIMIT;
}

function redactText(value, limit = RESPONSE_TEXT_LIMIT) {
  let text = String(value || "");
  for (const secret of knownSecretValues()) {
    text = text.split(secret).join("<redacted>");
  }
  text = text.replace(/(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)/gi, "$1$2<redacted>");
  text = text.replace(/bearer\s+[A-Za-z0-9._~+/=-]{12,}/gi, "Bearer <redacted>");
  text = text.replace(/sk-[A-Za-z0-9_-]{12,}/g, "sk-<redacted>");
  if (text.length > limit) return `${text.slice(0, limit)}...<truncated>`;
  return text;
}

function knownSecretValues() {
  const values = [];
  for (const [key, value] of Object.entries(process.env)) {
    if (!value || value.length < 8) continue;
    if (!/api[_-]?key|authorization|auth[_-]?token|token|password|secret/i.test(key)) continue;
    values.push(value);
  }
  return values;
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

async function readJsonIfExists(filePath, fallback = null, options = {}) {
  try {
    const maxBytes = Number(options.maxBytes || 0);
    if (maxBytes > 0) {
      const stats = await stat(filePath).catch(() => null);
      if (!stats || stats.size > maxBytes) return fallback;
    }
    return JSON.parse(await readFile(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

async function readJsonlText(filePath, options = {}) {
  const stats = await stat(filePath).catch(() => null);
  if (!stats) return null;
  const maxBytes = Math.max(1, Number(options.maxBytes || JSONL_TAIL_MAX_BYTES));
  const maxLines = Math.max(1, Number(options.maxLines || JSONL_TAIL_MAX_LINES));
  if (stats.size <= maxBytes) {
    return {
      text: await readFile(filePath, "utf8"),
      baseByteOffset: 0
    };
  }

  const start = Math.max(0, stats.size - maxBytes);
  const length = stats.size - start;
  const buffer = Buffer.alloc(length);
  const handle = await open(filePath, "r");
  try {
    await handle.read(buffer, 0, length, start);
  } finally {
    await handle.close();
  }

  let text = buffer.toString("utf8");
  let baseByteOffset = start;
  const firstNewline = text.indexOf("\n");
  if (firstNewline >= 0) {
    const prefix = text.slice(0, firstNewline + 1);
    baseByteOffset += Buffer.byteLength(prefix);
    text = text.slice(firstNewline + 1);
  }

  const lines = text.split(/\r?\n/);
  if (lines.length > maxLines) {
    const keepFrom = lines.length - maxLines;
    const dropped = lines.slice(0, keepFrom).join("\n");
    if (dropped) baseByteOffset += Buffer.byteLength(`${dropped}\n`);
    text = lines.slice(keepFrom).join("\n");
  }

  return { text, baseByteOffset };
}

async function readJsonlWithMeta(filePath, sourceFile, options = {}) {
  const loaded = await readJsonlText(filePath, options).catch(() => null);
  if (!loaded) {
    return [];
  }
  const text = loaded.text;
  const records = [];
  const lines = text.split(/\r?\n/);
  let sourceByteOffset = loaded.baseByteOffset || 0;
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
    const active = registry.activeRuns.get(repoKey) || {};
    const runSummary = await repoRunSummary(repoKey, active.latestRunId);
    byKey.set(repoKey, {
      repoKey,
      displayName: item.displayName || repoKey,
      repoRoot: item.repoRoot || "",
      git: item.git || {},
      isActive: Boolean(item.isActive || active.status === "running" || active.activeRunIds?.length),
      activeRunIds: active.activeRunIds || [],
      activeRunStatus: active.status || null,
      runCount: runSummary.runCount,
      latestRun: runSummary.latestRun,
      updatedAt: active.lastSeenAt || item.lastSeenAt || runSummary.latestRun?.updatedAt || item.updatedAt || null,
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
    const active = registry.activeRuns.get(repoKey) || {};
    const runSummary = await repoRunSummary(repoKey, active.latestRunId);
    const stats = await stat(repoPath).catch(() => null);
    const existing = byKey.get(repoKey) || {};
    byKey.set(repoKey, {
      ...existing,
      repoKey,
      displayName: existing.displayName || repoKey,
      runCount: existing.runCount ?? runSummary.runCount,
      latestRun: existing.latestRun || runSummary.latestRun,
      updatedAt: existing.updatedAt || runSummary.latestRun?.updatedAt || stats?.mtime?.toISOString() || null,
      registrySource: existing.registrySource || "scan"
    });
  }
  return [...byKey.values()].sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
}

async function repoRunSummary(repoKey, preferredRunId = null) {
  const repo = repoDir(repoKey);
  if (!repo) return { runCount: 0, latestRun: null };
  const runsRoot = path.join(repo, "runs");
  if (!(await exists(runsRoot))) return { runCount: 0, latestRun: null };
  const entries = await readdir(runsRoot, { withFileTypes: true });
  const runEntries = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const runPath = path.join(runsRoot, entry.name);
    runEntries.push({ runId: entry.name, runPath });
  }
  if (!runEntries.length) return { runCount: 0, latestRun: null };

  const preferred = preferredRunId ? runEntries.find(item => item.runId === preferredRunId) : null;
  const latestEntry = preferred || runEntries.sort((a, b) => String(b.runId).localeCompare(String(a.runId)))[0];
  const latestStats = latestEntry ? await stat(latestEntry.runPath).catch(() => null) : null;
  return {
    runCount: runEntries.length,
    latestRun: latestEntry ? await runSummaryFromDir(repoKey, latestEntry.runId, latestEntry.runPath, latestStats) : null
  };
}

async function runSummaryFromDir(repoKey, runId, runPath, stats = null) {
  const snapshot = await readJsonIfExists(path.join(runPath, "runtime_snapshot.json"), null, {
    maxBytes: RUN_LIST_JSON_MAX_BYTES
  });
  const ledger = snapshot ? null : await readJsonIfExists(path.join(runPath, "run_ledger.json"), null, {
    maxBytes: RUN_LIST_JSON_MAX_BYTES
  });
  return {
    repoKey,
    runId,
    phase: snapshot?.current_phase || ledger?.current_phase || null,
    lifecycleState: latestLifecycleState(snapshot || ledger),
    updatedAt:
      snapshot?.updated_at ||
      ledger?.updated_at ||
      stats?.mtime?.toISOString() ||
      null,
    hasSnapshot: Boolean(snapshot),
    hasTrajectory: await exists(path.join(runPath, "trajectory.jsonl"))
  };
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
    const snapshot = await readJsonIfExists(path.join(runPath, "runtime_snapshot.json"), null, {
      maxBytes: RUN_LIST_JSON_MAX_BYTES
    });
    const ledger = snapshot ? null : await readJsonIfExists(path.join(runPath, "run_ledger.json"), null, {
      maxBytes: RUN_LIST_JSON_MAX_BYTES
    });
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
  const readable = value => {
    if (!value || typeof value !== "object") return value || "";
    const raw = value.raw && typeof value.raw === "object" ? value.raw : {};
    const payload = value.payload && typeof value.payload === "object" ? value.payload : {};
    return (
      value.to_status ||
      value.status ||
      raw.to_status ||
      raw.status ||
      payload.to_status ||
      payload.status ||
      value.run_status ||
      ""
    );
  };
  const lifecycle = snapshot.lifecycle || {};
  const open = Array.isArray(lifecycle.open_bridge_window_ids)
    ? lifecycle.open_bridge_window_ids
    : [];
  const statusIndex =
    lifecycle.status_index && typeof lifecycle.status_index === "object"
      ? lifecycle.status_index
      : {};
  for (const windowId of open) {
    const status = readable(statusIndex[windowId]);
    if (status) return status;
  }
  const entries = Object.entries(statusIndex);
  if (entries.length) return readable(entries.at(-1)[1]) || "unknown";
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

function reportSubjectFrom(record) {
  const report = record?.report && typeof record.report === "object" && !Array.isArray(record.report)
    ? record.report
    : {};
  const teammateId =
    report.teammate_id ||
    report.teammateId ||
    report.teammate_name ||
    report.teammateName ||
    report.agent_type ||
    report.agentType ||
    report.role ||
    undefined;
  if (!teammateId) return null;
  return {
    role: report.role || report.agent_type || report.agentType || teammateId,
    teammateId,
    displayName: report.teammate_name || report.teammateName || teammateId
  };
}

function actorFrom(record, source = "") {
  const nestedActor =
    record.actor && typeof record.actor === "object" && !Array.isArray(record.actor)
      ? record.actor
      : {};
  const reportSubject = source === "teammate_report" ? reportSubjectFrom(record) : null;
  if (reportSubject) return reportSubject;
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

function compactReportText(value, fallback = "") {
  const source =
    value && typeof value === "object"
      ? JSON.stringify(value)
      : value ?? fallback ?? "";
  const text = String(source).replace(/\s+/g, " ").trim();
  return text.length > 4000 ? `${text.slice(0, 3997)}...` : text;
}

function displayReportText(value, fallback = "") {
  const source =
    value && typeof value === "object"
      ? JSON.stringify(value, null, 2)
      : value ?? fallback ?? "";
  const text = String(source)
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t]+$/gm, "")
    .replace(/\n{4,}/g, "\n\n\n")
    .trim();
  return text.length > 12000 ? `${text.slice(0, 11997)}...` : text;
}

function compactRawRef(rawRef) {
  if (!rawRef || typeof rawRef !== "object") return rawRef || null;
  return {
    sourceFile: rawRef.sourceFile,
    sourceOffset: rawRef.sourceOffset,
    sourceSequence: rawRef.sourceSequence,
    sourceByteOffset: rawRef.sourceByteOffset ?? undefined
  };
}

function compactReport(report) {
  if (!report || typeof report !== "object") return report || null;
  return {
    teammate_id: report.teammate_id || report.teammateId || undefined,
    teammate_name: report.teammate_name || report.teammateName || undefined,
    agent_type: report.agent_type || report.agentType || undefined,
    role: report.role || undefined,
    status: report.status || undefined,
    summary: report.summary ? compactReportText(report.summary) : undefined,
    decision: report.decision ? compactText(report.decision) : undefined,
    artifact_refs: Array.isArray(report.artifact_refs) ? report.artifact_refs.slice(0, 12) : undefined,
    evidence_refs: Array.isArray(report.evidence_refs) ? report.evidence_refs.slice(0, 12) : undefined
  };
}

function compactLeaderResult(result) {
  if (!result || typeof result !== "object") return result || null;
  return {
    status: result.status || undefined,
    bridge_window_id: result.bridge_window_id || result.bridgeWindowId || undefined,
    team_id: result.team_id || result.teamId || undefined,
    task_id: result.task_id || result.taskId || undefined,
    error_or_null: result.error_or_null ? compactText(result.error_or_null) : undefined,
    reports: Array.isArray(result.reports) ? result.reports.slice(0, 12).map(compactReport) : undefined
  };
}

function compactRawForResponse(raw) {
  if (!raw || typeof raw !== "object") return raw || null;
  const out = {};
  for (const key of [
    "event_kind",
    "event_type",
    "sdk_message_type",
    "raw_stream_event_type",
    "type",
    "status",
    "state",
    "tool_name",
    "tool_id",
    "tool_use_id",
    "exit_code",
    "bridge_window_id",
    "team_id",
    "task_id",
    "session_id",
    "teammate_id",
    "agent_type"
  ]) {
    if (raw[key] !== undefined && raw[key] !== null) out[key] = raw[key];
  }
  for (const key of ["message_preview", "summary", "title", "result", "safe_preview", "command_preview", "error"]) {
    if (raw[key] !== undefined && raw[key] !== null) out[key] = compactText(raw[key]);
  }
  if (raw.report) out.report = compactReport(raw.report);
  if (raw.leader_result) out.leader_result = compactLeaderResult(raw.leader_result);
  if (raw.payload?.leader_result) {
    out.payload = { leader_result: compactLeaderResult(raw.payload.leader_result) };
  }
  return Object.keys(out).length ? out : null;
}

function compactRuntimeEventForResponse(runtimeEvent) {
  if (!runtimeEvent || typeof runtimeEvent !== "object") return undefined;
  return {
    event_id: runtimeEvent.event_id || runtimeEvent.eventId || undefined,
    authority: runtimeEvent.authority || undefined,
    created_at: runtimeEvent.created_at || runtimeEvent.createdAt || undefined
  };
}

function compactEventForResponse(event, options = {}) {
  if (!event || typeof event !== "object") return event || null;
  const includeRaw = Boolean(options.includeRaw);
  const omitRaw = Boolean(options.omitRaw);
  const compact = {
    seq: event.seq,
    eventId: event.eventId,
    cursor: compactRawRef(event.cursor),
    ts: event.ts,
    repoKey: event.repoKey,
    runId: event.runId,
    bridgeWindowId: event.bridgeWindowId,
    teamId: event.teamId,
    taskId: event.taskId,
    sessionId: event.sessionId,
    source: event.source,
    kind: event.kind,
    lane: event.lane,
    streamEventType: event.streamEventType,
    actor: event.actor,
    textDelta: event.textDelta ? compactText(event.textDelta) : undefined,
    toolInputDelta: event.toolInputDelta ? compactText(event.toolInputDelta) : undefined,
    messagePreview: event.messagePreview ? compactText(event.messagePreview) : undefined,
    toolName: event.toolName,
    sdkToolName: event.sdkToolName,
    toolId: event.toolId,
    status: event.status,
    target: event.target ? compactText(event.target) : undefined,
    fileRefs: Array.isArray(event.fileRefs) ? event.fileRefs.slice(0, 12) : [],
    evidenceRefs: Array.isArray(event.evidenceRefs) ? event.evidenceRefs.slice(0, 12) : [],
    rawRef: compactRawRef(event.rawRef),
    parseError: event.parseError || undefined,
    runtimeEvent: compactRuntimeEventForResponse(event.runtimeEvent)
  };
  if (includeRaw) {
    compact.raw = event.raw;
    compact.rawLine = event.rawLine;
  } else if (!omitRaw) {
    compact.raw = compactRawForResponse(event.raw);
  }
  return compact;
}

function compactTrajectoryForResponse(step) {
  if (!step || typeof step !== "object") return step || null;
  return {
    step_index: step.step_index ?? step.step ?? undefined,
    step: step.step ?? step.step_index ?? undefined,
    ts: step.timestamp || step.created_at || step.completed_at || undefined,
    actor: step.actor || step.role || undefined,
    role: step.role || undefined,
    event_type: step.event_type || undefined,
    kind: step.kind || undefined,
    status: step.status || undefined,
    action: step.action ? compactText(step.action) : undefined,
    observation: step.observation ? compactText(step.observation) : undefined,
    message: step.message ? compactText(step.message) : undefined,
    rawRef: compactRawRef(step.rawRef)
  };
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
  if (sourceFile === "event_log.jsonl") {
    const lifecycleKind = String(record.event_kind || eventType || "lifecycle_transition");
    return { source: "runtime_snapshot", kind: lifecycleKind };
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
      return compactText([
        "leader",
        leaderResult.status,
        summarizeLeaderResultReports(leaderResult),
        leaderResult.error_or_null?.type || leaderResult.error_or_null?.message
      ].filter(Boolean).join(" "));
    }
    if (request.safe_preview) return compactText(request.safe_preview);
  }
  if (sourceFile === "sdk_stream_events.jsonl" && record.settings_diagnostics) {
    const diag = record.settings_diagnostics && typeof record.settings_diagnostics === "object"
      ? record.settings_diagnostics
      : {};
    return compactText([
      record.event_type || "sdk_stream",
      `model=${record.outer_leader_options?.model || diag.subprocess_anthropic_model || "none"}`,
      `cli=${record.outer_leader_options?.cli_path || diag.claude_command || "default"}`,
      `cli_source=${record.outer_leader_options?.cli_source || "unknown"}`,
      `mcp_config=${record.outer_leader_options?.cli_mcp_config || "inline"}`,
      `settings_arg=${record.outer_leader_options?.settings ? "flag" : "home"}`,
      `setting_sources=${Array.isArray(record.outer_leader_options?.setting_sources) ? record.outer_leader_options.setting_sources.join(",") : "unknown"}`,
      `settings=${diag.inferred_source_path || diag.settings_path || "none"}`,
      `base_url=${diag.settings_anthropic_base_url || diag.subprocess_anthropic_base_url || "none"}`,
      `settings_env_base_url=${Boolean(diag.settings_has_anthropic_base_url)}`,
      `settings_env_auth_token=${Boolean(diag.settings_has_anthropic_auth_token)}`,
      `settings_proxy_http=${Boolean(diag.settings_has_http_proxy)}`,
      `settings_proxy_https=${Boolean(diag.settings_has_https_proxy)}`,
      `process_env_base_url=${Boolean(diag.subprocess_env_has_anthropic_base_url)}`,
      `process_env_auth_token=${Boolean(diag.subprocess_env_has_anthropic_auth_token)}`,
      `process_proxy_http=${Boolean(diag.subprocess_env_has_http_proxy)}`,
      `process_proxy_https=${Boolean(diag.subprocess_env_has_https_proxy)}`
    ].join(" "));
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
    actor: actorFrom(record, source),
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

async function loadRunEvents(repoKey, runId, options = {}) {
  const dir = runDir(repoKey, runId);
  if (!dir) return [];
  const all = [];
  const sourceFiles = Array.isArray(options.sourceFiles) ? options.sourceFiles : runSourceFiles;
  const readOptions = options.readOptions && typeof options.readOptions === "object" ? options.readOptions : {};
  const perSourceReadOptions = options.perSourceReadOptions && typeof options.perSourceReadOptions === "object"
    ? options.perSourceReadOptions
    : {};
  const includeRecord = typeof options.includeRecord === "function" ? options.includeRecord : null;
  for (const sourceFile of sourceFiles) {
    const records = await readJsonlWithMeta(path.join(dir, sourceFile), sourceFile, {
      ...readOptions,
      ...(perSourceReadOptions[sourceFile] || {})
    });
    for (const item of records) {
      if (includeRecord && !includeRecord(sourceFile, item.record || {})) continue;
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
  const includeRaw = ["1", "true", "yes"].includes(String(query.get("raw") || query.get("include_raw") || "").toLowerCase());
  const includeCompactRaw = includeRaw || ["1", "true", "yes"].includes(String(query.get("compactRaw") || query.get("compact_raw") || "").toLowerCase());
  return {
    events: filtered.map(event => compactEventForResponse(event, { includeRaw, omitRaw: !includeCompactRaw })),
    latestSeq: events.reduce((max, event) => Math.max(max, event.seq), after),
    latestEventId: events.at(-1)?.eventId || null,
    sourceCursors: sourceCursorsFor(events),
    count: filtered.length
  };
}

function eventLoadOptionsForQuery(query) {
  const tail = ["1", "true", "yes", "latest"].includes(String(query.get("tail") || "").toLowerCase());
  const hasCursor = Boolean(query.get("after") || query.get("afterId") || query.get("after_id") || query.get("afterCursor") || query.get("after_cursor"));
  if (!tail || hasCursor) return {};
  const includeStreamDeltas = ["1", "true", "yes"].includes(String(query.get("includeStreamDeltas") || query.get("include_stream_deltas") || "").toLowerCase());
  const limit = Math.min(
    MAX_EVENT_LIMIT,
    Math.max(1, Number(query.get("limit") || DEFAULT_EVENT_LIMIT))
  );
  return {
    readOptions: {
      maxBytes: PROJECTION_JSONL_TAIL_MAX_BYTES,
      maxLines: Math.max(PROJECTION_JSONL_TAIL_MAX_LINES, limit)
    },
    includeRecord: includeStreamDeltas ? null : includeProjectionRecord
  };
}

function includeProjectionRecord(sourceFile, record) {
  if (sourceFile !== "sdk_stream_events.jsonl") return true;
  const { kind } = sourceAndKind(sourceFile, record || {});
  if (kind === "text_delta" && sdkRecordHasTeammateToolUseSummary(record || {})) return true;
  return !["text_delta", "sdk_delta"].includes(kind);
}

function sdkRecordHasTeammateToolUseSummary(record) {
  const text = String(
    record.message_preview ||
    record.messagePreview ||
    record.text_delta ||
    record.delta_text ||
    record.result ||
    record.raw?.result ||
    ""
  );
  if (!text || !text.includes("tool use")) return false;
  return /(?:^|\n)\s*\S.{0,160}?\b\d+\s+tool uses?\b/i.test(text);
}

function projectionEventLoadOptions() {
  return {
    readOptions: {
      maxBytes: PROJECTION_JSONL_TAIL_MAX_BYTES,
      maxLines: PROJECTION_JSONL_TAIL_MAX_LINES
    },
    includeRecord: includeProjectionRecord
  };
}

function statusEventLoadOptions() {
  return {
    readOptions: {
      maxBytes: PROJECTION_JSONL_TAIL_MAX_BYTES,
      maxLines: PROJECTION_JSONL_TAIL_MAX_LINES
    },
    includeRecord: includeProjectionRecord
  };
}

async function loadCompanionData(repoKey, runId, options = {}) {
  const dir = runDir(repoKey, runId);
  if (!dir) return null;
  const snapshot = await readJsonIfExists(path.join(dir, "runtime_snapshot.json"), null);
  const runLedger = await readJsonIfExists(path.join(dir, "run_ledger.json"), null, {
    maxBytes: COMPANION_RUN_LEDGER_MAX_BYTES
  });
  const activeOperations = await readJsonIfExists(path.join(dir, "active_operations.json"), null);
  const sessionBindings = (await readJsonlWithMeta(path.join(dir, "session_bindings.jsonl"), "session_bindings.jsonl")).map(item => ({
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
  const events = await loadRunEvents(repoKey, runId, options.events || {});
  return {
    repoKey,
    runId,
    snapshot,
    runLedger,
    activeOperations,
    sessionBindings,
    trajectory,
    events
  };
}

function packetSummaryFrom(snapshot, runLedger, events, options = {}) {
  const bridgeWindowId = stringOrEmpty(options.bridgeWindowId);
  const inScope = event => !bridgeWindowId || eventBridgeWindowId(event) === bridgeWindowId;
  const packetEvent = [...events].reverse().find(event => inScope(event) && (event.raw?.packet || event.raw?.payload?.packet));
  const packetSummaryEvent = [...events].reverse().find(event => inScope(event) && event.source === "bridge_packet");
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

function stringOrEmpty(value) {
  const text = String(value || "").trim();
  return text || "";
}

function eventBridgeWindowId(event) {
  const raw = event?.raw && typeof event.raw === "object" ? event.raw : {};
  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  const packet = raw.packet || payload.packet || {};
  const binding = packet.binding && typeof packet.binding === "object" ? packet.binding : {};
  return stringOrEmpty(
    event?.bridgeWindowId ||
    raw.bridge_window_id ||
    raw.bridgeWindowId ||
    payload.bridge_window_id ||
    payload.bridgeWindowId ||
    packet.bridge_window_id ||
    packet.bridgeWindowId ||
    binding.bridge_window_id ||
    binding.bridgeWindowId
  );
}

function runtimeBridgeWindowIds(snapshot, runLedger) {
  const ids = new Set();
  for (const source of [snapshot, runLedger]) {
    if (!source || typeof source !== "object") continue;
    const lifecycle = source.lifecycle && typeof source.lifecycle === "object" ? source.lifecycle : {};
    for (const id of Array.isArray(lifecycle.open_bridge_window_ids) ? lifecycle.open_bridge_window_ids : []) {
      const text = stringOrEmpty(id);
      if (text) ids.add(text);
    }
    const statusIndex = lifecycle.status_index && typeof lifecycle.status_index === "object" ? lifecycle.status_index : {};
    for (const id of Object.keys(statusIndex)) {
      const text = stringOrEmpty(id);
      if (text) ids.add(text);
    }
    const bindings = source.bindings && typeof source.bindings === "object" ? source.bindings : {};
    const bridgeWindows = bindings.bridge_windows && typeof bindings.bridge_windows === "object" ? bindings.bridge_windows : {};
    for (const id of Object.keys(bridgeWindows)) {
      const text = stringOrEmpty(id);
      if (text) ids.add(text);
    }
    const result = source.last_bridge_result && typeof source.last_bridge_result === "object" ? source.last_bridge_result : {};
    const resultId = stringOrEmpty(result.bridge_window_id || result.bridgeWindowId);
    if (resultId) ids.add(resultId);
  }
  return ids;
}

function runtimeBridgeWindowStatus(snapshot, runLedger, bridgeWindowId) {
  const id = stringOrEmpty(bridgeWindowId);
  if (!id) return "";
  for (const source of [snapshot, runLedger]) {
    const lifecycle = source?.lifecycle && typeof source.lifecycle === "object" ? source.lifecycle : {};
    const statusIndex = lifecycle.status_index && typeof lifecycle.status_index === "object" ? lifecycle.status_index : {};
    const status = stringOrEmpty(statusIndex[id]);
    if (status) return status;
  }
  return "";
}

function firstRuntimeBridgeWindowId(snapshot, runLedger, candidates = []) {
  const ids = runtimeBridgeWindowIds(snapshot, runLedger);
  for (const value of candidates) {
    const id = stringOrEmpty(value);
    if (id && ids.has(id)) return id;
  }
  return "";
}

function latestRuntimeBridgeWindowId(snapshot, runLedger) {
  const candidates = [];
  for (const source of [snapshot, runLedger]) {
    const lifecycle = source?.lifecycle && typeof source.lifecycle === "object" ? source.lifecycle : {};
    candidates.push(...(Array.isArray(lifecycle.open_bridge_window_ids) ? lifecycle.open_bridge_window_ids : []));
    const result = source?.last_bridge_result && typeof source.last_bridge_result === "object" ? source.last_bridge_result : {};
    candidates.push(result.bridge_window_id, result.bridgeWindowId);
    const statusIndex = lifecycle.status_index && typeof lifecycle.status_index === "object" ? lifecycle.status_index : {};
    candidates.push(...Object.keys(statusIndex).reverse());
  }
  return firstRuntimeBridgeWindowId(snapshot, runLedger, candidates);
}

function compactPacketSummaryForResponse(packetSummary) {
  if (!packetSummary || typeof packetSummary !== "object") return packetSummary || null;
  const packet = packetSummary.packet && typeof packetSummary.packet === "object" ? packetSummary.packet : null;
  const rawSummary = packetSummary.packetSummary && typeof packetSummary.packetSummary === "object" ? packetSummary.packetSummary : null;
  return {
    objective: compactText(packetSummary.objective),
    targetPhase: packetSummary.targetPhase || "unknown",
    completionSummary: compactText(packetSummary.completionSummary),
    reportSummary: compactText(packetSummary.reportSummary),
    packet: packet ? {
      bridge_packet_id: packet.bridge_packet_id || packet.packet_id || packet.id || undefined,
      target_phase: packet.target_phase || undefined,
      team_id: packet.team_id || packet.teamId || undefined,
      task_id: packet.task_id || packet.taskId || undefined
    } : undefined,
    packetSummary: rawSummary ? {
      bridge_window_id: rawSummary.bridge_window_id || rawSummary.bridgeWindowId || undefined,
      target_phase: rawSummary.target_phase || undefined,
      team_id: rawSummary.team_id || rawSummary.teamId || undefined,
      task_id: rawSummary.task_id || rawSummary.taskId || undefined,
      status: rawSummary.status || undefined
    } : undefined
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
    if (member.rawRefs.length < 50) member.rawRefs.push({ sourceFile: "session_bindings.jsonl" });
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
    if (member.rawRefs.length < 50) member.rawRefs.push(compactRawRef(event.rawRef));
  }

  if (!members.size) {
    ensure("runtime", {
      role: "runtime",
      displayName: "No bound sessions yet"
    });
  }
  return [...members.values()]
    .map(member => ({
      ...member,
      activeTool: member.activeTool ? compactToolForMember(member.activeTool) : null,
      lastCompletedTool: member.lastCompletedTool ? compactToolForMember(member.lastCompletedTool) : null,
      rawRefs: member.rawRefs.slice(-50).map(compactRawRef)
    }))
    .sort((a, b) => String(a.displayName).localeCompare(String(b.displayName)));
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

async function buildStatus(repoKey, runId, options = {}) {
  const includeDetail = Boolean(options.includeDetail);
  const data = await loadCompanionData(repoKey, runId, {
    events: options.events || (includeDetail ? {} : statusEventLoadOptions())
  });
  if (!data) return null;
  const packetSummary = packetSummaryFrom(data.snapshot, data.runLedger, data.events);
  const lifecycleState = latestLifecycleState(data.snapshot || data.runLedger);
  const latestEvent = compactEventForResponse(data.events.at(-1) || null, { omitRaw: true });
  const team = teamFrom(data);
  const activityFeed = data.events
    .slice(-STATUS_ACTIVITY_EVENT_LIMIT)
    .map(event => compactEventForResponse(event, { omitRaw: true }));
  const trajectory = data.trajectory
    .slice(-STATUS_TRAJECTORY_LIMIT)
    .map(step => compactTrajectoryForResponse(step));
  const compactDetail = {
    snapshotRefs: data.snapshot?.snapshot_refs || {},
    snapshotUpdatedAt: data.snapshot?.updated_at || null,
    runLedgerLoaded: Boolean(data.runLedger),
    activeOperationKeys: data.activeOperations && typeof data.activeOperations === "object"
      ? Object.keys(data.activeOperations).slice(0, 80)
      : [],
    sessionBindingCount: data.sessionBindings.length,
    eventCount: data.events.length,
    trajectoryCount: data.trajectory.length,
    displayedActivityCount: activityFeed.length,
    displayedTrajectoryCount: trajectory.length
  };
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
    packetSummary: compactPacketSummaryForResponse(packetSummary),
    teammates: team,
    activityFeed,
    trajectory,
    eventCount: data.events.length,
    trajectoryCount: data.trajectory.length,
    unknowns: unknownsFor(data),
    detail: includeDetail ? {
      ...compactDetail,
      snapshot: data.snapshot,
      runLedger: data.runLedger,
      activeOperations: data.activeOperations,
      sessionBindings: data.sessionBindings.map(item => ({
        ...item,
        rawRef: compactRawRef(item.rawRef),
        rawLine: undefined
      })),
      events: data.events.slice(-STATUS_ACTIVITY_EVENT_LIMIT).map(event => compactEventForResponse(event, { omitRaw: true })),
      trajectory
    } : compactDetail,
    streamContract: {
      transport: "sse",
      primarySources: ["sdk_stream_events.jsonl", "tool_events.jsonl"],
      fallbackSources: runSourceFiles.filter(file => !["sdk_stream_events.jsonl", "tool_events.jsonl"].includes(file)),
      readOnly: true
    }
  };
}

function compactTuiViewForProjection(view) {
  if (!view || typeof view !== "object") return view;
  const compactRefs = (items, limit = 8) => Array.isArray(items) ? items.filter(Boolean).slice(-limit) : [];
  const compactDisplayItem = item => {
    if (!item || typeof item !== "object") return item;
    const { inspector, ...rest } = item;
    const out = {
      ...rest,
      title: typeof rest.title === "string" ? compactText(rest.title) : rest.title,
      body: typeof rest.body === "string" ? compactText(rest.body) : rest.body,
      text: typeof rest.text === "string" ? compactText(rest.text) : rest.text,
      subtitle: typeof rest.subtitle === "string" ? compactText(rest.subtitle) : rest.subtitle,
      rawRefs: compactRefs(item.rawRefs),
      evidenceRefs: compactRefs(item.evidenceRefs),
      fileRefs: compactRefs(item.fileRefs)
    };
    if (Array.isArray(item.items)) out.items = item.items.slice(-20).map(compactDisplayItem);
    if (Array.isArray(item.children)) out.children = item.children.slice(-20).map(compactDisplayItem);
    return out;
  };
  const teamTree = Array.isArray(view.teamTree) ? view.teamTree.map(compactDisplayItem) : [];
  const activityItems = Array.isArray(view.activityItems) ? view.activityItems.slice(0, 20).map(compactDisplayItem) : [];
  const completion = compactDisplayItem(view.completion);
  const visibleIds = new Set();
  const collectIds = item => {
    if (!item || typeof item !== "object") return;
    if (item.id) visibleIds.add(String(item.id));
    for (const child of [...(Array.isArray(item.items) ? item.items : []), ...(Array.isArray(item.children) ? item.children : [])]) {
      collectIds(child);
    }
  };
  [view.mainReport, completion, ...teamTree, ...activityItems].forEach(collectIds);
  const inspectorIndex = {};
  for (const [id, payload] of Object.entries(view.inspectorIndex || {})) {
    if (visibleIds.size && !visibleIds.has(String(id))) continue;
    if (Object.keys(inspectorIndex).length >= 15) break;
    inspectorIndex[id] = {
      id: payload.id || id,
      displayKey: payload.displayKey,
      kind: payload.kind,
      title: compactText(payload.title),
      rawRefs: compactRefs(payload.rawRefs, 8),
      sourceCursors: compactRefs(payload.sourceCursors, 8),
      evidenceRefs: compactRefs(payload.evidenceRefs, 8),
      snapshotRefs: payload.snapshotRefs || {}
    };
  }
  return {
    ...view,
    mainReport: compactDisplayItem(view.mainReport),
    teamTree,
    activityItems,
    completion,
    inspectorIndex
  };
}

async function buildProjection(repoKey, runId) {
  const data = await loadCompanionData(repoKey, runId, { events: projectionEventLoadOptions() });
  if (!data) return null;
  const events = data.events || [];
  const projectionEvents = projectionReducerEvents(events);
  const unknowns = unknownsFor(data);
  const latestPacketEvent = [...events].reverse().find(event => event.source === "bridge_packet" || event.raw?.packet || event.raw?.payload?.packet);
  const latestPacketBridgeWindowId = eventBridgeWindowId(latestPacketEvent);
  const runtimeKnownBridgeWindowId = firstRuntimeBridgeWindowId(data.snapshot, data.runLedger, [
    latestPacketBridgeWindowId,
    data.snapshot?.last_bridge_result?.bridge_window_id ||
      data.snapshot?.last_bridge_result?.bridgeWindowId,
    latestRuntimeBridgeWindowId(data.snapshot, data.runLedger)
  ]);
  const observedUncommittedBridgeWindowId =
    latestPacketBridgeWindowId && latestPacketBridgeWindowId !== runtimeKnownBridgeWindowId
      ? latestPacketBridgeWindowId
      : "";
  const activeBridgeWindowId = observedUncommittedBridgeWindowId || runtimeKnownBridgeWindowId;
  const packetSummary = packetSummaryFrom(data.snapshot, data.runLedger, data.events, {
    bridgeWindowId: activeBridgeWindowId
  });
  const activeTeamId =
    packetSummary.packetSummary?.team_id ||
    packetSummary.packetSummary?.teamId ||
    packetSummary.packet?.team_id ||
    packetSummary.packet?.teamId ||
    data.snapshot?.last_bridge_result?.team_id ||
    data.snapshot?.last_bridge_result?.teamId ||
    latestPacketEvent?.teamId;
  const activeTaskId =
    packetSummary.packetSummary?.task_id ||
    packetSummary.packetSummary?.taskId ||
    packetSummary.packet?.task_id ||
    packetSummary.packet?.taskId ||
    data.snapshot?.last_bridge_result?.task_id ||
    data.snapshot?.last_bridge_result?.taskId ||
    latestPacketEvent?.taskId;
  const activeLifecycleState = observedUncommittedBridgeWindowId
    ? "observed_uncommitted"
    : runtimeBridgeWindowStatus(data.snapshot, data.runLedger, activeBridgeWindowId) || latestLifecycleState(data.snapshot || data.runLedger);
  const tuiView = compactTuiViewForProjection(reduceToTuiView(projectionEvents, data.snapshot, data.activeOperations, {
    repoKey,
    runId,
    packetSummary,
    runLedger: data.runLedger,
    sessionBindings: data.sessionBindings,
    unknowns,
    currentBridgeWindowId: activeBridgeWindowId || null,
    observedUncommittedBridgeWindowId: observedUncommittedBridgeWindowId || null,
    lifecycleStateOverride: observedUncommittedBridgeWindowId ? "observed_uncommitted" : null,
    currentTeamId: activeTeamId || null,
    currentTaskId: activeTaskId || null
  }));
  const leaderReportCards = projectLeaderReportCards(events).slice(-3);
  const projectedTuiView = promoteVisibleMainReport(tuiView, leaderReportCards);
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
      lifecycleState: activeLifecycleState,
      bridgeWindowId: activeBridgeWindowId || undefined,
      observedBridgeWindowId: observedUncommittedBridgeWindowId || undefined,
      bridgeWindowAuthority: observedUncommittedBridgeWindowId ? "observer_only" : activeBridgeWindowId ? "authoritative" : "unknown",
      warning: observedUncommittedBridgeWindowId
        ? "Latest observed bridge window is not present in run_ledger/runtime_snapshot; treat it as diagnostic evidence, not authoritative workflow state."
        : undefined,
      teamId: activeTeamId || undefined,
      taskId: activeTaskId || undefined,
      packetRef: _projectionRawRef(latestPacketEvent),
      completionSummary: packetSummary.completionSummary,
      reportSummary: packetSummary.reportSummary
    },
    timeline: projectionEvents.slice(-15).map(projectTimelineEvent),
    liveToolCards: projectionEvents.filter(event => event.source === "hook_tool_event").slice(-6).map(projectToolCard),
    agentMessageCards: projectionEvents.filter(event => event.source === "agent_message" || event.source === "sdk_stream" || event.source === "outer_host").slice(-6).map(projectMessageCard),
    artifactCards: projectionEvents.filter(event => event.source === "artifact" || event.evidenceRefs?.length).slice(-5).map(projectArtifactCard),
    completionChecklist: projectCompletionChecklist(events),
    leaderReportCards,
    failureRetryLane: projectionEvents.filter(event => event.lane === "failures" || ["failed", "blocked", "rejected"].includes(String(event.status || ""))).slice(-6).map(projectTimelineEvent),
    semanticCoverageMatrix: projectSemanticCoverage(events).slice(-10),
    rawJsonRefs: projectionEvents.slice(-10).map(event => ({ eventId: event.eventId, rawRef: compactRawRef(event.rawRef), sourceAuthority: event.runtimeEvent?.authority || "observed" })),
    unknowns,
    tuiView: projectedTuiView
  };
}

function promoteVisibleMainReport(tuiView, leaderReportCards) {
  const summary = String(tuiView?.mainReport?.summary || "");
  const shouldPromote =
    /\bNO_BRIDGE_DECISION\b/i.test(summary) ||
    cardHasTruncatedReportText(tuiView?.mainReport || {});
  if (!shouldPromote) return tuiView;
  const candidates = [...(leaderReportCards || [])].filter(card => {
    const cardSummary = String(card?.summary || "");
    return Number(card?.reportCount || 0) > 1 && !/\bNO_BRIDGE_DECISION\b/i.test(cardSummary);
  });
  const replacement = candidates.sort((a, b) => {
    const quality = reportCardQuality(b) - reportCardQuality(a);
    if (quality !== 0) return quality;
    return String(b?.summary || "").length - String(a?.summary || "").length;
  })[0];
  if (!replacement) return tuiView;
  const replacementSummary = compactReportText(replacement.summary || replacement.messagePreview || "");
  return {
    ...tuiView,
    mainReport: {
      ...tuiView.mainReport,
      displayKey: replacement.displayKey || tuiView.mainReport?.displayKey,
      status: replacement.reportStatus || replacement.status || tuiView.mainReport?.status,
      handledBy: replacement.handledBy || tuiView.mainReport?.handledBy,
      summary: replacementSummary,
      body: replacementSummary,
      reportSections: replacement.reportSections || [],
      rawRefs: replacement.rawRef ? [compactRawRef(replacement.rawRef)] : tuiView.mainReport?.rawRefs || [],
      evidenceRefs: replacement.evidenceRefs || tuiView.mainReport?.evidenceRefs || []
    }
  };
}

function projectionReducerEvents(events) {
  const recentEvents = events.slice(-PROJECTION_EVENT_WINDOW);
  const reportEvents = events
    .filter(event => bridgeResultFromEvent(event) || (event.source === "outer_host" && event.raw?.event_kind === "outer_leader_result"))
    .slice(-40);
  const byId = new Map();
  for (const event of [...reportEvents, ...recentEvents]) {
    byId.set(event.eventId || `${event.source}:${event.seq}`, event);
  }
  return sortEvents([...byId.values()]);
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

function compactEvidenceForCard(value) {
  if (!value) return null;
  if (typeof value === "string") return compactText(value);
  if (Array.isArray(value)) return value.slice(0, 8).map(item => compactText(item));
  if (typeof value === "object") {
    return {
      summary: compactText(value),
      keys: Object.keys(value).slice(0, 12)
    };
  }
  return compactText(value);
}

function projectLeaderReportCards(events) {
  const bridgeResults = events.filter(event => bridgeResultFromEvent(event));
  const outerResults = events.filter(event => event.source === "outer_host" && event.raw?.event_kind === "outer_leader_result");
  const sdkResults = events.filter(event => {
    const sdkType = String(event.raw?.sdk_message_type || event.raw?.event_type || "");
    return event.source === "sdk_stream" && ["ResultMessage", "sdk_stream_final_result"].includes(sdkType);
  });
  return dedupeLeaderReportCards([...bridgeResults, ...outerResults, ...sdkResults])
    .slice(-40)
    .map(projectLeaderReportCard)
    .map(card => backfillLeaderReportCardSections(card, events));
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
    const preferSource = event.source === "outer_host" && existing?.event?.source !== "outer_host";
    const quality = reportCardQuality(card);
    const existingQuality = existing ? reportCardQuality(existing.card) : -Infinity;
    const preferQuality = quality > existingQuality;
    const preferLength =
      quality === existingQuality &&
      String(card.summary || "").length > String(existing.card.summary || "").length;
    const preferLater =
      quality === existingQuality &&
      String(card.summary || "").length === String(existing.card.summary || "").length;
    if (!existing || preferSource || preferQuality || preferLength || preferLater) {
      byKey.set(key, { event, card });
    }
  }
  return sortEvents([...byKey.values()].map(item => item.event));
}

function reportCardQuality(card) {
  const summary = String(card?.summary || "");
  let score = 0;
  if (Number(card?.reportCount || 0) > 1) score += 2;
  if (card?.handledBy === "outer_sdk_host_latest_bridge_reports") score += 1;
  if (/runtime_snapshot\.last_bridge_result\.reports\[\d+\]/.test(summary)) score -= 4;
  if (/reports_preview\[\d+\]/.test(summary)) score -= 2;
  if (cardHasTruncatedReportText(card)) score -= 3;
  return score;
}

function cardHasTruncatedReportText(card) {
  const values = [
    card?.summary,
    ...(Array.isArray(card?.reportSections) ? card.reportSections.map(section => section.summary) : [])
  ];
  return values.some(value => /<truncated(?:\s+\d+\s+chars)?>/i.test(String(value || "")));
}

function backfillLeaderReportCardSections(card, events) {
  if (!card || !needsLeaderReportBackfill(card)) return card;
  const bridgeWindowId = bridgeWindowIdFromLeaderReportCard(card);
  const fullBridgeCard = latestFullBridgeReportCard(events, bridgeWindowId);
  if (!fullBridgeCard || !Array.isArray(fullBridgeCard.reportSections) || !fullBridgeCard.reportSections.length) return card;
  const systemSections = Array.isArray(card.reportSections)
    ? card.reportSections.filter(section => isSystemReportSection(section))
    : [];
  const reportSections = [
    ...systemSections,
    ...fullBridgeCard.reportSections.filter(section => !isSystemReportSection(section))
  ];
  return {
    ...card,
    summary: fullBridgeCard.summary || card.summary,
    reportCount: reportSections.length || fullBridgeCard.reportCount || card.reportCount,
    reportSections,
    backfilledFrom: fullBridgeCard.rawRef || fullBridgeCard.eventId || undefined
  };
}

function needsLeaderReportBackfill(card) {
  if (card?.handledBy === "outer_sdk_host_latest_bridge_reports") return true;
  if (cardHasTruncatedReportText(card)) return true;
  return Array.isArray(card?.reportSections) && card.reportSections.some(section => {
    const name = String(section?.name || section?.source || "");
    return /runtime_snapshot\.last_bridge_result\.reports\[\d+\]/.test(name);
  });
}

function latestFullBridgeReportCard(events, bridgeWindowId = "") {
  const bridgeCards = events
    .filter(event => bridgeResultFromEvent(event))
    .map(projectLeaderReportCard)
    .filter(candidate => Number(candidate.reportCount || 0) > 1 && !cardHasTruncatedReportText(candidate));
  const scoped = bridgeWindowId
    ? bridgeCards.filter(candidate => candidate.bridgeWindowId === bridgeWindowId)
    : bridgeCards;
  return scoped.at(-1) || bridgeCards.at(-1) || null;
}

function bridgeWindowIdFromLeaderReportCard(card) {
  const direct = stringOrEmpty(card?.bridgeWindowId);
  if (direct) return direct;
  const text = [
    card?.summary,
    ...(Array.isArray(card?.reportSections) ? card.reportSections.map(section => section.summary) : [])
  ].map(value => String(value || "")).join(" ");
  const match = text.match(/\bbridge_window_id=([A-Za-z0-9_.:-]+)/);
  return match ? match[1] : "";
}

function isSystemReportSection(section) {
  const text = String(section?.name || section?.title || section?.source || "");
  return text === "outer_sdk_host_latest_bridge_reports" || text === "System recovery note";
}

function projectLeaderReportCard(event) {
  const raw = event.raw || {};
  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  const bridgeResult = bridgeResultFromEvent(event);
  if (bridgeResult) {
    const reports = leaderReportsFromBridgeResult(bridgeResult);
    const evidence = bridgeResult.evidence || raw.evidence || null;
    return {
      ...projectTimelineEvent(event),
      handledBy: raw.agent_id || raw.agent_type || "main-leader",
      reportStatus: bridgeResult.status || raw.status || event.status || "unknown",
      summary: summarizeBridgeResult(bridgeResult),
      error: bridgeResult.error_or_null || raw.error_or_null || null,
      evidence: compactEvidenceForCard(evidence),
      reportCount: reports.length,
      reportSections: reportSectionsFromReports(reports)
    };
  }
  const leaderResult = payload.leader_result && typeof payload.leader_result === "object" ? payload.leader_result : null;
  const reports = Array.isArray(leaderResult?.reports) ? leaderResult.reports : [];
  const evidence = leaderResult?.evidence || raw.evidence || null;
  return {
    ...projectTimelineEvent(event),
    handledBy: leaderResult?.handled_by || raw.handled_by || raw.source || undefined,
    reportStatus: leaderResult?.status || raw.status || event.status || "unknown",
    summary: summarizeLeaderResultReports(leaderResult) || raw.result || event.messagePreview || "",
    error: leaderResult?.error_or_null || raw.error_or_null || null,
    evidence: compactEvidenceForCard(evidence),
    reportCount: reports.length,
    reportSections: reportSectionsFromReports(reports)
  };
}

function bridgeResultFromEvent(event) {
  const raw = event?.raw || {};
  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  if (payload.bridge_result && typeof payload.bridge_result === "object") return payload.bridge_result;
  if (raw.bridge_result && typeof raw.bridge_result === "object") return raw.bridge_result;
  if (raw.result?.bridge_result && typeof raw.result.bridge_result === "object") return raw.result.bridge_result;
  return null;
}

function summarizeBridgeResult(bridgeResult) {
  const reports = leaderReportsFromBridgeResult(bridgeResult);
  const header = [
    `BridgeResult status=${bridgeResult?.status || "unknown"}`,
    `report_count=${reports.length}`
  ].join("; ");
  const reportLines = reports
    .map((report, index) => {
      const name = report?.teammate_name || report?.agent_type || report?.role || `report ${index + 1}`;
      return `${name}: ${report?.summary || "report recorded"}`;
    })
    .filter(Boolean);
  return compactReportText([header, ...reportLines].join("\n\n"));
}

function leaderReportsFromBridgeResult(bridgeResult) {
  if (Array.isArray(bridgeResult?.reports) && bridgeResult.reports.length) return bridgeResult.reports;
  if (Array.isArray(bridgeResult?.reports_preview)) return bridgeResult.reports_preview;
  return [];
}

function summarizeLeaderResultReports(leaderResult) {
  const reports = Array.isArray(leaderResult?.reports) ? leaderResult.reports : [];
  if (!reports.length) return "";
  if (reports.length === 1) return compactReportText(reports[0]?.summary || reports[0]?.message || "leader result recorded");
  const header = [
    `LeaderResult status=${leaderResult?.status || "unknown"}`,
    leaderResult?.handled_by ? `handled_by=${leaderResult.handled_by}` : "",
    `report_count=${reports.length}`
  ].filter(Boolean).join("; ");
  const reportLines = reports
    .map((report, index) => {
      const name =
        report?.teammate_or_source ||
        report?.teammate_name ||
        report?.teammateName ||
        report?.teammate_id ||
        report?.teammateId ||
        report?.agent_type ||
        report?.role ||
        report?.source ||
        `report ${index + 1}`;
      return `${name}: ${report?.summary || report?.message || "report recorded"}`;
    })
    .filter(Boolean);
  return compactReportText([header, ...reportLines].join("\n\n"));
}

function reportSectionsFromReports(reports) {
  if (!Array.isArray(reports)) return [];
  return reports
    .map((report, index) => {
      if (!report || typeof report !== "object") return null;
      const name =
        report.teammate_or_source ||
        report.teammate_name ||
        report.teammateName ||
        report.teammate_id ||
        report.teammateId ||
        report.agent_type ||
        report.role ||
        report.source ||
        `report ${index + 1}`;
      const summary = displayReportText(report.summary || report.message || "report recorded");
      return {
        name,
        title: reportSectionTitle(name, index),
        summary,
        status: report.status || undefined,
        classification: report.classification || undefined,
        source: report.source || undefined
      };
    })
    .filter(Boolean);
}

function reportSectionTitle(name, index) {
  const text = String(name || "").trim();
  const anomaly = text.match(/^anomaly-analyst-([a-z])$/i);
  if (anomaly) return `Anomaly analyst ${anomaly[1].toUpperCase()}`;
  if (text === "outer_sdk_host_latest_bridge_reports") return "System recovery note";
  if (/runtime_snapshot\.last_bridge_result\.reports\[\d+\]/.test(text)) return `Anomaly report ${index}`;
  return text || `Report ${index + 1}`;
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
      name: compactText(item.name || "check"),
      status: item.status || "unknown",
      subject: compactText(item.subject || ""),
      message: compactText(item.message || ""),
      evidenceRef: item.evidence_ref || null
    });
  }
  if (!items.length && Array.isArray(raw.items)) {
    for (const item of raw.items) {
      if (!item || typeof item !== "object") continue;
      items.push({
        name: compactText(item.id || "contract_item"),
        status: item.status || "unknown",
        subject: compactText(item.text || ""),
        message: compactText(item.reason || ""),
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
        item: compactText(item),
        disposition: coverageDisposition(disposition),
        teammateId: event.actor?.teammateId || event.actor?.displayName || event.actor?.role || undefined,
        evidenceRefs: Array.isArray(report.evidence_refs) ? report.evidence_refs.slice(0, 6) : [],
        rawRef: compactRawRef(event.rawRef)
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
  if (!safeFile && !runJsonFiles.includes(sourceFile)) return null;
  if (!Number.isInteger(Number(sourceOffset)) || Number(sourceOffset) < 1) return null;
  const dir = runDir(repoKey, runId);
  if (!dir) return null;
  if (runJsonFiles.includes(sourceFile)) {
    if (Number(sourceOffset) !== 1) return null;
    const filePath = path.join(dir, sourceFile);
    const rawLine = await readFile(filePath, "utf8").catch(() => "");
    if (!rawLine) return null;
    let record = null;
    let parseError = null;
    try {
      record = JSON.parse(rawLine);
    } catch (error) {
      parseError = `${error.name}: ${error.message}`;
    }
    return {
      record,
      rawLine,
      parseError,
      sourceFile,
      sourceOffset: 1,
      sourceSequence: 1,
      sourceByteOffset: 0,
      rawRef: {
        sourceFile,
        sourceOffset: 1,
        sourceSequence: 1,
        sourceByteOffset: 0,
        sourceKind: "json"
      }
    };
  }
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

function requestJson(url, payload = null, headers = {}, method = "POST", options = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const body = payload === null || payload === undefined ? "" : JSON.stringify(payload);
    const transport = target.protocol === "http:" ? http : https;
    const timeoutMs = Math.max(1000, Number(options.timeoutMs || REQUEST_JSON_TIMEOUT_MS));
    let settled = false;
    const settle = (fn, value) => {
      if (settled) return;
      settled = true;
      fn(value);
    };
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
          if (res.statusCode >= 200 && res.statusCode < 300) settle(resolve, parsed);
          else settle(reject, new Error(`brief api http ${res.statusCode}: ${data.slice(0, 500)}`));
        } catch (error) {
          settle(reject, error);
        }
      });
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error(`request_json_timeout ${timeoutMs}ms ${target.pathname}`));
    });
    req.on("error", error => settle(reject, error));
    if (body) req.write(body);
    req.end();
  });
}

function httpsJson(url, payload, headers = {}) {
  return requestJson(url, payload, headers, "POST", { timeoutMs: BRIEF_REQUEST_TIMEOUT_MS });
}

function normalizeLeaderInput(input) {
  const payload = input && typeof input === "object" ? { ...input } : {};
  if (!String(payload.text || "").trim() && String(payload.message || "").trim()) {
    payload.text = String(payload.message);
  }
  const targetPhase = normalizeExplicitLeaderTargetPhase(
    payload.target_phase || payload.targetPhase || payload.target_phase_label || payload.targetPhaseLabel
  );
  const dispatchIntent = normalizeLeaderDispatchIntent(payload.dispatch_intent || payload.dispatchIntent);
  delete payload.targetPhase;
  delete payload.target_phase_label;
  delete payload.targetPhaseLabel;
  delete payload.dispatchIntent;
  if (targetPhase) payload.target_phase = targetPhase;
  if (dispatchIntent) payload.dispatch_intent = dispatchIntent;
  if (!payload.dispatch_intent && targetPhase) {
    payload.dispatch_intent = "advance_or_continue";
  }
  if (!payload.dispatch_intent) {
    const defaultIntent = defaultLeaderDispatchIntent(payload);
    if (defaultIntent) payload.dispatch_intent = defaultIntent;
  }
  return payload;
}

function normalizeExplicitLeaderTargetPhase(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (!normalized) return "";
  const compact = normalized.replace(/[\s_-]+/g, "");
  if (["l2", "l2advisory"].includes(compact)) return "l2_advisory";
  if (["l3", "l3bridge"].includes(compact)) return "l3_bridge";
  if (["l4anomaly"].includes(compact)) return "l4_anomaly";
  if (["l4implement", "l4impement", "l4implemnt", "l4implment"].includes(compact)) return "l4_implement";
  if (["l4execute"].includes(compact)) return "l4_execute";
  return normalized;
}

function normalizeLeaderDispatchIntent(value) {
  const normalized = String(value || "").trim();
  if (!normalized) return "";
  const compact = normalized.toLowerCase().replace(/[\s_-]+/g, "_");
  if (["advance_or_continue", "inspect_only", "leader_decide", "user_answer"].includes(compact)) return compact;
  return normalized;
}

function defaultLeaderDispatchIntent(payload) {
  const inputKind = String(payload.input_kind || payload.kind || "user_prompt").trim().toLowerCase();
  if (inputKind === "user_answer" || inputKind === "clarification_answer") return "user_answer";
  if (payload.inspect_only === true || payload.inspectOnly === true) return "inspect_only";
  if (inputKind === "inspect" || inputKind === "inspect_only" || inputKind === "status") return "inspect_only";
  if (inputKind === "advance" || inputKind === "continue") return "advance_or_continue";
  if (inputKind === "user_prompt" || inputKind === "prompt") return "leader_decide";
  return "";
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
  return requestJson(endpoint, { ...normalizeLeaderInput(input), source: "bridge_companion_gateway" }, headers, "POST", {
    timeoutMs: LEADER_INPUT_TIMEOUT_MS
  });
}

async function submitLeaderInputAsync(input) {
  if (!OUTER_HOST_URL) {
    return {
      accepted: false,
      error: "outer_host_not_configured",
      message: "Set BRIDGE_OUTER_HOST_URL to enable UI-to-host user input forwarding."
    };
  }
  const endpoint = new URL("/v1/input", OUTER_HOST_URL);
  endpoint.searchParams.set("async", "1");
  const headers = OUTER_HOST_TOKEN ? { "x-bridge-outer-host-token": OUTER_HOST_TOKEN } : {};
  return requestJson(endpoint.toString(), { ...normalizeLeaderInput(input), source: "bridge_companion_gateway" }, headers, "POST", {
    timeoutMs: LEADER_INPUT_ACK_TIMEOUT_MS
  });
}

function leaderInputWaitsForResult(input, searchParams) {
  if (truthy(searchParams?.get("wait")) || String(searchParams?.get("mode") || "").toLowerCase() === "sync") {
    return true;
  }
  const payload = input && typeof input === "object" ? input : {};
  return truthy(payload.wait_for_result || payload.waitForResult || payload.sync);
}

function truthy(value) {
  return ["1", "true", "yes", "on"].includes(String(value || "").trim().toLowerCase());
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
  return requestJson(endpoint, null, headers, "GET", { timeoutMs: REQUEST_JSON_TIMEOUT_MS });
}

async function outerHostHealth() {
  if (!OUTER_HOST_URL) {
    return {
      ok: false,
      error: "outer_host_not_configured",
      message: "Set BRIDGE_OUTER_HOST_URL to enable outer host health discovery."
    };
  }
  const endpoint = new URL("/health", OUTER_HOST_URL).toString();
  const headers = OUTER_HOST_TOKEN ? { "x-bridge-outer-host-token": OUTER_HOST_TOKEN } : {};
  return requestJson(endpoint, null, headers, "GET", { timeoutMs: OUTER_HOST_HEALTH_TIMEOUT_MS });
}

function selectedDebugEnv() {
  const env = {};
  for (const key of DEBUG_ENV_KEYS) {
    if (Object.prototype.hasOwnProperty.call(process.env, key)) {
      env[key] = process.env[key];
    }
  }
  return env;
}

async function pathStatus(filePath) {
  const stats = await stat(filePath).catch(() => null);
  return {
    path: filePath,
    exists: Boolean(stats),
    isDirectory: Boolean(stats?.isDirectory?.()),
    isFile: Boolean(stats?.isFile?.())
  };
}

async function gatewayDebugSnapshot({ includeOuterHost = true, includeEvents = false, eventLineLimit = 80 } = {}) {
  let outerHost = null;
  if (includeOuterHost) {
    try {
      outerHost = await outerHostStatus();
    } catch (error) {
      outerHost = { ok: false, error: error.message };
    }
  }
  const recentRuntime = includeEvents ? await latestRuntimeDebugTail(outerHost, eventLineLimit) : null;
  return redactForResponse({
    schemaVersion: "bridge_companion_debug.v1",
    generatedAt: new Date().toISOString(),
    source: "bridge_companion_gateway_process",
    process: {
      pid: process.pid,
      ppid: process.ppid,
      cwd: process.cwd(),
      argv: process.argv,
      execPath: process.execPath,
      nodeVersion: process.version,
      platform: process.platform,
      arch: process.arch,
      uptimeSeconds: Math.round(process.uptime())
    },
    gateway: {
      host: HOST,
      port: PORT,
      companionRoot,
      workspaceRoot,
      prototypeRoot,
      projectsRoot: PROJECTS_ROOT,
      registryRoot: REGISTRY_ROOT,
      sessionObserverRoot: SESSION_OBSERVER_ROOT,
      streamIntervalMs: STREAM_INTERVAL_MS,
      readOnly: true,
      outerHostUrl: OUTER_HOST_URL || null,
      outerHostConfigured: Boolean(OUTER_HOST_URL),
      companionCredentialConfigured: Boolean(COMPANION_TOKEN),
      outerHostCredentialConfigured: Boolean(OUTER_HOST_TOKEN),
      terminalEnabled: terminalEnabled(),
      activeSseClientCount: activeSseClients.size,
      shuttingDown: gatewayShuttingDown
    },
    paths: {
      projectsRoot: await pathStatus(PROJECTS_ROOT),
      registryRoot: await pathStatus(REGISTRY_ROOT),
      sessionObserverRoot: await pathStatus(SESSION_OBSERVER_ROOT),
      prototypeRoot: await pathStatus(prototypeRoot)
    },
    env: selectedDebugEnv(),
    envChecks: {
      bridgeOuterHostUrlConfigured: Boolean(process.env.BRIDGE_OUTER_HOST_URL || process.env.OUTER_SDK_HOST_URL),
      bridgeClaudeCommandConfigured: Boolean(process.env.BRIDGE_CLAUDE_COMMAND),
      bridgeClaudeCliConfigured: Boolean(process.env.BRIDGE_CLAUDE_CLI),
      outerLeaderClaudeCliConfigured: Boolean(process.env.OUTER_LEADER_CLAUDE_CLI),
      bridgeDisableStartupDefaults: ["1", "true", "yes"].includes(String(process.env.BRIDGE_DISABLE_CLAUDE_STARTUP_DEFAULTS || "").trim().toLowerCase()),
      processHasAnthropicBaseUrl: Boolean(process.env.ANTHROPIC_BASE_URL),
      processHasAnthropicAuthCredential: Boolean(process.env.ANTHROPIC_AUTH_TOKEN),
      processHasHttpProxy: Boolean(process.env.HTTP_PROXY || process.env.http_proxy),
      processHasHttpsProxy: Boolean(process.env.HTTPS_PROXY || process.env.https_proxy)
    },
    outerHost,
    recentRuntime
  });
}

function terminalEnabled() {
  const explicit = String(process.env.BRIDGE_COMPANION_ENABLE_TERMINAL || "").trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(explicit)) return true;
  if (["0", "false", "no", "off"].includes(explicit)) return false;
  return ["127.0.0.1", "localhost", "::1"].includes(String(HOST || "").trim().toLowerCase());
}

async function runTerminalCommand(input = {}) {
  if (!terminalEnabled()) {
    return {
      ok: false,
      error: "terminal_disabled",
      message: "Set BRIDGE_COMPANION_ENABLE_TERMINAL=1 or bind the gateway to loopback to enable the terminal endpoint."
    };
  }
  const command = String(input.command || "").trim();
  if (!command) {
    return { ok: false, error: "empty_command", message: "command is required" };
  }
  if (command.length > 6000) {
    return { ok: false, error: "command_too_long", message: "command exceeds 6000 characters" };
  }
  const cwd = await resolveTerminalCwd(input.cwd);
  const timeoutMs = Math.max(1000, Math.min(Number(input.timeoutMs || input.timeout_ms || TERMINAL_TIMEOUT_MS) || TERMINAL_TIMEOUT_MS, 120000));
  const shell = terminalShell(command);
  const startedAt = new Date();
  return new Promise(resolve => {
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    let settled = false;
    let child = null;
    try {
      child = spawn(shell.command, shell.args, {
        cwd,
        env: { ...process.env, TERM: process.env.TERM || "dumb", NO_COLOR: process.env.NO_COLOR || "1" },
        windowsHide: true,
        detached: process.platform !== "win32"
      });
    } catch (error) {
      resolve(redactForResponse({
        ok: false,
        command,
        cwd,
        shell: shell.label,
        error: "spawn_failed",
        message: error.message,
        stdout: "",
        stderr: ""
      }));
      return;
    }
    const finish = (exitCode, signal) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const endedAt = new Date();
      resolve(redactForResponse({
        ok: !timedOut && exitCode === 0,
        command,
        cwd,
        shell: shell.label,
        exitCode,
        signal,
        timedOut,
        durationMs: endedAt.getTime() - startedAt.getTime(),
        startedAt: startedAt.toISOString(),
        endedAt: endedAt.toISOString(),
        stdout: limitTerminalText(stdout),
        stderr: limitTerminalText(stderr)
      }));
    };
    const append = (stream, chunk) => {
      const text = chunk.toString("utf8");
      if (stream === "stdout") stdout = appendLimited(stdout, text, TERMINAL_TEXT_LIMIT);
      else stderr = appendLimited(stderr, text, TERMINAL_TEXT_LIMIT);
    };
    const timer = setTimeout(() => {
      timedOut = true;
      try {
        if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGTERM");
        else child.kill("SIGTERM");
      } catch {
        try {
          child.kill("SIGKILL");
        } catch {}
      }
    }, timeoutMs);
    child.stdout.on("data", chunk => append("stdout", chunk));
    child.stderr.on("data", chunk => append("stderr", chunk));
    child.on("error", error => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(redactForResponse({
        ok: false,
        command,
        cwd,
        shell: shell.label,
        error: "spawn_failed",
        message: error.message,
        stdout: limitTerminalText(stdout),
        stderr: limitTerminalText(stderr)
      }));
    });
    child.on("close", finish);
  });
}

async function resolveTerminalCwd(value) {
  const raw = String(value || "").trim();
  const cwd = raw ? path.resolve(raw) : process.cwd();
  const stats = await stat(cwd).catch(() => null);
  if (!stats || !stats.isDirectory()) {
    throw new Error(`terminal cwd is not a directory: ${cwd}`);
  }
  return cwd;
}

function terminalShell(command) {
  if (process.platform === "win32") {
    return {
      label: "powershell",
      command: "powershell.exe",
      args: ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    };
  }
  const shell = process.env.SHELL || "/bin/bash";
  return { label: path.basename(shell), command: shell, args: ["-lc", command] };
}

function appendLimited(current, next, limit) {
  const combined = `${current}${next}`;
  if (combined.length <= limit) return combined;
  return combined.slice(combined.length - limit);
}

function limitTerminalText(value) {
  const text = redactText(String(value || ""), TERMINAL_TEXT_LIMIT);
  if (text.length <= TERMINAL_TEXT_LIMIT) return text;
  return `<truncated to latest ${TERMINAL_TEXT_LIMIT} chars>\n${text}`;
}

async function latestRuntimeDebugTail(outerHost, eventLineLimit) {
  const limit = Math.max(1, Math.min(Number(eventLineLimit) || 80, 500));
  const runsRoot = outerHost?.runtime_runs_root || outerHost?.runtimeRunsRoot || "";
  const runId = outerHost?.run_id || outerHost?.runId || "";
  if (!runsRoot || !runId) {
    return { ok: false, error: "outer host status does not include runtime_runs_root/run_id" };
  }
  const runRoot = path.resolve(String(runsRoot), String(runId));
  const projectsRoot = path.resolve(PROJECTS_ROOT);
  if (runRoot !== projectsRoot && !runRoot.startsWith(projectsRoot + path.sep)) {
    return { ok: false, error: "outer host run root is outside gateway projects root", runRoot, projectsRoot };
  }
  const files = ["sdk_stream_events.jsonl", "outer_host_events.jsonl", "tool_events.jsonl"];
  const result = {
    ok: true,
    repoKey: outerHost?.repo_key || outerHost?.repoKey || null,
    runId,
    runRoot,
    limit,
    files: {}
  };
  for (const sourceFile of files) {
    const filePath = path.join(runRoot, sourceFile);
    const stats = await stat(filePath).catch(() => null);
    if (!stats) {
      result.files[sourceFile] = { exists: false, records: [] };
      continue;
    }
    const records = await readJsonlWithMeta(filePath, sourceFile);
    result.files[sourceFile] = {
      exists: true,
      lineCount: records.length,
      byteSize: stats.size,
      records: records.slice(-limit).map(item => ({
        sourceFile: item.sourceFile,
        sourceOffset: item.sourceOffset,
        sourceSequence: item.sourceSequence,
        parseError: item.parseError || null,
        summary: debugRecordSummary(item.record),
        record: item.record
      }))
    };
  }
  return result;
}

function debugRecordSummary(record) {
  const payload = record?.payload && typeof record.payload === "object" ? record.payload : {};
  const leaderResult = payload.leader_result && typeof payload.leader_result === "object" ? payload.leader_result : {};
  const request = payload.request && typeof payload.request === "object" ? payload.request : {};
  const error = leaderResult.error_or_null || record?.error_or_null || payload.error_or_null || null;
  return {
    timestamp: record?.timestamp || null,
    eventType: record?.event_type || record?.eventType || null,
    eventKind: record?.event_kind || record?.eventKind || null,
    sdkMessageType: record?.sdk_message_type || null,
    status: leaderResult.status || record?.status || null,
    resultSubtype: record?.result_subtype || null,
    messagePreview: record?.message_preview || record?.result || leaderResult.reports?.[0]?.summary || null,
    errorType: error?.type || null,
    errorMessage: error?.message || null,
    systemFailure: record?.system_failure || leaderResult.system_failure || null,
    settingsMode: record?.outer_leader_options?.settings ? "flag" : record?.outer_leader_options ? "home" : null,
    cliSource: record?.outer_leader_options?.cli_source || null,
    cliHome: record?.outer_leader_options?.cli_home || null,
    cliMcpConfig: record?.outer_leader_options?.cli_mcp_config || null,
    processEnvBaseUrl: Boolean(record?.settings_diagnostics?.subprocess_env_has_anthropic_base_url),
    processEnvAuth: Boolean(record?.settings_diagnostics?.subprocess_env_has_anthropic_auth_token),
    settingsBaseUrl: record?.settings_diagnostics?.settings_anthropic_base_url || null,
    requestPreview: request.safe_preview || null
  };
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

function writeSseRetry(res, milliseconds) {
  res.write(`retry: ${Math.max(0, Math.trunc(milliseconds))}\n\n`);
}

function registerSseClient(res) {
  activeSseClients.add(res);
  return () => activeSseClients.delete(res);
}

function closeSseClients(reason = "gateway_shutdown") {
  gatewayShuttingDown = true;
  for (const res of [...activeSseClients]) {
    try {
      writeSseRetry(res, 86400000);
      writeSseEvent(res, "gateway_shutdown", {
        reason,
        message: "Bridge Companion gateway is shutting down; live stream stopped."
      });
      res.end();
    } catch {
      try {
        res.destroy?.();
      } catch {}
    } finally {
      activeSseClients.delete(res);
    }
  }
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
  if (gatewayShuttingDown) {
    writeSseRetry(res, 86400000);
    writeSseEvent(res, "gateway_shutdown", { repoKey, runId, reason: "gateway_shutdown" }, null);
    res.end();
    return;
  }
  const unregisterSseClient = registerSseClient(res);
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
      writeSseEvent(res, "companion_event", compactEventForResponse(event), event.eventId);
    }
    sourceCursor = { ...(sourceCursor || {}), ...sourceCursorsFor(next) };
    return next.length;
  };

  const tailer = dir ? await createTailer(dir, runSourceFiles) : [];
  const initialEvents = await loadRunEvents(repoKey, runId, {
    readOptions: {
      maxBytes: PROJECTION_JSONL_TAIL_MAX_BYTES,
      maxLines: Math.max(PROJECTION_JSONL_TAIL_MAX_LINES, DEFAULT_EVENT_LIMIT * 2)
    },
    includeRecord: includeProjectionRecord
  });
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
  req.on("close", () => {
    clearInterval(timer);
    unregisterSseClient();
  });
}

async function streamSessionObserver(req, res, query) {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-store, no-transform",
    "connection": "keep-alive",
    "access-control-allow-origin": ACCESS_CONTROL_ALLOW_ORIGIN
  });
  if (gatewayShuttingDown) {
    writeSseRetry(res, 86400000);
    writeSseEvent(res, "gateway_shutdown", { repoKey: "session_observer", runId: "unbound", reason: "gateway_shutdown" }, null);
    res.end();
    return;
  }
  const unregisterSseClient = registerSseClient(res);
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
      writeSseEvent(res, "companion_event", compactEventForResponse(event), event.eventId);
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
  req.on("close", () => {
    clearInterval(timer);
    unregisterSseClient();
  });
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
        const status = await outerHostHealth();
        outerHost = {
          ok: status?.ok !== false,
          mode: status?.mode || null,
          source: "outer_host_health",
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
        const events = await loadRunEvents(repoKey, runId, eventLoadOptionsForQuery(url.searchParams));
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
        const includeDetail = ["1", "true", "yes"].includes(String(url.searchParams.get("detail") || "").toLowerCase());
        const status = await buildStatus(repoKey, runId, { includeDetail });
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
      const waitForResult = leaderInputWaitsForResult(input, url.searchParams);
      const response = waitForResult ? await submitLeaderInput(input) : await submitLeaderInputAsync(input);
      sendJson(res, response?.accepted === false ? 503 : waitForResult ? 200 : 202, response);
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

  if (pathname === "/api/debug") {
    const includeOuterHost = url.searchParams.get("outer") !== "0";
    const includeEvents = ["1", "true", "yes"].includes(String(url.searchParams.get("events") || "").trim().toLowerCase());
    const eventLineLimit = Number(url.searchParams.get("eventLimit") || url.searchParams.get("event_limit") || 80);
    sendJson(res, 200, await gatewayDebugSnapshot({ includeOuterHost, includeEvents, eventLineLimit }));
    return;
  }

  if (pathname === "/api/debug/terminal") {
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "POST required" });
      return;
    }
    try {
      const input = await parseBody(req);
      sendJson(res, 200, await runTerminalCommand(input));
    } catch (error) {
      sendJson(res, 400, { ok: false, error: "terminal_failed", message: error.message });
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
        const events = await loadRunEvents(repoKey, runId, eventLoadOptionsForQuery(url.searchParams));
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
  gatewayShuttingDown = false;
  return http.createServer(requestHandler).listen(PORT, HOST, () => {
    console.log(`Bridge Companion gateway listening on http://${HOST}:${PORT}`);
    console.log(`projects root: ${PROJECTS_ROOT}`);
    console.log(`session observer root: ${SESSION_OBSERVER_ROOT}`);
  });
}

function installShutdownHandlers(server) {
  let stopping = false;
  const shutdown = signal => {
    if (stopping) return;
    stopping = true;
    console.log(`Bridge Companion received ${signal}; closing live streams.`);
    closeSseClients(signal);
    server.close(() => {
      process.exitCode = 0;
    });
    setTimeout(() => {
      process.exit(0);
    }, 1500).unref();
  };
  process.once("SIGINT", () => shutdown("SIGINT"));
  process.once("SIGTERM", () => shutdown("SIGTERM"));
}

if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  installShutdownHandlers(startServer());
}

export {
  buildProjection,
  buildStatus,
  closeSseClients,
  filterEvents,
  gatewayDebugSnapshot,
  loadRunEvents,
  normalizeRunRecord,
  outerHostStatus,
  projectCompletionChecklist,
  projectSemanticCoverage,
  redactForResponse,
  requestHandler,
  runTerminalCommand,
  startServer,
  submitLeaderInput,
  submitLeaderInputAsync
};
