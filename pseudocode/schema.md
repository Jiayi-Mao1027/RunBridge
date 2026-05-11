# Concrete runtime schema draft

# This file turns the workflow pseudocode into implementation-facing data contracts.
# Field names should be treated as stable unless the workflow itself changes.
# Syntax is typed pseudocode,close to dataclass/json-schema.


# =====================================================
# 1. ID and enum primitives
# =====================================================

type RunId = string
type MainSessionId = string
type SubSessionId = string
type BridgeWindowId = string
type TeamId = string
type TaskId = string
type TeammateId = string
type AgentId = string
type ToolUseId = string
type EventId = string
type Timestamp = iso8601_string

struct ArtifactRef:
    schema_version: "artifact_ref.v1"
    ref_type: string
    id: string
    path: string | null
    sha256: string | null
    producer:
        agent_id: AgentId | null
        event_id: EventId | null
    created_at: Timestamp
    safe_preview: string
    run_id: RunId | null
    bridge_window_id: BridgeWindowId | null
    team_id: TeamId | null
    task_id: TaskId | null

struct TeamPlanningDecision:
    selector: string
    reason: string
    risk_profile: map[string,bool]
    original_teammate_names: list[string]
    selected_teammate_names: list[string]
    policy_ref: string

enum AgentType:
    main-leader
    bridge-leader
    teammate
    hook
    runtime

enum CheckDecision:
    allow
    deny
    needs_review

enum UpdateDecision:
    applied
    rejected
    noop

enum NotifyLevel:
    info
    warn
    error
    blocking

enum BridgeResultStatus:
    succeeded
    rejected
    failed
    partial
    partial_or_failed
    orphaned

enum FailureStage:
    packet_accept
    team_create
    task_create
    send_message
    team_wait
    task_complete
    team_delete
    bridge_return


# =====================================================
# 2. Event envelope
# =====================================================

enum RuntimeEventSource:
    outer_sdk
    inner_sdk
    cli
    hook
    runtime
    companion

enum RuntimeEventAuthority:
    authoritative
    source
    observed
    derived
    projection

struct RuntimeEventEnvelope:
    schema_version: "runtime_event_envelope.v1"
    event_id: EventId
    run_id: RunId | null
    session_id: string | null
    window_id: BridgeWindowId | null
    team_id: TeamId | null
    task_id: TaskId | null
    agent_id: AgentId | null
    phase: string | null
    event_kind: string
    source: RuntimeEventSource
    seq: int | null
    timestamp: Timestamp
    caused_by: EventId | null
    payload_ref: string | null
    safe_preview: string
    authority: RuntimeEventAuthority

struct RuntimeEvent:
    event_id: EventId
    event_kind: string
    timestamp: Timestamp

    run_id: RunId
    main_session_id: MainSessionId
    sub_session_id: SubSessionId | null
    bridge_window_id: BridgeWindowId | null
    team_id: TeamId | null
    task_id: TaskId | null
    teammate_id: TeammateId | null

    agent_id: AgentId
    agent_type: AgentType
    tool_name: string | null
    tool_use_id: ToolUseId | null

    payload: object
    payload_ref: string | null
    parent_event_id: EventId | null
    correlation_id: string | null


struct RuntimeContext:
    run_id: RunId
    main_session_id: MainSessionId
    sub_session_id_or_null: SubSessionId | null
    bridge_window_id_or_null: BridgeWindowId | null
    team_id_or_null: TeamId | null
    task_id_or_null: TaskId | null

    agent_id: AgentId
    agent_type: AgentType
    event_id: EventId
    event_kind: string
    timestamp: Timestamp
    tool_name_or_null: string | null
    tool_use_id_or_null: ToolUseId | null

    payload: object
    snapshot: RuntimeSnapshot


# =====================================================
# 3. Runtime snapshot
# =====================================================

struct RuntimeSnapshot:
    run_id: RunId
    main_session_id: MainSessionId
    current_phase: string

    semantic: SemanticState
    scope: ScopeState
    route: RouteState
    lifecycle: LifecycleState
    bindings: BindingIndex
    allowed_actions: list[string]
    allowed_routes: list[string]
    integrity: IntegrityState
    last_bridge_result: BridgeResult | null
    phase_exit_readiness: PhaseExitReadiness

struct SemanticState:
    frozen: object | null
    frozen_at: Timestamp | null
    requires_refresh: bool

struct ScopeState:
    frozen: object | null
    frozen_at: Timestamp | null
    requires_refresh: bool

struct RouteState:
    current_route: list[string]
    target_phase: string | null
    is_stale: bool
    decided_by_event_id: EventId | null

struct LifecycleState:
    # keyed by bridge_window_id/team_id/task_id as needed
    status_index: map[string,string]
    last_event_index: map[string,EventId]
    open_bridge_window_ids: list[BridgeWindowId]
    orphan_candidate_ids: list[BridgeWindowId]

struct BindingIndex:
    bridge_windows: map[BridgeWindowId,BridgeWindowBinding]
    teams: map[TeamId,TeamBinding]
    tasks: map[TaskId,TaskBinding]
    tool_uses: map[ToolUseId,ToolUseBinding]

struct IntegrityState:
    has_hard_stop: bool
    awaiting_approval: bool
    open_alerts: list[IntegrityAlert]

struct IntegrityAlert:
    level: NotifyLevel
    category: string
    message: string
    related_ids: map[string,string]

struct PhaseExitReadiness:
    current_phase: string
    exit_ready: bool
    blocking_event_ids: list[EventId]
    blocking_task_ids: list[TaskId]
    changed_recently: bool


# =====================================================
# 4. Bindings
# =====================================================

struct BridgeWindowBinding:
    run_id: RunId
    main_session_id: MainSessionId
    sub_session_id: SubSessionId
    bridge_window_id: BridgeWindowId
    parent_tool_use_id: ToolUseId

    opened_by_agent_id: AgentId
    opened_by_agent_type: "main-leader"
    bridge_leader_id_or_null: AgentId | null

    team_id_or_null: TeamId | null
    task_id_or_null: TaskId | null
    lifecycle_status: string

    created_at: Timestamp
    updated_at: Timestamp
    closed_at: Timestamp | null

struct TeamBinding:
    run_id: RunId
    sub_session_id: SubSessionId
    bridge_window_id: BridgeWindowId
    team_id: TeamId
    team_name: string
    teammate_ids: list[TeammateId]
    owner_agent_id: AgentId
    owner_agent_type: "bridge-leader"

struct TaskBinding:
    run_id: RunId
    sub_session_id: SubSessionId
    bridge_window_id: BridgeWindowId
    team_id: TeamId
    task_id: TaskId
    owner_agent_id: AgentId
    owner_agent_type: "bridge-leader"

struct ToolUseBinding:
    run_id: RunId
    sub_session_id: SubSessionId | null
    bridge_window_id: BridgeWindowId | null
    team_id: TeamId | null
    task_id: TaskId | null
    tool_use_id: ToolUseId
    tool_name: string
    agent_id: AgentId
    agent_type: AgentType


# =====================================================
# 5. Bridge packet and task/team specs
# =====================================================

struct BridgePacket:
    schema_version: "0.1"
    binding: BridgeWindowBinding

    frozen_semantics: object
    frozen_scope: object
    phase_route: list[string]
    target_phase: string

    team_spec: TeamSpec
    team_planning: TeamPlanningDecision | null
    task_spec: TaskSpec
    task_team_mapping: TaskTeamMapping

    completion_contract: CompletionContract
    report_contract: ReportContract

    allowed_actions: list[string]
    allowed_tools: list[string]
    approval_requirements: list[ApprovalRequirement]

    created_at: Timestamp
    expires_at: Timestamp | null

struct TeamSpec:
    team_id_or_null: TeamId | null
    team_name: string
    teammate_specs: list[TeammateSpec]
    ownership_boundary: OwnershipBoundary
    team_planning: TeamPlanningDecision | null

struct TeammateSpec:
    teammate_id_or_null: TeammateId | null
    teammate_name: string
    role: string
    allowed_tools: list[string]
    responsibilities: list[string]

struct TaskSpec:
    task_id_or_null: TaskId | null
    task_subject: string
    task_description: string
    task_kind: string
    target_phase: string
    completion_contract: CompletionContract
    report_contract: ReportContract

struct TaskTeamMapping:
    task_id_or_null: TaskId | null
    team_id_or_null: TeamId | null
    teammate_assignments: list[TeammateAssignment]

struct TeammateAssignment:
    teammate_id_or_null: TeammateId | null
    assignment: string
    expected_output: string

struct OwnershipBoundary:
    readable_scopes: list[string]
    writable_scopes: list[string]
    process_ownership_rules: list[string]
    forbidden_actions: list[string]

struct ApprovalRequirement:
    category: string
    required: bool
    reason: string


# =====================================================
# 6. Contracts
# =====================================================

struct CompletionContract:
    required_outputs: list[string]
    required_artifacts: list[string]
    validation_requirements: list[string]
    success_criteria: list[string]
    allowed_partial_result: bool
    timeout_policy: TimeoutPolicy | null

struct ReportContract:
    required_sections: list[string]
    required_evidence: list[string]
    artifact_reporting_format: string
    include_failure_reason: bool
    include_next_action_recommendation: bool

struct TimeoutPolicy:
    heartbeat_interval_seconds: int
    soft_timeout_seconds: int
    hard_timeout_seconds: int
    timeout_action: "continue_waiting" | "collect_partial" | "fail_task" | "ask_main_leader"

struct CompletionChecks:
    required_outputs_present: bool
    required_artifacts_present: bool
    validation_passed: bool
    missing_outputs: list[string]
    missing_artifacts: list[string]
    failed_validations: list[string]
    notes: list[string]


# =====================================================
# 7. Long-running work / TeamIdle
# =====================================================

struct OwnedProcessRef:
    process_ref: string
    launched_by_agent_id: AgentId
    launched_at: Timestamp
    command_or_job_name: string
    working_directory: string | null
    expected_outputs: list[string]
    last_observed_status: string

struct ArtifactProbe:
    probe_name: string
    paths_or_refs: list[string]
    expected_presence: list[string]
    last_probe_at: Timestamp | null
    last_probe_result: string | null

struct TeamIdlePayload:
    run_id: RunId
    main_session_id: MainSessionId
    sub_session_id: SubSessionId
    bridge_window_id: BridgeWindowId
    team_id: TeamId
    task_id: TaskId

    wait_reason: string
    owned_process_refs: list[OwnedProcessRef]
    last_heartbeat_at: Timestamp
    timeout_policy: TimeoutPolicy
    artifact_probe: ArtifactProbe
    partial_reports: list[object]
    partial_artifact_refs: list[ArtifactRef]


# =====================================================
# 8. Bridge result
# =====================================================

struct BridgeResult:
    run_id: RunId
    main_session_id: MainSessionId
    sub_session_id: SubSessionId
    bridge_window_id: BridgeWindowId
    team_id_or_null: TeamId | null
    task_id_or_null: TaskId | null

    status: BridgeResultStatus
    failure_stage_or_null: FailureStage | null

    reports: list[object]
    artifact_refs: list[ArtifactRef]
    evidence: object | null
    error_or_null: object | null
    cleanup_required: bool
    returned_at: Timestamp


# =====================================================
# 9. Check/update/notify results
# =====================================================

struct CheckResult:
    ok: bool
    decision: CheckDecision
    code: string
    reasons: list[string]
    normalized_payload: object
    derived_facts: object
    audit_ref: string

struct TransitionRecord:
    transition_id: string
    type: string
    run_id: RunId
    main_session_id: MainSessionId | null
    sub_session_id: SubSessionId | null
    bridge_window_id: BridgeWindowId | null
    team_id: TeamId | null
    task_id: TaskId | null
    from_status: string | null
    to_status: string | null
    based_on_event: EventId
    payload: object
    timestamp: Timestamp

struct UpdateResult:
    ok: bool
    decision: UpdateDecision
    transition_ids: list[string]
    new_snapshot_ref: string | null
    changed_fields: list[string]
    audit_ref: string

struct NotifyItem:
    level: NotifyLevel
    category: string
    message: string
    related_ids: map[string,string]
    recommended_action_or_null: string | null

struct NotifyResult:
    ok: bool
    notify_items: list[NotifyItem]
    main_leader_inbox_ref: string
    audit_ref: string
