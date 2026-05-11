import assert from "node:assert/strict";
import http from "node:http";

let captured = null;
let statusCaptured = null;
const server = http.createServer((req, res) => {
  let body = "";
  req.setEncoding("utf8");
  req.on("data", chunk => { body += chunk; });
  req.on("end", () => {
    if (req.method === "GET" && req.url === "/v1/status") {
      statusCaptured = {
        method: req.method,
        url: req.url,
        token: req.headers["x-bridge-outer-host-token"] || ""
      };
      res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({
        schema_version: "outer_sdk_host_status.v1",
        mode: "outer_sdk_host",
        adapter: "fixture",
        repo_key: "repo_fixture",
        run_id: "run_latest",
        default_run_id: "run_host_default",
        host_instance_id: "outer_host_fixture"
      }));
      return;
    }
    captured = {
      method: req.method,
      url: req.url,
      token: req.headers["x-bridge-outer-host-token"] || "",
      body: body ? JSON.parse(body) : {}
    };
    res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({
      schema_version: "outer_sdk_host_response.v1",
      accepted: true,
      host: { mode: "outer_sdk_host", adapter: "fixture" },
      runtime: { ok: true, event_id: "evt_fixture", event_kind: "user_prompt_submitted" },
      leader_result: { status: "blocked" }
    }));
  });
});

await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const { port } = server.address();
process.env.BRIDGE_OUTER_HOST_URL = `http://127.0.0.1:${port}`;
process.env.BRIDGE_OUTER_HOST_TOKEN = "fixture-token";

try {
  const { outerHostStatus, submitLeaderInput } = await import(`./server.mjs?leader-input-fixture=${Date.now()}`);
  const status = await outerHostStatus();
  assert.equal(status.default_run_id, "run_host_default");
  assert.equal(statusCaptured.method, "GET");
  assert.equal(statusCaptured.token, "fixture-token");
  const response = await submitLeaderInput({
    text: "continue the current run",
    repo_key: "repo_fixture",
    run_id: "run_fixture",
    input_kind: "user_answer"
  });
  assert.equal(response.accepted, true);
  assert.equal(captured.method, "POST");
  assert.equal(captured.url, "/v1/input");
  assert.equal(captured.token, "fixture-token");
  assert.equal(captured.body.text, "continue the current run");
  assert.equal(captured.body.source, "bridge_companion_gateway");
  console.log(JSON.stringify({ ok: true, leader_input_proxy: "passed" }, null, 2));
} finally {
  await new Promise(resolve => server.close(resolve));
}
