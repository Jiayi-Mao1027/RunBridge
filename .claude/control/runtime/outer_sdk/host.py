from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import uuid
from pathlib import Path
from typing import Any

from loader import load_json_file
from persist import append_jsonl, atomic_write_json
from repo_runtime import ensure_repo_registered, get_repo_runtime_root, resolve_repo_key, update_active_run_registry
from runtime_event_envelope import attach_runtime_event_envelope, normalize_runtime_event
from workflow_runtime import dispatch_workflow_event

from .adapters import OuterLeaderAdapter, build_outer_leader_adapter


HOST_EVENT_SCHEMA_VERSION = "outer_sdk_host_event.v1"
PAYLOAD_TEXT_LIMIT = 2000
REPORT_TEXT_LIMIT = 20000


@dataclass(frozen=True, slots=True)
class OuterSdkHostConfig:
    control_root: Path
    repo_root: Path | None = None
    repo_key: str | None = None
    default_main_session_id: str | None = None

    @classmethod
    def from_values(
        cls,
        *,
        control_root: str | Path,
        repo_root: str | Path | None = None,
        repo_key: str | None = None,
        default_main_session_id: str | None = None,
    ) -> "OuterSdkHostConfig":
        return cls(
            control_root=Path(control_root).expanduser().resolve(),
            repo_root=Path(repo_root).expanduser().resolve() if repo_root else None,
            repo_key=str(repo_key) if repo_key else None,
            default_main_session_id=default_main_session_id,
        )


class OuterSdkHost:
    """Long-lived outer leader host boundary.

    The host is intentionally process-owned, not UI-owned. It records user
    inputs and runtime facts, then delegates reasoning to an outer leader SDK
    adapter when one is configured.
    """

    def __init__(
        self,
        config: OuterSdkHostConfig,
        *,
        adapter: OuterLeaderAdapter | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter or build_outer_leader_adapter(config)
        self.started_at = _now_iso()
        self.host_instance_id = f"outer_host_{uuid.uuid4().hex[:12]}"
        self.default_run_id = _new_run_id()
        self._activate_default_run("outer_host_started")

    def handle_user_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._normalize_input(payload)
        self._write_host_event("user_input_received", request)
        runtime_result = self._dispatch_input_event(request)
        request["runtime_event_id"] = runtime_result.event_id
        self._write_sdk_stream_event("outer_user_input", request, runtime_result=runtime_result)
        if runtime_result.ok:
            self._write_outer_host_context(request)
            leader_result = self.adapter.handle_user_input(
                dict(request),
                event_sink=lambda event_type, payload, status="streaming", sequence=None: self.emit_sdk_observed_event(
                    request,
                    event_type,
                    payload,
                    status=status,
                    sequence=sequence,
                ),
            )
        else:
            leader_result = {
                "status": "blocked",
                "handled_by": "outer_sdk_host",
                "reports": [],
                "artifact_refs": [],
                "evidence": {"runtime_event_id": runtime_result.event_id, "event_kind": runtime_result.event_kind},
                "error_or_null": {
                    "type": "OuterHostRuntimeEventRejected",
                    "message": "Runtime rejected the outer user input event; leader SDK execution was not started.",
                },
                "cleanup_required": False,
            }
        self._write_host_event("outer_leader_result", {"request": request, "leader_result": leader_result})
        self._write_sdk_stream_event("outer_leader_result", {"request": request, "leader_result": leader_result}, runtime_result=runtime_result)
        return {
            "schema_version": "outer_sdk_host_response.v1",
            "accepted": bool(runtime_result.ok),
            "host": {
                "mode": "outer_sdk_host",
                "adapter": self.adapter.name,
                "run_id": request["run_id"],
                "default_run_id": self.default_run_id,
                "host_instance_id": self.host_instance_id,
                "repo_key": request["repo_key"],
                "main_session_id": request["main_session_id"],
                "input_kind": request["input_kind"],
            },
            "runtime": {
                "ok": runtime_result.ok,
                "event_id": runtime_result.event_id,
                "event_kind": runtime_result.event_kind,
                "check_result": runtime_result.check_result,
                "update_result": runtime_result.update_result,
                "snapshot_ref": runtime_result.written_paths.get("runtime_snapshot"),
            },
            "leader_result": leader_result,
        }

    def status(self, *, repo_key: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        resolved_repo_key = repo_key or self._repo_key()
        runs_root = get_repo_runtime_root(self.config.control_root, resolved_repo_key)
        run = run_id or self.default_run_id or _latest_run_id(runs_root)
        snapshot = load_json_file(runs_root / run / "runtime_snapshot.json", default={}) if run else {}
        startup_diagnostics = _startup_diagnostics(self.config.control_root, self.config.repo_root)
        return {
            "schema_version": "outer_sdk_host_status.v1",
            "mode": "outer_sdk_host",
            "adapter": self.adapter.name,
            "repo_key": resolved_repo_key,
            "run_id": run,
            "default_run_id": self.default_run_id,
            "host_instance_id": self.host_instance_id,
            "started_at": self.started_at,
            "runtime_runs_root": str(runs_root),
            "startup_diagnostics": startup_diagnostics,
            "snapshot": snapshot if isinstance(snapshot, dict) else {},
        }

    def _normalize_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("outer host input must be a JSON object")
        text = str(payload.get("text") or payload.get("message") or "").strip()
        if not text:
            raise ValueError("outer host input requires text")
        repo_key = str(payload.get("repo_key") or payload.get("repoKey") or self._repo_key()).strip()
        if _truthy(payload.get("start_new_run") or payload.get("startNewRun")):
            self.default_run_id = _new_run_id()
            self._activate_default_run("outer_host_default_run_rotated")
        run_id = str(payload.get("run_id") or payload.get("runId") or "").strip() or self.default_run_id
        main_session_id = str(
            payload.get("main_session_id")
            or payload.get("mainSessionId")
            or self.config.default_main_session_id
            or f"outer_{run_id}"
        ).strip()
        input_kind = str(payload.get("input_kind") or payload.get("kind") or "user_prompt").strip()
        event_kind = "user_answer_received" if input_kind in {"user_answer", "clarification_answer"} else "user_prompt_submitted"
        target_phase = payload.get("target_phase") or payload.get("targetPhase")
        dispatch_intent = _dispatch_intent(payload, text=text, input_kind=input_kind, target_phase=target_phase)
        now = _now_iso()
        return {
            "schema_version": "outer_user_input.v1",
            "input_id": str(payload.get("input_id") or payload.get("inputId") or f"in_{uuid.uuid4().hex[:16]}"),
            "repo_key": repo_key,
            "run_id": run_id,
            "main_session_id": main_session_id,
            "input_kind": input_kind,
            "event_kind": event_kind,
            "text": text,
            "safe_preview": _safe_preview(text),
            "target_phase": target_phase,
            "dispatch_intent": dispatch_intent,
            "task_spec": _payload_dict(payload, "task_spec", "taskSpec"),
            "created_at": now,
            "source": str(payload.get("source") or "outer_sdk_host_api"),
        }

    def _dispatch_input_event(self, request: dict[str, Any]):
        repo_key = request["repo_key"]
        run_id = request["run_id"]
        runs_root = get_repo_runtime_root(self.config.control_root, repo_key)
        update_active_run_registry(
            self.config.control_root,
            repo_key=repo_key,
            repo_root=str(self.config.repo_root) if self.config.repo_root else None,
            run_id=run_id,
            status="running",
        )
        payload_key = "answer" if request["event_kind"] == "user_answer_received" else "user_instruction"
        return dispatch_workflow_event(
            self.config.control_root,
            {
                "run_id": run_id,
                "repo_key": repo_key,
                "main_session_id": request["main_session_id"],
                "agent_id": "outer-sdk-host",
                "agent_type": "main-leader",
                "event_kind": request["event_kind"],
                "payload": {
                    "repo_key": repo_key,
                    payload_key: request["text"],
                    "input_id": request["input_id"],
                    "input_kind": request["input_kind"],
                    "target_phase": request.get("target_phase"),
                    "dispatch_intent": request.get("dispatch_intent"),
                    "task_spec": request.get("task_spec") or {},
                    "source": request["source"],
                },
            },
            repo_key=repo_key,
            runtime_runs_root=runs_root,
            persist=True,
        )

    def _activate_default_run(self, event_kind: str) -> None:
        repo_key = self._repo_key()
        update_active_run_registry(
            self.config.control_root,
            repo_key=repo_key,
            repo_root=str(self.config.repo_root) if self.config.repo_root else None,
            run_id=self.default_run_id,
            status="running",
        )
        self._write_host_event(
            event_kind,
            {
                "run_id": self.default_run_id,
                "repo_key": repo_key,
                "host_instance_id": self.host_instance_id,
                "default_run_id": self.default_run_id,
                "main_session_id": self.config.default_main_session_id,
                "started_at": self.started_at,
                "safe_preview": f"{event_kind}:{self.default_run_id}",
            },
        )

    def _write_host_event(self, event_kind: str, payload: dict[str, Any]) -> None:
        repo_key = str(payload.get("repo_key") or payload.get("request", {}).get("repo_key") or self._repo_key())
        run_id = str(payload.get("run_id") or payload.get("request", {}).get("run_id") or "unbound")
        run_root = get_repo_runtime_root(self.config.control_root, repo_key) / run_id
        event_path = run_root / "outer_host_events.jsonl"
        record = {
            "schema_version": HOST_EVENT_SCHEMA_VERSION,
            "timestamp": _now_iso(),
            "event_kind": event_kind,
            "source": "outer_sdk_host",
            "authority": "source",
            "run_id": payload.get("run_id") or payload.get("request", {}).get("run_id"),
            "repo_key": payload.get("repo_key") or payload.get("request", {}).get("repo_key"),
            "payload": _bounded_payload(payload),
        }
        record["runtime_event"] = normalize_runtime_event(record, source="outer_sdk", authority="source", safe_preview=event_kind)
        append_jsonl(event_path, record)

    def emit_sdk_observed_event(
        self,
        request: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        status: str = "streaming",
        sequence: int | None = None,
    ) -> None:
        self._write_sdk_stream_event(
            event_type,
            {"request": request, "payload": payload},
            status=status,
            sequence=sequence,
            authority="observed",
            payload_ref=f"sdk_stream_events.jsonl:{event_type}",
        )

    def _write_sdk_stream_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        runtime_result: Any | None = None,
        status: str | None = None,
        sequence: int | None = None,
        authority: str = "source",
        payload_ref: str | None = None,
    ) -> None:
        request = payload.get("request") if isinstance(payload.get("request"), dict) else payload
        event_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        repo_key = str(request.get("repo_key") or self._repo_key())
        record = {
            "timestamp": _now_iso(),
            "event_type": event_type,
            "source": "outer_sdk_host",
            "stream_source": "sdk",
            "run_id": request.get("run_id"),
            "repo_key": request.get("repo_key"),
            "session_id": request.get("main_session_id"),
            "agent_id": event_payload.get("agent_id") or "leader-orchestrator",
            "agent_type": "main-leader",
            "status": status or ("recorded" if getattr(runtime_result, "ok", False) else "blocked"),
            "message_preview": event_payload.get("message_preview") or _safe_preview(event_payload),
            "text_delta": event_payload.get("text_delta"),
            "input_json_delta": event_payload.get("input_json_delta"),
            "payload_keys": event_payload.get("payload_keys") or _payload_keys(event_payload),
            "payload_ref": payload_ref or f"outer_host_events.jsonl:{event_type}",
            "sequence": sequence,
        }
        for key in (
            "raw_stream_event_type",
            "sdk_message_type",
            "tool_id",
            "tool_name",
            "tool_block_type",
            "tool_input_keys",
            "tool_result_id",
            "is_error",
            "subtype",
            "result",
            "total_cost_usd",
            "duration_ms",
            "num_turns",
            "stop_reason",
            "permission_denials",
            "errors",
            "settings_diagnostics",
            "outer_leader_options",
            "timeout_seconds",
            "error_type",
        ):
            if key in event_payload:
                record[key] = event_payload.get(key)
        event_record = attach_runtime_event_envelope(
            record,
            source="outer_sdk",
            authority=authority,
            event_kind=event_type,
            seq=sequence,
            payload_ref=record["payload_ref"],
        )
        for stream_path in self._sdk_stream_event_paths(repo_key, str(request.get("run_id") or "unbound")):
            append_jsonl(stream_path, event_record)

    def _repo_key(self) -> str:
        if self.config.repo_key:
            return self.config.repo_key
        if self.config.repo_root:
            return ensure_repo_registered(self.config.control_root, self.config.repo_root).repo_key
        return resolve_repo_key(Path.cwd())

    def _run_root(self, run_id: str) -> Path:
        return get_repo_runtime_root(self.config.control_root, self._repo_key()) / run_id

    def _write_outer_host_context(self, request: dict[str, Any]) -> None:
        repo_key = str(request.get("repo_key") or self._repo_key())
        project_root = get_repo_runtime_root(self.config.control_root, repo_key).parent
        atomic_write_json(
            project_root / ".outer_host_context.json",
            {
                "schema_version": "outer_host_context.v1",
                "written_at": _now_iso(),
                "host_instance_id": self.host_instance_id,
                "repo_key": repo_key,
                "repo_root": str(self.config.repo_root) if self.config.repo_root else "",
                "run_id": request.get("run_id"),
                "main_session_id": request.get("main_session_id"),
                "input_id": request.get("input_id"),
                "input_kind": request.get("input_kind"),
                "event_kind": request.get("event_kind"),
                "user_instruction": request.get("text"),
                "target_phase": request.get("target_phase"),
                "dispatch_intent": request.get("dispatch_intent"),
                "task_spec": request.get("task_spec") if isinstance(request.get("task_spec"), dict) else {},
                "runtime_event_id": request.get("runtime_event_id"),
                "source": "outer_sdk_host",
            },
        )

    def _sdk_stream_event_paths(self, repo_key: str, run_id: str) -> list[Path]:
        return [
            get_repo_runtime_root(self.config.control_root, repo_key) / run_id / "sdk_stream_events.jsonl",
            self.config.control_root.parent / "runtime_state" / "session_observer" / "sdk_stream_events.jsonl",
        ]


def _latest_run_id(runs_root: Path) -> str | None:
    if not runs_root.exists():
        return None
    dirs = [item for item in runs_root.iterdir() if item.is_dir()]
    if not dirs:
        return None
    return sorted(dirs, key=lambda item: item.stat().st_mtime, reverse=True)[0].name


def _startup_diagnostics(control_root: Path, repo_root: Path | None) -> dict[str, Any]:
    try:
        from .claude_agent_adapter import outer_leader_startup_diagnostics

        return outer_leader_startup_diagnostics(control_root, repo_root)
    except Exception as exc:
        return {"schema_version": "outer_leader_startup_diagnostics.v1", "error": type(exc).__name__, "message": str(exc)}


def _new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"


def _safe_preview(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(str(text).split())
    return text[:700]


def _bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _bound(payload, path=())


def _payload_keys(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return sorted(str(key) for key in payload.keys())[:20]


def _bound(value: Any, depth: int = 0, path: tuple[str, ...] = ()) -> Any:
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, str):
        return value[:_payload_text_limit(path)]
    if isinstance(value, dict):
        return {str(key)[:200]: _bound(item, depth + 1, (*path, str(key))) for key, item in list(value.items())[:80]}
    if isinstance(value, list):
        return [_bound(item, depth + 1, (*path, str(index))) for index, item in enumerate(value[:80])]
    return value


def _payload_text_limit(path: tuple[str, ...]) -> int:
    if _is_report_summary_path(path):
        return _env_int("BRIDGE_OUTER_SDK_REPORT_TEXT_LIMIT", REPORT_TEXT_LIMIT, minimum=PAYLOAD_TEXT_LIMIT, maximum=100000)
    return PAYLOAD_TEXT_LIMIT


def _is_report_summary_path(path: tuple[str, ...]) -> bool:
    if len(path) < 4 or path[-1] != "summary":
        return False
    return path[-3] == "reports" and path[-4] in {"leader_result", "leaderResult"}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "new"}


def _dispatch_intent(payload: dict[str, Any], *, text: str, input_kind: str, target_phase: Any) -> str:
    explicit = str(payload.get("dispatch_intent") or payload.get("dispatchIntent") or "").strip()
    if explicit in {"advance_or_continue", "inspect_only"}:
        return explicit
    if input_kind not in {"user_prompt", "advance", "continue"}:
        return "inspect_only"
    if str(target_phase or "").strip() in {"l2_advisory", "l3_bridge", "l4_execute"}:
        return "advance_or_continue"
    lowered = str(text or "").strip().lower()
    advance_markers = (
        "推进",
        "继续",
        "开始",
        "重新开始",
        "跑项目",
        "执行项目",
        "advance",
        "continue",
        "proceed",
        "run the project",
        "execute the project",
    )
    if any(marker in lowered for marker in advance_markers):
        return "advance_or_continue"
    return "inspect_only"


def _payload_dict(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = default
    else:
        value = default
    return max(minimum, min(value, maximum))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
