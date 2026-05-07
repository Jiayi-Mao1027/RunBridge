from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

from loader import ControlPaths, load_json_file
from workflow_runtime import SCHEMA_VERSION, build_runtime_snapshot


PACKET_SCHEMA_VERSION = "0.1"
DEFAULT_BRIDGE_ACTIONS = ["team_create", "task_create", "send_messages", "task_complete", "team_delete"]
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "LS"]
RESEARCH_TOOLS = [*READ_ONLY_TOOLS, "WebSearch", "WebFetch"]
READ_CHECK_TOOLS = [*READ_ONLY_TOOLS, "Bash"]
L3_WRITE_TOOLS = [*READ_ONLY_TOOLS, "Edit", "Write"]
WRITE_TOOLS = [*READ_ONLY_TOOLS, "Bash", "Edit", "Write"]
DEFAULT_BRIDGE_LEADER_TOOLS = ["Agent", *WRITE_TOOLS]
PHASE_BRIDGE_TOOLS = {
    "l2_advisory": ["Agent", *RESEARCH_TOOLS],
    "l3_bridge": ["Agent", *L3_WRITE_TOOLS],
    "l4_implement": DEFAULT_BRIDGE_LEADER_TOOLS,
    "l4_execute": ["Agent", "Read", "Grep", "Glob", "LS", "Bash", "Write"],
    "l4_anomaly": ["Agent", *READ_CHECK_TOOLS],
}
DEFAULT_FORBIDDEN_ACTIONS = [
    "destructive filesystem operations outside writable scopes",
    "external network calls unless explicitly approved",
    "dependency installation unless explicitly approved",
    "implementation content edits during L3 artifact curation unless the file is human-facing documentation already in L3 doc scope",
    "physical deletion of user/project artifacts unless the item is clearly regenerable trash, an empty duplicate, or explicitly approved",
]
PHASE_ACTIVE_SURFACE_POLICIES = {
    "l3_bridge": [
        "Before curation, identify the current step, the prior completed work, and the artifacts that are actually needed for the next downstream phase.",
        "Keep the active code, log, checkpoint, data, document, and script surfaces minimum viable: anything not needed for current understanding or next execution should leave the active surface.",
        "Prefer archive/move-out over retention-with-labeling for material that is clearly unused. Archive is the default for ambiguous or stale project artifacts; active retention requires a concrete next-phase reason.",
        "Logs are cleanup targets but may be reusable evidence or expensive generated output. Retain logs with a concrete current-step, audit, comparison, or reuse reason; archive only logs that are clearly obsolete, duplicate, superseded, or unrelated.",
        "Physical deletion is exceptional and must be limited to clearly regenerable trash, empty duplicates, or explicitly approved removals.",
        "L3 may archive or organize files, but must not implement code behavior changes; code/config content changes belong to L4 implement.",
    ],
    "l4_implement": [
        "Preserve the minimum viable repository surface while implementing: edit existing files when practical, use temporary scripts for one-off work, and create long-lived files only when there is a durable need.",
        "Do not leave exploratory logs, checkpoints, data extracts, scratch code, or one-off scripts active unless they are required for the next phase.",
        "Archive or remove from active reach any implementation byproducts that would confuse rungater, executor, or later readers.",
        "Report every new long-lived file with its reason to remain active.",
    ],
}
PHASE_OWNERSHIP_DEFAULTS = {
    "l2_advisory": {"readable_scopes": ["."], "writable_scopes": []},
    "l3_bridge": {"readable_scopes": ["."], "writable_scopes": ["."]},
    "l4_implement": {"readable_scopes": ["."], "writable_scopes": ["."]},
    "l4_execute": {"readable_scopes": ["."], "writable_scopes": ["."]},
    "l4_anomaly": {"readable_scopes": ["."], "writable_scopes": []},
}

DEFAULT_TIMEOUT_POLICY = {
    "heartbeat_interval_seconds": 60,
    "soft_timeout_seconds": 900,
    "hard_timeout_seconds": 3600,
    "timeout_action": "ask_main_leader",
}

PHASE_TIMEOUT_POLICY = {
    "l4_execute": {
        "heartbeat_interval_seconds": 120,
        "soft_timeout_seconds": 21600,
        "hard_timeout_seconds": 86400,
        "timeout_action": "ask_main_leader",
        "wait_until_process_complete": True,
        "partial_return_allowed_only_after_process_terminal": True,
    },
}

PHASE_TEAM_DEFAULTS = {
    "l2_advisory": [
        ("chiefmate-a", "advisory", RESEARCH_TOOLS, "produce upstream interpretation, assumptions, plan critique, and peer-aware advisory judgment"),
        ("chiefmate-b", "advisory", RESEARCH_TOOLS, "produce independent upstream advisory judgment and critique chiefmate-a when relevant"),
    ],
    "l3_bridge": [
        ("curator", "artifact_curation", L3_WRITE_TOOLS, "clarify active logs, datasets, checkpoints, outputs, archive boundaries, and traceability without running commands"),
        ("preflight-initial", "preflight_audit", READ_ONLY_TOOLS, "inspect implementation-facing repo/config state and surface required changes before implementation without running commands"),
        ("refresher", "documentation_refresh", ["Read", "Grep", "Glob", "LS", "Edit", "Write"], "refresh CLAUDE.md and bounded human-facing repository documentation when needed"),
    ],
    "l4_implement": [
        ("implementor", "implement", WRITE_TOOLS, "make approved code/config changes and collect bounded validation evidence"),
        ("rungater", "implementation_gate", READ_CHECK_TOOLS, "judge post-implementation readiness and recommend proceed, repair, reroute, or stop"),
    ],
    "l4_execute": [
        ("executor", "formal_execute", ["Read", "Grep", "Glob", "LS", "Bash", "Write"], "run the approved workflow through conda env mjy, force formal GPU runs above 90% of selected GPU memory when applicable, and record exact execution evidence"),
        ("postrun", "postrun_audit", READ_CHECK_TOOLS, "audit execution artifacts, conda env use, GPU memory utilization, outcome classification, and recommend anomaly routing when needed"),
    ],
    "l4_anomaly": [
        ("anomaly-analyst-a", "anomaly_analysis", READ_CHECK_TOOLS, "build evidence-backed anomaly hypotheses and discriminative next checks"),
        ("anomaly-analyst-b", "anomaly_analysis", READ_CHECK_TOOLS, "build independent anomaly hypotheses and critique anomaly-analyst-a when relevant"),
    ],
}


def read_runtime_snapshot(
    control_root: str | Path,
    run_id: str,
    *,
    runtime_runs_root: str | Path | None = None,
) -> dict[str, Any]:
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    run_ledger = load_json_file(paths.run_ledger_path(run_id), default={}) or {}
    if not run_ledger:
        now = _now_iso()
        run_ledger = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "main_session_id": run_id,
            "workflow_name": "bridge_window_workflow",
            "workflow_version": SCHEMA_VERSION,
            "run_status": "in_progress",
            "current_phase": "leader_freeze",
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
        }
    return build_runtime_snapshot(paths, run_ledger)


def decide_next_bridge_packet(
    control_root: str | Path,
    run_id: str,
    *,
    runtime_runs_root: str | Path | None = None,
    main_session_id: str | None = None,
    user_instruction: str | None = None,
    task_spec: dict[str, Any] | None = None,
    target_phase: str | None = None,
) -> dict[str, Any]:
    snapshot = read_runtime_snapshot(control_root, run_id, runtime_runs_root=runtime_runs_root)
    return build_bridge_instruction_packet_for_this_invoke(
        snapshot=snapshot,
        main_session_id=main_session_id,
        user_instruction=user_instruction,
        task_spec=task_spec,
        target_phase=target_phase,
    )


def build_bridge_instruction_packet_for_this_invoke(
    *,
    snapshot: dict[str, Any],
    main_session_id: str | None = None,
    user_instruction: str | None = None,
    task_spec: dict[str, Any] | None = None,
    target_phase: str | None = None,
) -> dict[str, Any]:
    if snapshot.get("integrity", {}).get("has_hard_stop"):
        raise ValueError("cannot build bridge packet while hard_stop is active")
    if snapshot.get("integrity", {}).get("awaiting_approval"):
        raise ValueError("cannot build bridge packet while approval is pending")
    if "call_bridge_sdk" not in snapshot.get("allowed_actions", []):
        raise ValueError("current runtime snapshot does not allow call_bridge_sdk")

    run_id = str(snapshot["run_id"])
    resolved_main_session_id = str(main_session_id or snapshot.get("main_session_id") or run_id)
    sub_session_id = f"sub_{uuid.uuid4().hex[:12]}"
    bridge_window_id = f"bw_{run_id}_{sub_session_id}"
    parent_tool_use_id = f"tool_{uuid.uuid4().hex[:12]}"
    now = _now_iso()

    resolved_target_phase = _resolve_target_phase(snapshot, target_phase)
    resolved_route = _resolve_phase_route(snapshot, resolved_target_phase)
    resolved_completion = _default_completion_contract(resolved_target_phase)
    resolved_report = _default_report_contract(resolved_target_phase)
    bridge_allowed_tools = _default_bridge_tools(resolved_target_phase)
    resolved_team = _normalize_team_spec(target_phase=resolved_target_phase)
    resolved_task = _normalize_task_spec(
        task_spec,
        user_instruction=user_instruction,
        target_phase=resolved_target_phase,
        completion_contract=resolved_completion,
        report_contract=resolved_report,
    )
    mapping = _build_task_team_mapping(resolved_task, resolved_team)

    binding = {
        "run_id": run_id,
        "main_session_id": resolved_main_session_id,
        "sub_session_id": sub_session_id,
        "bridge_window_id": bridge_window_id,
        "parent_tool_use_id": parent_tool_use_id,
        "opened_by_agent_id": "main-leader",
        "opened_by_agent_type": "main-leader",
        "bridge_leader_id_or_null": None,
        "team_id_or_null": resolved_team.get("team_id_or_null"),
        "task_id_or_null": resolved_task.get("task_id_or_null"),
        "lifecycle_status": "bridge_call_intended",
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
    }

    packet = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "binding": binding,
        "frozen_semantics": deepcopy(snapshot.get("semantic", {}).get("frozen") or {}),
        "frozen_scope": deepcopy(snapshot.get("scope", {}).get("frozen") or {}),
        "phase_route": resolved_route,
        "target_phase": resolved_target_phase,
        "team_spec": resolved_team,
        "task_spec": resolved_task,
        "task_team_mapping": mapping,
        "completion_contract": resolved_completion,
        "report_contract": resolved_report,
        "allowed_actions": list(DEFAULT_BRIDGE_ACTIONS),
        "allowed_tools": list(bridge_allowed_tools),
        "approval_requirements": [],
        "created_at": now,
        "expires_at": None,
    }
    return packet


def _resolve_target_phase(snapshot: dict[str, Any], requested: str | None) -> str:
    if requested:
        return requested
    route = snapshot.get("route", {})
    if route.get("target_phase"):
        return str(route["target_phase"])
    allowed = snapshot.get("allowed_routes") or []
    if allowed:
        return str(allowed[0])
    return str(snapshot.get("current_phase") or "leader_freeze")


def _resolve_phase_route(snapshot: dict[str, Any], target_phase: str) -> list[str]:
    route = snapshot.get("route", {}).get("current_route")
    if isinstance(route, list) and route and str(route[-1]) == target_phase:
        return [str(item) for item in route]
    current = str(snapshot.get("current_phase") or "leader_freeze")
    return [current] if current == target_phase else [current, target_phase]


def _normalize_team_spec(
    *,
    target_phase: str,
) -> dict[str, Any]:
    if target_phase in PHASE_TEAM_DEFAULTS:
        teammates = _default_teammate_specs(target_phase)
        ownership = _default_ownership_boundary(target_phase)
    else:
        teammates = _default_teammate_specs(target_phase)
        ownership = _default_ownership_boundary(target_phase)
    return {
        "team_id_or_null": None,
        "team_name": f"bridge-{target_phase}-team",
        "teammate_specs": teammates,
        "ownership_boundary": ownership,
    }


def _default_bridge_tools(target_phase: str) -> list[str]:
    return list(PHASE_BRIDGE_TOOLS.get(target_phase, DEFAULT_BRIDGE_LEADER_TOOLS))


def _default_ownership_boundary(target_phase: str) -> dict[str, Any]:
    scopes = PHASE_OWNERSHIP_DEFAULTS.get(target_phase, {"readable_scopes": ["."], "writable_scopes": []})
    return {
        "readable_scopes": list(scopes["readable_scopes"]),
        "writable_scopes": list(scopes["writable_scopes"]),
        "process_ownership_rules": ["only manage processes launched inside this bridge window"],
        "forbidden_actions": list(DEFAULT_FORBIDDEN_ACTIONS),
        "active_surface_policy": list(PHASE_ACTIVE_SURFACE_POLICIES.get(target_phase, [])),
    }


def _default_teammate_specs(target_phase: str) -> list[dict[str, Any]]:
    defaults = PHASE_TEAM_DEFAULTS.get(target_phase)
    if not defaults:
        return [
            {
                "teammate_id_or_null": None,
                "teammate_name": "bridge-worker",
                "role": "execute",
                "allowed_tools": list(READ_CHECK_TOOLS),
                "responsibilities": ["execute the single bridge-window task and report evidence"],
            }
        ]

    specs = []
    for name, role, default_tools, responsibility in defaults:
        specs.append(
            {
                "teammate_id_or_null": None,
                "teammate_name": name,
                "role": role,
                "allowed_tools": list(default_tools),
                "responsibilities": [responsibility],
            }
        )
    return specs


def _normalize_task_spec(
    task_spec: dict[str, Any] | None,
    *,
    user_instruction: str | None,
    target_phase: str,
    completion_contract: dict[str, Any],
    report_contract: dict[str, Any],
) -> dict[str, Any]:
    source = deepcopy(task_spec or {})
    original_instruction = str(
        source.get("original_user_instruction")
        or source.get("user_instruction")
        or user_instruction
        or ""
    ).strip()
    subject = str(source.get("task_subject") or source.get("subject") or _derive_subject(original_instruction) or "bridge-window task")
    description = str(source.get("task_description") or source.get("description") or original_instruction or subject)
    normalized = {
        "task_id_or_null": source.get("task_id_or_null") or source.get("task_id"),
        "task_subject": subject,
        "task_description": description,
        "original_user_instruction": original_instruction,
        "instruction_coverage_checklist": _derive_instruction_coverage_checklist(source, original_instruction, description),
        "semantic_resolution_contract": _semantic_resolution_contract(source, original_instruction, target_phase),
        "preserved_task_context": _preserved_task_context(source),
        "task_kind": str(source.get("task_kind") or "bridge_window_task"),
        "target_phase": target_phase,
        "completion_contract": deepcopy(completion_contract),
        "report_contract": deepcopy(report_contract),
    }
    return normalized


def _build_task_team_mapping(task_spec: dict[str, Any], team_spec: dict[str, Any]) -> dict[str, Any]:
    assignments = []
    ownership = team_spec.get("ownership_boundary", {}) if isinstance(team_spec.get("ownership_boundary"), dict) else {}
    for teammate in team_spec.get("teammate_specs", []):
        name = str(teammate.get("teammate_name") or "bridge-worker")
        responsibilities = teammate.get("responsibilities") if isinstance(teammate.get("responsibilities"), list) else []
        assignments.append(
            {
                "teammate_id_or_null": teammate.get("teammate_id_or_null"),
                "assignment": "\n".join(
                    [
                        f"{name}: {task_spec['task_description']}",
                        f"Original user instruction: {task_spec.get('original_user_instruction') or task_spec['task_description']}",
                        f"Instruction coverage checklist: {_json_list(task_spec.get('instruction_coverage_checklist'))}",
                        f"Semantic resolution contract: {_json_dict(task_spec.get('semantic_resolution_contract'))}",
                        f"Preserved task context: {_json_dict(task_spec.get('preserved_task_context'))}",
                        "Coverage rule: do not mark the task complete until every checklist item is completed, explicitly deferred with a concrete reason, or escalated to main-leader/user.",
                        "Semantic identity rule: resolve or explicitly carry model/method identity, checkpoint identity, dataset identity, prompt identity, code/config basis, and inherited defaults before downstream implementation or execution. Do not silently change them.",
                        "Report rule: include an instruction coverage section that lists completed, deferred, blocked, and escalated checklist items.",
                        "Report rule: include a semantic identity resolution section with resolved, inherited, unknown, blocked, or escalated disposition for each required identity field.",
                        f"Role: {teammate.get('role') or 'bridge teammate'}",
                        f"Responsibilities: {_json_list(responsibilities)}",
                        f"Allowed tools: {_json_list(teammate.get('allowed_tools'))}",
                        f"Readable scopes: {_json_list(ownership.get('readable_scopes'))}",
                        f"Writable scopes: {_json_list(ownership.get('writable_scopes'))}",
                        f"Forbidden actions: {_json_list(ownership.get('forbidden_actions'))}",
                        f"Active surface policy: {_json_list(ownership.get('active_surface_policy'))}",
                        f"Completion contract: {_json_dict(task_spec.get('completion_contract'))}",
                        f"Report contract: {_json_dict(task_spec.get('report_contract'))}",
                        *_phase_assignment_instructions(str(task_spec.get("target_phase") or ""), name),
                        "Do not read .claude/runtime_state/bridge_prompts for task context; that bridge prompt artifact is for audit only.",
                        "When using Read, omit optional parameters you do not need. Never pass pages as an empty string.",
                    ]
                ),
                "expected_output": "completion report and declared artifact refs",
            }
        )
    return {
        "task_id_or_null": task_spec.get("task_id_or_null"),
        "team_id_or_null": team_spec.get("team_id_or_null"),
        "teammate_assignments": assignments,
    }


def _phase_assignment_instructions(target_phase: str, teammate_name: str) -> list[str]:
    if target_phase == "l3_bridge":
        return [
            "L3 no-run-tools rule: do not run shell commands or other execution tools in L3. Use Read/Grep/Glob/LS for inspection and Edit/Write only for explicitly permitted curation or documentation updates.",
            "L3 semantic identity rule: actively identify which model/method/checkpoint/dataset/prompt/config the user means. For comparisons such as DPO vs OPD, report the concrete ckpts or say exactly what is ambiguous.",
            "L3 inheritance rule: when the user does not request a dataset, prompt, split, metric, or config change, inspect the active repo/docs enough to identify the current basis and explicitly recommend inheriting it.",
            "L3 packet handoff rule: report the resolved semantic basis in a form L4 implement/execute can copy directly: model/method identity, ckpt paths/IDs, dataset/split, prompt/template, config files, and any unresolved field.",
            "L3 log curation rule: keep the log surface minimum viable, but do not archive logs merely because they are old or bulky. Retain logs that may be reused for comparison, audit, avoiding expensive regeneration, or downstream interpretation; archive only logs that are clearly unused, duplicate, superseded, or unrelated, and report the reason.",
            "L3 minimum-active-surface rule: first identify what this step is trying to do, what prior work is already done, and what files/artifacts are genuinely required for the next phase.",
            "Archive-first curation rule: keep the active code, log, checkpoint, data, document, and script surfaces minimum viable. Archive or move out stale, duplicate, ambiguous, or non-current material instead of leaving it active with only a label.",
            "Active-retention burden: every retained log/dataset/checkpoint/output/scratch script/document/code copy needs a concrete current-step or next-phase reason. If the reason is weak, archive it.",
            "Deletion boundary: prefer archive over physical deletion. Delete only clearly regenerable trash, empty duplicates, or items explicitly approved for deletion; otherwise archive and report the archive path/reason.",
            "L3 must not implement behavior changes in source/config code. If code or scripts are not active but may be historically useful, archive them; if code behavior must change, report it for L4 implement.",
            "L3 documentation rule: explicitly decide whether CLAUDE.md, README.md, docs/, or other Markdown files need updates for this task.",
            "If the task touches documentation, Markdown, repo-facing instructions, workflow rules, or agent behavior, make the smallest correct documentation update within writable scope; prioritize CLAUDE.md when it is relevant.",
            "If no documentation update is made, report the inspected documentation files and the concrete reason no update was needed.",
        ]
    if target_phase == "l4_implement":
        base = [
            "Semantic basis rule: implement exactly against the resolved semantic identity in task_spec.semantic_resolution_contract and preserved context; do not silently swap checkpoint, dataset, prompt, metric, or objective identity.",
            "Minimum-viable repository rule: keep the active project surface as small as practical while implementing.",
            "Prefer modifying existing code/config over creating new long-lived files. For one-off analysis or migration, use temporary scripts and cleanly archive/remove them from the active surface before handoff.",
            "New long-lived code, script, data, checkpoint, or document files need a concrete durable reason and must be reported explicitly.",
            "Do not leave exploratory logs, debug outputs, scratch checkpoints, duplicate code copies, or stale data active for rungater to disambiguate.",
        ]
        if teammate_name == "rungater":
            return [
                *base,
                "Gate the repository surface as part of readiness: flag active ambiguous logs, checkpoints, data, scripts, documents, or code copies that should have been archived before execution.",
            ]
        return base
    if target_phase == "l4_execute" and teammate_name == "executor":
        return [
            "Semantic basis rule: execute exactly the resolved model/method, checkpoint, dataset, prompt/template, config, and metric basis. If any identity field is unresolved, stop and report blocked rather than guessing.",
            "Execution environment rule: all formal execute commands must use the conda environment named mjy. Prefer `conda run -n mjy ...` for auditable commands, or explicitly record an equivalent `conda activate mjy` shell context. Do not create or use venv.",
            "Long-task ETA rule: before launching any long-running command, estimate expected wall-clock runtime as a range, state the basis for the estimate, and include that estimate in the execution report.",
            "Smoke-shape rule: before formal execution, use bounded smoke evidence to choose formal parameters such as per-device batch size, microbatch size, gradient accumulation, sequence length, precision, and effective batch size. Record why formal settings differ from smoke settings.",
            "Log manifest rule: every generated formal log folder must contain a manifest file inside that folder, analogous to checkpoint manifests. Do not rely on folder/file names alone. The manifest must record run/window/task IDs, command, cwd, environment, semantic basis, smoke evidence refs, formal parameters/effective batch size, process refs, log files, expected outputs/checkpoints, status, timestamps, and reuse/dependency notes.",
            "If runtime cannot be estimated, state that explicitly with the missing information and still record command, start time, owned process refs, logs, and expected outputs.",
            "L4 execute terminality rule: run formal long jobs in a way the bridge can wait on or poll until terminal completion. Do not return a final or partial bridge report while an owned process is still running; emit progress evidence and keep waiting.",
            "Formal GPU memory rule: unless the task is explicitly smoke/dry-run/conservative, configure formal GPU execution to exceed 90% of the selected GPU's total memory after warmup. On a typical 80GB GPU this usually means observed usage above 70GB.",
            "If >90% memory use cannot be reached safely, stop or classify the run as blocked/deviated with evidence; do not silently accept a low-memory formal run.",
        ]
    if target_phase == "l4_execute" and teammate_name == "postrun":
        return [
            "Semantic audit rule: verify the actual run used the resolved model/method, checkpoint, dataset, prompt/template, config, and metric basis; classify mismatches as execution deviations or defects.",
            "Log manifest audit rule: verify each generated formal log folder has an internal manifest and that the manifest matches the command, environment, semantic basis, formal parameters/effective batch size, process refs, log files, artifact refs, and terminal status. Missing or stale manifests are execution deviations.",
            "Audit ETA rule: compare actual runtime against the executor's estimate when available, and flag material deviation as execution evidence rather than treating it as chat context.",
            "Postrun must run after the formal execution process has reached a terminal state or produced terminal failure evidence; do not audit a still-running process as complete.",
            "Environment audit rule: verify formal execution used conda env mjy and did not use venv. Missing or contradictory environment evidence is an execution deviation.",
            "GPU memory audit rule: for formal GPU execution, verify observed memory exceeded 90% of selected GPU total memory after warmup; for typical 80GB GPUs, usage should usually exceed 70GB. Lower usage requires explicit smoke/conservative approval or hard resource evidence.",
        ]
    return []


def _json_list(value: Any) -> str:
    items = value if isinstance(value, list) else []
    return json.dumps([str(item) for item in items], ensure_ascii=False, default=str)


def _json_dict(value: Any) -> str:
    data = value if isinstance(value, dict) else {}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _default_completion_contract(target_phase: str | None = None) -> dict[str, Any]:
    timeout_policy = deepcopy(PHASE_TIMEOUT_POLICY.get(str(target_phase or ""), DEFAULT_TIMEOUT_POLICY))
    contract = {
        "required_outputs": ["report"],
        "required_artifacts": [],
        "validation_requirements": [],
        "success_criteria": [
            "bridge leader collected a report from the team",
            "every instruction coverage checklist item is completed, deferred with reason, blocked, or escalated",
        ],
        "allowed_partial_result": True,
        "timeout_policy": timeout_policy,
    }
    if str(target_phase or "") == "l4_execute":
        contract["required_artifacts"] = ["log_manifest"]
        contract["validation_requirements"] = ["generated formal log folders include internal manifests"]
        contract["success_criteria"].append("formal execution log folders are not identified by filename alone; each generated log folder has an internal manifest")
    return contract


def _default_report_contract(target_phase: str | None = None) -> dict[str, Any]:
    contract = {
        "required_sections": ["summary", "evidence", "instruction_coverage", "semantic_identity_resolution"],
        "required_evidence": ["runtime event ids", "instruction coverage disposition", "semantic identity resolution"],
        "artifact_reporting_format": "list",
        "include_failure_reason": True,
        "include_next_action_recommendation": True,
    }
    if str(target_phase or "") == "l4_execute":
        contract["required_sections"].append("artifact_manifests")
        contract["required_evidence"].extend(["log manifest path", "formal execution parameter manifest"])
    return contract


def _derive_subject(original_instruction: str) -> str:
    text = " ".join(str(original_instruction or "").split())
    if not text:
        return ""
    return text[:72]


def _derive_instruction_coverage_checklist(source: dict[str, Any], original_instruction: str, description: str) -> list[str]:
    items: list[str] = []
    for key in (
        "instruction_coverage_checklist",
        "requirements",
        "acceptance_criteria",
        "constraints",
        "must_do",
        "must_not_do",
    ):
        items.extend(_string_items(source.get(key)))
    items.extend(_split_instruction_text(original_instruction))
    if not items:
        items.extend(_split_instruction_text(description))
    return _dedupe_nonempty(items) or [str(description)]


def _semantic_resolution_contract(source: dict[str, Any], original_instruction: str, target_phase: str) -> dict[str, Any]:
    supplied = source.get("semantic_resolution_contract")
    if isinstance(supplied, dict):
        contract = deepcopy(supplied)
    else:
        contract = {}
    required_fields = [
        "model_or_method_identity",
        "checkpoint_identity",
        "dataset_identity",
        "prompt_or_template_identity",
        "code_config_basis",
        "metric_or_objective_identity",
        "inherited_defaults",
    ]
    contract.setdefault("required_identity_fields", required_fields)
    contract.setdefault("user_instruction_preview", _derive_subject(original_instruction))
    contract.setdefault("target_phase", target_phase)
    contract.setdefault(
        "resolution_policy",
        [
            "actively resolve identities from the frozen instruction and current repository state",
            "if the user did not request a change, inherit the current active dataset/prompt/config basis and say where it came from",
            "for model or method comparisons, name the concrete checkpoints or checkpoint-selection rule for each side",
            "do not let L4 implement or execute infer unresolved identities silently",
            "unknown identity fields must be marked unknown, blocked, or escalated with a concrete reason",
        ],
    )
    contract.setdefault(
        "report_disposition_values",
        ["resolved", "inherited", "unknown", "blocked", "escalated", "not_applicable"],
    )
    return contract


def _preserved_task_context(source: dict[str, Any]) -> dict[str, Any]:
    reserved = {
        "task_id_or_null",
        "task_id",
        "task_subject",
        "subject",
        "task_description",
        "description",
        "original_user_instruction",
        "user_instruction",
        "instruction_coverage_checklist",
        "requirements",
        "acceptance_criteria",
        "constraints",
        "must_do",
        "must_not_do",
        "semantic_resolution_contract",
        "task_kind",
    }
    preserved = {}
    for key, value in source.items():
        if key not in reserved:
            preserved[str(key)] = deepcopy(value)
    return preserved


def _string_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return _split_instruction_text(str(value))


def _split_instruction_text(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[str] = []
    for line in normalized.split("\n"):
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = cleaned.lstrip("-*0123456789.、)） \t")
        for part in cleaned.replace("；", ";").replace("。", ";").split(";"):
            stripped = part.strip()
            if stripped:
                pieces.append(stripped)
    return pieces


def _dedupe_nonempty(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
