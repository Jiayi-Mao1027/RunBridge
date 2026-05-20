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
from bridge_sdk import call_bridge_sdk
from main_leader import decide_next_bridge_packet, read_runtime_snapshot
from repo_runtime import ensure_repo_registered, get_repo_runtime_root, registry_root, resolve_repo_key, update_active_run_registry
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
        auto_bridge_runner: Any | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter or build_outer_leader_adapter(config)
        self._auto_bridge_runner = auto_bridge_runner
        self.started_at = _now_iso()
        self.host_instance_id = f"outer_host_{uuid.uuid4().hex[:12]}"
        repo_key = self._repo_key()
        self.default_run_id = _initial_default_run_id(config, repo_key)
        self._activate_default_run("outer_host_started")

    def handle_user_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        request, runtime_result = self.accept_user_input(payload)
        leader_result = self._leader_result_for_request(request, runtime_result)
        self._write_outer_leader_result(request, runtime_result, leader_result)
        return self._build_user_input_response(request, runtime_result, leader_result)

    def accept_user_input(self, payload: dict[str, Any]):
        request = self._normalize_input(payload)
        return self._accept_normalized_user_input(request)

    def queue_user_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._normalize_input(payload)
        self._write_host_event("user_input_queued", request)
        return request

    def handle_queued_user_input(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            request, runtime_result = self._accept_normalized_user_input(request)
            leader_result = self._leader_result_for_request(request, runtime_result)
            self._write_outer_leader_result(request, runtime_result, leader_result)
            return leader_result
        except Exception as exc:
            leader_result = _outer_leader_exception_result(request, None, exc)
            self._write_host_event("outer_leader_result", {"request": request, "leader_result": leader_result})
            return leader_result

    def _accept_normalized_user_input(self, request: dict[str, Any]):
        self._write_host_event("user_input_received", request)
        runtime_result = self._dispatch_input_event(request)
        request["runtime_event_id"] = runtime_result.event_id
        self._write_sdk_stream_event("outer_user_input", request, runtime_result=runtime_result)
        if runtime_result.ok:
            self._write_outer_host_context(request)
        return request, runtime_result

    def handle_accepted_user_input(self, request: dict[str, Any], runtime_result: Any) -> dict[str, Any]:
        try:
            leader_result = self._leader_result_for_request(request, runtime_result)
        except Exception as exc:
            leader_result = _outer_leader_exception_result(request, runtime_result, exc)
        self._write_outer_leader_result(request, runtime_result, leader_result)
        return leader_result

    def build_user_input_ack(self, request: dict[str, Any], runtime_result: Any) -> dict[str, Any]:
        if runtime_result.ok:
            leader_result = {
                "status": "queued",
                "handled_by": "outer_sdk_host_async",
                "reports": [],
                "artifact_refs": [],
                "evidence": {"runtime_event_id": runtime_result.event_id, "event_kind": runtime_result.event_kind},
                "error_or_null": None,
                "cleanup_required": False,
            }
        else:
            leader_result = _runtime_rejected_leader_result(runtime_result)
        response = self._build_user_input_response(request, runtime_result, leader_result)
        response["async"] = bool(runtime_result.ok)
        return response

    def build_queued_input_ack(self, request: dict[str, Any]) -> dict[str, Any]:
        leader_result = {
            "status": "queued",
            "handled_by": "outer_sdk_host_async",
            "reports": [],
            "artifact_refs": [],
            "evidence": {"input_id": request.get("input_id"), "event_kind": request.get("event_kind")},
            "error_or_null": None,
            "cleanup_required": False,
        }
        return {
            "schema_version": "outer_sdk_host_response.v1",
            "accepted": True,
            "async": True,
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
                "ok": None,
                "queued": True,
                "event_id": None,
                "event_kind": request["event_kind"],
                "input_id": request["input_id"],
                "snapshot_ref": None,
            },
            "leader_result": leader_result,
        }

    def _leader_result_for_request(self, request: dict[str, Any], runtime_result: Any) -> dict[str, Any]:
        if not runtime_result.ok:
            return _runtime_rejected_leader_result(runtime_result)
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
        return self._maybe_auto_bridge_after_outer_leader(request, leader_result)

    def _write_outer_leader_result(self, request: dict[str, Any], runtime_result: Any, leader_result: dict[str, Any]) -> None:
        self._write_host_event("outer_leader_result", {"request": request, "leader_result": leader_result})
        self._write_sdk_stream_event("outer_leader_result", {"request": request, "leader_result": leader_result}, runtime_result=runtime_result)

    def _build_user_input_response(self, request: dict[str, Any], runtime_result: Any, leader_result: dict[str, Any]) -> dict[str, Any]:
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

    def _maybe_auto_bridge_after_outer_leader(self, request: dict[str, Any], leader_result: dict[str, Any]) -> dict[str, Any]:
        leader_decide_violation = self._leader_decide_contract_violation(request, leader_result)
        if leader_decide_violation:
            failed = _leader_decide_contract_failure(request, leader_result, leader_decide_violation)
            self._write_host_event(
                "outer_leader_contract_violation",
                {
                    "request": request,
                    "leader_result": failed,
                    "contract_violation": leader_decide_violation,
                },
            )
            return failed
        error_type = ""
        if isinstance(leader_result.get("error_or_null"), dict):
            error_type = str(leader_result["error_or_null"].get("type") or "")
        if leader_result.get("status") != "succeeded" and not _outer_leader_failure_allows_auto_bridge(error_type):
            return leader_result
        decision = self._auto_bridge_decision(request)
        if not decision.get("should_auto_bridge"):
            return leader_result
        self._write_host_event(
            "outer_host_auto_bridge_started",
            {
                "request": request,
                "leader_result": leader_result,
                "decision": decision,
            },
        )
        try:
            runner = self._auto_bridge_runner or self._run_auto_bridge
            bridge_result = runner(request)
            wrapped = _auto_bridge_leader_result(request, leader_result, bridge_result, decision=decision)
            self._write_host_event(
                "outer_host_auto_bridge_result",
                {
                    "request": request,
                    "leader_result": wrapped,
                    "decision": decision,
                },
            )
            return wrapped
        except Exception as exc:
            failed = {
                "status": "failed",
                "handled_by": "outer_sdk_host",
                "reports": [
                    {
                        "summary": f"Outer host auto-bridge dispatch failed: {exc}",
                        "source": "outer_sdk_host",
                    }
                ],
                "artifact_refs": [],
                "evidence": {
                    "repo_key": request.get("repo_key"),
                    "run_id": request.get("run_id"),
                    "decision": decision,
                    "previous_leader_result": _bound(leader_result),
                },
                "error_or_null": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "cleanup_required": False,
            }
            self._write_host_event(
                "outer_host_auto_bridge_result",
                {
                    "request": request,
                    "leader_result": failed,
                    "decision": decision,
                },
            )
            return failed

    def _leader_decide_contract_violation(self, request: dict[str, Any], leader_result: dict[str, Any]) -> str | None:
        if str(request.get("dispatch_intent") or "").strip() != "leader_decide":
            return None
        if leader_result.get("status") != "succeeded":
            return None
        repo_key = str(request.get("repo_key") or "").strip()
        run_id = str(request.get("run_id") or "").strip()
        if repo_key and run_id:
            run_root = get_repo_runtime_root(self.config.control_root, repo_key) / run_id
            if _bridge_started_after_request(run_root, request):
                return None
        summary_text = _leader_result_summary_text(leader_result)
        if _has_explicit_no_bridge_decision(summary_text):
            return None
        return (
            "leader_decide returned without a bridge call or an explicit NO_BRIDGE_DECISION. "
            "The leader must either call build_bridge_packet/call_bridge_sdk for forward-moving intent "
            "or state a no-bridge semantic decision."
        )

    def _auto_bridge_decision(self, request: dict[str, Any]) -> dict[str, Any]:
        if str(request.get("dispatch_intent") or "").strip() != "advance_or_continue":
            return {"should_auto_bridge": False, "reason": "dispatch_intent_not_advance_or_continue"}
        repo_key = str(request.get("repo_key") or "").strip()
        run_id = str(request.get("run_id") or "").strip()
        if not repo_key or not run_id:
            return {"should_auto_bridge": False, "reason": "missing_repo_or_run"}
        run_root = get_repo_runtime_root(self.config.control_root, repo_key) / run_id
        if _bridge_started_after_request(run_root, request):
            return {"should_auto_bridge": False, "reason": "bridge_already_started_after_request"}
        snapshot = read_runtime_snapshot(
            self.config.control_root,
            run_id,
            repo_key=repo_key,
            runtime_runs_root=run_root.parent,
        )
        allowed_actions = set(snapshot.get("allowed_actions") or [])
        if "call_bridge_sdk" not in allowed_actions:
            return {"should_auto_bridge": False, "reason": "call_bridge_sdk_not_allowed", "allowed_actions": sorted(allowed_actions)}
        integrity = snapshot.get("integrity") if isinstance(snapshot.get("integrity"), dict) else {}
        blocking_keys = [
            "has_hard_stop",
            "awaiting_approval",
            "awaiting_user_answer",
            "has_blocking_orchestration_anomaly",
            "has_execute_watchdog_alert",
        ]
        active_blockers = [key for key in blocking_keys if integrity.get(key)]
        if active_blockers:
            return {"should_auto_bridge": False, "reason": "runtime_integrity_blocker", "active_blockers": active_blockers}
        lifecycle = snapshot.get("lifecycle") if isinstance(snapshot.get("lifecycle"), dict) else {}
        open_windows = lifecycle.get("open_bridge_window_ids")
        if open_windows:
            return {"should_auto_bridge": False, "reason": "open_bridge_window_exists", "open_bridge_window_ids": open_windows}
        return {
            "should_auto_bridge": True,
            "reason": "advance_or_continue_without_bridge_call_after_outer_leader",
            "repo_key": repo_key,
            "run_id": run_id,
            "current_phase": snapshot.get("current_phase"),
        }

    def _run_auto_bridge(self, request: dict[str, Any]) -> dict[str, Any]:
        repo_key = str(request["repo_key"])
        run_id = str(request["run_id"])
        runs_root = get_repo_runtime_root(self.config.control_root, repo_key)
        main_session_id = str(request.get("main_session_id") or run_id)
        self._freeze_semantics_for_auto_bridge_if_needed(request, runs_root, main_session_id=main_session_id)
        packet = decide_next_bridge_packet(
            self.config.control_root,
            run_id,
            repo_key=repo_key,
            runtime_runs_root=runs_root,
            main_session_id=main_session_id,
            user_instruction=str(request.get("text") or ""),
            task_spec=request.get("task_spec") if isinstance(request.get("task_spec"), dict) else {},
            target_phase=str(request.get("target_phase") or "").strip() or None,
        )
        _write_last_bridge_packet(runs_root, run_id, packet)
        return call_bridge_sdk(
            self.config.control_root,
            packet,
            runtime_runs_root=runs_root,
            persist=True,
        )

    def _freeze_semantics_for_auto_bridge_if_needed(
        self,
        request: dict[str, Any],
        runs_root: Path,
        *,
        main_session_id: str,
    ) -> None:
        snapshot = read_runtime_snapshot(
            self.config.control_root,
            str(request["run_id"]),
            repo_key=str(request["repo_key"]),
            runtime_runs_root=runs_root,
        )
        semantic = snapshot.get("semantic") if isinstance(snapshot.get("semantic"), dict) else {}
        if semantic.get("frozen") is not None and not semantic.get("requires_refresh"):
            return
        task_spec = request.get("task_spec") if isinstance(request.get("task_spec"), dict) else {}
        result = dispatch_workflow_event(
            self.config.control_root,
            {
                "run_id": str(request["run_id"]),
                "repo_key": str(request["repo_key"]),
                "main_session_id": main_session_id,
                "agent_id": "outer-sdk-host",
                "agent_type": "main-leader",
                "event_kind": "semantic_frozen",
                "payload": {
                    "repo_key": str(request["repo_key"]),
                    "frozen_semantics": {
                        "user_instruction": str(request.get("text") or ""),
                        "task_subject": task_spec.get("task_subject") or task_spec.get("subject"),
                        "task_kind": task_spec.get("task_kind"),
                        "target_phase": request.get("target_phase"),
                        "freeze_source": "outer_host_auto_bridge",
                    },
                    "reason": "outer host auto-bridge requires current frozen semantics before deterministic bridge dispatch",
                },
            },
            repo_key=str(request["repo_key"]),
            runtime_runs_root=runs_root,
            persist=True,
        )
        if not result.ok:
            raise RuntimeError(f"semantic_frozen rejected by runtime: {result.check_result.get('reasons')}")

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
        explicit_run_id = str(payload.get("run_id") or payload.get("runId") or "").strip()
        run_id = explicit_run_id or self.default_run_id
        if explicit_run_id and explicit_run_id != self.default_run_id:
            self.default_run_id = explicit_run_id
        main_session_id = str(
            payload.get("main_session_id")
            or payload.get("mainSessionId")
            or self.config.default_main_session_id
            or f"outer_{run_id}"
        ).strip()
        input_kind = str(payload.get("input_kind") or payload.get("kind") or "user_prompt").strip()
        event_kind = "user_answer_received" if input_kind in {"user_answer", "clarification_answer"} else "user_prompt_submitted"
        target_phase = payload.get("target_phase") or payload.get("targetPhase")
        dispatch_intent = _dispatch_intent(payload, input_kind=input_kind, target_phase=target_phase)
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


def _runtime_rejected_leader_result(runtime_result: Any) -> dict[str, Any]:
    return {
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


def _outer_leader_exception_result(request: dict[str, Any], runtime_result: Any, exc: Exception) -> dict[str, Any]:
    return {
        "status": "failed",
        "handled_by": "outer_sdk_host_async",
        "reports": [
            {
                "summary": f"Outer leader background execution failed: {exc}",
                "source": "outer_sdk_host",
            }
        ],
        "artifact_refs": [],
        "evidence": {
            "repo_key": request.get("repo_key"),
            "run_id": request.get("run_id"),
            "input_id": request.get("input_id"),
            "runtime_event_id": getattr(runtime_result, "event_id", None),
            "event_kind": getattr(runtime_result, "event_kind", request.get("event_kind")),
        },
        "error_or_null": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
        "cleanup_required": False,
    }


def _write_last_bridge_packet(runs_root: Path, run_id: str, packet: dict[str, Any]) -> None:
    atomic_write_json(runs_root / run_id / ".last_bridge_packet.json", packet)


def _bridge_started_after_request(run_root: Path, request: dict[str, Any]) -> bool:
    request_created_at = _parse_iso_timestamp(request.get("created_at"))
    event_names = {
        "bridge_call_intended",
        "pretooluse_allowed_by_main_leader",
        "call_bridge_sdk_started",
        "bridge_result_returned",
        "bridge_result_returned_with_failure",
        "bridge_result_returned_with_partial",
        "bridge_window_failed",
        "bridge_window_partial_returned",
        "bridge_window_returned",
    }
    tool_names = {"call_bridge_sdk", "mcp__bridge__call_bridge_sdk"}
    for record in _read_jsonl_safely(run_root / "event_log.jsonl")[-250:]:
        if not _record_is_after(record, request_created_at):
            continue
        if str(record.get("event_kind") or "") in event_names:
            return True
        if str(record.get("tool_name") or "") in tool_names:
            return True
    for record in _read_jsonl_safely(run_root / "tool_events.jsonl")[-250:]:
        if not _record_is_after(record, request_created_at):
            continue
        if str(record.get("tool_name") or "") in tool_names:
            return True
    return False


def _record_is_after(record: dict[str, Any], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    timestamp = _parse_iso_timestamp(record.get("timestamp"))
    return timestamp is not None and timestamp >= cutoff


def _parse_iso_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
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


def _auto_bridge_leader_result(
    request: dict[str, Any],
    previous_leader_result: dict[str, Any],
    bridge_result: dict[str, Any],
    *,
    decision: dict[str, Any],
) -> dict[str, Any]:
    bridge_status = str(bridge_result.get("status") or "failed").strip() or "failed"
    bridge_error = bridge_result.get("error_or_null") if isinstance(bridge_result.get("error_or_null"), dict) else None
    bridge_window_id = bridge_result.get("bridge_window_id") or bridge_result.get("binding", {}).get("bridge_window_id")
    report_count = len(bridge_result.get("reports") or []) if isinstance(bridge_result.get("reports"), list) else bridge_result.get("report_count")
    summary = (
        "Outer host auto-dispatched one bridge because an advance/continue request returned from outer leader "
        "without any call_bridge_sdk event after the request. "
        f"BridgeResult status={bridge_status}; bridge_window_id={bridge_window_id or 'unknown'}; report_count={report_count or 0}."
    )
    if bridge_error:
        summary = f"{summary} {bridge_error.get('type') or 'BridgeError'}: {bridge_error.get('message') or ''}".strip()
    return {
        "status": bridge_status,
        "handled_by": "outer_sdk_host_auto_bridge",
        "reports": [{"summary": summary, "source": "outer_sdk_host"}],
        "artifact_refs": bridge_result.get("artifact_refs") if isinstance(bridge_result.get("artifact_refs"), list) else [],
        "evidence": {
            "repo_key": request.get("repo_key"),
            "run_id": request.get("run_id"),
            "decision": decision,
            "previous_leader_result": _bound(previous_leader_result),
            "bridge_result": _bound(bridge_result),
        },
        "error_or_null": bridge_error if bridge_status != "succeeded" else None,
        "cleanup_required": bool(bridge_result.get("cleanup_required")),
    }


def _leader_decide_contract_failure(
    request: dict[str, Any],
    previous_leader_result: dict[str, Any],
    contract_violation: str,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "handled_by": "outer_sdk_host_contract_guard",
        "reports": [{"summary": contract_violation, "source": "outer_sdk_host"}],
        "artifact_refs": previous_leader_result.get("artifact_refs") if isinstance(previous_leader_result.get("artifact_refs"), list) else [],
        "evidence": {
            "repo_key": request.get("repo_key"),
            "run_id": request.get("run_id"),
            "input_id": request.get("input_id"),
            "dispatch_intent": request.get("dispatch_intent"),
            "previous_leader_result": _bound(previous_leader_result),
        },
        "error_or_null": {
            "type": "OuterLeaderContractViolation",
            "message": contract_violation,
        },
        "cleanup_required": bool(previous_leader_result.get("cleanup_required")),
    }


def _leader_result_summary_text(leader_result: dict[str, Any]) -> str:
    parts: list[str] = []
    for report in leader_result.get("reports") or []:
        if isinstance(report, dict) and report.get("summary"):
            parts.append(str(report.get("summary")))
    return "\n".join(parts)


def _has_explicit_no_bridge_decision(text: Any) -> bool:
    return "NO_BRIDGE_DECISION:" in str(text or "")


def _outer_leader_failure_allows_auto_bridge(error_type: str) -> bool:
    return error_type in {
        "OuterLeaderContractViolation",
        "OuterLeaderTmuxTerminalApiError",
        "OuterLeaderTmuxNoAssistantText",
        "OuterLeaderTmuxStartupFailed",
        "OuterLeaderTransportApiFailure",
        "OuterLeaderApiError",
    }


def _latest_run_id(runs_root: Path) -> str | None:
    if not runs_root.exists():
        return None
    dirs = [item for item in runs_root.iterdir() if item.is_dir()]
    if not dirs:
        return None
    return sorted(dirs, key=lambda item: item.stat().st_mtime, reverse=True)[0].name


def _initial_default_run_id(config: OuterSdkHostConfig, repo_key: str) -> str:
    runs_root = get_repo_runtime_root(config.control_root, repo_key)
    active = load_json_file(registry_root(config.control_root) / "active_runs.json", default={})
    repos = active.get("repos") if isinstance(active.get("repos"), dict) else {}
    entry = repos.get(repo_key) if isinstance(repos.get(repo_key), dict) else {}
    latest = str(entry.get("latest_run_id") or "").strip()
    active_ids = [str(item).strip() for item in entry.get("active_run_ids", []) if str(item).strip()]
    candidates: list[str] = []
    for run_id in [latest, *reversed(active_ids)]:
        if run_id and run_id not in candidates:
            candidates.append(run_id)
    for run_id in candidates:
        if _run_has_runtime_truth(runs_root / run_id):
            return run_id
    latest_meaningful = _latest_meaningful_run_id(runs_root)
    return latest_meaningful or _new_run_id()


def _latest_meaningful_run_id(runs_root: Path) -> str | None:
    if not runs_root.exists():
        return None
    dirs = sorted((item for item in runs_root.iterdir() if item.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True)
    for run_dir in dirs:
        if _run_has_runtime_truth(run_dir):
            return run_dir.name
    return None


def _run_has_runtime_truth(run_root: Path) -> bool:
    if not run_root.exists():
        return False
    for name in ("runtime_snapshot.json", "run_ledger.json", "event_log.jsonl"):
        path = run_root / name
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


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


def _dispatch_intent(payload: dict[str, Any], *, input_kind: str, target_phase: Any) -> str:
    explicit = str(payload.get("dispatch_intent") or payload.get("dispatchIntent") or "").strip()
    if explicit in {"advance_or_continue", "inspect_only", "leader_decide", "user_answer"}:
        return explicit
    normalized_input_kind = str(input_kind or "").strip()
    if normalized_input_kind in {"user_answer", "clarification_answer"}:
        return "user_answer"
    if normalized_input_kind in {"inspect", "inspect_only", "status"}:
        return "inspect_only"
    if normalized_input_kind in {"advance", "continue"}:
        return "advance_or_continue"
    if str(target_phase or "").strip() in {"l2_advisory", "l3_bridge", "l4_anomaly", "l4_implement", "l4_execute"}:
        return "advance_or_continue"
    return "leader_decide"


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
