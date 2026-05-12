from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import Any
import uuid

from .adapters import OuterLeaderEventSink
from .claude_agent_adapter import (
    _env_int,
    _leader_model,
    _outer_leader_cli_info,
)


PREVIEW_LIMIT = 700
REPORT_TEXT_LIMIT = 20000
TMUX_DEFAULT_TIMEOUT_SECONDS = 0


class TmuxReplOuterLeaderAdapter:
    """One-shot outer leader adapter backed by Claude Code's interactive TTY path.

    Claude Code's SDK/headless entrypoint can behave differently from the real
    interactive CLI entrypoint for custom provider configurations. This adapter
    keeps the outer host HTTP contract while driving the same TTY path that the
    user's working alias uses.
    """

    name = "claude-tmux-repl"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._sequence = 0

    def handle_user_input(
        self,
        request: dict[str, Any],
        *,
        event_sink: OuterLeaderEventSink | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        session_name = self._session_name(request)
        control_root = Path(self.config.control_root).expanduser().resolve()
        repo_root = Path(self.config.repo_root).expanduser().resolve() if self.config.repo_root else Path.cwd().resolve()
        if not shutil.which("tmux"):
            return _blocked_result("OuterLeaderTmuxMissing", "tmux is required for the interactive outer leader adapter.", request)
        cli_info = _outer_leader_cli_info(control_root, repo_root)
        cli_path = str(cli_info.get("cli_path") or "claude")
        prompt = _build_user_prompt(request)
        self._emit(
            event_sink,
            request,
            "sdk_stream_started",
            {
                "session_id": request.get("main_session_id"),
                "input_id": request.get("input_id"),
                "outer_leader_options": {
                    "adapter": self.name,
                    "tty_entrypoint": "tmux",
                    "cli_path": cli_path,
                    "cli_source": cli_info.get("cli_source"),
                    "cli_home": (cli_info.get("env") or {}).get("HOME"),
                    "cli_mcp_config": cli_info.get("mcp_config"),
                    "model": _leader_model(control_root),
                },
            },
            status="streaming",
        )
        try:
            launch = self._launch_command(control_root, repo_root, cli_info, request)
            self._run(["tmux", "new-session", "-d", "-s", session_name, "-x", "140", "-y", "42", launch])
            self._wait_for_prompt(session_name, timeout=45)
            self._run(["tmux", "send-keys", "-t", session_name, "C-l"], check=False)
            time.sleep(0.2)
            self._paste_prompt(session_name, prompt)
            capture = self._wait_for_completion(session_name, prompt, timeout=_tmux_timeout_seconds())
            assistant_text = extract_assistant_text(capture, prompt)
            if not assistant_text:
                assistant_text = _tail_capture(capture)
            duration_ms = int((time.time() - started) * 1000)
            self._emit(
                event_sink,
                request,
                "sdk_stream_assistant_text",
                _assistant_record(request, assistant_text),
                status="completed",
            )
            self._emit(
                event_sink,
                request,
                "sdk_stream_final_result",
                {
                    "session_id": request.get("main_session_id"),
                    "sdk_message_type": "ResultMessage",
                    "is_error": False,
                    "subtype": "success",
                    "result": assistant_text,
                    "duration_ms": duration_ms,
                    "num_turns": 1,
                    "permission_denials": [],
                },
                status="completed",
            )
            self._emit(
                event_sink,
                request,
                "sdk_stream_final",
                {"session_id": request.get("main_session_id"), "message_count": 1},
                status="completed",
            )
            return {
                "status": "succeeded",
                "handled_by": self.name,
                "reports": [{"summary": _limit_text(assistant_text, REPORT_TEXT_LIMIT), "source": "outer_leader_tmux_repl", "session_id": request.get("main_session_id")}],
                "artifact_refs": [],
                "evidence": {
                    "adapter": self.name,
                    "tmux_session": session_name,
                    "duration_ms": duration_ms,
                    "repo_key": request.get("repo_key"),
                    "run_id": request.get("run_id"),
                },
                "error_or_null": None,
                "cleanup_required": False,
            }
        except Exception as exc:
            self._emit(
                event_sink,
                request,
                "sdk_stream_error",
                {"error_type": type(exc).__name__, "message": str(exc), "adapter": self.name},
                status="failed",
            )
            return _blocked_result(type(exc).__name__, str(exc), request)
        finally:
            self._run(["tmux", "kill-session", "-t", session_name], check=False)

    def _launch_command(self, control_root: Path, repo_root: Path, cli_info: dict[str, Any], request: dict[str, Any]) -> str:
        model = _leader_model(control_root)
        env = {
            "CLAUDE_CONTROL_ROOT": str(control_root),
            "BRIDGE_RUNTIME_REPO_KEY": str(request.get("repo_key") or ""),
            "BRIDGE_RUN_ID": str(request.get("run_id") or ""),
            "CLAUDE_CONTROL_RUN_ID": str(request.get("run_id") or ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        }
        env.update({str(key): str(value) for key, value in (cli_info.get("env") or {}).items()})
        parts = ["cd", _q(str(repo_root)), "&&", "env"]
        parts.extend(f"{key}={_q(value)}" for key, value in sorted(env.items()))
        parts.append(_q(str(cli_info.get("cli_path") or "claude")))
        if cli_info.get("mcp_config"):
            parts.extend(["--mcp-config", _q(str(cli_info["mcp_config"]))])
        if cli_info.get("strict_mcp_config", True):
            parts.append("--strict-mcp-config")
        parts.extend(["--model", _q(model)])
        if os.environ.get("OUTER_LEADER_TMUX_DEBUG", "").strip().lower() in {"1", "true", "yes"}:
            debug_file = Path("/tmp") / f"outer_leader_tmux_{request.get('input_id') or uuid.uuid4().hex}.log"
            parts.extend(["--debug", "--debug-file", _q(str(debug_file))])
        return " ".join(parts)

    def _wait_for_prompt(self, session_name: str, *, timeout: int) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self._capture(session_name)
            if "❯" in last:
                return
            time.sleep(0.5)
        raise RuntimeError(f"Claude TTY did not become ready within {timeout}s: {_tail_capture(last)}")

    def _paste_prompt(self, session_name: str, prompt: str) -> None:
        buffer_name = f"outer_leader_{uuid.uuid4().hex[:8]}"
        self._run(["tmux", "load-buffer", "-b", buffer_name, "-"], input_text=prompt)
        self._run(["tmux", "paste-buffer", "-b", buffer_name, "-t", session_name])
        self._run(["tmux", "delete-buffer", "-b", buffer_name], check=False)
        self._run(["tmux", "send-keys", "-t", session_name, "Enter"])

    def _wait_for_completion(self, session_name: str, prompt: str, *, timeout: int | None) -> str:
        deadline = time.time() + timeout if timeout else None
        last = ""
        while deadline is None or time.time() < deadline:
            last = self._capture(session_name)
            if _looks_complete(last, prompt):
                return last
            time.sleep(1.0)
        raise TimeoutError(f"Claude TTY did not complete within {timeout}s: {_tail_capture(last)}")

    def _capture(self, session_name: str) -> str:
        result = self._run(["tmux", "capture-pane", "-p", "-t", session_name, "-S", "-500"])
        return _strip_ansi(result.stdout)

    def _run(self, args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
            timeout=30,
        )

    def _session_name(self, request: dict[str, Any]) -> str:
        raw = f"outer_{request.get('repo_key')}_{request.get('run_id')}_{request.get('input_id')}_{uuid.uuid4().hex[:8]}"
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:120]

    def _emit(
        self,
        event_sink: OuterLeaderEventSink | None,
        request: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        status: str = "streaming",
    ) -> None:
        if event_sink is None:
            return
        self._sequence += 1
        event_sink(event_type, payload, status=status, sequence=self._sequence)


def extract_assistant_text(capture: str, prompt: str | None = None) -> str:
    lines = [_clean_line(line) for line in _strip_ansi(capture).splitlines()]
    lines = [line for line in lines if line.strip()]
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("●"):
            continue
        collected: list[str] = []
        for item in lines[index:]:
            stripped = item.strip()
            if stripped.startswith("✻") or stripped.startswith("❯"):
                break
            if "leader-orchestrator" in stripped and set(stripped.replace("leader-orchestrator", "").strip()) <= {"─", "-"}:
                break
            if _is_transient_tui_status(stripped):
                continue
            collected.append(item.rstrip())
        candidate = _limit_text("\n".join(collected).strip(), REPORT_TEXT_LIMIT)
        if candidate and not _is_transient_tui_status(candidate):
            return candidate
    return ""


def _looks_complete(capture: str, prompt: str) -> bool:
    text = _strip_ansi(capture)
    assistant_text = extract_assistant_text(text, prompt)
    return "✻" in text and text.count("❯") >= 1 and bool(assistant_text) and not _is_transient_tui_status(assistant_text)


def _is_transient_tui_status(text: str) -> bool:
    normalized = str(text or "").strip().lstrip("●").strip().lower()
    if not normalized:
        return False
    if "ctrl+o to expand" in normalized:
        return True
    if normalized.startswith("calling ") and ("…" in normalized or "..." in normalized):
        return True
    return False


def _assistant_record(request: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "timestamp": _now_iso(),
        "event_type": "sdk_stream_assistant_text",
        "source": "outer_tmux_repl",
        "stream_source": "tmux_repl",
        "run_id": request.get("run_id"),
        "repo_key": request.get("repo_key"),
        "session_id": request.get("main_session_id"),
        "agent_id": "leader-orchestrator",
        "agent_type": "main-leader",
        "status": "completed",
        "message_preview": _limit_text(text, PREVIEW_LIMIT),
        "text_delta": _limit_text(text, REPORT_TEXT_LIMIT),
        "sdk_message_type": "AssistantMessage",
    }


def _build_user_prompt(request: dict[str, Any]) -> str:
    text = str(request.get("text") or "").strip()
    if "\n" not in text and "\r" not in text:
        return text
    return f"Follow this user input exactly: {json.dumps(text, ensure_ascii=False)}"


def _blocked_result(error_type: str, message: str, request: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "failed",
        "handled_by": "claude-tmux-repl",
        "reports": [{"summary": message, "source": "outer_leader_tmux_repl"}],
        "artifact_refs": [],
        "evidence": {"repo_key": request.get("repo_key"), "run_id": request.get("run_id")},
        "error_or_null": {"type": error_type, "message": message},
        "cleanup_required": False,
    }


def _strip_ansi(value: str) -> str:
    text = re.sub(r"\x1b\][^\a]*(?:\a|\x1b\\)", "", value)
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
    return text.replace("\r", "")


def _clean_line(value: str) -> str:
    return value.replace("\u00a0", " ").rstrip()


def _tail_capture(value: str, limit: int = 1800) -> str:
    text = _strip_ansi(value).strip()
    return text[-limit:]


def _limit_text(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


def _tmux_timeout_seconds() -> int | None:
    raw = _env_int("OUTER_LEADER_TMUX_TIMEOUT_SECONDS")
    if raw is not None:
        if raw <= 0:
            return None
        return max(30, min(raw, 3600))
    raw = _env_int("OUTER_LEADER_IDLE_TIMEOUT_SECONDS")
    if raw is not None:
        if raw <= 0:
            return None
        return max(30, min(raw, 3600))
    return None if TMUX_DEFAULT_TIMEOUT_SECONDS <= 0 else TMUX_DEFAULT_TIMEOUT_SECONDS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _q(value: str) -> str:
    return shlex.quote(str(value))
