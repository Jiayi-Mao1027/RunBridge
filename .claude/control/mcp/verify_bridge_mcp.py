from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


REQUIRED_TOOLS = {
    "read_runtime_snapshot",
    "build_bridge_packet",
    "call_bridge_sdk",
    "dispatch_workflow_event",
    "reconcile_workflow_from_ledger",
}


def main() -> int:
    project_root = Path(__file__).resolve().parents[3]
    settings_path = project_root / ".claude" / "settings.json"
    mcp_config = json.loads(settings_path.read_text(encoding="utf-8"))
    bridge = mcp_config["mcpServers"]["bridge"]
    command = bridge["command"]
    args = bridge.get("args", [])
    cwd_context = _verify_cwd(project_root, args)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "verify", "version": "0.1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_runtime_snapshot", "arguments": {"run_id": "verify_install_run"}}},
    ]
    env = None
    if isinstance(bridge.get("env"), dict):
        import os

        env = {**os.environ, **{str(k): str(v) for k, v in bridge["env"].items()}}
    with cwd_context as repo_cwd:
        proc = subprocess.run(
            [command, *args],
            input="\n".join(json.dumps(message, separators=(",", ":")) for message in messages) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(repo_cwd),
            env=env,
            timeout=10,
        )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return proc.returncode
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    tool_response = next((item for item in responses if item.get("id") == 2), None)
    if not tool_response:
        print("missing tools/list response", file=sys.stderr)
        return 1
    tools = {tool["name"] for tool in tool_response.get("result", {}).get("tools", [])}
    missing = sorted(REQUIRED_TOOLS - tools)
    if missing:
        print(f"missing MCP tools: {missing}", file=sys.stderr)
        return 1
    call_response = next((item for item in responses if item.get("id") == 3), None)
    if not call_response or call_response.get("error"):
        print(f"tools/call failed: {call_response}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "tools": sorted(tools), "tested_tool_call": "read_runtime_snapshot"}, ensure_ascii=False, indent=2))
    return 0


def _verify_cwd(project_root: Path, args: list[str]):
    configured = None
    for index, arg in enumerate(sys.argv):
        if arg == "--repo-cwd" and index + 1 < len(sys.argv):
            configured = Path(sys.argv[index + 1]).expanduser().resolve()
            break
    if configured:
        return _StaticCwd(configured)

    first_script = next((Path(item) for item in args if str(item).endswith("bridge_server.py")), None)
    if first_script and not (project_root / first_script).exists() and str(first_script).startswith(".."):
        smoke_repo = project_root / "smoke01"
        if smoke_repo.exists():
            return _StaticCwd(smoke_repo)
        return _StaticCwd(Path(tempfile.mkdtemp(prefix="bridge_verify_repo_", dir=str(project_root))))
    return _StaticCwd(project_root)


class _StaticCwd:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
