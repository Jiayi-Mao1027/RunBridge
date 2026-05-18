import http from "node:http";
import https from "node:https";

import { gatewayDebugSnapshot } from "./server.mjs";

function parseArgs(argv) {
  const args = {
    gatewayUrl: process.env.BRIDGE_COMPANION_DEBUG_URL || "",
    local: false,
    noOuter: false,
    events: false,
    eventLimit: 80,
    strictLive: false,
    compact: false
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--local") {
      args.local = true;
    } else if (value === "--no-outer") {
      args.noOuter = true;
    } else if (value === "--events") {
      args.events = true;
    } else if (value === "--event-limit" && argv[index + 1]) {
      args.eventLimit = Number(argv[index + 1]) || args.eventLimit;
      index += 1;
    } else if (value.startsWith("--event-limit=")) {
      args.eventLimit = Number(value.split("=", 2)[1]) || args.eventLimit;
    } else if (value === "--strict-live") {
      args.strictLive = true;
    } else if (value === "--compact") {
      args.compact = true;
    } else if (value === "--gateway" && argv[index + 1]) {
      args.gatewayUrl = argv[index + 1];
      index += 1;
    } else if (value.startsWith("--gateway=")) {
      args.gatewayUrl = value.split("=", 2)[1] || "";
    }
  }
  return args;
}

function defaultGatewayUrl() {
  const host = process.env.BRIDGE_COMPANION_HOST || "127.0.0.1";
  const port = process.env.BRIDGE_COMPANION_PORT || "8787";
  return `http://${host}:${port}`;
}

function requestJson(url) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const transport = target.protocol === "http:" ? http : https;
    const headers = {};
    if (process.env.BRIDGE_COMPANION_TOKEN) {
      headers["x-bridge-companion-token"] = process.env.BRIDGE_COMPANION_TOKEN;
    }
    const req = transport.request({
      method: "GET",
      hostname: target.hostname,
      port: target.port || (target.protocol === "http:" ? 80 : 443),
      path: `${target.pathname}${target.search}`,
      headers
    }, res => {
      let data = "";
      res.setEncoding("utf8");
      res.on("data", chunk => { data += chunk; });
      res.on("end", () => {
        let parsed = {};
        try {
          parsed = data ? JSON.parse(data) : {};
        } catch (error) {
          reject(error);
          return;
        }
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(parsed);
        } else {
          reject(new Error(`gateway debug http ${res.statusCode}: ${data.slice(0, 500)}`));
        }
      });
    });
    req.on("error", reject);
    req.setTimeout(4000, () => {
      req.destroy(new Error("gateway debug request timed out"));
    });
    req.end();
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  let payload;
  if (!args.local) {
    const gatewayUrl = args.gatewayUrl || defaultGatewayUrl();
    const endpoint = new URL("/api/debug", gatewayUrl);
    if (args.noOuter) endpoint.searchParams.set("outer", "0");
    if (args.events) endpoint.searchParams.set("events", "1");
    if (args.events) endpoint.searchParams.set("eventLimit", String(args.eventLimit));
    try {
      payload = await requestJson(endpoint.toString());
      payload.debugSource = "live_gateway_http";
      payload.debugGatewayUrl = gatewayUrl;
    } catch (error) {
      if (args.strictLive) throw error;
      payload = await gatewayDebugSnapshot({
        includeOuterHost: !args.noOuter,
        includeEvents: args.events,
        eventLineLimit: args.eventLimit
      });
      payload.debugSource = "local_fallback";
      payload.liveGatewayError = error.message;
      payload.debugGatewayUrl = gatewayUrl;
    }
  } else {
    payload = await gatewayDebugSnapshot({
      includeOuterHost: !args.noOuter,
      includeEvents: args.events,
      eventLineLimit: args.eventLimit
    });
    payload.debugSource = "local_module";
  }
  process.stdout.write(`${JSON.stringify(payload, null, args.compact ? 0 : 2)}\n`);
}

main().catch(error => {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exitCode = 1;
});
