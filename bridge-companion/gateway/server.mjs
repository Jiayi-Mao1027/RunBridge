import http from "node:http";
import https from "node:https";
import { readFile, readdir, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const companionRoot = path.resolve(__dirname, "..");
const prototypeRoot = path.join(companionRoot, "prototype");

const PORT = Number(process.env.BRIDGE_COMPANION_PORT || 8787);
const HOST = process.env.BRIDGE_COMPANION_HOST || "127.0.0.1";
const RUNTIME_ROOT = process.env.BRIDGE_RUNTIME_ROOT || "";
const DEFAULT_BRIEF_BASE_URL = "https://api.deepseek.com";
const DEFAULT_BRIEF_MODEL = "deepseek-v4-pro";
const BRIEF_SECRET_PATH = process.env.BRIDGE_BRIEF_SECRET_PATH || path.join(os.homedir(), ".bridge-companion", "brief-secret.json");

const readOnlyMethods = new Set(["GET", "HEAD", "OPTIONS"]);
const allowedMethods = new Set(["GET", "HEAD", "OPTIONS", "POST"]);

const lifecycleNextEvents = {
  leader_freeze: ["bridge_call_intended", "bridge_call_denied"],
  bridge_packet_built: ["bridge_call_prechecked", "bridge_call_denied"],
  bridge_window_opened: ["bridge_packet_accepted", "bridge_packet_rejected", "bridge_call_failed"],
  bridge_packet_accepted: ["team_create_completed", "team_create_failed"],
  team_create_completed: ["task_create_completed", "task_create_failed"],
  task_create_completed: ["message_dispatch_completed", "message_dispatch_failed"],
  message_dispatch_completed: ["team_waiting", "artifacts_ready", "task_failed"],
  team_waiting: ["artifacts_ready", "task_completion_completed", "task_completion_rejected", "team_wait_timeout", "task_failed", "bridge_window_partial_returned"],
  artifacts_ready: ["task_completion_completed", "task_completion_rejected", "bridge_window_partial_returned", "task_failed"],
  task_completion_rejected: ["team_waiting", "task_failed", "bridge_window_partial_returned"],
  task_completion_completed: ["team_delete_completed", "team_delete_failed"],
  team_delete_completed: ["bridge_window_returned"],
  bridge_window_partial_returned: [],
  bridge_window_returned: [],
  task_failed: [],
  bridge_call_failed: []
};

const statusCopy = {
  leader_freeze: {
    title: "任务语义已冻结",
    factText: "主控已锁定本轮任务目标、约束和执行范围。下一步将根据当前阶段构建桥接任务包。",
    explanationText: "这表示任务含义已经被固定，后续执行不应再静默改变范围或完成条件。",
    companionNote: "委托内容已经定稿，等待进入下一步运行流程。"
  },
  bridge_window_opened: {
    title: "桥接窗口已开启",
    factText: "Bridge leader 已接收任务包，并为本轮执行创建独立的桥接窗口。",
    explanationText: "这表示任务已经进入 bridge window 流程，但尚不代表队员已经返回结果。",
    companionNote: "桥接窗口已经打开，但卷宗尚未返回。"
  },
  team_create_completed: {
    title: "执行团队已创建",
    factText: "本轮任务所需的执行团队已经创建完成，后续消息将发送给对应队员。",
    explanationText: "这表示执行团队已绑定到本轮 bridge window。后续仍需等待任务说明下发和结果返回。",
    companionNote: "队伍已经集合，等待任务卷宗交付。"
  },
  message_dispatch_completed: {
    title: "任务说明已下发",
    factText: "执行团队已收到任务目标、完成条件和报告要求。runtime 正在等待后续执行结果。",
    explanationText: "这表示任务已经交给执行团队，但 runtime 尚未证明具体文件级进展或完成状态。",
    companionNote: "任务卷宗已经交付，等待队员带回记录。"
  },
  team_waiting: {
    title: "等待执行结果",
    factText: "当前处于 bridge window 内部等待阶段。runtime 暂未收到新的结构化报告、artifact 或完成信号。",
    explanationText: "前台暂时安静，不等于卡死；它只表示当前还没有新的 runtime 事件。",
    companionNote: "小队还没有带回新卷宗。这里不会猜测尚未返回的细节。"
  },
  artifacts_ready: {
    title: "已收到阶段性产物",
    factText: "runtime 已记录本轮任务返回的 artifact。是否满足完成条件仍需根据 completion contract 检查。",
    explanationText: "这表示已有可检查的产物，但不自动等同于任务成功。",
    companionNote: "有证物带回来了，仍需检查是否满足委托条件。"
  },
  task_completion_rejected: {
    title: "完成条件未满足",
    factText: "队员已有返回，但 runtime 检查发现结果尚未满足本轮 completion contract。",
    explanationText: "请查看缺失项：可能是报告、artifact、验证结果或指定输出不完整。",
    companionNote: "卷宗已回，但证据还不够完整。"
  },
  bridge_window_partial_returned: {
    title: "部分结果返回",
    factText: "本轮 bridge window 返回了部分结果。任务没有完整失败，但仍有未满足或未确认的项目。",
    explanationText: "这表示已有结果可用，但不能按完整成功处理。",
    companionNote: "小队带回了一部分记录，仍有缺口需要确认。"
  },
  task_failed: {
    title: "执行失败",
    factText: "本轮执行已返回失败结果。失败阶段、错误信息和可用证据已记录在 runtime 中。",
    explanationText: "这是 runtime 记录的失败事实，不应被视觉层弱化或包装成成功。",
    companionNote: "委托遇到异常，记录已经归档。"
  },
  bridge_window_returned: {
    title: "任务完成",
    factText: "本轮 bridge window 已返回成功结果，并满足当前 completion contract。",
    explanationText: "这表示 runtime 已接受完成证据，bridge window 已返回最终结果。",
    companionNote: "卷宗已归档，本轮委托完成。"
  },
  unknown: {
    title: "状态未知",
    factText: "当前无法从 runtime 数据中确认本轮任务状态。",
    explanationText: "这可能是因为网关未配置 runtime 根目录、事件日志缺失，或当前 run 尚未产生可读快照。",
    companionNote: "没有足够卷宗可读；这里不会猜测任务状态。"
  }
};

function sendJson(res, statusCode, body) {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, HEAD, OPTIONS",
    "access-control-allow-headers": "content-type"
  });
  res.end(payload);
}

function sendText(res, statusCode, body, contentType = "text/plain; charset=utf-8") {
  res.writeHead(statusCode, {
    "content-type": contentType,
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, HEAD, OPTIONS",
    "access-control-allow-headers": "content-type"
  });
  res.end(body);
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

async function readJsonlIfExists(filePath) {
  try {
    const text = await readFile(filePath, "utf8");
    return text.split(/\r?\n/).filter(Boolean).map(line => {
      try { return JSON.parse(line); } catch { return { raw: line }; }
    });
  } catch {
    return [];
  }
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

function buildBriefPrompt(status) {
  return [
    "你是 Bridge Companion 的只读解释层。只能基于提供的 runtime facts 总结。",
    "不要声称知道未记录的文件级进展；不要控制任务；不要给 agent 下指令。",
    "输出中文，分为：当前在做什么、已经完成哪些 bridge 内部步骤、队员/packet 信息、仍未知什么、下一步可能发生什么。",
    "保持克制，必须标注未知项。",
    "runtime facts:",
    JSON.stringify(status, null, 2).slice(0, 12000)
  ].join("\n");
}

async function generateBrief(status) {
  const secret = await loadBriefSecret();
  if (!secret.apiKey) {
    return { configured: false, text: "模型解读 API 未配置。请在本机权限受限配置文件中设置 apiKey。" };
  }
  const base = secret.baseUrl.replace(/\/+$/, "");
  const response = await httpsJson(`${base}/chat/completions`, {
    model: secret.model,
    messages: [
      { role: "system", content: "You summarize runtime facts for a read-only UI. Never invent progress or control execution." },
      { role: "user", content: buildBriefPrompt(status) }
    ],
    temperature: 0.2
  }, { authorization: `Bearer ${secret.apiKey}` });
  return {
    configured: true,
    model: secret.model,
    text: response?.choices?.[0]?.message?.content || "模型未返回可读摘要。"
  };
}

function safeJoinRun(runId) {
  if (!/^[a-zA-Z0-9_.-]+$/.test(runId)) return null;
  if (!RUNTIME_ROOT) return null;
  return path.resolve(RUNTIME_ROOT, runId);
}

async function listRuns() {
  if (!RUNTIME_ROOT || !(await exists(RUNTIME_ROOT))) return [];
  const entries = await readdir(RUNTIME_ROOT, { withFileTypes: true });
  const runs = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const runDir = path.join(RUNTIME_ROOT, entry.name);
    const snapshot = await readJsonIfExists(path.join(runDir, "runtime_snapshot.json"), null);
    const stats = await stat(runDir);
    runs.push({
      runId: entry.name,
      phase: snapshot?.phase || snapshot?.route_state?.current_phase || null,
      lifecycleState: snapshot?.lifecycle_state || snapshot?.lifecycle_status || null,
      updatedAt: snapshot?.updated_at || snapshot?.last_updated_at || stats.mtime.toISOString()
    });
  }
  return runs.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
}

function eventTypeOf(event) {
  return event.event_type || event.type || event.name || event.lifecycle_state || event.transition || "unknown";
}

function timestampOf(event) {
  return event.timestamp || event.created_at || event.time || null;
}

function summarizeEvent(event) {
  const payload = event.payload || event.data || event;
  if (typeof payload === "string") return payload.slice(0, 180);
  const keys = Object.keys(payload || {}).filter(k => !["event_type", "type", "name", "timestamp", "created_at"].includes(k));
  return keys.slice(0, 4).map(k => `${k}: ${JSON.stringify(payload[k]).slice(0, 80)}`).join("; ");
}

function inferLifecycle(snapshot, events) {
  const direct = snapshot?.lifecycle_state || snapshot?.lifecycle_status || snapshot?.status || snapshot?.bridge?.lifecycle_state;
  if (direct) return direct;
  const last = events.at(-1);
  if (!last) return "unknown";
  const type = eventTypeOf(last);
  if (statusCopy[type]) return type;
  if (type.includes("waiting") || type.includes("TeamIdle")) return "team_waiting";
  if (type.includes("partial")) return "bridge_window_partial_returned";
  if (type.includes("failed") || type.includes("failure")) return "task_failed";
  if (type.includes("returned") || type.includes("completed")) return type;
  return type;
}

function buildUnknowns(statusKey, snapshot, events, hasCompletionReport, hasArtifacts) {
  const unknowns = [];
  if (!hasCompletionReport) unknowns.push("尚未收到 completion report。 ");
  if (!hasArtifacts) unknowns.push("尚未收到 artifact。 ");
  if (["team_waiting", "message_dispatch_completed", "team_create_completed", "bridge_window_opened"].includes(statusKey)) {
    unknowns.push("如果 report / event 未记录文件名，则当前无法确认具体正在修改哪些文件。 ");
    unknowns.push("如果 TeamIdle 之后没有新事件，则只能确认 bridge 正在等待，不能确认队员内部细节。 ");
  }
  if (!snapshot) unknowns.push("当前缺少 runtime_snapshot.json，只能依据 event_log 推断有限状态。 ");
  if (!events.length) unknowns.push("当前没有可读 event_log 事件。 ");
  return [...new Set(unknowns.map(x => x.trim()))];
}

function progressLabelFor(eventType) {
  if (eventType.includes("bridge_window_opened")) return "桥接窗口已开启";
  if (eventType.includes("bridge_packet_accepted")) return "任务包已被 bridge leader 接收";
  if (eventType.includes("team_create_completed")) return "执行团队已创建";
  if (eventType.includes("task_create_completed")) return "任务记录已创建";
  if (eventType.includes("message_dispatch_completed")) return "任务说明已下发";
  if (eventType.includes("artifact")) return "收到 artifact / 阶段性产物";
  if (eventType.includes("completion") && eventType.includes("rejected")) return "完成检查未通过";
  if (eventType.includes("completion") && eventType.includes("completed")) return "完成检查通过";
  if (eventType.includes("partial")) return "部分结果返回";
  if (eventType.includes("waiting") || eventType.includes("idle")) return "等待队员回报";
  if (eventType.includes("failed") || eventType.includes("failure")) return "失败事件已记录";
  if (eventType.includes("returned")) return "bridge window 已返回";
  return eventType;
}

function extractPacketSummary(snapshot) {
  const packet = snapshot?.bridge_packet || snapshot?.packet || snapshot?.last_bridge_packet || snapshot?.bridgePacket || null;
  const semantics = snapshot?.frozen_semantics || packet?.frozen_semantics || {};
  const scope = snapshot?.frozen_scope || packet?.frozen_scope || {};
  const taskSpec = snapshot?.task_spec || packet?.task_spec || {};
  const completion = snapshot?.completion_contract || packet?.completion_contract || {};
  const report = snapshot?.report_contract || packet?.report_contract || {};
  return {
    hasPacket: Boolean(packet || Object.keys(taskSpec).length || Object.keys(completion).length),
    objective: taskSpec.objective || taskSpec.title || semantics.objective || semantics.task || semantics.task_title || snapshot?.task_title || null,
    targetPhase: packet?.target_phase || snapshot?.target_phase || snapshot?.phase || null,
    scopeSummary: typeof scope === "string" ? scope : JSON.stringify(scope).slice(0, 260),
    completionSummary: typeof completion === "string" ? completion : JSON.stringify(completion).slice(0, 260),
    reportSummary: typeof report === "string" ? report : JSON.stringify(report).slice(0, 220),
    allowedTools: packet?.allowed_tools || snapshot?.allowed_tools || [],
    teamSpec: packet?.team_spec || snapshot?.team_spec || null
  };
}

function extractTeammates(snapshot, events) {
  const fromSnapshot = snapshot?.teammates || snapshot?.team?.teammates || snapshot?.team_spec?.teammates || snapshot?.team_spec?.members || [];
  const teammates = Array.isArray(fromSnapshot) ? fromSnapshot.map((item, index) => ({
    id: item.id || item.teammate_id || item.name || `teammate_${index + 1}`,
    role: item.role || item.agent_type || item.type || item.name || "teammate",
    status: item.status || item.lifecycle_state || "declared",
    latest: item.latest_report || item.summary || null
  })) : [];

  const eventRoles = new Map();
  for (const event of events) {
    const payload = event.payload || event.data || event;
    const role = payload.teammate_role || payload.teammate_id || payload.agent_type || payload.agent_id;
    if (!role) continue;
    eventRoles.set(role, {
      id: String(role),
      role: String(payload.agent_type || payload.teammate_role || role),
      status: progressLabelFor(eventTypeOf(event)),
      latest: summarizeEvent(event)
    });
  }
  return [...teammates, ...eventRoles.values()].slice(0, 8);
}

function buildInternalProgress(snapshot, events, inbox, artifacts) {
  const recentEvents = events.slice(-8).map(event => {
    const type = eventTypeOf(event);
    return {
      eventType: type,
      label: progressLabelFor(type),
      timestamp: timestampOf(event),
      summary: summarizeEvent(event)
    };
  });

  const reportCandidates = [];
  if (snapshot?.last_bridge_result) reportCandidates.push({ source: "last_bridge_result", value: snapshot.last_bridge_result });
  if (snapshot?.completion_report) reportCandidates.push({ source: "completion_report", value: snapshot.completion_report });
  if (snapshot?.partial_reports) reportCandidates.push({ source: "partial_reports", value: snapshot.partial_reports });
  for (const item of inbox.slice(-5)) {
    const type = eventTypeOf(item).toLowerCase();
    const text = JSON.stringify(item).toLowerCase();
    if (type.includes("report") || text.includes("report") || text.includes("artifact") || text.includes("partial")) {
      reportCandidates.push({ source: "inbox", value: item });
    }
  }

  const evidence = [];
  if (recentEvents.length) evidence.push(`最近 ${recentEvents.length} 条 runtime 事件可读。`);
  if (reportCandidates.length) evidence.push(`已发现 ${reportCandidates.length} 条 report / result 相关记录。`);
  if (Array.isArray(artifacts) && artifacts.length) evidence.push(`已记录 ${artifacts.length} 个 artifact 引用。`);
  if (snapshot?.team_idle?.wait_reason || snapshot?.wait_reason) evidence.push(`等待原因：${snapshot?.team_idle?.wait_reason || snapshot?.wait_reason}`);

  const lastMeaningful = [...recentEvents].reverse().find(item => !item.eventType.toLowerCase().includes("idle")) || recentEvents.at(-1) || null;

  return {
    summary: lastMeaningful ? lastMeaningful.label : "尚未读到 bridge 内部进展事件",
    lastMeaningfulEvent: lastMeaningful,
    recentEvents,
    evidence,
    reports: reportCandidates.slice(-3).map(item => ({
      source: item.source,
      summary: typeof item.value === "string" ? item.value.slice(0, 220) : JSON.stringify(item.value).slice(0, 220)
    })),
    artifactCount: Array.isArray(artifacts) ? artifacts.length : 0,
    hasInternalEvidence: Boolean(recentEvents.length || reportCandidates.length || (Array.isArray(artifacts) && artifacts.length))
  };
}

function normalizeStatus(runId, snapshot, events, inbox, artifacts) {
  const latest = events.at(-1) || null;
  const lifecycleState = inferLifecycle(snapshot, events);
  const copy = statusCopy[lifecycleState] || statusCopy.unknown;
  const hasCompletionReport = Boolean(snapshot?.last_bridge_result || snapshot?.completion_report || events.some(e => eventTypeOf(e).includes("completion")));
  const hasArtifacts = Boolean((artifacts && artifacts.length) || snapshot?.artifacts || events.some(e => eventTypeOf(e).includes("artifact")));
  const phase = snapshot?.phase || snapshot?.target_phase || snapshot?.route_state?.current_phase || "未知阶段";
  const lastUpdatedAt = latest ? timestampOf(latest) : (snapshot?.updated_at || snapshot?.last_updated_at || null);
  const possibleNextEvents = lifecycleNextEvents[lifecycleState] || [];
  const unknowns = buildUnknowns(lifecycleState, snapshot, events, hasCompletionReport, hasArtifacts);
  const internalProgress = buildInternalProgress(snapshot, events, inbox, artifacts);
  const packetSummary = extractPacketSummary(snapshot);
  const teammates = extractTeammates(snapshot, events);

  return {
    runId,
    taskTitle: snapshot?.task_title || snapshot?.frozen_semantics?.task_title || snapshot?.task_spec?.title || "未命名任务",
    phase,
    lifecycleState,
    latestEvent: latest ? {
      eventType: eventTypeOf(latest),
      timestamp: timestampOf(latest),
      payloadSummary: summarizeEvent(latest)
    } : null,
    lastUpdatedAt,
    bridgeWindowId: snapshot?.bridge_window_id || snapshot?.binding?.bridge_window_id || null,
    teamId: snapshot?.team_id || snapshot?.binding?.team_id || null,
    taskId: snapshot?.task_id || snapshot?.binding?.task_id || null,
    waiting: lifecycleState === "team_waiting" || eventTypeOf(latest || {}).toLowerCase().includes("waiting"),
    waitReason: snapshot?.wait_reason || snapshot?.team_idle?.wait_reason || null,
    hasCompletionReport,
    hasArtifacts,
    resultState: resultStateFor(lifecycleState),
    authority: snapshot ? "runtime_fact" : (events.length ? "derived_from_events" : "unknown"),
    facts: factsFor(lifecycleState, latest),
    unknowns,
    internalProgress,
    packetSummary,
    teammates,
    detail: {
      packet: packetSummary,
      teammates,
      reports: internalProgress.reports,
      artifacts,
      recentEvents: internalProgress.recentEvents,
      inbox: inbox.slice(-10)
    },
    possibleNextEvents,
    inboxCount: inbox.length,
    artifactCount: Array.isArray(artifacts) ? artifacts.length : 0,
    gateway: {
      state: "connected",
      label: "本地只读网关已连接",
      runtimeRootConfigured: Boolean(RUNTIME_ROOT)
    },
    display: {
      ...copy,
      nextStepText: nextStepTextFor(possibleNextEvents)
    }
  };
}

function resultStateFor(state) {
  if (["bridge_window_returned", "task_completion_completed"].includes(state)) return "succeeded";
  if (["task_failed", "bridge_call_failed"].includes(state)) return "failed";
  if (state === "bridge_window_partial_returned") return "partial";
  if (state === "team_waiting") return "waiting";
  if (state === "unknown") return "unknown";
  return "running";
}

function factsFor(state, latest) {
  const copy = statusCopy[state] || statusCopy.unknown;
  const facts = [copy.factText];
  if (latest) facts.push(`最新事件：${eventTypeOf(latest)}`);
  return facts;
}

function nextStepTextFor(nextEvents) {
  if (!nextEvents.length) return "当前状态没有可展示的下一步事件，可能已经结束或需要人工检查 runtime 记录。";
  return "按当前 lifecycle，下一步只会从状态机允许的后续事件中产生；界面不会预测未被 runtime 记录的内部进展。";
}

async function getRunBundle(runId) {
  const runDir = safeJoinRun(runId);
  if (!runDir) return null;
  const snapshot = await readJsonIfExists(path.join(runDir, "runtime_snapshot.json"), null);
  const events = await readJsonlIfExists(path.join(runDir, "event_log.jsonl"));
  const inbox = await readJsonlIfExists(path.join(runDir, "main_leader_inbox.jsonl"));
  const artifacts = snapshot?.artifacts || snapshot?.artifact_refs || [];
  return { snapshot, events, inbox, artifacts };
}

function readRequestJson(req, limitBytes = 200000) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", chunk => {
      body += chunk;
      if (Buffer.byteLength(body) > limitBytes) {
        reject(new Error("request body too large"));
        req.destroy();
      }
    });
    req.on("end", () => {
      try { resolve(body ? JSON.parse(body) : {}); }
      catch (error) { reject(error); }
    });
    req.on("error", reject);
  });
}

async function serveStatic(req, res, pathname) {
  let filePath = pathname === "/" ? path.join(prototypeRoot, "index.html") : path.join(companionRoot, pathname.replace(/^\/+/, ""));
  filePath = path.resolve(filePath);
  if (!filePath.startsWith(companionRoot)) {
    sendText(res, 403, "Forbidden");
    return;
  }
  try {
    const data = await readFile(filePath);
    const ext = path.extname(filePath);
    const contentType = ext === ".html" ? "text/html; charset=utf-8" : ext === ".js" ? "text/javascript; charset=utf-8" : ext === ".css" ? "text/css; charset=utf-8" : "application/octet-stream";
    res.writeHead(200, { "content-type": contentType, "cache-control": "no-store" });
    res.end(data);
  } catch {
    sendText(res, 404, "Not found");
  }
}

const server = http.createServer(async (req, res) => {
  try {
    if (!allowedMethods.has(req.method || "")) {
      sendJson(res, 405, { error: "Method not allowed." });
      return;
    }
    if (req.method === "OPTIONS") {
      sendJson(res, 204, {});
      return;
    }

    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    const pathname = url.pathname;

    if (req.method === "POST" && pathname !== "/brief") {
      sendJson(res, 405, { error: "POST is only allowed for /brief model summarization. It never mutates runtime." });
      return;
    }
    if (req.method !== "POST" && !readOnlyMethods.has(req.method || "")) {
      sendJson(res, 405, { error: "Bridge Companion gateway is read-only except /brief summarization." });
      return;
    }

    if (pathname === "/health") {
      sendJson(res, 200, {
        ok: true,
        service: "bridge-companion-gateway",
        readOnly: true,
        runtimeRootConfigured: Boolean(RUNTIME_ROOT),
        runtimeRootExists: RUNTIME_ROOT ? await exists(RUNTIME_ROOT) : false
      });
      return;
    }

    if (pathname === "/runs") {
      sendJson(res, 200, { runs: await listRuns() });
      return;
    }

    if (req.method === "POST" && pathname === "/brief") {
      const body = await readRequestJson(req);
      const status = body.status || body.runtimeFacts || body;
      const brief = await generateBrief(status);
      sendJson(res, 200, brief);
      return;
    }

    const match = pathname.match(/^\/runs\/([^/]+)\/(status|events|inbox|artifacts|stream)$/);
    if (match) {
      const [, runId, kind] = match;
      const bundle = await getRunBundle(runId);
      if (!bundle) {
        sendJson(res, 404, { error: "Runtime root is not configured or run id is invalid.", runId });
        return;
      }
      if (kind === "status") sendJson(res, 200, normalizeStatus(runId, bundle.snapshot, bundle.events, bundle.inbox, bundle.artifacts));
      else if (kind === "events") sendJson(res, 200, { runId, events: bundle.events });
      else if (kind === "inbox") sendJson(res, 200, { runId, inbox: bundle.inbox });
      else if (kind === "artifacts") sendJson(res, 200, { runId, artifacts: bundle.artifacts });
      else if (kind === "stream") {
        res.writeHead(200, {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-store",
          "connection": "keep-alive",
          "access-control-allow-origin": "*"
        });
        const writeStatus = async () => {
          const fresh = await getRunBundle(runId);
          const status = fresh ? normalizeStatus(runId, fresh.snapshot, fresh.events, fresh.inbox, fresh.artifacts) : { error: "run unavailable" };
          res.write(`event: status\n`);
          res.write(`data: ${JSON.stringify(status)}\n\n`);
        };
        await writeStatus();
        const timer = setInterval(writeStatus, 2500);
        req.on("close", () => clearInterval(timer));
      }
      return;
    }

    await serveStatic(req, res, pathname);
  } catch (error) {
    sendJson(res, 500, { error: String(error?.stack || error) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`Bridge Companion gateway listening on http://${HOST}:${PORT}`);
  console.log(`Read-only mode: enabled`);
  console.log(`Runtime root: ${RUNTIME_ROOT || "not configured; prototype UI still available"}`);
});
