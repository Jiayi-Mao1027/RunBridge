from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import uuid

from claude_cli_executor import claude_cli_team_executor, simulated_team_executor
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
    team_executor: TeamExecutor | None = None,
    persist: bool = True,
    bridge_leader_id: str = "bridge-leader",
) -> dict[str, Any]:
    runner = BridgeLeaderRuntime(
        control_root=control_root,
        runtime_runs_root=runtime_runs_root,
        packet=packet,
        team_executor=team_executor or default_team_executor(),
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
        team_executor: TeamExecutor,
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
        self.team_id = str(self.packet.get("team_spec", {}).get("team_id_or_null") or f"team_{uuid.uuid4().hex[:12]}")
        self.task_id = str(self.packet.get("task_spec", {}).get("task_id_or_null") or f"task_{uuid.uuid4().hex[:12]}")
        self.event_ids: list[str] = []

    def run(self) -> dict[str, Any]:
        try:
            self._accept_packet()
            self._team_create()
            self._task_create()
            self._send_messages()
            execution = self._run_team()
            if execution.get("waiting"):
                self._team_waiting(execution)
            if execution.get("status") in {"partial", "partial_or_failed"}:
                if not execution.get("waiting"):
                    self._team_waiting(execution)
                self._wait_timeout(execution)
                self._event("partial_evidence_collected", team_id=self.team_id, task_id=self.task_id, payload={"evidence": execution.get("evidence"), "reports": execution.get("reports", []), "artifact_refs": execution.get("artifact_refs", [])})
                self._team_delete()
                bridge_result = self._bridge_result("partial_or_failed", "team_wait", execution)
                self._return_bridge_result("bridge_result_returned_with_partial", bridge_result, agent_type="main-leader")
                return bridge_result
            if execution.get("status") == "failed":
                if not execution.get("waiting"):
                    self._team_waiting(execution)
                self._wait_timeout(execution)
                self._fail_task(execution, event_kind="task_failed_by_bridge_leader")
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

    def _accept_packet(self) -> None:
        if not self.run_id or not self.main_session_id or not self.sub_session_id or not self.bridge_window_id:
            raise BridgeExecutionError("packet_accept", "packet binding is incomplete")
        if not isinstance(self.packet.get("team_spec"), dict) or not isinstance(self.packet.get("task_spec"), dict):
            self._event("bridge_packet_rejected", payload={"packet": self.packet, "reasons": ["packet_spec_missing"]})
            raise BridgeExecutionError("packet_accept", "packet rejected", payload={"reasons": ["packet_spec_missing"]})
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
        result = self.team_executor(execution_input)
        if not isinstance(result, dict):
            raise BridgeExecutionError("task_complete", "team executor must return a dict")
        result.setdefault("status", "succeeded")
        result.setdefault("reports", [{"summary": "team executor completed", "event_ids": list(self.event_ids)}])
        result.setdefault("artifact_refs", [])
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
        checks = _completion_checks(self.packet.get("completion_contract", {}), execution)
        self._event("artifacts_ready", team_id=self.team_id, task_id=self.task_id, tool_name="task_complete", payload={"artifact_refs": execution.get("artifact_refs", [])})
        if not _checks_satisfied(checks):
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

    def _reject_completion(self, execution: dict[str, Any]) -> None:
        checks = _completion_checks(self.packet.get("completion_contract", {}), execution)
        missing = list(checks.get("missing_outputs", [])) + list(checks.get("missing_artifacts", [])) + list(checks.get("failed_validations", []))
        self._event(
            "completion_contract_rejected",
            team_id=self.team_id,
            task_id=self.task_id,
            agent_id="hook.task_completed",
            agent_type="hook",
            payload={
                "completion_contract": self.packet.get("completion_contract", {}),
                "completion_evidence": execution.get("evidence"),
                "reports": execution.get("reports", []),
                "artifact_refs": execution.get("artifact_refs", []),
                "completion_checks": checks,
                "missing_contract_items": missing,
            },
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
    import os

    mode = os.environ.get("BRIDGE_EXECUTOR", "claude-cli").strip().lower()
    if mode in {"simulate", "simulated", "smoke"}:
        return simulated_team_executor
    return claude_cli_team_executor


def _completion_checks(contract: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    required_outputs = contract.get("required_outputs", [])
    required_artifacts = contract.get("required_artifacts", [])
    validation_requirements = contract.get("validation_requirements", [])
    reports = execution.get("reports", [])
    artifact_refs = execution.get("artifact_refs", [])
    return {
        "required_outputs_present": not required_outputs or bool(reports),
        "required_artifacts_present": not required_artifacts or bool(artifact_refs),
        "validation_passed": not validation_requirements or execution.get("validation_passed", True),
        "missing_outputs": [] if (not required_outputs or reports) else list(required_outputs),
        "missing_artifacts": [] if (not required_artifacts or artifact_refs) else list(required_artifacts),
        "failed_validations": [] if (not validation_requirements or execution.get("validation_passed", True)) else list(validation_requirements),
        "notes": [],
    }


def _checks_satisfied(checks: dict[str, Any]) -> bool:
    return (
        bool(checks.get("required_outputs_present", False))
        and bool(checks.get("required_artifacts_present", False))
        and bool(checks.get("validation_passed", False))
        and not checks.get("missing_outputs")
        and not checks.get("missing_artifacts")
        and not checks.get("failed_validations")
    )


def _teammate_ids(team_spec: dict[str, Any]) -> list[str]:
    ids = []
    for teammate in team_spec.get("teammate_specs", []):
        ids.append(str(teammate.get("teammate_id_or_null") or teammate.get("teammate_name") or f"mate_{uuid.uuid4().hex[:8]}"))
    return ids


def _failure_stage_for_event(event_kind: str) -> str:
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
