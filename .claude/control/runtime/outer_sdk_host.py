from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from outer_sdk import OuterSdkHost, OuterSdkHostConfig, build_outer_leader_adapter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the long-lived RunBridge outer SDK host.")
    parser.add_argument("--control-root", required=True, help="Path to .claude/control")
    parser.add_argument("--repo-root", default=None, help="Target repo root this host owns")
    parser.add_argument("--repo-key", default=None, help="Existing repo key")
    parser.add_argument("--main-session-id", default=None, help="Stable outer leader session id")
    parser.add_argument("--host", default=os.environ.get("OUTER_SDK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OUTER_SDK_HOST_PORT", "8791")))
    parser.add_argument(
        "--adapter",
        default=os.environ.get("BRIDGE_OUTER_LEADER_ADAPTER") or os.environ.get("OUTER_LEADER_ADAPTER") or "auto",
        help="Outer leader adapter: auto|sdk|unavailable. auto uses the Claude Agent SDK wrapper.",
    )
    parser.add_argument("--input-json", default=None, help="Submit one input JSON and exit")
    parser.add_argument("--status", action="store_true", help="Print host status and exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = OuterSdkHostConfig.from_values(
        control_root=args.control_root,
        repo_root=args.repo_root,
        repo_key=args.repo_key,
        default_main_session_id=args.main_session_id,
    )
    host = OuterSdkHost(config, adapter=build_outer_leader_adapter(config, mode=args.adapter))
    if args.input_json:
        payload = json.loads(args.input_json)
        print(json.dumps(host.handle_user_input(payload), ensure_ascii=False, indent=2))
        return
    if args.status:
        print(json.dumps(host.status(repo_key=args.repo_key), ensure_ascii=False, indent=2))
        return
    serve(host, bind_host=args.host, port=args.port)


def serve(host: OuterSdkHost, *, bind_host: str, port: int) -> None:
    token = os.environ.get("OUTER_SDK_HOST_TOKEN") or os.environ.get("BRIDGE_OUTER_HOST_TOKEN") or ""

    class Handler(BaseHTTPRequestHandler):
        server_version = "RunBridgeOuterSdkHost/0.1"

        def do_OPTIONS(self) -> None:
            self._json(204, {})

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json(200, {"ok": True, "mode": "outer_sdk_host"})
                return
            if parsed.path == "/v1/status":
                if not self._authorized(token):
                    self._json(401, {"error": "unauthorized"})
                    return
                query = parse_qs(parsed.query)
                self._json(
                    200,
                    host.status(
                        repo_key=_first(query, "repo_key") or _first(query, "repoKey"),
                        run_id=_first(query, "run_id") or _first(query, "runId"),
                    ),
                )
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/v1/input":
                self._json(404, {"error": "not_found"})
                return
            if not self._authorized(token):
                self._json(401, {"error": "unauthorized"})
                return
            try:
                payload = self._read_json_body()
                self._json(200, host.handle_user_input(payload))
            except Exception as exc:
                self._json(400, {"error": "bad_request", "message": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _authorized(self, expected: str) -> bool:
            if not expected:
                return True
            auth = self.headers.get("authorization") or ""
            bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            header = self.headers.get("x-bridge-outer-host-token") or ""
            return expected in {bearer, header}

        def _json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status_code)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    server = ThreadingHTTPServer((bind_host, port), Handler)
    print(f"RunBridge outer SDK host listening on http://{bind_host}:{port}", flush=True)
    print(f"control root: {Path(host.config.control_root)}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


if __name__ == "__main__":
    main()
