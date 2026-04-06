#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import subprocess
import time
from typing import Any

from claude_agent_sdk import query, ClaudeAgentOptions

try:
    import tomllib
except Exception:
    tomllib = None


FORCED_CLAUDE_SETTINGS_PATH = pathlib.Path("/data03/liang/mjy/.claude-isolated/settings.json")


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: pathlib.Path, payload: Any) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tail_text(path: pathlib.Path, max_lines: int = 120) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:]).rstrip()


def role_slug(role: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", role.strip()).strip("_") or "role"


# ---------------------------------------------------------------------------
# Path resolution: do NOT trust $HOME in this deployment.
# Runner path is expected to be: <user_root>/.codex/protocol/bin/claude_skill_runner.py
# ---------------------------------------------------------------------------

def resolve_codex_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]  # .../.codex


def resolve_user_root() -> pathlib.Path:
    return resolve_codex_root().parent


def parse_frontmatter(md_text: str) -> dict[str, str]:
    text = md_text.lstrip()
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    block = text[4:end]
    meta: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def load_skill_metadata(skill_dir: pathlib.Path) -> dict[str, str]:
    text = read_text(skill_dir / "SKILL.md")
    meta = parse_frontmatter(text)
    if "worker_contract" not in meta:
        meta["worker_contract"] = "worker.md"
    if "entrypoint" not in meta:
        meta["entrypoint"] = "bin/run.sh"
    return meta


def infer_run_id(packet: pathlib.Path | None, explicit_run_id: str | None) -> str:
    if explicit_run_id:
        return explicit_run_id
    if packet is not None:
        parts = list(packet.resolve().parts)
        for i, part in enumerate(parts):
            if part == "runs" and i + 1 < len(parts):
                candidate = parts[i + 1].strip()
                if candidate:
                    return candidate
    return time.strftime("manual_%Y%m%d_%H%M%S")


def infer_run_root(
    repo_root: pathlib.Path,
    packet: pathlib.Path | None,
    run_id: str,
    output_dir: pathlib.Path | None,
) -> pathlib.Path:
    if packet is not None:
        parts = list(packet.resolve().parts)
        for i, part in enumerate(parts):
            if part == "runs" and i + 1 < len(parts) and parts[i + 1] == run_id:
                return pathlib.Path(*parts[: i + 2])
    if output_dir is not None and output_dir.parent.name == run_id:
        return output_dir.parent
    return repo_root / "artifacts" / "runs" / run_id


def default_output_dir(run_root: pathlib.Path, role: str) -> pathlib.Path:
    return run_root / role_slug(role)


def ensure_project_state(repo_root: pathlib.Path) -> None:
    helper = resolve_codex_root() / "protocol" / "bin" / "ensure_project_state.sh"
    if helper.exists():
        subprocess.run([str(helper), str(repo_root)], check=True)


# ---------------------------------------------------------------------------
# bridge_api.toml
# ---------------------------------------------------------------------------

def load_bridge_config() -> dict[str, Any]:
    path = resolve_codex_root() / "bridge_api.toml"
    if not path.exists():
        return {}
    if tomllib is None:
        raise RuntimeError("Python tomllib unavailable; use Python 3.11+")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = data.get("claude_bridge", {})
    if not isinstance(cfg, dict):
        raise RuntimeError("invalid bridge config: [claude_bridge] must be a table")
    return cfg


def resolved_cli_path(bridge_cfg: dict[str, Any]) -> str | None:
    value = str(bridge_cfg.get("cli_path", "")).strip()
    return value or None


def normalize_model_name(model: str | None) -> str | None:
    if model is None:
        return None
    value = model.strip()
    if not value:
        return None
    aliases = {
        "claude-sonnet4-6": "claude-sonnet-4-6",
        "claude-opus4-6": "claude-opus-4-6",
        "claude-haiku4-5": "claude-haiku-4-5",
    }
    return aliases.get(value, value)


def resolved_model(args_model: str | None, bridge_cfg: dict[str, Any]) -> str | None:
    if args_model:
        return normalize_model_name(args_model)
    value = str(bridge_cfg.get("model", "")).strip()
    return normalize_model_name(value)


def resolved_settings_path(bridge_cfg: dict[str, Any]) -> str | None:
    if not FORCED_CLAUDE_SETTINGS_PATH.is_file():
        raise RuntimeError(
            f"required Claude settings file is missing: {FORCED_CLAUDE_SETTINGS_PATH}"
        )
    return str(FORCED_CLAUDE_SETTINGS_PATH)


def resolved_setting_sources(bridge_cfg: dict[str, Any]) -> list[str]:
    raw = bridge_cfg.get("setting_sources", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuntimeError("claude_bridge.setting_sources must be a list")
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def resolved_claude_config_dir(bridge_cfg: dict[str, Any]) -> str | None:
    value = str(bridge_cfg.get("claude_config_dir", "")).strip()
    return value or None


def resolved_enable_tool_search(bridge_cfg: dict[str, Any]) -> str | None:
    value = str(bridge_cfg.get("enable_tool_search", "")).strip()
    return value or None


def resolved_enable_claudeai_mcp_servers(bridge_cfg: dict[str, Any]) -> str | None:
    value = str(bridge_cfg.get("enable_claudeai_mcp_servers", "")).strip()
    return value or None


def resolved_headers(bridge_cfg: dict[str, Any]) -> dict[str, str]:
    raw = bridge_cfg.get("headers", {}) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def resolved_global_claude_md(bridge_cfg: dict[str, Any]) -> pathlib.Path:
    cfg_dir = resolved_claude_config_dir(bridge_cfg)
    if cfg_dir:
        return pathlib.Path(cfg_dir).resolve() / "CLAUDE.md"
    # fallback only if not provided
    return resolve_user_root() / ".claude" / "CLAUDE.md"


def merge_headers_for_env(headers_obj: dict[str, str]) -> str | None:
    if not headers_obj:
        return None
    return "\n".join(f"{k}: {v}" for k, v in headers_obj.items())


def build_runtime_env(bridge_cfg: dict[str, Any]) -> dict[str, str]:
    """
    Internal runtime env built from TOML.
    User does not need to export anything manually.
    """
    env_updates: dict[str, str] = {}
    env_updates["HOME"] = str(resolve_user_root())
    env_updates["CODEX_HOME"] = str(resolve_codex_root())

    cfg_dir = resolved_claude_config_dir(bridge_cfg)
    if cfg_dir:
        env_updates["CLAUDE_CONFIG_DIR"] = cfg_dir

    enable_tool_search = resolved_enable_tool_search(bridge_cfg)
    if enable_tool_search:
        env_updates["ENABLE_TOOL_SEARCH"] = enable_tool_search

    enable_mcp = resolved_enable_claudeai_mcp_servers(bridge_cfg)
    if enable_mcp:
        env_updates["ENABLE_CLAUDEAI_MCP_SERVERS"] = enable_mcp

    merged_headers = merge_headers_for_env(resolved_headers(bridge_cfg))
    if merged_headers:
        env_updates["ANTHROPIC_CUSTOM_HEADERS"] = merged_headers

    return env_updates


def build_cli_extra_args(bridge_cfg: dict[str, Any], user_extra_args: list[str]) -> dict[str, str | None]:
    extra: dict[str, str | None] = {}

    for item in user_extra_args:
        token = item.strip()
        if not token:
            continue
        if "=" in token:
            k, v = token.split("=", 1)
            extra[k.lstrip("-")] = v
        else:
            extra[token.lstrip("-")] = None

    return extra


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_system_prompt(skill_dir: pathlib.Path, repo_root: pathlib.Path, bridge_cfg: dict[str, Any]) -> str:
    meta = load_skill_metadata(skill_dir)
    worker_path = skill_dir / meta.get("worker_contract", "worker.md")
    global_claude_md = resolved_global_claude_md(bridge_cfg)

    parts: list[str] = []
    for title, path in [
        ("GLOBAL_CODEX_AGENTS", resolve_codex_root() / "AGENTS.md"),
        ("GLOBAL_CLAUDE_RUNTIME", global_claude_md),
        ("REPOSITORY_AGENTS", repo_root / "AGENTS.md"),
        ("REPOSITORY_CLAUDE", repo_root / "CLAUDE.md"),
        ("BOUND_WORKER_CONTRACT", worker_path),
    ]:
        text = read_text(path).strip()
        if text:
            parts.append(f"BEGIN {title}\n{text}\nEND {title}")

    parts.append(
        "Execution rule: the active cc-* skill is the authoritative Claude leaf-worker entrypoint. "
        "Do not assume a separate ~/.claude/agents registry exists."
    )
    parts.append(
        "Reading rule: you may only claim to have read files whose contents were actually provided through "
        "this bridge payload, packet payload, or repository documents embedded into the prompt."
    )
    return "\n\n".join(parts).strip()


def output_contract_text() -> str:
    return """
Return exactly one JSON object and no surrounding commentary.

Required top-level fields:
- assistant_markdown: string
- report_markdown: string
- handoff_markdown: string
- orchestrator_summary: object
- receipt: object

Required orchestrator_summary fields:
- headline: string
- status: string
- next_action_owner: string
- next_action: string
- key_points: array of strings
- evidence_paths: array of strings

Required receipt fields:
- role: string
- phase: string
- scope_completed: boolean
- issues: array

Each issue object should include when available:
- title
- issue_type
- severity
- evidence_paths
- next_action

Optional top-level field:
- extra_artifacts: object mapping relative paths to either string content or JSON values

Do not wrap the JSON in markdown fences unless unavoidable.
""".strip()


def build_user_prompt(
    *,
    role: str,
    repo_root: pathlib.Path,
    packet: pathlib.Path,
    phase: str | None,
    run_id: str,
) -> str:
    packet_text = read_text(packet).strip()
    if not packet_text:
        raise RuntimeError(f"packet is empty: {packet}")

    phase_line = f"Phase: {phase}\n" if phase else ""

    return (
        f"Role: {role}\n"
        f"Run ID: {run_id}\n"
        f"Repository root: {repo_root}\n"
        f"Packet path: {packet}\n"
        f"{phase_line}"
        "Follow the bound worker contract and packet exactly.\n\n"
        "You must return outputs that are directly usable by the Codex orchestrator.\n\n"
        f"{output_contract_text()}\n\n"
        f"BEGIN PACKET\n{packet_text}\nEND PACKET"
    ).strip()


# ---------------------------------------------------------------------------
# Output parsing / validation
# ---------------------------------------------------------------------------

def result_message_payload(message: Any) -> dict[str, Any]:
    return {
        "type": type(message).__name__,
        "result": getattr(message, "result", None),
        "total_cost_usd": getattr(message, "total_cost_usd", None),
        "duration_ms": getattr(message, "duration_ms", None),
        "num_turns": getattr(message, "num_turns", None),
        "session_id": getattr(message, "session_id", None),
    }


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise RuntimeError("assistant returned empty output")

    candidates: list[str] = [text]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(fenced)

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first:last + 1])

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    raise RuntimeError("worker output is not a valid JSON object")


def validate_worker_payload(payload: dict[str, Any], role: str, phase: str | None) -> dict[str, Any]:
    required_top = [
        "assistant_markdown",
        "report_markdown",
        "handoff_markdown",
        "orchestrator_summary",
        "receipt",
    ]
    for key in required_top:
        if key not in payload:
            raise RuntimeError(f"worker payload missing required field: {key}")

    if not isinstance(payload["assistant_markdown"], str):
        raise RuntimeError("assistant_markdown must be a string")
    if not isinstance(payload["report_markdown"], str):
        raise RuntimeError("report_markdown must be a string")
    if not isinstance(payload["handoff_markdown"], str):
        raise RuntimeError("handoff_markdown must be a string")

    summary = payload["orchestrator_summary"]
    if not isinstance(summary, dict):
        raise RuntimeError("orchestrator_summary must be an object")
    for key in ["headline", "status", "next_action_owner", "next_action", "key_points", "evidence_paths"]:
        if key not in summary:
            raise RuntimeError(f"orchestrator_summary missing required field: {key}")
    if not isinstance(summary["key_points"], list):
        raise RuntimeError("orchestrator_summary.key_points must be a list")
    if not isinstance(summary["evidence_paths"], list):
        raise RuntimeError("orchestrator_summary.evidence_paths must be a list")

    receipt = payload["receipt"]
    if not isinstance(receipt, dict):
        raise RuntimeError("receipt must be an object")
    for key in ["role", "phase", "scope_completed", "issues"]:
        if key not in receipt:
            raise RuntimeError(f"receipt missing required field: {key}")
    if not isinstance(receipt["scope_completed"], bool):
        raise RuntimeError("receipt.scope_completed must be boolean")
    if not isinstance(receipt["issues"], list):
        raise RuntimeError("receipt.issues must be a list")

    receipt["role"] = receipt.get("role") or role
    if phase is not None and not receipt.get("phase"):
        receipt["phase"] = phase

    if "extra_artifacts" in payload and not isinstance(payload["extra_artifacts"], dict):
        raise RuntimeError("extra_artifacts must be an object when present")

    return payload


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def summary_markdown(role: str, summary: dict[str, Any]) -> str:
    key_points = summary.get("key_points", []) or []
    evidence_paths = summary.get("evidence_paths", []) or []
    return (
        f"# Orchestrator Summary: {role}\n\n"
        f"- headline: {summary.get('headline', '')}\n"
        f"- status: {summary.get('status', '')}\n"
        f"- next_action_owner: {summary.get('next_action_owner', '')}\n"
        f"- next_action: {summary.get('next_action', '')}\n\n"
        f"## Key Points\n"
        + ("\n".join(f"- {x}" for x in key_points) if key_points else "- None")
        + "\n\n## Evidence Paths\n"
        + ("\n".join(f"- {x}" for x in evidence_paths) if evidence_paths else "- None")
        + "\n"
    )


def write_protocol_artifacts(
    *,
    run_root: pathlib.Path,
    output_dir: pathlib.Path,
    role: str,
    payload: dict[str, Any],
) -> None:
    slug = role_slug(role)

    output_dir.mkdir(parents=True, exist_ok=True)
    (run_root / "reports").mkdir(parents=True, exist_ok=True)
    (run_root / "handoffs").mkdir(parents=True, exist_ok=True)
    (run_root / "completion_receipts").mkdir(parents=True, exist_ok=True)
    (run_root / "orchestrator_summaries").mkdir(parents=True, exist_ok=True)

    assistant_markdown = payload["assistant_markdown"].strip() + "\n"
    report_markdown = payload["report_markdown"].strip() + "\n"
    handoff_markdown = payload["handoff_markdown"].strip() + "\n"
    summary = payload["orchestrator_summary"]
    receipt = payload["receipt"]

    write_text(output_dir / "output" / "assistant_output.md", assistant_markdown)
    write_json(output_dir / "output" / "worker_payload.json", payload)

    write_text(run_root / "reports" / f"{slug}.md", report_markdown)
    write_text(run_root / "handoffs" / f"{slug}.md", handoff_markdown)
    write_json(run_root / "completion_receipts" / f"{slug}.json", receipt)
    write_json(run_root / "orchestrator_summaries" / f"{slug}.json", summary)
    write_text(run_root / "orchestrator_summaries" / f"{slug}.md", summary_markdown(role, summary))

    for rel_path, content in (payload.get("extra_artifacts") or {}).items():
        dst = run_root / rel_path
        if isinstance(content, str):
            write_text(dst, content if content.endswith("\n") else content + "\n")
        else:
            write_json(dst, content)


def write_failure_artifacts(
    *,
    run_root: pathlib.Path,
    output_dir: pathlib.Path,
    role: str,
    phase: str | None,
    error_text: str,
    debug_log_path: pathlib.Path | None = None,
) -> None:
    slug = role_slug(role)
    (output_dir / "output").mkdir(parents=True, exist_ok=True)
    (run_root / "completion_receipts").mkdir(parents=True, exist_ok=True)
    (run_root / "orchestrator_summaries").mkdir(parents=True, exist_ok=True)
    protocol_error = error_text.rstrip()
    if debug_log_path is not None:
        protocol_error += f"\n\nClaude debug log: {debug_log_path}"
        debug_tail = tail_text(debug_log_path)
        if debug_tail:
            protocol_error += f"\n\nLast Claude debug lines:\n{debug_tail}"
    protocol_error += "\n"

    write_text(output_dir / "output" / "protocol_error.md", protocol_error)

    receipt = {
        "role": role,
        "phase": phase or "",
        "scope_completed": False,
        "issues": [
            {
                "title": "claude_skill_runner_failure",
                "issue_type": "hard_stop",
                "severity": "high",
                "evidence_paths": [str(output_dir / "output" / "protocol_error.md")],
                "next_action": "inspect runner failure and worker output",
            }
        ],
    }
    summary = {
        "headline": f"{role} runner failure",
        "status": "runner_error",
        "next_action_owner": "orchestrator",
        "next_action": "inspect protocol_error.md and raw artifacts",
        "key_points": [error_text.splitlines()[0] if error_text.strip() else "unknown runner failure"],
        "evidence_paths": [str(output_dir / "output" / "protocol_error.md")],
    }

    write_json(run_root / "completion_receipts" / f"{slug}.json", receipt)
    write_json(run_root / "orchestrator_summaries" / f"{slug}.json", summary)
    write_text(run_root / "orchestrator_summaries" / f"{slug}.md", summary_markdown(role, summary))


# ---------------------------------------------------------------------------
# Main SDK execution
# ---------------------------------------------------------------------------

async def run_sdk(args: argparse.Namespace) -> int:
    skill_dir = pathlib.Path(args.skill_dir).resolve()
    repo_root = pathlib.Path(args.repo_root).resolve()
    packet = pathlib.Path(args.packet).resolve()

    if not repo_root.is_dir():
        raise RuntimeError(f"invalid repo root: {repo_root}")
    if not skill_dir.is_dir():
        raise RuntimeError(f"invalid skill dir: {skill_dir}")
    if not packet.is_file():
        raise RuntimeError(f"invalid packet path: {packet}")

    run_id = infer_run_id(packet, args.run_id)
    tentative_output_dir = pathlib.Path(args.output_dir).resolve() if args.output_dir else None
    run_root = infer_run_root(repo_root, packet, run_id, tentative_output_dir)
    output_dir = tentative_output_dir if tentative_output_dir is not None else default_output_dir(run_root, args.role)

    raw_dir = output_dir / "raw"
    out_dir = output_dir / "output"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    ensure_project_state(repo_root)

    bridge_cfg = load_bridge_config()
    cli_path = resolved_cli_path(bridge_cfg)
    model = resolved_model(args.model, bridge_cfg)
    settings_path = resolved_settings_path(bridge_cfg)
    setting_sources = resolved_setting_sources(bridge_cfg)
    runtime_env = build_runtime_env(bridge_cfg)
    debug_log_path = raw_dir / "claude_debug.log"
    cli_extra_args = build_cli_extra_args(bridge_cfg, args.extra_arg)
    # Always run in bare mode so the bridge does not depend on workspace-side
    # CLAUDE discovery or bubblewrap permission plumbing in this environment.
    cli_extra_args.setdefault("bare", None)
    cli_extra_args.setdefault("debug-file", str(debug_log_path))

    system_prompt = build_system_prompt(skill_dir, repo_root, bridge_cfg)
    user_prompt = build_user_prompt(
        role=args.role,
        repo_root=repo_root,
        packet=packet,
        phase=args.phase,
        run_id=run_id,
    )

    write_text(raw_dir / "system_prompt.md", system_prompt + "\n")
    write_text(raw_dir / "user_prompt.md", user_prompt + "\n")

    sdk_options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        cwd=str(repo_root),
        add_dirs=[str(p) for p in args.add_dir],
        model=model,
        cli_path=cli_path,
        settings=settings_path,
        setting_sources=setting_sources,
        max_turns=args.max_turns,
        max_thinking_tokens=args.max_thinking_tokens,
        allowed_tools=args.allowed_tool,
        disallowed_tools=args.disallowed_tool,
        permission_mode=args.permission_mode,
        extra_args=cli_extra_args,
    )

    meta = {
        "role": args.role,
        "run_id": run_id,
        "repo_root": str(repo_root),
        "skill_dir": str(skill_dir),
        "packet": str(packet),
        "phase": args.phase or "",
        "run_root": str(run_root),
        "output_dir": str(output_dir),
        "bridge_config_applied": {
            "cli_path": cli_path,
            "model": model,
            "settings": settings_path,
            "setting_sources": setting_sources,
            "claude_config_dir": resolved_claude_config_dir(bridge_cfg),
            "enable_tool_search": resolved_enable_tool_search(bridge_cfg),
            "enable_claudeai_mcp_servers": resolved_enable_claudeai_mcp_servers(bridge_cfg),
            "claude_md_path": str(resolved_global_claude_md(bridge_cfg)),
        },
        "sdk_options": {
            "cli_path": cli_path,
            "model": model,
            "max_thinking_tokens": args.max_thinking_tokens,
            "max_turns": args.max_turns,
            "allowed_tools": args.allowed_tool,
            "disallowed_tools": args.disallowed_tool,
            "permission_mode": args.permission_mode,
            "add_dirs": [str(p) for p in args.add_dir],
            "extra_args": cli_extra_args,
        },
        "runtime_env_keys": sorted(runtime_env.keys()),
        "sent": False,
    }
    write_json(raw_dir / "meta.json", meta)

    append_jsonl(
        run_root / "stage_journal.jsonl",
        {
            "ts": now_ts(),
            "role": args.role,
            "phase": args.phase or "",
            "state": "started",
            "output_dir": str(output_dir),
        },
    )

    if args.no_send:
        append_jsonl(
            run_root / "stage_journal.jsonl",
            {
                "ts": now_ts(),
                "role": args.role,
                "phase": args.phase or "",
                "state": "completed",
                "output_dir": str(output_dir),
                "no_send": True,
            },
        )
        print(str(output_dir))
        return 0

    old_env = os.environ.copy()
    streamed_text: list[str] = []
    result_meta: dict[str, Any] | None = None
    message_log: list[dict[str, Any]] = []

    try:
        os.environ.update(runtime_env)

        async for message in query(prompt=user_prompt, options=sdk_options):
            msg_type = type(message).__name__
            entry: dict[str, Any] = {"type": msg_type}

            if hasattr(message, "content"):
                blocks = []
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if isinstance(text, str) and text:
                        blocks.append(text)
                        streamed_text.append(text)
                if blocks:
                    entry["text_blocks"] = blocks

            if hasattr(message, "result"):
                result_meta = result_message_payload(message)
                entry.update(result_meta)

            message_log.append(entry)

        assistant_output = "".join(streamed_text).strip()
        if not assistant_output and result_meta and result_meta.get("result"):
            assistant_output = str(result_meta["result"]).strip()

        write_json(raw_dir / "messages.json", {"messages": message_log})
        write_json(raw_dir / "result_meta.json", result_meta or {})

        payload = extract_json_object(assistant_output)
        payload = validate_worker_payload(payload, args.role, args.phase)

        write_protocol_artifacts(
            run_root=run_root,
            output_dir=output_dir,
            role=args.role,
            payload=payload,
        )

        append_jsonl(
            run_root / "stage_journal.jsonl",
            {
                "ts": now_ts(),
                "role": args.role,
                "phase": args.phase or "",
                "state": "completed",
                "output_dir": str(output_dir),
            },
        )

        print(str(output_dir))
        return 0

    except Exception as e:
        error_text = f"{type(e).__name__}: {e}"
        write_failure_artifacts(
            run_root=run_root,
            output_dir=output_dir,
            role=args.role,
            phase=args.phase,
            error_text=error_text,
            debug_log_path=debug_log_path,
        )
        append_jsonl(
            run_root / "stage_journal.jsonl",
            {
                "ts": now_ts(),
                "role": args.role,
                "phase": args.phase or "",
                "state": "failed",
                "output_dir": str(output_dir),
                "error": error_text,
            },
        )
        raise
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True)
    ap.add_argument("--skill-dir", required=True)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--packet", required=True)
    ap.add_argument("--phase")
    ap.add_argument("--run-id")
    ap.add_argument("--output-dir")

    ap.add_argument("--model")
    ap.add_argument("--max-thinking-tokens", type=int)
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--permission-mode")

    ap.add_argument("--add-dir", action="append", default=[])
    ap.add_argument("--allowed-tool", action="append", default=[])
    ap.add_argument("--disallowed-tool", action="append", default=[])
    ap.add_argument("--extra-arg", action="append", default=[])

    ap.add_argument("--no-send", action="store_true")
    return ap


def main() -> int:
    ap = build_argparser()
    args = ap.parse_args()
    return asyncio.run(run_sdk(args))


if __name__ == "__main__":
    raise SystemExit(main())
