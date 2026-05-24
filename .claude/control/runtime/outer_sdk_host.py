from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from outer_sdk import OuterSdkHost, OuterSdkHostConfig, build_outer_leader_adapter
from outer_sdk.claude_agent_adapter import outer_leader_startup_diagnostics


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
        help="Outer leader adapter: auto|sdk|tmux|unavailable. auto uses tmux for custom providers on Linux when available, otherwise the Claude Agent SDK wrapper.",
    )
    parser.add_argument("--input-json", default=None, help="Submit one input JSON and exit")
    parser.add_argument("--status", action="store_true", help="Print host status and exit")
    parser.add_argument("--diagnose-startup", action="store_true", help="Print Claude startup diagnostics and exit without sending an API request")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    applied_defaults = install_claude_startup_defaults(args.control_root, repo_root=args.repo_root)
    if args.diagnose_startup:
        diagnostics = outer_leader_startup_diagnostics(args.control_root, repo_root=args.repo_root)
        if applied_defaults:
            diagnostics["applied_startup_defaults"] = applied_defaults
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        return
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


def install_claude_startup_defaults(control_root: str | Path, repo_root: str | Path | None = None) -> dict[str, str]:
    """Install the same Claude startup defaults users normally put in an alias.

    The outer host is the system entrypoint, so it must not rely on an
    interactive shell alias such as:
      HOME=/path/to/workspace claude --mcp-config /path/to/.claude/mcp.json
    """

    claude_root = _discover_parent_claude_root(control_root, repo_root=repo_root)
    workspace_home = claude_root.parent
    mcp_config = claude_root / "mcp.json"
    applied: dict[str, str] = {}

    disabled = os.environ.get("BRIDGE_DISABLE_CLAUDE_STARTUP_DEFAULTS", "").strip().lower()
    if disabled in {"1", "true", "yes"}:
        return applied

    has_explicit_command = bool((os.environ.get("BRIDGE_CLAUDE_COMMAND") or "").strip())
    has_explicit_cli = bool((os.environ.get("BRIDGE_CLAUDE_CLI") or "").strip())
    has_outer_cli = bool((os.environ.get("OUTER_LEADER_CLAUDE_CLI") or "").strip())
    if not has_explicit_command and not has_explicit_cli and not has_outer_cli and mcp_config.exists():
        command = f"HOME={_shell_quote(str(workspace_home))} claude --mcp-config {_shell_quote(str(mcp_config))}"
        os.environ["BRIDGE_CLAUDE_COMMAND"] = command
        applied["BRIDGE_CLAUDE_COMMAND"] = command

    return applied


def _discover_parent_claude_root(control_root: str | Path, repo_root: str | Path | None = None) -> Path:
    if repo_root:
        repo = Path(repo_root).expanduser().resolve()
        candidate = repo.parent / ".claude"
        if candidate.exists():
            return candidate
    control = Path(control_root).expanduser().resolve()
    return control.parent


def _shell_quote(value: str) -> str:
    if os.name != "nt":
        return shlex.quote(value)
    if not value or any(ch.isspace() for ch in value) or '"' in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


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
                query = parse_qs(parsed.query)
                if _truthy(_first(query, "async") or _first(query, "background")):
                    request = host.queue_user_input(payload)
                    response = host.build_queued_input_ack(request)
                    worker = threading.Thread(
                        target=_run_queued_input_background,
                        args=(host, request),
                        daemon=True,
                    )
                    try:
                        self._json(202, response)
                    except OSError:
                        pass
                    finally:
                        worker.start()
                    return
                self._json(200, host.handle_user_input(payload))
            except Exception as exc:
                self._json(400, {"error": "bad_request", "message": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(_decode_request_body(raw))
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


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _run_queued_input_background(host: OuterSdkHost, request: dict[str, Any]) -> None:
    host.handle_queued_user_input(request)


def _decode_request_body(raw: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


if __name__ == "__main__":
    main()
