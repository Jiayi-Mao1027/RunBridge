function notify(ctx: RuntimeContext, trigger, source_result_or_null) -> NotifyResult:
    # ----------------------------------------
    # 0. start audit
    # ----------------------------------------
    audit_id = audit.start(
        kind="notify",
        run_id=ctx.run_id,
        main_session_id=ctx.main_session_id,
        sub_session_id=ctx.sub_session_id_or_null,
        event_id=ctx.event_id,
        trigger=trigger,
        actor=ctx.agent_type
    )

    snapshot = runtime.read_snapshot(ctx.run_id)
    recent_events = runtime.read_recent_events(ctx.run_id, limit=50)
    recent_alerts = runtime.read_open_alerts(ctx.run_id)
    recent_checks = runtime.read_recent_check_results(ctx.run_id, limit=20)

    notify_items = []

    # ----------------------------------------
    # 1. source-result based notify
    # ----------------------------------------
    if source_result_or_null is not null:
        if source_result_or_null.type == "check_result":
            if source_result_or_null.decision == "deny":
                notify_items.append(
                    NotifyItem(
                        level="blocking",
                        category="policy_deny",
                        message=build_check_fail_message(source_result_or_null),
                        related_ids=collect_related_ids(ctx, source_result_or_null),
                        recommended_action_or_null="stop_current_action_and_replan"
                    )
                )
            else if source_result_or_null.decision == "needs_review":
                notify_items.append(
                    NotifyItem(
                        level="warn",
                        category="needs_review",
                        message=build_review_message(source_result_or_null),
                        related_ids=collect_related_ids(ctx, source_result_or_null),
                        recommended_action_or_null="wait_or_request_explicit_resolution"
                    )
                )

        else if source_result_or_null.type == "update_result":
            if source_result_or_null.decision == "applied":
                notify_items.append(
                    NotifyItem(
                        level="info",
                        category="state_updated",
                        message=build_update_message(source_result_or_null),
                        related_ids=collect_related_ids(ctx, source_result_or_null),
                        recommended_action_or_null=null
                    )
                )
            else if source_result_or_null.decision == "rejected":
                notify_items.append(
                    NotifyItem(
                        level="error",
                        category="update_rejected",
                        message=build_update_reject_message(source_result_or_null),
                        related_ids=collect_related_ids(ctx, source_result_or_null),
                        recommended_action_or_null="read_runtime_snapshot_and_recover"
                    )
                )

    # ----------------------------------------
    # 2. runtime-condition based notify
    # ----------------------------------------
    if snapshot.integrity.has_hard_stop:
        notify_items.append(
            NotifyItem(
                level="blocking",
                category="hard_stop",
                message="runtime is in hard_stop state",
                related_ids={"run_id": ctx.run_id},
                recommended_action_or_null="do_not_dispatch_any_new_bridge"
            )
        )

    if snapshot.integrity.awaiting_approval:
        notify_items.append(
            NotifyItem(
                level="blocking",
                category="approval_pending",
                message="approval pending blocks next external or scoped action",
                related_ids={"run_id": ctx.run_id},
                recommended_action_or_null="pause_execution_until_approval_resolved"
            )
        )

    if snapshot.route.is_stale:
        notify_items.append(
            NotifyItem(
                level="warn",
                category="route_stale",
                message="route view is stale and should be refreshed before next dispatch",
                related_ids={"run_id": ctx.run_id},
                recommended_action_or_null="recompute_possible_phase_route"
            )
        )

    if snapshot.phase_exit_readiness.changed_recently:
        notify_items.append(
            NotifyItem(
                level="info",
                category="phase_exit_readiness_changed",
                message="phase exit readiness updated",
                related_ids={"run_id": ctx.run_id},
                recommended_action_or_null="re-evaluate next step"
            )
        )

    # ----------------------------------------
    # 3. event-specific notify
    # ----------------------------------------
    if trigger == "userpromptsubmit":
        if snapshot.semantic.requires_refresh:
            notify_items.append(
                NotifyItem(
                    level="warn",
                    category="semantic_refresh_needed",
                    message="new user input may invalidate frozen semantics",
                    related_ids={"run_id": ctx.run_id},
                    recommended_action_or_null="refresh semantic understanding before dispatch"
                )
            )

    if trigger == "post_bridge_return":
        if snapshot.last_bridge_result.is_partial:
            notify_items.append(
                NotifyItem(
                    level="warn",
                    category="partial_bridge_result",
                    message="bridge returned partial result; not completion-ready",
                    related_ids={"run_id": ctx.run_id},
                    recommended_action_or_null="decide retry, followup task, or reroute"
                )
            )

    # ----------------------------------------
    # 4. dedupe + prioritize
    # ----------------------------------------
    notify_items = notify.deduplicate(notify_items)
    notify_items = notify.sort_by_priority(notify_items)

    # ----------------------------------------
    # 5. deliver to main leader inbox
    # ----------------------------------------
    inbox_ref = runtime.append_main_leader_inbox(
        run_id=ctx.run_id,
        main_session_id=ctx.main_session_id,
        items=notify_items
    )

    # ----------------------------------------
    # 6. audit end
    # ----------------------------------------
    audit.finish(
        audit_id=audit_id,
        status="ok",
        summary={
            "notify_count": len(notify_items),
            "inbox_ref": inbox_ref
        }
    )

    return NotifyResult(
        ok=True,
        notify_items=notify_items,
        main_leader_inbox_ref=inbox_ref,
        audit_ref=audit_id
    )