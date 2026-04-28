function update(ctx: RuntimeContext, update_kind, checked_result: CheckResult) -> UpdateResult:
    audit_id = audit.start(
        kind="update",
        run_id=ctx.run_id,
        main_session_id=ctx.main_session_id,
        sub_session_id=ctx.sub_session_id_or_null,
        event_id=ctx.event_id,
        update_kind=update_kind,
        actor=ctx.agent_type
    )

    failure_update_kinds = {
        "record_bridge_call_denied",
        "record_bridge_call_failed",
        "persist_bridge_packet_rejected",
        "persist_team_create_failed",
        "persist_task_create_failed",
        "persist_message_dispatch_failed",
        "persist_team_wait_timeout",
        "persist_task_completion_rejected",
        "persist_task_failed",
        "persist_team_delete_failed",
        "persist_bridge_window_orphaned"
    }

    # denied/failed events are still real lifecycle facts.
    # they must be persisted when the update_kind is explicitly a failure/deny update.
    if checked_result.decision != "allow" and update_kind not in failure_update_kinds:
        runtime.append_update_record(
            run_id=ctx.run_id,
            event_id=ctx.event_id,
            update_kind=update_kind,
            decision="rejected",
            reason="check_not_allowed",
            based_on_check=checked_result.audit_ref
        )

        audit.finish(
            audit_id=audit_id,
            status="rejected",
            summary={"reason": "check_not_allowed"}
        )

        return UpdateResult(
            ok=False,
            decision="rejected",
            transition_ids=[],
            new_snapshot_ref=null,
            changed_fields=[],
            audit_ref=audit_id
        )

    snapshot_before = runtime.read_snapshot(ctx.run_id)
    transition_ids = []
    changed_fields = []

    # ----------------------------------------
    # 1. persist raw event first
    # ----------------------------------------
    event_ref = runtime.append_event_record(
        run_id=ctx.run_id,
        event_id=ctx.event_id,
        event_kind=ctx.event_kind,
        agent_type=ctx.agent_type,
        tool_name=ctx.tool_name_or_null,
        tool_use_id=ctx.tool_use_id_or_null,
        task_id=ctx.task_id_or_null,
        payload=checked_result.normalized_payload
    )

    # ----------------------------------------
    # 2. build authoritative transitions
    # ----------------------------------------
    transitions = []

    if update_kind == "record_bridge_call_intent":
        transitions.append(
            transition.build(
                type="bridge_call_intended",
                run_id=ctx.run_id,
                main_session_id=ctx.main_session_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                tool_use_id=ctx.tool_use_id_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "record_bridge_call_prechecked":
        transitions.append(
            transition.build(
                type="bridge_call_prechecked",
                run_id=ctx.run_id,
                main_session_id=ctx.main_session_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                tool_use_id=ctx.tool_use_id_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "record_bridge_call_denied":
        transitions.append(
            transition.build(
                type="bridge_call_denied",
                run_id=ctx.run_id,
                main_session_id=ctx.main_session_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                tool_use_id=ctx.tool_use_id_or_null,
                reasons=checked_result.reasons,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "record_bridge_call_failed":
        transitions.append(
            transition.build(
                type="bridge_call_failed",
                run_id=ctx.run_id,
                main_session_id=ctx.main_session_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                tool_use_id=ctx.tool_use_id_or_null,
                error=checked_result.normalized_payload.error_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "register_bridge_window_open":
        transitions.append(
            transition.build(
                type="bridge_window_opened",
                run_id=ctx.run_id,
                main_session_id=ctx.main_session_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_bridge_packet_rejected":
        transitions.append(
            transition.build(
                type="bridge_packet_rejected",
                run_id=ctx.run_id,
                main_session_id=ctx.main_session_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                reasons=checked_result.reasons,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_team_created":
        transitions.append(
            transition.build(
                type="team_create_completed",
                run_id=ctx.run_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_team_create_failed":
        transitions.append(
            transition.build(
                type="team_create_failed",
                run_id=ctx.run_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                error=checked_result.normalized_payload.error_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_task_created":
        transitions.append(
            transition.build(
                type="task_created_recorded",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_task_create_failed":
        transitions.append(
            transition.build(
                type="task_create_failed",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                reasons=checked_result.reasons,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_message_dispatched":
        transitions.append(
            transition.build(
                type="message_dispatch_completed",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                tool_use_id=ctx.tool_use_id_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_message_dispatch_failed":
        transitions.append(
            transition.build(
                type="message_dispatch_failed",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                tool_use_id=ctx.tool_use_id_or_null,
                error=checked_result.normalized_payload.error_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_team_waiting":
        transitions.append(
            transition.build(
                type="team_waiting",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                wait_reason=checked_result.normalized_payload.wait_reason,
                owned_process_refs=checked_result.normalized_payload.owned_process_refs,
                last_heartbeat_at=checked_result.normalized_payload.last_heartbeat_at,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_team_wait_timeout":
        transitions.append(
            transition.build(
                type="team_wait_timeout",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                evidence=checked_result.normalized_payload.evidence,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_task_completed":
        transitions.append(
            transition.build(
                type="task_completion_completed",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                artifact_refs=checked_result.normalized_payload.artifact_refs,
                reports=checked_result.normalized_payload.reports,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_task_completion_rejected":
        transitions.append(
            transition.build(
                type="task_completion_rejected",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                missing_contract_items=checked_result.normalized_payload.missing_contract_items,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_task_failed":
        transitions.append(
            transition.build(
                type="task_failed",
                run_id=ctx.run_id,
                task_id=ctx.task_id_or_null,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                evidence=checked_result.normalized_payload.evidence,
                error=checked_result.normalized_payload.error_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_team_deleted":
        transitions.append(
            transition.build(
                type="team_delete_completed",
                run_id=ctx.run_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_team_delete_failed":
        transitions.append(
            transition.build(
                type="team_delete_failed",
                run_id=ctx.run_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                cleanup_required=true,
                error=checked_result.normalized_payload.error_or_null,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_bridge_result_returned":
        transitions.append(
            transition.build(
                type=transition.resolve_bridge_result_type(checked_result.normalized_payload.bridge_result.status),
                # resolve_bridge_result_type:
                #   succeeded -> bridge_window_returned
                #   rejected  -> bridge_window_returned
                #   failed    -> bridge_window_failed
                #   partial   -> bridge_window_partial_returned
                #   partial_or_failed -> bridge_window_partial_returned
                #   orphaned  -> bridge_window_orphaned
                run_id=ctx.run_id,
                main_session_id=ctx.main_session_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                team_id=ctx.team_id_or_null,
                task_id=ctx.task_id_or_null,
                bridge_result=checked_result.normalized_payload.bridge_result,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "persist_bridge_window_orphaned":
        transitions.append(
            transition.build(
                type="bridge_window_orphaned",
                run_id=ctx.run_id,
                main_session_id=ctx.main_session_id,
                sub_session_id=ctx.sub_session_id_or_null,
                bridge_window_id=ctx.bridge_window_id_or_null,
                last_known_event_ref=checked_result.normalized_payload.last_known_event_ref,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "advance_phase":
        next_phase = policy.resolve_next_phase(snapshot_before, checked_result)
        transitions.append(
            transition.build(
                type="phase_advanced",
                run_id=ctx.run_id,
                old_phase=snapshot_before.phase.current,
                new_phase=next_phase,
                based_on_event=ctx.event_id
            )
        )

    else if update_kind == "reroute_phase":
        next_route = policy.resolve_next_route(snapshot_before, checked_result)
        transitions.append(
            transition.build(
                type="route_rerouted",
                run_id=ctx.run_id,
                old_route=snapshot_before.route.current,
                new_route=next_route,
                based_on_event=ctx.event_id
            )
        )

    else:
        transitions.append(
            transition.build(
                type="generic_runtime_update",
                run_id=ctx.run_id,
                based_on_event=ctx.event_id
            )
        )

    # ----------------------------------------
    # 3. persist transitions
    # ----------------------------------------
    for t in transitions:
        tid = runtime.append_transition_record(t)
        transition_ids.append(tid)

    # ----------------------------------------
    # 4. reconcile authoritative runtime truth
    # ----------------------------------------
    reconcile_result = runtime.reconcile(ctx.run_id)

    # ----------------------------------------
    # 5. compute changed fields
    # ----------------------------------------
    snapshot_after = runtime.read_snapshot(ctx.run_id)
    changed_fields = diff(snapshot_before, snapshot_after)

    # ----------------------------------------
    # 6. persist update ledger
    # ----------------------------------------
    runtime.append_update_record(
        run_id=ctx.run_id,
        event_id=ctx.event_id,
        update_kind=update_kind,
        decision="applied",
        based_on_check=checked_result.audit_ref,
        based_on_event=event_ref,
        transition_ids=transition_ids,
        reconcile_ref=reconcile_result.ref,
        changed_fields=changed_fields
    )

    audit.finish(
        audit_id=audit_id,
        status="ok",
        summary={
            "transition_count": len(transition_ids),
            "changed_fields": changed_fields,
            "snapshot_ref": snapshot_after.ref
        }
    )

    return UpdateResult(
        ok=True,
        decision="applied",
        transition_ids=transition_ids,
        new_snapshot_ref=snapshot_after.ref,
        changed_fields=changed_fields,
        audit_ref=audit_id
    )
