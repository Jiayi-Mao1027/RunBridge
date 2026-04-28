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

    if ctx.bridge_window_id_or_null is not null:
        if not validate.bridge_window_binding(
            run_id=ctx.run_id,
            main_session_id=ctx.main_session_id,
            sub_session_id=ctx.sub_session_id_or_null,
            bridge_window_id=ctx.bridge_window_id_or_null
        ):
            reasons.append("bridge_window_id_not_bound_to_session_chain")

    if ctx.team_id_or_null is not null:
        if not validate.team_binding(
            run_id=ctx.run_id,
            sub_session_id=ctx.sub_session_id_or_null,
            bridge_window_id=ctx.bridge_window_id_or_null,
            team_id=ctx.team_id_or_null
        ):
            reasons.append("team_id_not_bound_to_bridge_window")

    if ctx.task_id_or_null is not null:
        if not validate.task_binding(ctx.run_id, ctx.task_id_or_null):
            reasons.append("task_id_not_bound_to_run")

        if ctx.bridge_window_id_or_null is not null:
            if not validate.task_bridge_window_binding(
                run_id=ctx.run_id,
                bridge_window_id=ctx.bridge_window_id_or_null,
                task_id=ctx.task_id_or_null
            ):
                reasons.append("task_id_not_bound_to_bridge_window")

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
    current_phase = snapshot.current_phase
    allowed_actions = snapshot.allowed_actions
    allowed_routes = snapshot.allowed_routes

    if ctx.event_kind is not null:
        if not policy.lifecycle_transition_allowed(
            current_lifecycle_status=snapshot.lifecycle.status_for(ctx.bridge_window_id_or_null, ctx.team_id_or_null, ctx.task_id_or_null),
            event_kind=ctx.event_kind,
            actor=ctx.agent_type,
            required_ids={
                "run_id": ctx.run_id,
                "main_session_id": ctx.main_session_id,
                "sub_session_id": ctx.sub_session_id_or_null,
                "bridge_window_id": ctx.bridge_window_id_or_null,
                "team_id": ctx.team_id_or_null,
                "task_id": ctx.task_id_or_null,
                "tool_use_id": ctx.tool_use_id_or_null
            }
        ):
            reasons.append("lifecycle_transition_not_allowed")

    if check_kind == "pre_call_bridge_sdk":
        if "call_bridge_sdk" not in allowed_actions:
            reasons.append("bridge_call_not_allowed_in_current_phase")

        if snapshot.integrity.has_hard_stop:
            reasons.append("hard_stop_blocks_bridge_call")

        if snapshot.integrity.awaiting_approval:
            reasons.append("approval_pending_blocks_bridge_call")

        if not packet.valid_bridge_packet_shape(normalized_payload):
            reasons.append("bridge_packet_schema_invalid")

        if not packet.has_exactly_one_team_and_one_task(normalized_payload):
            reasons.append("bridge_packet_must_bind_exactly_one_team_and_one_task")

        if not packet.binding_matches_context(normalized_payload.binding, ctx):
            reasons.append("bridge_packet_binding_mismatch")

        if not packet.route_allowed(
            phase_route=normalized_payload.phase_route,
            target_phase=normalized_payload.target_phase,
            allowed_routes=allowed_routes
        ):
            reasons.append("bridge_packet_route_not_allowed")

        if not semantic.same_frozen_semantics(
            payload_semantics=normalized_payload.frozen_semantics,
            snapshot_semantics=snapshot.semantic.frozen
        ):
            reasons.append("bridge_packet_frozen_semantics_mismatch")

    if check_kind == "team_create":
        if ctx.agent_type != "bridge-leader":
            reasons.append("only_bridge_leader_may_create_team")

        if not validate.one_team_per_bridge_window(
            run_id=ctx.run_id,
            bridge_window_id=ctx.bridge_window_id_or_null
        ):
            reasons.append("bridge_window_already_has_team")

        if not policy.team_spec_allowed(normalized_payload.team_spec, snapshot):
            reasons.append("team_spec_not_allowed")

    if check_kind == "task_create":
        if ctx.agent_type != "bridge-leader":
            reasons.append("only_bridge_leader_may_create_task")

        if not validate.one_task_per_bridge_window(
            run_id=ctx.run_id,
            bridge_window_id=ctx.bridge_window_id_or_null
        ):
            reasons.append("bridge_window_already_has_task")

        if not policy.task_team_mapping_allowed(normalized_payload, snapshot):
            reasons.append("task_team_mapping_not_allowed")
        if not policy.task_kind_allowed_in_phase(normalized_payload.task_kind, current_phase):
            reasons.append("task_kind_not_allowed_in_current_phase")

        if not validate.task_team_mapping_is_singular(normalized_payload.task_team_mapping):
            reasons.append("task_team_mapping_not_singular")

        if not validate.task_team_mapping_matches_packet(
            mapping=normalized_payload.task_team_mapping,
            packet_ref=normalized_payload.packet_ref_or_inline_packet
        ):
            reasons.append("task_team_mapping_does_not_match_packet")

    if check_kind == "send_message":
        if ctx.agent_type != "bridge-leader":
            reasons.append("only_bridge_leader_may_send_teammate_messages")

        if not validate.message_targets_teammates_in_team(
            team_id=ctx.team_id_or_null,
            payload=normalized_payload
        ):
            reasons.append("message_target_not_in_bridge_team")

        if not policy.message_allowed_by_packet_boundary(normalized_payload, snapshot):
            reasons.append("message_not_allowed_by_packet_boundary")

    if check_kind == "task_complete":
        if ctx.agent_type != "bridge-leader":
            reasons.append("only_bridge_leader_may_complete_task")

        if not policy.task_completion_allowed(ctx.task_id_or_null, snapshot):
            reasons.append("task_not_completion_eligible")

        if not validate.completion_evidence_bound_to_task_and_team(
            run_id=ctx.run_id,
            bridge_window_id=ctx.bridge_window_id_or_null,
            team_id=ctx.team_id_or_null,
            task_id=ctx.task_id_or_null,
            payload=normalized_payload
        ):
            reasons.append("completion_evidence_binding_invalid")

    if check_kind == "team_delete":
        if ctx.agent_type != "bridge-leader":
            reasons.append("only_bridge_leader_may_delete_team")

        if not validate.team_can_be_deleted_without_losing_task_evidence(
            team_id=ctx.team_id_or_null,
            snapshot=snapshot
        ):
            reasons.append("team_delete_would_drop_required_evidence")

    if check_kind == "team_idle":
        if not validate.team_idle_payload_has_wait_contract(normalized_payload):
            reasons.append("team_idle_payload_missing_wait_contract")

        if not validate.owned_process_refs_bound_to_task(
            task_id=ctx.task_id_or_null,
            owned_process_refs=normalized_payload.owned_process_refs
        ):
            reasons.append("owned_process_refs_not_bound_to_task")

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
