struct RuntimeContext:
    run_id
    main_session_id
    sub_session_id_or_null
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