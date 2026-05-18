from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable
import uuid

from artifact_refs import normalize_artifact_refs
from bridge.executors import BridgeExecutionRequest, bridge_executor_from_env
from completion_validator import completion_succeeded, validate_bridge_completion
from dispatch_contract import validate_dispatch_contract
from workflow_runtime import dispatch_workflow_event


TeamExecutor = Callable[[dict[str, Any]], dict[str, Any]]


class BridgeExecutionError(RuntimeError):
    def __init__(self, failure_stage: str, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.failure_stage = failure_stage
        self.payload = payload or {}


def execute_bridge_window(
    control_root: str | Path,
    packet: dict[str, Any],
    *,
    runtime_runs_root: str | Path | None = None,
    team_executor: Any | None = None,
    persist: bool = True,
    bridge_leader_id: str = "bridge-leader",
) -> dict[str, Any]:
    runner = BridgeLeaderRuntime(
        control_root=control_root,
        runtime_runs_root=runtime_runs_root,
        packet=packet,
        team_executor=team_executor if team_executor is not None else bridge_executor_from_env(),
        persist=persist,
        bridge_leader_id=bridge_leader_id,
    )
    return runner.run()


class BridgeLeaderRuntime:
    def __init__(
        self,
        *,
        control_root: str | Path,
        runtime_runs_root: str | Path | None,
        packet: dict[str, Any],
        team_executor: Any,
        persist: bool,
        bridge_leader_id: str,
    ) -> None:
        self.control_root = control_root
        self.runtime_runs_root = runtime_runs_root
        self.packet = deepcopy(packet)
        self.team_executor = team_executor
        self.persist = persist
        self.bridge_leader_id = bridge_leader_id
        self.binding = self.packet.get("binding", {})
        self.run_id = str(self.binding.get("run_id") or "")
        self.main_session_id = str(self.binding.get("main_session_id") or self.run_id)
        self.sub_session_id = str(self.binding.get("sub_session_id") or "")
        self.bridge_window_id = str(self.binding.get("bridge_window_id") or "")
        self.team_id = str(self.binding.get("team_id_or_null") or self.packet.get("team_spec", {}).get("team_id_or_null") or "")
        self.task_id = str(self.binding.get("task_id_or_null") or self.packet.get("task_spec", {}).get("task_id_or_null") or "")
        self.event_ids: list[str] = []

    def run(self) -> dict[str, Any]:
        try:
            self._accept_packet()
            self._team_create()
            self._task_create()
            self._send_messages()
            try:
                execution = self._run_team()
            except Exception as exc:
                execution = {
                    "status": "failed",
                    "reports": [],
                    "artifact_refs": [],
                    "evidence": {"error_type": exc.__class__.__name__},
                    "error_or_null": {"message": str(exc), "type": exc.__class__.__name__},
                    "cleanup_required": False,
                    "wait_reason": "team_executor_exception",
                }
                self._fail_task(execution, event_kind="team_executor_failed")
                self._team_delete()
                bridge_result = self._bridge_result("failed", "team_wait", execution)
                self._return_bridge_result("bridge_result_returned_with_failure", bridge_result)
                return bridge_result
            if execution.get("waiting"):
                self._team_waiting(execution)
            if execution.get("status") in {"partial", "partial_or_failed"}:
                if self._l4_execute_owned_process_still_running(execution):
                    execution = deepcopy(execution)
                    execution["error_or_null"] = {
                        "message": "l4_execute team returned partial while an owned process was still running",
                        "type": "L4ExecutePrematurePartialReturn",
                    }
                    evidence = execution.get("evidence") if isinstance(execution.get("evidence"), dict) else {}
                    execution["evidence"] = {
                        **evidence,
                        "protocol_violation": "l4_execute_partial_returned_before_owned_process_terminal",
                        "owned_process_refs": execution.get("owned_process_refs", []),
                    }
                    self._fail_task(execution, event_kind="bridge_leader_fails_task")
                    self._team_delete()
                    bridge_result = self._bridge_result("failed", "team_wait", execution)
                    self._return_bridge_result("bridge_result_returned_with_failure", bridge_result)
                    return bridge_result
                if execution.get("waiting"):
                    self._wait_timeout(execution)
                self._event("partial_evidence_collected", team_id=self.team_id, task_id=self.task_id, payload={"evidence": execution.get("evidence"), "reports": execution.get("reports", []), "artifact_refs": execution.get("artifact_refs", [])})
                self._team_delete()
                bridge_result = self._bridge_result("partial_or_failed", "team_wait", execution)
                self._return_bridge_result("bridge_result_returned_with_partial", bridge_result, agent_type="main-leader")
                return bridge_result
            if execution.get("status") == "failed":
                if execution.get("waiting"):
                    self._team_waiting(execution)
                    self._wait_timeout(execution)
                    self._fail_task(execution, event_kind="task_failed_by_bridge_leader")
                else:
                    self._fail_task(execution, event_kind="team_executor_failed")
                self._team_delete()
                bridge_result = self._bridge_result("failed", "task_complete", execution)
                self._return_bridge_result("bridge_result_returned_with_failure", bridge_result)
                return bridge_result

            if not self._complete_task(execution):
                self._reject_completion(execution)
                self._fail_task(execution, event_kind="bridge_leader_fails_task")
                self._team_delete()
                bridge_result = self._bridge_result("failed", "task_complete", execution)
                self._return_bridge_result("bridge_result_returned_with_failure", bridge_result)
                return bridge_result
            self._team_delete()
            bridge_result = self._bridge_result("succeeded", None, execution)
            self._return_bridge_result("bridge_result_returned", bridge_result)
            return bridge_result
        except BridgeExecutionError as exc:
            return self._fail_window(exc)
        except KeyboardInterrupt as exc:
            return self._interrupt_window(exc)
        except Exception as exc:
            return self._fail_window(
                BridgeExecutionError(
                    "bridge_return",
                    str(exc),
                    payload={"type": exc.__class__.__name__},
                )
            )

    def _accept_packet(self) -> None:
        if not self.run_id or not self.main_session_id or not self.sub_session_id or not self.bridge_window_id:
            raise BridgeExecutionError("packet_accept", "packet binding is incomplete")
        if not self.team_id or not self.task_id:
            self._event("bridge_packet_rejected", payload={"packet": self.packet, "reasons": ["packet_missing_concrete_team_or_task_id"]})
            raise BridgeExecutionError("packet_accept", "packet rejected", payload={"reasons": ["packet_missing_concrete_team_or_task_id"]})
        if not isinstance(self.packet.get("team_spec"), dict) or not isinstance(self.packet.get("task_spec"), dict):
            self._event("bridge_packet_rejected", payload={"packet": self.packet, "reasons": ["packet_spec_missing"]})
            raise BridgeExecutionError("packet_accept", "packet rejected", payload={"reasons": ["packet_spec_missing"]})
        contract_reasons = validate_dispatch_contract(self.packet, self.packet.get("dispatch_contract"))
        if contract_reasons:
            self._event("bridge_packet_rejected", payload={"packet": self.packet, "reasons": contract_reasons})
            raise BridgeExecutionError("packet_accept", "packet rejected", payload={"reasons": contract_reasons})
        self._event("bridge_window_opened", payload={"packet": self.packet})
        self._event("bridge_packet_accepted", payload={"packet": self.packet})

    def _team_create(self) -> None:
        team_spec = self.packet["team_spec"]
        teammate_ids = _teammate_ids(team_spec)
        payload = {"packet": self.packet, "team_name": team_spec.get("team_name") or self.team_id, "teammate_ids": teammate_ids}
        self._event("team_create_started", team_id=self.team_id, tool_name="team_create", payload={"packet": self.packet})
        self._event("team_create_succeeded", team_id=self.team_id, tool_name="team_create", payload=payload)

    def _task_create(self) -> None:
        task_spec = self.packet["task_spec"]
        hook_payload = {
            "task_id": self.task_id,
            "task_subject": task_spec.get("task_subject"),
            "task_description": task_spec.get("task_description"),
            "task_spec": task_spec,
            "team_spec": self.packet["team_spec"],
            "task_team_mapping": self.packet["task_team_mapping"],
            "teammate_ids": _teammate_ids(self.packet["team_spec"]),
        }
        self._event("task_create_started", team_id=self.team_id, task_id=self.task_id, tool_name="task_create", payload={"packet": self.packet})
        self._event("task_create_succeeded", team_id=self.team_id, task_id=self.task_id, tool_name="task_create", payload={"packet": self.packet})
        self._event(
            "taskcreated_hook_accepted",
            team_id=self.team_id,
            task_id=self.task_id,
            agent_id="hook.task_created",
            agent_type="hook",
            payload=hook_payload,
        )

    def _send_messages(self) -> None:
        payload = {
            "packet": self.packet,
            "messages": [
                {
                    "teammate_id_or_null": assignment.get("teammate_id_or_null"),
                    "body": assignment.get("assignment"),
                }
                for assignment in self.packet.get("task_team_mapping", {}).get("teammate_assignments", [])
            ],
        }
        self._event("message_dispatch_started", team_id=self.team_id, task_id=self.task_id, tool_name="send_messages", payload=payload)
        self._event("message_dispatch_succeeded", team_id=self.team_id, task_id=self.task_id, tool_name="send_messages", payload=payload)

    def _run_team(self) -> dict[str, Any]:
        execution_input = {
            "packet": deepcopy(self.packet),
            "run_id": self.run_id,
            "main_session_id": self.main_session_id,
            "sub_session_id": self.sub_session_id,
            "bridge_window_id": self.bridge_window_id,
            "team_id": self.team_id,
            "task_id": self.task_id,
        }
        executor = self.team_executor
        if hasattr(executor, "execute"):
            result = executor.execute(BridgeExecutionRequest.from_execution_input(execution_input), event_sink=self._executor_event)
        else:
            result = executor(execution_input)
        if not isinstance(result, dict):
            raise BridgeExecutionError("task_complete", "team executor must return a dict")
        status = result.get("status")
        if status not in {"succeeded", "failed", "partial", "partial_or_failed"}:
            raise BridgeExecutionError("task_complete", "team executor result must include a valid status")
        reports = result.get("reports")
        if not isinstance(reports, list):
            raise BridgeExecutionError("task_complete", "team executor result must include reports list")
        if status in {"succeeded", "partial", "partial_or_failed"} and not reports:
            raise BridgeExecutionError("task_complete", "team executor result requires reports for non-failed status")
        if not isinstance(result.get("artifact_refs"), list):
            result["artifact_refs"] = []
        result["artifact_refs"] = normalize_artifact_refs(
            result.get("artifact_refs", []),
            context=self._artifact_context(),
            base_dir=_project_root(),
        )
        return result

    def _team_waiting(self, execution: dict[str, Any]) -> None:
        timeout_policy = self.packet.get("completion_contract", {}).get("timeout_policy") or {}
        self._event(
            "team_idle_waiting",
            team_id=self.team_id,
            task_id=self.task_id,
            agent_id="hook.team_idle",
            agent_type="hook",
            payload={
                "wait_reason": execution.get("wait_reason", "team_executor_waiting"),
                "owned_process_refs": execution.get("owned_process_refs", []),
                "last_heartbeat_at": _now_iso(),
                "timeout_policy": timeout_policy,
                "artifact_probe": execution.get("artifact_probe", {}),
                "partial_reports": execution.get("reports", []),
                "partial_artifact_refs": execution.get("artifact_refs", []),
            },
        )

    def _wait_timeout(self, execution: dict[str, Any]) -> None:
        self._event(
            "wait_timeout_or_process_lost",
            team_id=self.team_id,
            task_id=self.task_id,
            agent_id="hook.team_idle",
            agent_type="hook",
            payload={
                "wait_reason": execution.get("wait_reason", "partial_result"),
                "owned_process_refs": execution.get("owned_process_refs", []),
                "last_heartbeat_at": _now_iso(),
                "timeout_policy": self.packet.get("completion_contract", {}).get("timeout_policy") or {},
                "artifact_probe": execution.get("artifact_probe", {}),
                "partial_reports": execution.get("reports", []),
                "partial_artifact_refs": execution.get("artifact_refs", []),
                "evidence": execution.get("evidence"),
            },
        )

    def _complete_task(self, execution: dict[str, Any]) -> bool:
        checks = self._completion_validation(execution)
        execution["_completion_checks"] = checks
        self._event("artifacts_ready", team_id=self.team_id, task_id=self.task_id, tool_name="task_complete", payload={"artifact_refs": execution.get("artifact_refs", [])})
        if not completion_succeeded(checks):
            return False
        self._event(
            "completion_contract_satisfied",
            team_id=self.team_id,
            task_id=self.task_id,
            agent_id="hook.task_completed",
            agent_type="hook",
            payload={
                "completion_contract": self.packet.get("completion_contract", {}),
                "completion_evidence": execution.get("evidence") or {"event_ids": list(self.event_ids)},
                "reports": execution.get("reports", []),
                "artifact_refs": execution.get("artifact_refs", []),
                "completion_checks": checks,
            },
        )
        return True

    def _reject_completion(self, execution: dict[str, Any]) -> dict[str, Any]:
        checks = execution.get("_completion_checks") if isinstance(execution.get("_completion_checks"), dict) else self._completion_validation(execution)
        missing = list(checks.get("missing_outputs", [])) + list(checks.get("missing_artifacts", [])) + list(checks.get("failed_validations", []))
        error = {
            "type": "CompletionContractRejected",
            "message": _completion_rejection_message(checks, missing),
        }
        evidence = execution.get("evidence") if isinstance(execution.get("evidence"), dict) else {}
        evidence = {
            **evidence,
            "completion_checks": checks,
            "missing_contract_items": missing,
        }
        execution["evidence"] = evidence
        execution["error_or_null"] = error
        self._event(
            "completion_contract_rejected",
            team_id=self.team_id,
            task_id=self.task_id,
            agent_id="hook.task_completed",
            agent_type="hook",
            payload={
                "completion_contract": self.packet.get("completion_contract", {}),
                "completion_evidence": evidence,
                "reports": execution.get("reports", []),
                "artifact_refs": execution.get("artifact_refs", []),
                "completion_checks": checks,
                "missing_contract_items": missing,
                "error_or_null": error,
            },
        )
        return checks

    def _completion_validation(self, execution: dict[str, Any]) -> dict[str, Any]:
        validation = validate_bridge_completion(
            self.packet,
            execution,
            context=self._artifact_context(),
            control_root=self.control_root,
            base_dir=_project_root(),
        )
        normalized_refs = validation.get("artifact_refs_normalized")
        if isinstance(normalized_refs, list):
            execution["artifact_refs"] = normalized_refs
        return validation

    def _artifact_context(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "main_session_id": self.main_session_id,
            "sub_session_id": self.sub_session_id,
            "bridge_window_id": self.bridge_window_id,
            "team_id": self.team_id,
            "task_id": self.task_id,
            "agent_id": self.bridge_leader_id,
            "event_id": self.event_ids[-1] if self.event_ids else None,
            "timestamp": _now_iso(),
        }

    def _executor_event(
        self,
        event_kind: str,
        *,
        payload: dict[str, Any] | None = None,
        agent_id: str | None = None,
        agent_type: str = "bridge-leader",
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        return self._event(
            event_kind,
            team_id=self.team_id,
            task_id=self.task_id,
            agent_id=agent_id,
            agent_type=agent_type,
            tool_name=tool_name,
            payload=payload,
        )

    def _fail_task(self, execution: dict[str, Any], *, event_kind: str) -> None:
        self._event(
            event_kind,
            team_id=self.team_id,
            task_id=self.task_id,
            payload={"evidence": execution.get("evidence"), "error_or_null": execution.get("error_or_null")},
        )

    def _team_delete(self) -> None:
        self._event("team_delete_started", team_id=self.team_id, task_id=self.task_id, tool_name="team_delete")
        self._event("team_delete_succeeded", team_id=self.team_id, task_id=self.task_id, tool_name="team_delete")

    def _return_bridge_result(self, event_kind: str, bridge_result: dict[str, Any], *, agent_type: str = "main-leader") -> None:
        bind_window_objects = bridge_result.get("failure_stage_or_null") != "packet_accept"
        self._event(
            event_kind,
            team_id=self.team_id if bind_window_objects else None,
            task_id=self.task_id if bind_window_objects else None,
            agent_id="main-leader" if agent_type == "main-leader" else self.bridge_leader_id,
            agent_type=agent_type,
            tool_name="call_bridge_sdk" if agent_type == "main-leader" else None,
            payload={"bridge_result": bridge_result},
        )

    def _l4_execute_owned_process_still_running(self, execution: dict[str, Any]) -> bool:
        if str(self.packet.get("target_phase")) != "l4_execute":
            return False
        refs = execution.get("owned_process_refs")
        if not isinstance(refs, list) or not refs:
            return bool(execution.get("waiting")) and _status_looks_running(execution.get("process_status"))
        terminal = {"completed", "complete", "succeeded", "success", "failed", "failure", "exited", "stopped", "dead", "terminated", "terminal"}
        for ref in refs:
            if not isinstance(ref, dict):
                return True
            status = str(ref.get("status") or ref.get("state") or ref.get("process_status") or "").strip().lower()
            if not status:
                return bool(execution.get("waiting"))
            if status not in terminal:
                return True
        return False

    def _fail_window(self, exc: BridgeExecutionError) -> dict[str, Any]:
        bridge_result = self._bridge_result("failed", exc.failure_stage, {"error_or_null": {"message": str(exc), **exc.payload}})
        if exc.failure_stage == "packet_accept":
            self._return_bridge_result("bridge_result_returned", bridge_result, agent_type="bridge-leader")
            return bridge_result
        if self.team_id:
            try:
                self._event("team_delete_started", team_id=self.team_id, task_id=self.task_id, tool_name="team_delete")
                self._event("team_delete_succeeded", team_id=self.team_id, task_id=self.task_id, tool_name="team_delete")
            except Exception:
                bridge_result["cleanup_required"] = True
        self._return_bridge_result("bridge_result_returned", bridge_result)
        return bridge_result

    def _interrupt_window(self, exc: KeyboardInterrupt) -> dict[str, Any]:
        execution = {
            "reports": [
                {
                    "summary": "Bridge window was interrupted by the user before a normal BridgeResult could be completed.",
                    "failure_reason": "manual_interrupt",
                    "next_action_recommendation": "Read the runtime snapshot and dispatch the next legal bridge once no other blocker remains.",
                }
            ],
            "artifact_refs": [],
            "evidence": {
                "classification": "manual_bridge_interrupt",
                "event_ids_before_interrupt": list(self.event_ids),
            },
            "error_or_null": {"message": "bridge window interrupted by user", "type": exc.__class__.__name__},
            "cleanup_required": True,
        }
        try:
            self._event(
                "bridge_call_interrupted",
                team_id=self.team_id if self.team_id else None,
                task_id=self.task_id if self.task_id else None,
                agent_id="runtime.interrupt",
                agent_type="runtime",
                tool_name="call_bridge_sdk",
                payload={
                    "error_or_null": execution["error_or_null"],
                    "interrupt_source": "manual_user_interrupt",
                    "event_ids_before_interrupt": list(self.event_ids),
                },
            )
        except Exception:
            execution["evidence"]["interrupt_event_persist_failed"] = True
        return self._bridge_result("failed", "manual_interrupt", execution)

    def _bridge_result(self, status: str, failure_stage: str | None, execution: dict[str, Any]) -> dict[str, Any]:
        bind_window_objects = failure_stage != "packet_accept"
        return {
            "run_id": self.run_id,
            "main_session_id": self.main_session_id,
            "sub_session_id": self.sub_session_id,
            "bridge_window_id": self.bridge_window_id,
            "team_id_or_null": self.team_id if bind_window_objects else None,
            "task_id_or_null": self.task_id if bind_window_objects else None,
            "status": status,
            "failure_stage_or_null": failure_stage,
            "reports": execution.get("reports", []),
            "artifact_refs": execution.get("artifact_refs", []),
            "evidence": execution.get("evidence") or {"event_ids": list(self.event_ids)},
            "error_or_null": execution.get("error_or_null"),
            "cleanup_required": bool(execution.get("cleanup_required", False)),
            "returned_at": _now_iso(),
        }

    def _event(
        self,
        event_kind: str,
        *,
        team_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        agent_type: str = "bridge-leader",
        tool_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_payload = {
            "run_id": self.run_id,
            "main_session_id": self.main_session_id,
            "sub_session_id": self.sub_session_id,
            "bridge_window_id": self.bridge_window_id,
            "team_id": team_id,
            "task_id": task_id,
            "agent_id": agent_id or self.bridge_leader_id,
            "agent_type": agent_type,
            "tool_name": tool_name,
            "tool_use_id": f"tool_{uuid.uuid4().hex[:12]}" if tool_name else None,
            "event_kind": event_kind,
            "timestamp": _now_iso(),
            "payload": payload or {},
        }
        result = dispatch_workflow_event(
            self.control_root,
            event_payload,
            runtime_runs_root=self.runtime_runs_root,
            persist=self.persist,
        )
        self.event_ids.append(result.event_id)
        if not result.ok:
            reasons = result.check_result.get("reasons", [])
            stage = _failure_stage_for_event(event_kind)
            raise BridgeExecutionError(stage, f"{event_kind} rejected by runtime: {reasons}", payload={"reasons": reasons})
        return result.runtime_snapshot


def default_team_executor() -> TeamExecutor:
    executor = bridge_executor_from_env()
    return lambda execution_input: executor.execute(BridgeExecutionRequest.from_execution_input(execution_input))


def _project_root() -> Path:
    return Path(os.environ.get("BRIDGE_PROJECT_ROOT") or Path.cwd()).resolve()


def _completion_checks(contract: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    required_outputs = contract.get("required_outputs", [])
    required_artifacts = contract.get("required_artifacts", [])
    validation_requirements = contract.get("validation_requirements", [])
    reports = execution.get("reports", [])
    artifact_refs = execution.get("artifact_refs", [])
    missing_artifacts = _missing_required_artifacts(required_artifacts, artifact_refs)
    failed_validations = _failed_validation_requirements_for_contract(contract, validation_requirements, execution)
    return {
        "required_outputs_present": not required_outputs or bool(reports),
        "required_artifacts_present": not missing_artifacts,
        "validation_passed": not failed_validations,
        "missing_outputs": [] if (not required_outputs or reports) else list(required_outputs),
        "missing_artifacts": missing_artifacts,
        "failed_validations": failed_validations,
        "notes": [],
    }


def _completion_rejection_message(checks: dict[str, Any], missing: list[Any]) -> str:
    disposition = str(checks.get("final_disposition") or "unknown")
    failed_subjects: list[str] = []
    for item in checks.get("checks", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").lower() not in {"fail", "block"}:
            continue
        subject = str(item.get("subject") or item.get("name") or "completion_check")
        if subject:
            failed_subjects.append(subject[:160])
    parts = [f"completion contract rejected at task_complete; final_disposition={disposition}"]
    if missing:
        parts.append("missing=" + ", ".join(str(item) for item in missing[:8]))
    if failed_subjects:
        parts.append("failed_checks=" + " | ".join(failed_subjects[:6]))
    return "; ".join(parts)


def _missing_required_artifacts(required_artifacts: Any, artifact_refs: Any) -> list[str]:
    required = [str(item) for item in required_artifacts] if isinstance(required_artifacts, list) else []
    refs = [str(item) for item in artifact_refs] if isinstance(artifact_refs, list) else []
    if not required:
        return []
    if not refs:
        return required
    missing = []
    for item in required:
        if not _artifact_requirement_satisfied(item, refs):
            missing.append(item)
    return missing


def _artifact_requirement_satisfied(required: str, refs: list[str]) -> bool:
    key = required.strip().casefold()
    if not key:
        return True
    if key == "log_manifest":
        return any(_looks_like_log_manifest(ref) for ref in refs)
    return any(key in ref.casefold() for ref in refs)


def _looks_like_log_manifest(ref: str) -> bool:
    lowered = ref.casefold().replace("\\", "/")
    name = lowered.rsplit("/", 1)[-1]
    return "manifest" in name and ("log" in name or "artifact" in name or "run" in name)


def _failed_validation_requirements(validation_requirements: Any, execution: dict[str, Any]) -> list[str]:
    return _failed_validation_requirements_for_contract({}, validation_requirements, execution)


def _failed_validation_requirements_for_contract(contract: dict[str, Any], validation_requirements: Any, execution: dict[str, Any]) -> list[str]:
    required = [str(item) for item in validation_requirements] if isinstance(validation_requirements, list) else []
    if not required:
        return []
    if execution.get("validation_passed") is False:
        return required
    return [item for item in required if not _validation_requirement_satisfied(item, execution, contract)]


def _validation_requirement_satisfied(requirement: str, execution: dict[str, Any], contract: dict[str, Any] | None = None) -> bool:
    lowered = requirement.casefold()
    if "manifest" in lowered:
        refs = execution.get("artifact_refs") if isinstance(execution.get("artifact_refs"), list) else []
        has_manifest_ref = any(_looks_like_log_manifest(str(ref)) for ref in refs)
        if "required" in lowered and "field" in lowered:
            return has_manifest_ref and _manifest_field_evidence_present(execution, contract or {})
        return has_manifest_ref
    return bool(execution.get("validation_passed", True))


def _manifest_field_evidence_present(execution: dict[str, Any], contract: dict[str, Any] | None = None) -> bool:
    evidence_blob = json.dumps(
        {
            "reports": execution.get("reports", []),
            "evidence": execution.get("evidence"),
        },
        ensure_ascii=False,
        default=str,
    ).casefold()
    required_fields = contract.get("manifest_required_fields") if isinstance(contract, dict) else None
    if isinstance(required_fields, list) and required_fields:
        missing = [str(field) for field in required_fields if str(field).casefold() not in evidence_blob]
        if missing:
            return False
        return True
    required_markers = [
        ("manifest required fields", "required fields checklist", "manifest checklist"),
        ("run_id", "run id"),
        ("bridge_window_id", "bridge window id"),
        ("task_id", "task id"),
        ("command",),
        ("cwd",),
        ("batchbasis", "batch basis"),
        ("gpu_id", "gpu id", "device id"),
        ("memory observed", "smoke memory", "warmup memory", "formal observed memory"),
        ("model",),
        ("dataset",),
        ("method", "objective"),
    ]
    return all(any(marker in evidence_blob for marker in group) for group in required_markers)


def _checks_satisfied(checks: dict[str, Any]) -> bool:
    return (
        bool(checks.get("required_outputs_present", False))
        and bool(checks.get("required_artifacts_present", False))
        and bool(checks.get("validation_passed", False))
        and not checks.get("missing_outputs")
        and not checks.get("missing_artifacts")
        and not checks.get("failed_validations")
    )


def _status_looks_running(status: Any) -> bool:
    if not status:
        return False
    return str(status).strip().lower() not in {"completed", "complete", "succeeded", "success", "failed", "failure", "exited", "stopped", "dead", "terminated", "terminal"}


def _teammate_ids(team_spec: dict[str, Any]) -> list[str]:
    ids = []
    for teammate in team_spec.get("teammate_specs", []):
        ids.append(str(teammate.get("teammate_id_or_null") or teammate.get("teammate_name") or f"mate_{uuid.uuid4().hex[:8]}"))
    return ids


def _failure_stage_for_event(event_kind: str) -> str:
    if event_kind == "team_executor_failed":
        return "team_wait"
    if event_kind.startswith("team_create"):
        return "team_create"
    if event_kind.startswith("task_create") or event_kind.startswith("taskcreated"):
        return "task_create"
    if event_kind.startswith("message_dispatch"):
        return "send_message"
    if event_kind.startswith("team_delete"):
        return "team_delete"
    if event_kind.startswith("completion") or event_kind == "artifacts_ready":
        return "task_complete"
    return "bridge_return"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
