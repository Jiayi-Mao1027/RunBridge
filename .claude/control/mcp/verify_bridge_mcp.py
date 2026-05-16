from __future__ import annotations

import json
import shutil
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
    "list_registered_repos",
    "list_runs",
    "read_repo_snapshot",
}


def main() -> int:
    project_root = Path(__file__).resolve().parents[3]
    mcp_config = _load_mcp_config(project_root)
    bridge = mcp_config["mcpServers"]["bridge"]
    command = bridge["command"]
    args = bridge.get("args", [])
    cwd_context = _verify_cwd(project_root, args)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "verify", "version": "0.1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_runtime_snapshot", "arguments": {"run_id": "verify_install_run"}}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "build_bridge_packet",
                "arguments": {
                    "run_id": "verify_install_run",
                    "target_phase": "l4_implement",
                    "user_instruction": "系统测试目标冻结为：在当前仓库中搭建一个 DPO 框架；遇到报错立即停止。",
                },
            },
        },
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
    chinese_response = next((item for item in responses if item.get("id") == 4), None)
    if not chinese_response or chinese_response.get("error"):
        print(f"chinese build_bridge_packet failed: {chinese_response}", file=sys.stderr)
        return 1
    content = chinese_response.get("result", {}).get("content", [])
    text = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
    packet_result = json.loads(text)
    packet_ref = packet_result.get("packet_ref")
    if packet_result.get("packet_saved") is True and packet_ref:
        packet = json.loads(Path(packet_ref).read_text(encoding="utf-8"))
    else:
        packet = packet_result["packet"]
    description = packet["task_spec"]["task_description"]
    if "系统测试目标冻结为" not in description or "当前仓库" not in description:
        print(f"chinese roundtrip failed: {description}", file=sys.stderr)
        return 1
    dispatch_contract = packet.get("dispatch_contract")
    if not isinstance(dispatch_contract, dict):
        print("build_bridge_packet missing dispatch_contract", file=sys.stderr)
        return 1
    teammate_names = set(dispatch_contract.get("allowed_agent_subagent_types") or [])
    if not {"implementor", "rungater"}.issubset(teammate_names):
        print(f"dispatch_contract teammate mismatch: {sorted(teammate_names)}", file=sys.stderr)
        return 1
    for teammate_name in ["implementor", "rungater"]:
        teammate = (dispatch_contract.get("teammates") or {}).get(teammate_name)
        agent_dispatch = teammate.get("agent_dispatch") if isinstance(teammate, dict) else None
        if not isinstance(agent_dispatch, dict):
            print(f"dispatch_contract missing agent_dispatch for {teammate_name}", file=sys.stderr)
            return 1
        if "model" in agent_dispatch:
            print(f"dispatch_contract agent_dispatch must not include model for {teammate_name}: {agent_dispatch}", file=sys.stderr)
            return 1
        allowed_input_keys = set(agent_dispatch.get("allowed_input_keys") or [])
        if allowed_input_keys != {"description", "prompt", "subagent_type"}:
            print(f"dispatch_contract allowed_input_keys mismatch for {teammate_name}: {agent_dispatch}", file=sys.stderr)
            return 1
        model_binding = teammate.get("model_binding") if isinstance(teammate, dict) else None
        if (
            not isinstance(model_binding, dict)
            or model_binding.get("model") != "gpt-main"
            or model_binding.get("agent_tool_model_field") != "system_payload_must_be_absent"
            or model_binding.get("tolerated_schema_carrier") != "sonnet"
        ):
            print(f"dispatch_contract model_binding mismatch for {teammate_name}: {teammate}", file=sys.stderr)
            return 1
    print(
        json.dumps(
            {
                "ok": True,
                "tools": sorted(tools),
                "tested_tool_call": "read_runtime_snapshot",
                "chinese_roundtrip": "passed",
                "dispatch_contract": "passed",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _load_mcp_config(project_root: Path) -> dict:
    mcp_path = project_root / ".claude" / "mcp.json"
    if mcp_path.exists():
        return json.loads(mcp_path.read_text(encoding="utf-8"))
    settings_path = project_root / ".claude" / "settings.json"
    return json.loads(settings_path.read_text(encoding="utf-8"))


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
        return _TemporaryCwd(Path(tempfile.mkdtemp(prefix="bridge_verify_repo_", dir=str(project_root))))
    return _StaticCwd(project_root)


class _StaticCwd:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _TemporaryCwd(_StaticCwd):
    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
        return None


if __name__ == "__main__":
    raise SystemExit(main())
