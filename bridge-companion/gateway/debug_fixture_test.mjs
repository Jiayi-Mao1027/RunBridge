import assert from "node:assert/strict";
import http from "node:http";
import { mkdir, mkdtemp, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

process.env.BRIDGE_OUTER_HOST_URL = "";
process.env.BRIDGE_OUTER_HOST_TOKEN = "debug-secret-token";
process.env.ANTHROPIC_AUTH_TOKEN = "provider-secret-token";
process.env.ANTHROPIC_BASE_URL = "https://provider.example/v1";
const tmpRoot = await mkdtemp(path.join(os.tmpdir(), "bridge-companion-debug-"));
const projectsRoot = path.join(tmpRoot, "projects");
const repoKey = "repo_debug";
const runId = "run_debug";
const runRoot = path.join(projectsRoot, repoKey, "runs", runId);
process.env.BRIDGE_RUNTIME_PROJECTS_ROOT = projectsRoot;

const { gatewayDebugSnapshot, redactForResponse, requestHandler, runTerminalCommand } = await import(`./server.mjs?debug-fixture=${Date.now()}`);

try {
  await mkdir(runRoot, { recursive: true });
  await appendJsonl(path.join(runRoot, "sdk_stream_events.jsonl"), {
    event_type: "sdk_stream_started",
    settings_diagnostics: {
      settings_has_anthropic_auth_token: true,
      subprocess_env_has_anthropic_auth_token: false
    }
  });
  await appendJsonl(path.join(runRoot, "outer_host_events.jsonl"), {
    event_kind: "outer_leader_result",
    payload: {
      leader_result: {
        status: "failed",
        error_or_null: { type: "OuterLeaderSdkApiRequestFailed", message: "API Error fixture" }
      }
    }
  });

  const snapshot = await gatewayDebugSnapshot({ includeOuterHost: false });
  assert.equal(snapshot.schemaVersion, "bridge_companion_debug.v1");
  assert.equal(snapshot.envChecks.processHasAnthropicBaseUrl, true);
  assert.equal(snapshot.envChecks.processHasAnthropicAuthCredential, true);
  assert.equal(snapshot.env.ANTHROPIC_AUTH_TOKEN, "<redacted>");
  assert.equal(snapshot.env.BRIDGE_OUTER_HOST_TOKEN, "<redacted>");
  assert.equal(snapshot.outerHost, null);
  const redacted = redactForResponse({
    settings_has_anthropic_auth_token: true,
    nested: { ANTHROPIC_AUTH_TOKEN: "provider-secret-token" }
  });
  assert.equal(redacted.settings_has_anthropic_auth_token, true);
  assert.equal(redacted.nested.ANTHROPIC_AUTH_TOKEN, "<redacted>");

  const terminalProbe = await runTerminalCommand({
    command: terminalProbeCommand(),
    cwd: process.cwd(),
    timeoutMs: 5000
  });
  assertTerminalProbe(terminalProbe, /terminal-ok/);

  const server = http.createServer(requestHandler);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  try {
    const response = await requestJson(`http://127.0.0.1:${port}/api/debug?outer=0`);
    assert.equal(response.schemaVersion, "bridge_companion_debug.v1");
    assert.equal(response.env.ANTHROPIC_AUTH_TOKEN, "<redacted>");
    assert.equal(response.gateway.readOnly, true);
    const terminalResponse = await requestJson(`http://127.0.0.1:${port}/api/debug/terminal`, {
      method: "POST",
      body: {
        command: "node -e \"console.log('terminal-http-ok')\"",
        cwd: process.cwd(),
        timeoutMs: 5000
      }
    });
    assertTerminalProbe(terminalResponse, /terminal-http-ok/);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }

  const outerServer = http.createServer((req, res) => {
    if (req.url === "/v1/status") {
      res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({
        schema_version: "outer_sdk_host_status.v1",
        repo_key: repoKey,
        run_id: runId,
        runtime_runs_root: path.join(projectsRoot, repoKey, "runs")
      }));
      return;
    }
    res.writeHead(404, { "content-type": "application/json; charset=utf-8" });
    res.end("{}");
  });
  await new Promise(resolve => outerServer.listen(0, "127.0.0.1", resolve));
  process.env.BRIDGE_OUTER_HOST_URL = `http://127.0.0.1:${outerServer.address().port}`;
  try {
    const { gatewayDebugSnapshot: liveSnapshot } = await import(`./server.mjs?debug-fixture-events=${Date.now()}`);
    const withEvents = await liveSnapshot({ includeOuterHost: true, includeEvents: true, eventLineLimit: 1 });
    assert.equal(withEvents.recentRuntime.ok, true);
    assert.equal(withEvents.recentRuntime.files["sdk_stream_events.jsonl"].records.length, 1);
    assert.equal(withEvents.recentRuntime.files["outer_host_events.jsonl"].records[0].summary.errorMessage, "API Error fixture");
  } finally {
    await new Promise(resolve => outerServer.close(resolve));
  }

  console.log(JSON.stringify({ ok: true, debug: "passed" }, null, 2));
} finally {
  await rm(tmpRoot, { recursive: true, force: true });
}

function requestJson(url, options = {}) {
  return new Promise((resolve, reject) => {
    const requestBody = options.body ? JSON.stringify(options.body) : "";
    const req = http.request(url, {
      method: options.method || "GET",
      headers: requestBody ? {
        "content-type": "application/json; charset=utf-8",
        "content-length": Buffer.byteLength(requestBody)
      } : undefined
    }, res => {
      let data = "";
      res.setEncoding("utf8");
      res.on("data", chunk => { data += chunk; });
      res.on("end", () => {
        try {
          resolve(JSON.parse(data));
        } catch (error) {
          reject(error);
        }
      });
    });
    req.on("error", reject);
    if (requestBody) req.write(requestBody);
    req.end();
  });
}

function terminalProbeCommand() {
  return "node -e \"console.log(process.env.ANTHROPIC_AUTH_TOKEN); console.log('terminal-ok')\"";
}

function assertTerminalProbe(result, pattern) {
  if (result.error === "spawn_failed") {
    assert.equal(result.ok, false);
    assert.match(result.message || "", /spawn/i);
    return;
  }
  assert.equal(result.ok, true);
  assert.match(result.stdout, pattern);
  assert.doesNotMatch(result.stdout, /provider-secret-token/);
  if (pattern.test("terminal-ok")) assert.match(result.stdout, /<redacted>/);
}

async function appendJsonl(filePath, payload) {
  await writeFile(filePath, `${JSON.stringify(payload)}\n`, { encoding: "utf8", flag: "a" });
}
