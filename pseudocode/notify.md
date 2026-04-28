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
        if snapshot.last_bridge_result is not null and snapshot.last_bridge_result.status in {"partial","partial_or_failed"}:
            notify_items.append(
                NotifyItem(
                    level="warn",
                    category="partial_bridge_result",
                    message="bridge returned partial result; not completion-ready",
                    related_ids={"run_id": ctx.run_id},
                    recommended_action_or_null="decide retry, followup task, or reroute"
                )
            )

        if snapshot.last_bridge_result is not null and snapshot.last_bridge_result.status == "failed":
            notify_items.append(
                NotifyItem(
                    level="error",
                    category="bridge_result_failed",
                    message="bridge returned failed result",
                    related_ids=collect_bridge_result_ids(snapshot.last_bridge_result),
                    recommended_action_or_null="read_runtime_snapshot_and_decide_retry_or_report"
                )
            )

        if snapshot.last_bridge_result is not null and snapshot.last_bridge_result.cleanup_required:
            notify_items.append(
                NotifyItem(
                    level="warn",
                    category="cleanup_required",
                    message="bridge returned with cleanup still required",
                    related_ids=collect_bridge_result_ids(snapshot.last_bridge_result),
                    recommended_action_or_null="schedule_cleanup_followup_or_report_cleanup_risk"
                )
            )

    if trigger == "bridge_call_failed":
        notify_items.append(
            NotifyItem(
                level="error",
                category="bridge_call_failed",
                message="main-leader bridge sdk call failed before normal bridge result",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="read_runtime_snapshot_and_decide_retry_or_report"
            )
        )

    if trigger == "bridge_window_orphaned":
        notify_items.append(
            NotifyItem(
                level="blocking",
                category="bridge_window_orphaned",
                message="bridge window has start/open events but no normal return before timeout",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="recover_or_mark_failed_before_dispatching_new_dependent_work"
            )
        )

    if trigger == "bridge_packet_rejected":
        notify_items.append(
            NotifyItem(
                level="error",
                category="bridge_packet_rejected",
                message="bridge-leader rejected the packet",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="rebuild_packet_from_runtime_truth_or_report_blocked"
            )
        )

    if trigger == "team_create_failed":
        notify_items.append(
            NotifyItem(
                level="error",
                category="team_create_failed",
                message="bridge-leader failed to create team",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="retry_bridge_window_or_report_failure"
            )
        )

    if trigger == "task_create_failed":
        notify_items.append(
            NotifyItem(
                level="error",
                category="task_create_failed",
                message="bridge-leader failed to create or record task",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="delete_team_if_created_then_rebuild_task_packet"
            )
        )

    if trigger == "message_dispatch_failed":
        notify_items.append(
            NotifyItem(
                level="warn",
                category="message_dispatch_failed",
                message="bridge-leader failed to dispatch teammate message",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="retry_send_or_fail_task_inside_same_bridge_window"
            )
        )

    if trigger == "team_waiting":
        notify_items.append(
            NotifyItem(
                level="info",
                category="team_waiting",
                message="team is waiting for long-running work or owned process results",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="continue_waiting_or_poll_according_to_timeout_policy"
            )
        )

    if trigger == "team_wait_timeout":
        notify_items.append(
            NotifyItem(
                level="error",
                category="team_wait_timeout",
                message="team wait timed out or owned process was lost",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="collect_partial_evidence_then_decide_retry_or_fail"
            )
        )

    if trigger == "task_completion_rejected":
        notify_items.append(
            NotifyItem(
                level="warn",
                category="task_completion_rejected",
                message="task completion contract is not satisfied",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="continue_waiting_retry_collection_or_fail_task"
            )
        )

    if trigger == "team_delete_failed":
        notify_items.append(
            NotifyItem(
                level="warn",
                category="team_delete_failed",
                message="team deletion failed after task/window work",
                related_ids=collect_related_ids(ctx, source_result_or_null),
                recommended_action_or_null="mark_cleanup_required_and_schedule_cleanup_followup"
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
