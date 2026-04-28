# Concrete typed data contracts live in schema.md.
# This file keeps the dispatch-facing summary and lifecycle rules.

struct RuntimeContext:
    run_id
    main_session_id
    sub_session_id_or_null
    bridge_window_id_or_null
    team_id_or_null
    agent_id
    agent_type
    event_id
    event_kind
    timestamp
    tool_name_or_null
    tool_use_id_or_null
    task_id_or_null
    payload
    snapshot

struct BridgeWindowBinding:
    # identity chain for audit and replay
    run_id
    main_session_id
    sub_session_id
    bridge_window_id
    parent_tool_use_id
    opened_by_agent_id
    opened_by_agent_type = "main-leader"

    # bridge-owned runtime objects
    bridge_leader_id_or_null
    team_id_or_null
    task_id_or_null

    # lifecycle status is fine-grained
    # examples:
    #   bridge_call_intended
    #   bridge_call_prechecked
    #   bridge_call_started
    #   bridge_call_denied
    #   bridge_call_failed
    #   bridge_window_opened
    #   bridge_window_returned
    #   bridge_window_partial_returned
    #   bridge_window_failed
    #   bridge_window_orphaned
    #   bridge_packet_rejected
    #   team_create_started
    #   team_create_completed
    #   team_create_failed
    #   task_create_started
    #   task_create_completed
    #   task_create_failed
    #   message_dispatch_started
    #   message_dispatch_completed
    #   message_dispatch_failed
    #   team_waiting
    #   team_wait_timeout
    #   task_completion_started
    #   task_completion_completed
    #   task_completion_rejected
    #   team_delete_started
    #   team_delete_completed
    #   team_delete_failed
    lifecycle_status

struct BridgeResult:
    run_id
    main_session_id
    sub_session_id
    bridge_window_id
    team_id_or_null
    task_id_or_null

    # status examples:
    #   succeeded
    #   rejected
    #   failed
    #   partial
    #   partial_or_failed
    #   orphaned
    status

    # failure_stage examples:
    #   packet_accept
    #   team_create
    #   task_create
    #   send_message
    #   team_wait
    #   task_complete
    #   team_delete
    #   bridge_return
    failure_stage_or_null

    reports
    artifact_refs
    evidence
    error_or_null
    cleanup_required

struct LifecycleTransitionRule:
    entity: "bridge_call" | "bridge_window" | "team" | "task" | "message" | "wait" | "cleanup"
    from_status
    event
    to_status
    actor_required
    required_ids
    terminal: bool

LifecycleTransitionTable:
    # main-leader side bridge call lifecycle
    bridge_call_intended --pretooluse_allowed_by_main_leader--> bridge_call_prechecked
    bridge_call_intended --pretooluse_denied_by_main_leader--> bridge_call_denied
    bridge_call_prechecked --call_bridge_sdk_started--> bridge_call_started
    bridge_call_started --call_bridge_sdk_error--> bridge_call_failed
    bridge_call_started --bridge_window_opened--> bridge_window_opened

    # bridge window packet acceptance
    bridge_window_opened --bridge_packet_accepted--> bridge_packet_accepted
    bridge_window_opened --bridge_packet_rejected--> bridge_packet_rejected
    bridge_packet_rejected --bridge_result_returned--> bridge_window_returned

    # team lifecycle,owned by bridge-leader
    bridge_packet_accepted --team_create_started--> team_create_started
    team_create_started --team_create_succeeded--> team_create_completed
    team_create_started --team_create_failed--> team_create_failed

    # task lifecycle,owned by bridge-leader
    team_create_completed --task_create_started--> task_create_started
    task_create_started --task_create_succeeded--> task_create_completed
    task_create_started --task_create_failed--> task_create_failed
    task_create_completed --taskcreated_hook_accepted--> task_created_recorded
    task_create_completed --taskcreated_hook_denied--> task_create_failed

    # message dispatch lifecycle
    task_created_recorded --message_dispatch_started--> message_dispatch_started
    message_dispatch_started --message_dispatch_succeeded--> message_dispatch_completed
    message_dispatch_started --message_dispatch_failed--> message_dispatch_failed
    message_dispatch_failed --message_dispatch_retry_started--> message_dispatch_started
    message_dispatch_failed --bridge_leader_fails_task--> task_failed

    # long-running wait lifecycle
    message_dispatch_completed --team_idle_waiting--> team_waiting
    team_waiting --team_idle_waiting--> team_waiting
    team_waiting --artifacts_ready--> task_completion_started
    team_waiting --wait_timeout_or_process_lost--> team_wait_timeout
    team_wait_timeout --partial_evidence_collected--> bridge_window_partial_returned
    team_wait_timeout --task_failed_by_bridge_leader--> task_failed

    # task completion lifecycle
    task_completion_started --completion_contract_satisfied--> task_completion_completed
    task_completion_started --completion_contract_rejected--> task_completion_rejected
    task_completion_rejected --continue_waiting--> team_waiting
    task_completion_rejected --retry_artifact_collection--> task_completion_started
    task_completion_rejected --bridge_leader_fails_task--> task_failed

    # cleanup and bridge return
    task_completion_completed --team_delete_started--> team_delete_started
    task_failed --team_delete_started--> team_delete_started
    bridge_window_partial_returned --team_delete_started--> team_delete_started
    team_delete_started --team_delete_succeeded--> team_delete_completed
    team_delete_started --team_delete_failed--> team_delete_failed
    team_delete_completed --bridge_result_returned--> bridge_window_returned
    team_delete_failed --bridge_result_returned_with_cleanup_required--> bridge_window_partial_returned

    # orphan detection
    bridge_call_started --orphan_timeout_without_bridge_return--> bridge_window_orphaned
    bridge_window_opened --orphan_timeout_without_bridge_return--> bridge_window_orphaned
    bridge_packet_accepted --orphan_timeout_without_bridge_return--> bridge_window_orphaned
    team_waiting --orphan_timeout_without_heartbeat--> bridge_window_orphaned

TerminalLifecycleStatuses:
    bridge_call_denied
    bridge_call_failed
    bridge_window_returned
    bridge_window_partial_returned
    bridge_window_failed
    bridge_window_orphaned

struct HookPayloadContract:
    hook_name
    required_common_fields:
        run_id
        main_session_id
        sub_session_id
        bridge_window_id
        agent_id
        agent_type
        event_id
        timestamp
    required_by_hook:
        PreToolUse:
            tool_name
            tool_use_id
            tool_input
            packet_or_packet_ref
        PostToolUse:
            tool_name
            tool_use_id
            tool_input
            tool_response
            status
            error_or_null
        TaskCreated:
            task_id
            task_subject
            task_description
            team_id
            teammate_ids
            task_spec
            team_spec
            task_team_mapping
        TaskCompleted:
            task_id
            team_id
            completion_contract
            completion_evidence
            reports
            artifact_refs
            completion_checks
        TeamIdle:
            team_id
            task_id
            wait_reason
            owned_process_refs
            last_heartbeat_at
            timeout_policy
            artifact_probe
            partial_reports
            partial_artifact_refs

struct BridgePacket:
    # packet is rebuilt for every bridge invocation.
    # it is not a global run contract.
    binding: BridgeWindowBinding

    # frozen execution meaning from main-leader.
    # bridge-leader may use it but must not silently redefine it.
    frozen_semantics
    frozen_scope

    # route chosen by main-leader.
    # l2/l3/l4 are route targets,but all downstream team/task creation
    # still happens inside bridge-leader.
    phase_route
    target_phase

    # exactly one team and one task per bridge window.
    team_spec
    task_spec
    task_team_mapping

    # contracts for ending the task and reporting back to main-leader.
    completion_contract
    report_contract

    # boundary of what bridge-leader is allowed to create/send/use
    # in this invocation window.
    allowed_actions
    allowed_tools
    approval_requirements

struct TaskSpec:
    task_id_or_null
    task_subject
    task_description
    task_kind
    target_phase
    completion_contract
    report_contract

struct TeamSpec:
    team_id_or_null
    team_name
    teammate_specs
    ownership_boundary

struct TaskTeamMapping:
    task_id_or_null
    team_id_or_null
    teammate_assignments
    # one task may be assigned to multiple teammates,
    # but this mapping must not describe multiple independent tasks.

struct CheckResult:
    ok: bool
    decision: "allow" | "deny" | "needs_review"
    code: str
    reasons: list[str]
    normalized_payload
    derived_facts
    audit_ref

struct UpdateResult:
    ok: bool
    decision: "applied" | "rejected" | "noop"
    transition_ids: list[str]
    new_snapshot_ref
    changed_fields: list[str]
    audit_ref

struct NotifyResult:
    ok: bool
    notify_items: list[NotifyItem]
    main_leader_inbox_ref
    audit_ref

struct NotifyItem:
    level: "info" | "warn" | "error" | "blocking"
    category: str
    message: str
    related_ids: dict
    recommended_action_or_null
