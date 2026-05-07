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
const configuredRuntimeRoot = process.env.BRIDGE_RUNTIME_ROOT || process.env.BRIDGE_RUNTIME_RUNS_ROOT || "";
const DEFAULT_REMOTE_RUNTIME_ROOT = "/data03/liang/mjy/.claude/runtime_state/projects/safe_opd_08b4b45403e2/runs";
const RUNTIME_ROOT = configuredRuntimeRoot || DEFAULT_REMOTE_RUNTIME_ROOT;
const SESSION_OBSERVER_ROOT = process.env.BRIDGE_SESSION_OBSERVER_ROOT || path.resolve(RUNTIME_ROOT, "..", "..", "..", "session_observer");
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
  const resolvedRoot = path.resolve(RUNTIME_ROOT);
  const resolvedRun = path.resolve(resolvedRoot, runId);
  if (resolvedRun !== resolvedRoot && !resolvedRun.startsWith(resolvedRoot + path.sep)) return null;
  return resolvedRun;
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

function collectTextMatches(value, predicate, pathParts = [], out = []) {
  if (out.length > 80 || value == null) return out;
  if (typeof value === "string") {
    if (predicate(value, pathParts)) out.push({ path: pathParts.join("."), value });
    return out;
  }
  if (typeof value !== "object") return out;
  if (Array.isArray(value)) {
    value.slice(0, 20).forEach((item, index) => collectTextMatches(item, predicate, [...pathParts, String(index)], out));
    return out;
  }
  for (const [key, nested] of Object.entries(value).slice(0, 80)) {
    collectTextMatches(nested, predicate, [...pathParts, key], out);
  }
  return out;
}

function eventActor(event) {
  const payload = event.payload || event.data || event;
  return payload.teammate_id || payload.teammate_role || payload.agent_id || payload.agent_type || payload.role || payload.sender || payload.from || "runtime";
}

function eventTool(event) {
  const payload = event.payload || event.data || event;
  return payload.tool_name || payload.tool || payload.name || null;
}

function eventFileRefs(event) {
  const payload = event.payload || event.data || event;
  const refs = new Set();
  collectTextMatches(payload, (text, pathParts) => {
    const key = pathParts.at(-1) || "";
    return /path|file|artifact|target|source/i.test(key) && /[\\/]|\.[a-zA-Z0-9]{1,8}$/.test(text);
  }).forEach(match => refs.add(match.value));
  return [...refs].slice(0, 8);
}

function concisePath(value) {
  const text = String(value || "");
  if (!text) return "";
  const parts = text.split(/[\\/]/).filter(Boolean);
  return parts.length > 3 ? `…/${parts.slice(-3).join("/")}` : text;
}

function toolDetailSummary(record) {
  if (record.edit_summary) {
    const e = record.edit_summary;
    return `${e.operation || "edit"}: +${e.lines_added ?? "?"}/-${e.lines_removed ?? "?"}, hunks ${e.hunks ?? "?"}`;
  }
  if (record.read_options) {
    const lines = record.output_summary?.lines_returned ?? record.output_summary?.stdout_lines ?? "?";
    return `read: ${lines} lines`;
  }
  if (record.search_summary) {
    const s = record.search_summary;
    return `search: ${s.files_matched ?? "?"} files, ${s.matches_returned ?? "?"} matches`;
  }
  if (record.command_preview) {
    return `bash: ${record.exit_code ?? "running"}`;
  }
  if (record.output_summary) {
    return record.output_summary.notable || "tool output recorded";
  }
  return "";
}

function firstFileRef(record) {
  const refs = Array.isArray(record.file_refs) ? record.file_refs : [];
  const first = refs.find(Boolean);
  if (!first) return "";
  return typeof first === "string" ? first : (first.path || first.file_path || first.target || "");
}

function humanStatus(status) {
  const value = String(status || "").toLowerCase();
  if (value === "started" || value === "running") return "正在";
  if (value === "completed" || value === "done" || value === "success") return "刚刚";
  if (value === "failed" || value === "error") return "刚刚尝试但失败";
  return "正在";
}

function actionVerb(status, runningVerb, completedVerb, failedVerb = "尝试") {
  const value = String(status || "").toLowerCase();
  if (value === "completed" || value === "done" || value === "success") return completedVerb;
  if (value === "failed" || value === "error") return failedVerb;
  return runningVerb;
}

function cleanCommand(command) {
  const text = String(command || "").replace(/\s+/g, " ").trim();
  return text.length > 110 ? `${text.slice(0, 107)}…` : text;
}

function humanizeToolActivity(record = {}) {
  const toolName = record.tool_name || record.action || "tool";
  const status = record.status || "started";
  const file = concisePath(record.target || firstFileRef(record) || record.file_path || record.path || "");
  const command = cleanCommand(record.command_preview || record.normalized_input?.command || record.safe_input_preview || "");
  const prefix = humanStatus(status);
  let text = "";
  let objectText = file;
  let evidenceText = "";

  if (toolName === "Read") text = `${prefix}${actionVerb(status, "读取", "读完", "读取失败")}文件${file ? `：${file}` : ""}`;
  else if (toolName === "Edit") text = `${prefix}${actionVerb(status, "修改", "修改了", "修改失败")}文件${file ? `：${file}` : ""}`;
  else if (toolName === "Write") text = `${prefix}${actionVerb(status, "写入", "写入了", "写入失败")}文件${file ? `：${file}` : ""}`;
  else if (toolName === "MultiEdit") text = `${prefix}${actionVerb(status, "批量修改", "批量修改了", "批量修改失败")}文件${file ? `：${file}` : ""}`;
  else if (toolName === "Grep") text = `${prefix}${actionVerb(status, "搜索", "完成搜索", "搜索失败")}代码${record.search_summary?.pattern ? `：${record.search_summary.pattern}` : file ? `：${file}` : ""}`;
  else if (toolName === "Glob") text = `${prefix}${actionVerb(status, "匹配", "完成匹配", "匹配失败")}文件${record.search_summary?.pattern ? `：${record.search_summary.pattern}` : file ? `：${file}` : ""}`;
  else if (toolName === "Bash") text = `${prefix}${actionVerb(status, "运行", "运行完", "运行失败")}命令${command ? `：${command}` : ""}`;
  else if (toolName === "mcp__bridge__build_bridge_packet") text = `${prefix}${actionVerb(status, "整理", "整理完", "整理失败")}本轮桥接任务包`;
  else if (toolName === "mcp__bridge__call_bridge_sdk") text = status === "started" ? "主控正在等待桥接窗口执行并返回结果" : actionVerb(status, "", "桥接窗口已经返回结果", "桥接窗口返回失败或异常");
  else if (toolName === "mcp__bridge__read_runtime_snapshot") text = `${prefix}${actionVerb(status, "读取", "读完", "读取失败")} runtime 快照`;
  else if (toolName === "mcp__bridge__reconcile_workflow_from_ledger") text = `${prefix}${actionVerb(status, "核对", "核对完", "核对失败")} runtime ledger 与状态一致性`;
  else if (/send_messages?/i.test(toolName)) text = status === "started" ? "桥接负责人正在把任务说明发给队员" : actionVerb(status, "", "桥接负责人已经下发任务说明", "任务说明下发失败");
  else if (/team_create/i.test(toolName)) text = `${prefix}${actionVerb(status, "创建", "创建好", "创建失败")}执行小队`;
  else if (/task_create/i.test(toolName)) text = `${prefix}${actionVerb(status, "登记", "登记好", "登记失败")}本轮任务`;
  else if (/task_complete|completion/i.test(toolName)) text = `${prefix}${actionVerb(status, "提交", "提交了", "提交失败")}完成证据`;
  else if (/team_delete/i.test(toolName)) text = `${prefix}${actionVerb(status, "清理", "清理完", "清理失败")}本轮执行小队`;
  else text = `${prefix}执行一项内部操作：${toolName}`;

  if (record.edit_summary) {
    const e = record.edit_summary;
    evidenceText = `改动规模：增加 ${e.lines_added ?? "?"} 行，删除 ${e.lines_removed ?? "?"} 行。`;
  } else if (record.read_options || record.output_summary?.lines_returned) {
    evidenceText = `读取返回 ${record.output_summary?.lines_returned ?? record.output_summary?.stdout_lines ?? "?"} 行。`;
  } else if (record.search_summary) {
    const s = record.search_summary;
    evidenceText = `搜索结果：${s.files_matched ?? "?"} 个文件，${s.matches_returned ?? "?"} 条匹配。`;
  } else if (record.command_preview && record.status !== "started") {
    evidenceText = `命令退出码：${record.exit_code ?? "未知"}。`;
  } else if (record.output_summary?.notable) {
    evidenceText = String(record.output_summary.notable).slice(0, 140);
  }

  return {
    label: text,
    text,
    target: objectText,
    detail: evidenceText,
    evidenceText,
    rawLabel: `${toolName}${record.status ? ` · ${record.status}` : ""}`,
    raw: record
  };
}

function toolDisplay(record) {
  return humanizeToolActivity(record);
}

function classifyKind(type, source = "") {
  const text = `${type} ${source}`.toLowerCase();
  if (text.includes("tool")) return "tool";
  if (text.includes("process")) return "process";
  if (text.includes("message") || text.includes("agent_messages")) return "message";
  if (text.includes("artifact")) return "artifact";
  if (text.includes("report") || text.includes("teammate_reports")) return "report";
  if (text.includes("completion")) return "check";
  if (text.includes("packet") || text.includes("bridge_packets")) return "packet";
  if (text.includes("inbox")) return "inbox";
  return "event";
}

function titleForCompanionRecord(record, source) {
  if (source === "tool_events") return humanizeToolActivity(record).text;
  if (source === "agent_messages") return "任务消息已记录";
  if (source === "teammate_reports") return "队员返回了进度报告";
  if (source === "process_events") return `长运行进程${record.state ? `：${record.state}` : "有新状态"}`;
  if (source === "bridge_packets") return "本轮桥接任务包已记录";
  if (source === "artifacts") return "产物引用已记录";
  if (source === "completion_checks") return `完成检查${record.status ? `：${record.status}` : "已记录"}`;
  return progressLabelFor(eventTypeOf(record));
}

function fileRefsFromRecord(record) {
  if (Array.isArray(record.file_refs)) return record.file_refs.map(ref => typeof ref === "string" ? ref : ref.path).filter(Boolean).slice(0, 8);
  return eventFileRefs(record);
}

function plainSummary(value, fallback = "已记录一条 runtime 事实。") {
  if (!value) return fallback;
  if (typeof value === "string") return value.length > 220 ? `${value.slice(0, 217)}…` : value;
  if (value.summary) return plainSummary(value.summary, fallback);
  if (value.report_name && value.summary) return plainSummary(`${value.report_name}：${value.summary}`, fallback);
  return fallback;
}

function summaryForCompanionRecord(record) {
  if (record.tool_name || record.command_preview || record.edit_summary || record.read_options || record.search_summary) {
    const display = humanizeToolActivity(record);
    return display.evidenceText || (record.status === "started" ? "操作已经开始，等待完成事件返回更多证据。" : "操作已经记录，原始输入/输出保留在详情卷宗中。");
  }
  if (record.progress_state && (record.completed_items || record.open_items || record.blocked_items)) {
    return `${plainSummary(record.summary, "队员进度已记录")} 完成 ${record.completed_items?.length || 0} 项，未完成 ${record.open_items?.length || 0} 项，阻塞 ${record.blocked_items?.length || 0} 项。`;
  }
  if (record.state && record.process_ref) return plainSummary(record.summary, `进程 ${record.process_ref} 当前状态为 ${record.state}。`);
  if (record.report_name) return plainSummary(record.summary, `${record.report_name} 已记录，详情保留在卷宗中。`);
  return plainSummary(record.summary, summarizeEvent(record));
}

function toActivityItem(record, source = "event_log") {
  const type = eventTypeOf(record);
  return {
    kind: classifyKind(type, source),
    source,
    sequence: record.companion_sequence || record.sequence || record.monotonic_index || null,
    timestamp: timestampOf(record),
    actor: eventActor(record),
    title: titleForCompanionRecord(record, source),
    eventType: type,
    tool: eventTool(record),
    files: fileRefsFromRecord(record),
    summary: summaryForCompanionRecord(record),
    status: record.status,
    raw: record
  };
}

function buildActivityFeed(events, inbox, companion = {}) {
  const items = [
    ...events.slice(-60).map(item => toActivityItem(item, "event_log")),
    ...inbox.slice(-20).map(item => toActivityItem(item, "main_leader_inbox")),
    ...(companion.bridgePackets || []).slice(-10).map(item => toActivityItem(item, "bridge_packets")),
    ...(companion.agentMessages || []).slice(-30).map(item => toActivityItem(item, "agent_messages")),
    ...(companion.toolEvents || []).slice(-80).map(item => toActivityItem(item, "tool_events")),
    ...(companion.teammateReports || []).slice(-30).map(item => toActivityItem(item, "teammate_reports")),
    ...(companion.processEvents || []).slice(-30).map(item => toActivityItem(item, "process_events")),
    ...(companion.artifactEvents || []).slice(-20).map(item => toActivityItem(item, "artifacts")),
    ...(companion.completionChecks || []).slice(-20).map(item => toActivityItem(item, "completion_checks"))
  ];
  return items
    .sort((a, b) => String(a.timestamp || "").localeCompare(String(b.timestamp || "")) || Number(a.sequence || 0) - Number(b.sequence || 0))
    .slice(-120);
}

function buildCommunicationFeed(events, inbox, companion = {}) {
  const combined = [...events, ...inbox, ...(companion.agentMessages || []), ...(companion.teammateReports || [])];
  return combined.filter(item => {
    const text = JSON.stringify(item).toLowerCase();
    const type = eventTypeOf(item).toLowerCase();
    return type.includes("message") || type.includes("dispatch") || type.includes("report") || text.includes("teammate") || text.includes("agent") || text.includes("report");
  }).slice(-50).map(item => ({
    timestamp: timestampOf(item),
    actor: eventActor(item),
    eventType: eventTypeOf(item),
    summary: item.summary || summarizeEvent(item),
    raw: item
  }));
}

function extractPacketSummary(snapshot, companion = {}) {
  const latestPacketRecord = (companion.bridgePackets || []).at(-1) || null;
  const packet = snapshot?.bridge_packet || snapshot?.packet || snapshot?.last_bridge_packet || snapshot?.bridgePacket || latestPacketRecord || null;
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

function actorKey(record) {
  return String(record.teammate_id || record.agent_id || record.session_id || record.agent_type || record.display_name || "unknown");
}

function displayRoleName(cardOrSeed = {}) {
  const raw = String(cardOrSeed.agent_type || cardOrSeed.role || cardOrSeed.teammate_role || cardOrSeed.display_name || cardOrSeed.id || "执行会话");
  const lower = raw.toLowerCase();
  if (lower.includes("main-leader")) return "主控";
  if (lower.includes("bridge-leader")) return "桥接负责人";
  if (lower.includes("implement")) return "实现队员";
  if (lower.includes("execution") || lower.includes("executor")) return "执行队员";
  if (lower.includes("rungate") || lower.includes("gate")) return "验收队员";
  if (lower.includes("postrun")) return "复核队员";
  if (lower.includes("hook")) return "运行记录器";
  if (lower === "direct_session") return "直接会话";
  if (lower === "unknown") return "执行会话";
  return raw;
}

function statusTextForCard(card) {
  if (card.activeTool?.text) return card.activeTool.text;
  if (card.lastCompletedTool?.text) return `当前没有新的工具动作；${card.lastCompletedTool.text}`;
  const value = String(card.status || "").toLowerCase();
  if (value === "declared") return "已登记为本轮任务成员，但还没记录到具体工具操作。";
  if (value === "idle" || value === "done") return "当前没有活动工具，等待下一条报告或操作记录。";
  if (value === "running") return "正在执行，但当前工具细节还没有写入观察记录。";
  if (value === "bound_to_run" || value === "inferred") return "会话已绑定到本轮 run，等待记录具体动作。";
  return progressLabelFor(card.status || "unknown");
}

function nextTextForCard(card) {
  if (card.activeTool) return "下一步等待这项操作完成，并由 hook 写入结果或文件证据。";
  if (card.lastCompletedTool) return "下一步等待新的工具调用、队员报告或完成检查记录。";
  return "下一步等待 runtime 记录这个会话的具体操作。";
}

function mergeTeammateCard(map, seed = {}) {
  const key = actorKey(seed);
  if (!map.has(key)) {
    map.set(key, {
      id: key,
      role: seed.agent_type || seed.role || seed.teammate_role || seed.display_name || key,
      displayRole: displayRoleName(seed),
      status: seed.status || seed.lifecycle_state || "declared",
      latest: seed.latest_report || seed.summary || null,
      sessionId: seed.session_id || null,
      sessionKind: seed.session_kind || null,
      runBindingState: seed.run_binding_state || null,
      activeTool: null,
      lastCompletedTool: null,
      recentTools: [],
      fileRefs: []
    });
  }
  const card = map.get(key);
  card.role = seed.agent_type || seed.role || seed.teammate_role || card.role;
  card.displayRole = displayRoleName(card);
  card.sessionId = seed.session_id || card.sessionId;
  card.sessionKind = seed.session_kind || card.sessionKind;
  card.runBindingState = seed.run_binding_state || card.runBindingState;
  return card;
}

function extractTeammates(snapshot, events, companion = {}) {
  const latestPacketRecord = (companion.bridgePackets || []).at(-1) || {};
  const fromSnapshot = snapshot?.teammates || snapshot?.team?.teammates || snapshot?.team_spec?.teammates || snapshot?.team_spec?.members || latestPacketRecord.team_spec || [];
  const cards = new Map();
  if (Array.isArray(fromSnapshot)) {
    fromSnapshot.forEach((item, index) => mergeTeammateCard(cards, {
      id: item.id || item.teammate_id || item.name || `teammate_${index + 1}`,
      teammate_id: item.id || item.teammate_id || item.name || `teammate_${index + 1}`,
      role: item.role || item.agent_type || item.type || item.name || "teammate",
      status: item.status || item.lifecycle_state || "declared",
      latest_report: item.latest_report || item.summary || null
    }));
  }

  for (const binding of companion.sessionBindings || []) {
    const card = mergeTeammateCard(cards, binding);
    card.status = binding.run_binding_state || card.status;
  }

  for (const item of companion.activeOperations?.teammates || []) {
    const card = mergeTeammateCard(cards, item);
    card.status = item.active_tool ? "running" : "idle";
    card.activeTool = item.active_tool ? toolDisplay(item.active_tool) : null;
    card.lastCompletedTool = item.last_completed_tool ? toolDisplay(item.last_completed_tool) : card.lastCompletedTool;
  }

  const recentToolEvents = [...(companion.sessionToolEvents || []), ...(companion.toolEvents || [])].slice(-160);
  for (const event of recentToolEvents) {
    const card = mergeTeammateCard(cards, event);
    const display = toolDisplay(event);
    if (event.status === "started") card.activeTool = display;
    if (event.status === "completed" || event.status === "failed") card.lastCompletedTool = display;
    card.status = event.status === "started" ? "running" : (event.status || card.status);
    card.latest = display.detail || event.summary || card.latest;
    card.recentTools.push(display);
    for (const ref of event.file_refs || []) {
      const file = typeof ref === "string" ? ref : ref.path;
      if (file && !card.fileRefs.includes(file)) card.fileRefs.push(file);
    }
  }

  for (const event of [...events, ...(companion.teammateReports || []), ...(companion.agentMessages || []), ...(companion.processEvents || [])]) {
    const payload = event.payload || event.data || event;
    const role = payload.teammate_role || payload.teammate_id || payload.agent_type || payload.agent_id || payload.session_id;
    if (!role) continue;
    const card = mergeTeammateCard(cards, payload);
    card.status = payload.progress_state || progressLabelFor(eventTypeOf(event));
    card.latest = payload.summary || summarizeEvent(event);
  }

  return [...cards.values()].map(card => ({
    ...card,
    displayRole: displayRoleName(card),
    currentText: statusTextForCard(card),
    lastText: card.lastCompletedTool?.text || (card.latest ? plainSummary(card.latest, "最近有一条记录，详情保留在卷宗中。") : ""),
    nextText: nextTextForCard(card),
    evidenceText: card.activeTool?.evidenceText || card.lastCompletedTool?.evidenceText || "",
    recentTools: card.recentTools.slice(-5),
    fileRefs: card.fileRefs.slice(-6)
  })).slice(0, 12);
}

function buildInternalProgress(snapshot, events, inbox, artifacts, companion = {}) {
  const allEvents = [...events, ...(companion.agentMessages || []), ...(companion.sessionEvents || []), ...(companion.sessionToolEvents || []), ...(companion.toolEvents || []), ...(companion.teammateReports || []), ...(companion.processEvents || []), ...(companion.artifactEvents || []), ...(companion.completionChecks || [])];
  const recentEvents = allEvents.slice(-12).map(event => {
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
  for (const report of companion.teammateReports || []) reportCandidates.push({ source: "teammate_reports", value: report });
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
  const artifactTotal = (Array.isArray(artifacts) ? artifacts.length : 0) + (companion.artifactEvents || []).length;
  if (artifactTotal) evidence.push(`已记录 ${artifactTotal} 个 artifact 引用。`);
  const toolEventTotal = (companion.toolEvents || []).length + (companion.sessionToolEvents || []).length;
  if (toolEventTotal) evidence.push(`已记录 ${toolEventTotal} 条直接工具调用事件。`);
  if ((companion.activeOperations?.teammates || []).length) evidence.push(`已记录 ${(companion.activeOperations?.teammates || []).length} 个 session / teammate 的当前操作快照。`);
  if ((companion.processEvents || []).length) evidence.push(`已记录 ${(companion.processEvents || []).length} 条长运行进程事件。`);
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
    artifactCount: artifactTotal,
    hasInternalEvidence: Boolean(recentEvents.length || reportCandidates.length || artifactTotal)
  };
}

function normalizeStatus(runId, snapshot, events, inbox, artifacts, companion = {}) {
  const latest = events.at(-1) || null;
  const lifecycleState = inferLifecycle(snapshot, events);
  const copy = statusCopy[lifecycleState] || statusCopy.unknown;
  const hasCompletionReport = Boolean(snapshot?.last_bridge_result || snapshot?.completion_report || events.some(e => eventTypeOf(e).includes("completion")) || (companion.completionChecks || []).length || (companion.teammateReports || []).length);
  const hasArtifacts = Boolean((artifacts && artifacts.length) || snapshot?.artifacts || events.some(e => eventTypeOf(e).includes("artifact")) || (companion.artifactEvents || []).length);
  const phase = snapshot?.phase || snapshot?.target_phase || snapshot?.route_state?.current_phase || "未知阶段";
  const lastUpdatedAt = latest ? timestampOf(latest) : (snapshot?.updated_at || snapshot?.last_updated_at || null);
  const possibleNextEvents = lifecycleNextEvents[lifecycleState] || [];
  const unknowns = buildUnknowns(lifecycleState, snapshot, events, hasCompletionReport, hasArtifacts);
  const internalProgress = buildInternalProgress(snapshot, events, inbox, artifacts, companion);
  const packetSummary = extractPacketSummary(snapshot, companion);
  const teammates = extractTeammates(snapshot, events, companion);
  const activityFeed = buildActivityFeed(events, inbox, companion);
  const communicationFeed = buildCommunicationFeed(events, inbox, companion);

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
    activityFeed,
    communicationFeed,
    detail: {
      packet: packetSummary,
      teammates,
      activityFeed,
      communicationFeed,
      reports: internalProgress.reports,
      artifacts: [...(Array.isArray(artifacts) ? artifacts : []), ...(companion.artifactEvents || [])],
      completionChecks: companion.completionChecks || [],
      processEvents: companion.processEvents || [],
      sessionEvents: companion.sessionEvents || [],
      sessionBindings: companion.sessionBindings || [],
      activeOperations: companion.activeOperations || null,
      companionEvents: companion.companionEvents || [],
      recentEvents: internalProgress.recentEvents,
      inbox: inbox.slice(-10)
    },
    possibleNextEvents,
    inboxCount: inbox.length,
    artifactCount: Array.isArray(artifacts) ? artifacts.length : 0,
    gateway: {
      state: "connected",
      label: "本地只读网关已连接",
      runtimeRootConfigured: Boolean(configuredRuntimeRoot),
      runtimeRootSource: configuredRuntimeRoot ? "env" : "default-safe-opd"
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
  const activeOperations = await readJsonIfExists(path.join(runDir, "active_operations.json"), null);
  const runSessionBindings = await readJsonlIfExists(path.join(runDir, "session_bindings.jsonl"));
  const runSessionEvents = await readJsonlIfExists(path.join(runDir, "session_events.jsonl"));
  const globalToolEvents = await readJsonlIfExists(path.join(SESSION_OBSERVER_ROOT, "tool_events.jsonl"));
  const globalSessionEvents = await readJsonlIfExists(path.join(SESSION_OBSERVER_ROOT, "session_events.jsonl"));
  const globalSessionBindings = await readJsonlIfExists(path.join(SESSION_OBSERVER_ROOT, "session_bindings.jsonl"));
  const globalActiveOperations = await readJsonIfExists(path.join(SESSION_OBSERVER_ROOT, "active_operations.json"), null);
  const runMatches = record => !record.run_id || record.run_id === runId;
  const companion = {
    bridgePackets: await readJsonlIfExists(path.join(runDir, "bridge_packets.jsonl")),
    agentMessages: await readJsonlIfExists(path.join(runDir, "agent_messages.jsonl")),
    toolEvents: await readJsonlIfExists(path.join(runDir, "tool_events.jsonl")),
    sessionToolEvents: globalToolEvents.filter(runMatches).slice(-160),
    teammateReports: await readJsonlIfExists(path.join(runDir, "teammate_reports.jsonl")),
    artifactEvents: await readJsonlIfExists(path.join(runDir, "artifacts.jsonl")),
    completionChecks: await readJsonlIfExists(path.join(runDir, "completion_checks.jsonl")),
    processEvents: await readJsonlIfExists(path.join(runDir, "process_events.jsonl")),
    sessionEvents: [...runSessionEvents, ...globalSessionEvents.filter(runMatches).slice(-160)],
    sessionBindings: [...runSessionBindings, ...globalSessionBindings.filter(runMatches).slice(-80)],
    activeOperations: activeOperations || (globalActiveOperations?.run_id === runId || !globalActiveOperations?.run_id ? globalActiveOperations : null),
    companionEvents: await readJsonlIfExists(path.join(runDir, "companion_events.jsonl"))
  };
  const artifacts = snapshot?.artifacts || snapshot?.artifact_refs || [];
  return { snapshot, events, inbox, artifacts, companion };
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
        runtimeRootConfigured: Boolean(configuredRuntimeRoot),
        runtimeRoot: RUNTIME_ROOT,
        runtimeRootSource: configuredRuntimeRoot ? "env" : "default-safe-opd",
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
      if (kind === "status") sendJson(res, 200, normalizeStatus(runId, bundle.snapshot, bundle.events, bundle.inbox, bundle.artifacts, bundle.companion));
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
          const status = fresh ? normalizeStatus(runId, fresh.snapshot, fresh.events, fresh.inbox, fresh.artifacts, fresh.companion) : { error: "run unavailable" };
          res.write(`event: status\n`);
          res.write(`data: ${JSON.stringify(status)}\n\n`);
        };
        await writeStatus();
        const timer = setInterval(writeStatus, 1000);
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
