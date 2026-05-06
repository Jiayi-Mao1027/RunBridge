import http from "node:http";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const companionRoot = path.resolve(__dirname, "..");
const prototypeRoot = path.join(companionRoot, "prototype");

const PORT = Number(process.env.BRIDGE_COMPANION_PORT || 8787);
const HOST = process.env.BRIDGE_COMPANION_HOST || "127.0.0.1";
const RUNTIME_ROOT = process.env.BRIDGE_RUNTIME_ROOT || "";

const readOnlyMethods = new Set(["GET", "HEAD", "OPTIONS"]);

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
    unknowns.push("当前无法确认具体正在修改哪些文件。 ");
    unknowns.push("当前无法确认队员内部执行进度。 ");
  }
  if (!snapshot) unknowns.push("当前缺少 runtime_snapshot.json，只能依据 event_log 推断有限状态。 ");
  if (!events.length) unknowns.push("当前没有可读 event_log 事件。 ");
  return [...new Set(unknowns.map(x => x.trim()))];
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
    if (!readOnlyMethods.has(req.method || "")) {
      sendJson(res, 405, { error: "Bridge Companion gateway is read-only. Only GET, HEAD, and OPTIONS are allowed." });
      return;
    }
    if (req.method === "OPTIONS") {
      sendJson(res, 204, {});
      return;
    }

    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    const pathname = url.pathname;

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
