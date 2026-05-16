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
    _outer_leader_allowed_tools,
    _outer_leader_cli_info,
    _outer_leader_disallowed_tools,
    _outer_leader_permission_mode,
    _outer_leader_settings_path,
    _outer_leader_tools,
    _settings_env,
)


PREVIEW_LIMIT = 700
REPORT_TEXT_LIMIT = 20000
TMUX_DEFAULT_TIMEOUT_SECONDS = 0
_TUI_TOOL_LINE_RE = re.compile(
    r"^(?:Read|Grep|Glob|LS|Bash|Edit|Write|MultiEdit|NotebookEdit|TodoWrite|WebFetch|WebSearch|Task|mcp__[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+)\s*\("
)


class OuterLeaderTmuxTerminalError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "OuterLeaderTmuxTerminalError",
        capture: str = "",
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.capture = capture


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
            capture = self._wait_for_completion(session_name, prompt, request=request, timeout=_tmux_timeout_seconds())
            assistant_text = extract_assistant_text(capture, prompt)
            if not assistant_text:
                assistant_text = _tail_capture(capture)
            duration_ms = int((time.time() - started) * 1000)
            contract_violation = _outer_leader_tmux_contract_violation(self.config, request, assistant_text)
            if contract_violation:
                self._emit(
                    event_sink,
                    request,
                    "sdk_stream_error",
                    {"error_type": "OuterLeaderContractViolation", "message": contract_violation, "adapter": self.name},
                    status="failed",
                )
                return _blocked_result(
                    "OuterLeaderContractViolation",
                    contract_violation,
                    request,
                    evidence_extra={
                        "adapter": self.name,
                        "tmux_session": session_name,
                        "duration_ms": duration_ms,
                        "assistant_text_preview": _limit_text(assistant_text, PREVIEW_LIMIT),
                        "outer_leader_tool_state": _outer_leader_tool_state(self.config, request),
                    },
                )
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
        except OuterLeaderTmuxTerminalError as exc:
            self._emit(
                event_sink,
                request,
                "sdk_stream_error",
                {"error_type": exc.error_type, "message": str(exc), "adapter": self.name},
                status="failed",
            )
            return _blocked_result(
                exc.error_type,
                str(exc),
                request,
                evidence_extra={
                    "adapter": self.name,
                    "tmux_session": session_name,
                    "capture_tail": _tail_capture(exc.capture),
                    "failure_classification": _outer_leader_failure_classification(exc.error_type),
                    "same_provider_assumption": "outer leader and bridge team use the same API provider; if only one path fails, treat it as system transport/config/runtime evidence",
                },
            )
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
        settings_path = _outer_leader_settings_path(control_root, cli_info, repo_root)
        env = {
            "CLAUDE_CONTROL_ROOT": str(control_root),
            "BRIDGE_RUNTIME_REPO_KEY": str(request.get("repo_key") or ""),
            "BRIDGE_RUN_ID": str(request.get("run_id") or ""),
            "BRIDGE_MAIN_SESSION_ID": str(request.get("main_session_id") or ""),
            "CLAUDE_CONTROL_RUN_ID": str(request.get("run_id") or ""),
            "CLAUDE_CONTROL_MAIN_SESSION_ID": str(request.get("main_session_id") or ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        env.update(_settings_env(settings_path))
        env.update({str(key): str(value) for key, value in (cli_info.get("env") or {}).items()})
        env["ANTHROPIC_MODEL"] = model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
        _ensure_env_api_key_alias(env)
        parts = ["cd", _q(str(repo_root)), "&&", "env"]
        parts.extend(f"{key}={_q(value)}" for key, value in sorted(env.items()))
        parts.append(_q(str(cli_info.get("cli_path") or "claude")))
        if cli_info.get("mcp_config"):
            parts.extend(["--mcp-config", _q(str(cli_info["mcp_config"]))])
        if cli_info.get("strict_mcp_config", True):
            parts.append("--strict-mcp-config")
        if settings_path:
            parts.extend(["--settings", _q(str(settings_path))])
        parts.extend(["--model", _q(model)])
        for directory in _outer_leader_add_dirs(control_root, repo_root):
            parts.extend(["--add-dir", _q(str(directory))])
        if _tmux_policy_args_enabled():
            permission_mode = _outer_leader_permission_mode()
            if permission_mode:
                parts.extend(["--permission-mode", _q(permission_mode)])
        if _tmux_tool_args_enabled():
            tools = _outer_leader_tools()
            allowed_tools = _outer_leader_allowed_tools(tools)
            if allowed_tools:
                parts.extend(["--allowedTools", _q(",".join(allowed_tools))])
            disallowed_tools = _outer_leader_disallowed_tools()
            if disallowed_tools:
                parts.extend(["--disallowedTools", _q(",".join(disallowed_tools))])
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
        self._wait_for_paste_visible(session_name, prompt)
        time.sleep(_tmux_submit_delay_seconds(prompt))
        self._run(["tmux", "send-keys", "-t", session_name, "Enter"])

    def _wait_for_paste_visible(self, session_name: str, prompt: str) -> None:
        deadline = time.time() + _tmux_paste_visible_timeout_seconds(prompt)
        needle = _prompt_capture_needle(prompt)
        marker_seen = False
        while time.time() < deadline:
            capture = self._capture(session_name)
            normalized_capture = " ".join(capture.split())
            if needle and needle in normalized_capture:
                return
            if "[Pasted text #" in capture:
                marker_seen = True
                if len(prompt) > 2000:
                    return
            time.sleep(0.1)
        if marker_seen:
            time.sleep(0.2)

    def _wait_for_completion(self, session_name: str, prompt: str, *, request: dict[str, Any], timeout: int | None) -> str:
        deadline = time.time() + timeout if timeout else None
        last = ""
        stable_prompt_text = ""
        stable_prompt_count = 0
        stable_no_assistant_tail = ""
        stable_no_assistant_count = 0
        stable_idle_prompt_tail = ""
        stable_idle_prompt_count = 0
        idle_prompt_submit_retries = 0
        idle_prompt_resubmits = 0
        while deadline is None or time.time() < deadline:
            try:
                last = self._capture(session_name)
            except subprocess.CalledProcessError as exc:
                message = (exc.stderr or exc.stdout or str(exc)).strip()
                raise OuterLeaderTmuxTerminalError(
                    f"Claude TTY session ended before completion: {message}",
                    error_type="OuterLeaderTmuxSessionLost",
                    capture=last,
                ) from exc
            if _looks_complete(last, prompt):
                return last
            prompt_completion = _tmux_prompt_completion_candidate(last, prompt)
            if prompt_completion:
                if prompt_completion == stable_prompt_text:
                    stable_prompt_count += 1
                else:
                    stable_prompt_text = prompt_completion
                    stable_prompt_count = 1
                if stable_prompt_count >= _tmux_stable_completion_polls():
                    return last
            else:
                stable_prompt_text = ""
                stable_prompt_count = 0
            terminal_error = _tmux_terminal_error(last)
            if terminal_error:
                raise OuterLeaderTmuxTerminalError(
                    terminal_error["message"],
                    error_type=terminal_error["type"],
                    capture=last,
                )
            if _tmux_completed_without_assistant(last, prompt):
                if _tmux_waiting_on_bridge_status(last):
                    runtime_bridge = _runtime_bridge_completion_state(self.config, request)
                    if _tmux_bridge_status_should_wait(runtime_bridge):
                        stable_no_assistant_tail = ""
                        stable_no_assistant_count = 0
                        time.sleep(1.0)
                        continue
                no_assistant_tail = _tmux_no_assistant_signature(last)
                if no_assistant_tail == stable_no_assistant_tail:
                    stable_no_assistant_count += 1
                else:
                    stable_no_assistant_tail = no_assistant_tail
                    stable_no_assistant_count = 1
                if stable_no_assistant_count >= _tmux_stable_completion_polls():
                    raise OuterLeaderTmuxTerminalError(
                        "Claude TTY returned to the prompt without assistant text; capture contained only tool or status output.",
                        error_type="OuterLeaderTmuxNoAssistantText",
                        capture=last,
                    )
            else:
                stable_no_assistant_tail = ""
                stable_no_assistant_count = 0
            if _tmux_idle_prompt_after_submit(last, prompt):
                if _tmux_prompt_text_visible(last, prompt) and idle_prompt_submit_retries < _tmux_idle_prompt_submit_retries():
                    self._run(["tmux", "send-keys", "-t", session_name, "Enter"], check=False)
                    idle_prompt_submit_retries += 1
                    stable_idle_prompt_tail = ""
                    stable_idle_prompt_count = 0
                    time.sleep(1.0)
                    continue
                if not _tmux_prompt_text_visible(last, prompt) and idle_prompt_resubmits < _tmux_idle_prompt_resubmit_retries():
                    self._paste_prompt(session_name, prompt)
                    idle_prompt_resubmits += 1
                    stable_idle_prompt_tail = ""
                    stable_idle_prompt_count = 0
                    time.sleep(1.0)
                    continue
                idle_prompt_tail = _tmux_no_assistant_signature(last)
                if idle_prompt_tail == stable_idle_prompt_tail:
                    stable_idle_prompt_count += 1
                else:
                    stable_idle_prompt_tail = idle_prompt_tail
                    stable_idle_prompt_count = 1
                if stable_idle_prompt_count >= _tmux_idle_prompt_polls():
                    raise OuterLeaderTmuxTerminalError(
                        "Claude TTY stayed at an idle prompt after submit; the prompt may not have been accepted by the interactive session.",
                        error_type="OuterLeaderTmuxIdlePromptNoSubmission",
                        capture=last,
                    )
            else:
                stable_idle_prompt_tail = ""
                stable_idle_prompt_count = 0
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
        if _is_tui_tool_or_output_line(line):
            continue
        collected: list[str] = []
        for item in lines[index:]:
            stripped = item.strip()
            if stripped.startswith("✻") or stripped.startswith("❯"):
                break
            if _is_tui_chrome_line(stripped):
                break
            if _is_transient_tui_status(stripped) or _is_tui_tool_or_output_line(stripped):
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


def _tmux_prompt_completion_candidate(capture: str, prompt: str) -> str:
    text = _strip_ansi(capture)
    assistant_text = extract_assistant_text(text, prompt)
    if not assistant_text or _is_transient_tui_status(assistant_text):
        return ""
    if "leader-orchestrator" not in text:
        return ""
    if "esc to interrupt" not in text and "? for shortcuts" not in text:
        return ""
    return assistant_text


def _tmux_stable_completion_polls() -> int:
    raw = os.environ.get("OUTER_LEADER_TMUX_STABLE_COMPLETION_POLLS")
    try:
        value = int(raw) if raw else 3
    except ValueError:
        value = 3
    return max(2, value)


def _tmux_completed_without_assistant(capture: str, prompt: str) -> bool:
    text = _strip_ansi(capture)
    if _tmux_retrying_api_status(text):
        return False
    if not _tmux_prompt_visible(text):
        return False
    if extract_assistant_text(text, prompt):
        return False
    if "Cooked for" in text or "Baked for" in text:
        return True
    return _tmux_status_summary_without_assistant(text)


def _tmux_idle_prompt_after_submit(capture: str, prompt: str) -> bool:
    text = _strip_ansi(capture)
    if _tmux_retrying_api_status(text):
        return False
    if not _tmux_prompt_visible(text) and not _tmux_prompt_text_visible(text, prompt):
        return False
    if extract_assistant_text(text, prompt):
        return False
    if _tmux_waiting_on_bridge_status(text):
        return False
    if _tmux_status_summary_without_assistant(text):
        return False
    if "Cooked for" in text or "Baked for" in text:
        return False
    return not _tmux_active_status_visible(text)


def _tmux_prompt_text_visible(capture: str, prompt: str) -> bool:
    needle = _prompt_capture_needle(prompt)
    if not needle:
        return False
    current_prompt = _tmux_current_prompt_line(capture)
    if not current_prompt:
        return False
    return needle in " ".join(current_prompt.split())


def _tmux_active_status_visible(text: str) -> bool:
    for raw_line in _strip_ansi(text).splitlines():
        line = _clean_line(raw_line).strip()
        if not line:
            continue
        normalized = line.lower().replace("\u2026", "...")
        if _tmux_retrying_api_status(line):
            return True
        if "ctrl+o to expand" in normalized and re.search(r"\b(reading|calling|searching|running|using)\b", normalized):
            return True
        spinner_text = re.sub(r"^[^\w]+", "", normalized).strip()
        if "..." in spinner_text and ("(" in spinner_text or "thinking" in spinner_text or "tokens" in spinner_text):
            return True
    return False


def _tmux_status_summary_without_assistant(text: str) -> bool:
    for raw_line in text.splitlines():
        line = _clean_line(raw_line).strip()
        if not line:
            continue
        normalized = line.lstrip("●").strip().lower()
        if "ctrl+o to expand" not in normalized:
            continue
        if re.search(r"\b(read|reading|call|calling|called|recalled|used|ran|searched|listed|edited|wrote)\b", normalized):
            return True
    return False


def _tmux_retrying_api_status(text: str) -> bool:
    for raw_line in _strip_ansi(text).splitlines():
        line = re.sub(r"\s+", " ", _clean_line(raw_line)).strip().lower()
        if not line:
            continue
        if re.search(r"\bretrying in \d+s\b", line) and re.search(r"\battempt \d+/\d+\b", line):
            return True
    return False


def _tmux_terminal_error(capture: str) -> dict[str, str] | None:
    tail = _strip_ansi(capture)[-8000:]
    lines = tail.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        message = _tmux_terminal_api_error_line(lines[index])
        if not message:
            continue
        after_error = "\n".join(lines[index + 1 :])
        if not _tmux_prompt_visible(after_error):
            continue
        return {"type": "OuterLeaderTmuxTerminalApiError", "message": message}
    return None


def _tmux_terminal_api_error_line(line: str) -> str | None:
    candidate = re.sub(r"\s+", " ", _clean_line(line)).strip()
    if not candidate:
        return None
    if candidate.startswith("⎿"):
        candidate = candidate.lstrip("⎿").strip()
    for prefix in ("API Error:", "Unable to connect to API"):
        if candidate.startswith(prefix):
            return candidate
    if candidate.startswith("Error:") and "500 Internal Server Error" in candidate:
        return candidate
    if candidate.startswith("500 Internal Server Error"):
        return candidate
    return None


def _tmux_prompt_visible(text: str) -> bool:
    if "? for shortcuts" not in text and "esc to interrupt" not in text:
        return False
    current_prompt = _tmux_current_prompt_line(text)
    return bool(re.match(r"^\s*(?:\u276f|>)\s*$", current_prompt or ""))


def _tmux_current_prompt_line(text: str) -> str:
    tail_lines = _strip_ansi(text).splitlines()[-20:]
    for raw_line in reversed(tail_lines):
        line = _clean_line(raw_line).strip()
        if re.match(r"^(?:\u276f|>)\s*", line):
            return line
    return ""


def _is_transient_tui_status(text: str) -> bool:
    normalized = str(text or "").strip().lstrip("●").strip().lower()
    if not normalized:
        return False
    if "ctrl+o to expand" in normalized:
        return True
    if "/effort" in normalized:
        return True
    if "don't ask on" in normalized or "dont ask on" in normalized:
        return True
    if normalized.startswith("calling ") and ("…" in normalized or "..." in normalized):
        return True
    spinner_text = re.sub(r"^[^\w]+", "", normalized.replace("…", "...")).strip()
    if re.match(r"^(frosting|cooking|baking|thinking|reading)\b", spinner_text) and "..." in spinner_text:
        return True
    return False


def _tmux_waiting_on_bridge_status(text: str) -> bool:
    for raw_line in _strip_ansi(text).splitlines():
        line = _clean_line(raw_line).strip().lower()
        if "ctrl+o to expand" in line and "calling bridge" in line:
            return True
    return False


def _tmux_no_assistant_signature(capture: str) -> str:
    lines: list[str] = []
    for raw_line in _tail_capture(capture).splitlines():
        line = _clean_line(raw_line).strip()
        if not line:
            continue
        lowered = line.lower()
        if _is_transient_tui_status(line) or any(word in lowered for word in ["cooking", "forging", "frosting", "sketching", "thinking"]):
            lines.append("<tui-status>")
            continue
        normalized = re.sub(r"\b\d+(?:m|s|h)\b", "<time>", line)
        normalized = re.sub(r"attempt \d+/\d+", "attempt <n>/<n>", normalized, flags=re.IGNORECASE)
        lines.append(normalized)
    return "\n".join(lines)


def _runtime_bridge_completion_state(config: Any, request: dict[str, Any]) -> dict[str, Any]:
    repo_key = str(request.get("repo_key") or "").strip()
    run_id = str(request.get("run_id") or "").strip()
    if not repo_key or not run_id:
        return {"open_bridge_windows": None, "terminal_bridge_result_seen": False, "request_age_seconds": None}
    try:
        control_root = Path(config.control_root).expanduser().resolve()
        run_root = control_root.parent / "runtime_state" / "projects" / repo_key / "runs" / run_id
        snapshot_path = run_root / "runtime_snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
        open_windows = snapshot.get("lifecycle", {}).get("open_bridge_window_ids")
        if not isinstance(open_windows, list):
            open_windows = []
        request_created_at = _parse_iso_timestamp(request.get("created_at"))
        request_age_seconds = None
        if request_created_at is not None:
            request_age_seconds = max(0.0, (datetime.now(timezone.utc) - request_created_at).total_seconds())
        terminal_seen = False
        event_log = run_root / "event_log.jsonl"
        if event_log.exists():
            for line in event_log.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]:
                if not line.strip():
                    continue
                event = json.loads(line)
                event_timestamp = _parse_iso_timestamp(event.get("timestamp"))
                if request_created_at is not None and event_timestamp is not None and event_timestamp < request_created_at:
                    continue
                event_kind = str(event.get("event_kind") or "")
                if event_kind.startswith("bridge_result_returned") or event_kind in {"bridge_call_failed", "bridge_window_failed"}:
                    terminal_seen = True
        return {
            "open_bridge_windows": open_windows,
            "terminal_bridge_result_seen": terminal_seen,
            "request_age_seconds": request_age_seconds,
        }
    except Exception:
        return {"open_bridge_windows": None, "terminal_bridge_result_seen": False, "request_age_seconds": None}


def _tmux_bridge_status_should_wait(state: dict[str, Any]) -> bool:
    if state.get("open_bridge_windows"):
        return True
    if state.get("terminal_bridge_result_seen"):
        return False
    age = state.get("request_age_seconds")
    if age is None:
        return True
    return float(age) < _tmux_bridge_status_grace_seconds()


def _tmux_bridge_status_grace_seconds() -> float:
    raw = os.environ.get("OUTER_LEADER_TMUX_BRIDGE_STATUS_GRACE_SECONDS")
    try:
        value = float(raw) if raw else 120.0
    except ValueError:
        value = 120.0
    return max(10.0, value)


def _tmux_idle_prompt_polls() -> int:
    raw = os.environ.get("OUTER_LEADER_TMUX_IDLE_PROMPT_POLLS")
    try:
        value = int(raw) if raw else 8
    except ValueError:
        value = 8
    return max(2, value)


def _tmux_idle_prompt_submit_retries() -> int:
    raw = os.environ.get("OUTER_LEADER_TMUX_IDLE_PROMPT_SUBMIT_RETRIES")
    try:
        value = int(raw) if raw else 1
    except ValueError:
        value = 1
    return max(0, min(value, 3))


def _tmux_idle_prompt_resubmit_retries() -> int:
    raw = os.environ.get("OUTER_LEADER_TMUX_IDLE_PROMPT_RESUBMIT_RETRIES")
    try:
        value = int(raw) if raw else 1
    except ValueError:
        value = 1
    return max(0, min(value, 2))


def _outer_leader_failure_classification(error_type: str) -> str:
    if error_type == "OuterLeaderTmuxTerminalApiError":
        return "outer_leader_transport_api_failure"
    if error_type == "OuterLeaderTmuxIdlePromptNoSubmission":
        return "outer_leader_tmux_prompt_submission_failure"
    if error_type == "OuterLeaderTmuxSessionLost":
        return "outer_leader_tmux_session_lost"
    return "outer_leader_tmux_runtime_failure"


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_tui_chrome_line(text: str) -> bool:
    stripped = str(text or "").strip()
    if "leader-orchestrator" not in stripped:
        return False
    chrome = stripped.replace("leader-orchestrator", "").strip()
    return bool(chrome) and set(chrome) <= {"─", "-", " "}


def _is_tui_tool_or_output_line(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if _is_tool_artifact_filename(stripped):
        return True
    if stripped.startswith("⎿"):
        return True
    normalized = stripped.lstrip("●").strip()
    if normalized.startswith("⎿"):
        return True
    return bool(_TUI_TOOL_LINE_RE.match(normalized))


def _is_tool_artifact_filename(text: str) -> bool:
    normalized = str(text or "").strip()
    normalized = re.sub(r"^[^\w./-]+", "", normalized)
    return bool(re.fullmatch(r"(?:br)?idge_packet-\d+\.txt", normalized))


def _outer_leader_tmux_contract_violation(config: Any, request: dict[str, Any], assistant_text: str) -> str | None:
    state = _outer_leader_tool_state(config, request)
    if _is_tool_artifact_filename(assistant_text):
        return "Outer leader returned only a tool artifact filename instead of a runtime-backed report."
    if state.get("build_bridge_packet_completed") and not state.get("call_bridge_sdk_started"):
        return "Outer leader built a BridgePacket but stopped before mcp__bridge__call_bridge_sdk."
    return None


def _outer_leader_tool_state(config: Any, request: dict[str, Any]) -> dict[str, Any]:
    run_root = _outer_request_run_root(config, request)
    state = {
        "run_root": str(run_root) if run_root else None,
        "build_bridge_packet_completed": False,
        "call_bridge_sdk_started": False,
        "last_build_tool_use_id": None,
        "last_call_tool_use_id": None,
    }
    if run_root is None:
        return state
    for record in _read_jsonl_safely(run_root / "tool_events.jsonl")[-200:]:
        tool_name = str(record.get("tool_name") or "")
        status = str(record.get("status") or "")
        if tool_name == "mcp__bridge__build_bridge_packet" and status == "completed":
            state["build_bridge_packet_completed"] = True
            state["last_build_tool_use_id"] = record.get("tool_use_id")
        if tool_name == "mcp__bridge__call_bridge_sdk" and status in {"started", "completed", "failed", "denied"}:
            state["call_bridge_sdk_started"] = True
            state["last_call_tool_use_id"] = record.get("tool_use_id")
    return state


def _outer_request_run_root(config: Any, request: dict[str, Any]) -> Path | None:
    repo_key = str(request.get("repo_key") or "").strip()
    run_id = str(request.get("run_id") or "").strip()
    if not repo_key or not run_id:
        return None
    try:
        control_root = Path(config.control_root).expanduser().resolve()
    except Exception:
        return None
    return control_root.parent / "runtime_state" / "projects" / repo_key / "runs" / run_id


def _read_jsonl_safely(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


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


def _blocked_result(
    error_type: str,
    message: str,
    request: dict[str, Any],
    *,
    evidence_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "handled_by": "claude-tmux-repl",
        "reports": [{"summary": message, "source": "outer_leader_tmux_repl"}],
        "artifact_refs": [],
        "evidence": {"repo_key": request.get("repo_key"), "run_id": request.get("run_id"), **(evidence_extra or {})},
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


def _prompt_capture_needle(prompt: str) -> str:
    return " ".join(prompt.split())[:120]


def _tmux_paste_visible_timeout_seconds(prompt: str) -> float:
    return min(8.0, max(0.5, len(prompt) / 8000.0))


def _tmux_submit_delay_seconds(prompt: str) -> float:
    return min(3.0, max(0.2, len(prompt) / 20000.0))


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


def _tmux_policy_args_enabled() -> bool:
    raw = os.environ.get("OUTER_LEADER_TMUX_POLICY_ARGS")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _tmux_tool_args_enabled() -> bool:
    raw = os.environ.get("OUTER_LEADER_TMUX_TOOL_ARGS")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _outer_leader_add_dirs(control_root: Path, repo_root: Path) -> list[Path]:
    add_dirs = [repo_root]
    parent_claude_root = control_root.parent
    if parent_claude_root != repo_root:
        add_dirs.append(parent_claude_root)
    return add_dirs


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _q(value: str) -> str:
    return shlex.quote(str(value))


def _ensure_env_api_key_alias(env: dict[str, str]) -> None:
    if env.get("ANTHROPIC_API_KEY"):
        return
    auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
    if auth_token:
        env["ANTHROPIC_API_KEY"] = auth_token
