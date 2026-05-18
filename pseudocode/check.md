function check(ctx: RuntimeContext, check_kind, target) -> CheckResult:
    audit_id = audit.start(
        kind="check",
        run_id=ctx.run_id,
        main_session_id=ctx.main_session_id,
        sub_session_id=ctx.sub_session_id_or_null,
        event_id=ctx.event_id,
        check_kind=check_kind,
        actor=ctx.agent_type
    )

    snapshot = runtime.read_snapshot(ctx.run_id)
    reasons = []
    derived_facts = {}
    normalized_payload = normalize(target)

    # ----------------------------------------
    # 1. identity checks
    # ----------------------------------------
    if not validate.run_id(ctx.run_id):
        reasons.append("invalid_run_id")

    if not validate.main_session_binding(ctx.run_id, ctx.main_session_id):
        reasons.append("main_session_id_not_bound_to_run")

    if ctx.sub_session_id_or_null is not null:
        if not validate.sub_session_binding(ctx.run_id, ctx.sub_session_id_or_null):
            reasons.append("sub_session_id_not_bound_to_run")

    if ctx.task_id_or_null is not null:
        if not validate.task_binding(ctx.run_id, ctx.task_id_or_null):
            reasons.append("task_id_not_bound_to_run")

    # ----------------------------------------
    # 2. schema checks
    # ----------------------------------------
    if not schema.validate(check_kind, normalized_payload):
        reasons.append("schema_invalid")

    # ----------------------------------------
    # 3. authority checks
    # ----------------------------------------
    if not authz.is_allowed_actor_for_check_target(
        agent_type=ctx.agent_type,
        check_kind=check_kind,
        tool_name=ctx.tool_name_or_null,
        payload=normalized_payload
    ):
        reasons.append("actor_not_authorized_for_target")

    # ----------------------------------------
    # 4. policy checks
    # ----------------------------------------
    current_phase = snapshot.phase.current
    allowed_actions = snapshot.allowed_actions
    allowed_routes = snapshot.allowed_routes

    if check_kind == "pre_call_bridge_sdk":
        if "call_bridge_sdk" not in allowed_actions:
            reasons.append("bridge_call_not_allowed_in_current_phase")

        if snapshot.integrity.has_hard_stop:
            reasons.append("hard_stop_blocks_bridge_call")

        if snapshot.integrity.awaiting_approval:
            reasons.append("approval_pending_blocks_bridge_call")

    if check_kind == "team_create":
        if not policy.team_spec_allowed(normalized_payload.team_spec, snapshot):
            reasons.append("team_spec_not_allowed")

    if check_kind == "task_create":
        if not policy.task_team_mapping_allowed(normalized_payload, snapshot):
            reasons.append("task_team_mapping_not_allowed")
        if not policy.task_kind_allowed_in_phase(normalized_payload.task_kind, current_phase):
            reasons.append("task_kind_not_allowed_in_current_phase")

    if check_kind == "task_complete":
        if not policy.task_completion_allowed(ctx.task_id_or_null, snapshot):
            reasons.append("task_not_completion_eligible")

    # ----------------------------------------
    # 5. semantic checks
    # ----------------------------------------
    if not semantic.aligned_with_frozen_semantics(
        payload=normalized_payload,
        frozen_semantics=snapshot.semantic.frozen,
        frozen_scope=snapshot.scope.frozen
    ):
        reasons.append("payload_not_aligned_with_frozen_semantics")

    # ----------------------------------------
    # 6. completion checks
    # ----------------------------------------
    if check_kind == "task_complete":
        contract = runtime.read_completion_contract(ctx.task_id_or_null)
        if not completion.satisfy_contract(normalized_payload, contract):
            reasons.append("completion_contract_not_satisfied")

    # ----------------------------------------
    # 7. derive verdict
    # ----------------------------------------
    if len(reasons) == 0:
        decision = "allow"
        code = "ok"
        ok = True
    else:
        if contains_blocking_reason(reasons):
            decision = "deny"
            code = "check_failed"
            ok = False
        else:
            decision = "needs_review"
            code = "check_ambiguous"
            ok = False

    derived_facts = derive_facts_from_check(
        ctx=ctx,
        check_kind=check_kind,
        snapshot=snapshot,
        payload=normalized_payload,
        reasons=reasons
    )

    # ----------------------------------------
    # 8. persist check ledger
    # ----------------------------------------
    runtime.append_check_record(
        run_id=ctx.run_id,
        event_id=ctx.event_id,
        check_kind=check_kind,
        actor=ctx.agent_type,
        input_ref=store_temp_payload(normalized_payload),
        decision=decision,
        code=code,
        reasons=reasons,
        derived_facts=derived_facts
    )

    audit.finish(
        audit_id=audit_id,
        status="ok",
        summary={
            "decision": decision,
            "code": code,
            "reason_count": len(reasons)
        }
    )

    return CheckResult(
        ok=ok,
        decision=decision,
        code=code,
        reasons=reasons,
        normalized_payload=normalized_payload,
        derived_facts=derived_facts,
        audit_ref=audit_id
    )