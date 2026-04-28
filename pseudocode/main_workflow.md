START MAIN_SESSION(main_session_id, run_id)

# =====================================================
# 0. Main leader reads current truth and current notify
# =====================================================
main-leader.read_runtime_snapshot(run_id)
main-leader.accept_user_instruction_if_new(user_input)
Userpromptsubmit(hook).phase():
    if phase is null
        phase.init
    else
        phase.update()
    phase.notify()
main-leader.skim_project_if_needed()
main-leader.freeze_semantics_if_needed()

#frozen_semantic refers to the accurate undestanding of the user input based on the current project,rather than phase.

#we use phase as an living state who could always be modified by hooks.the action after phase. refers to the actions that was taken to phase.
#phase is a fine-grained runtime trace,not only a final result marker.
#phase should record action intent/action start/action end/action failure where the event matters.
#for example,even if call_bridge_sdk fails,the phase history should still show:
#   1. main-leader intended to open a bridge window
#   2. pretooluse allowed or denied that attempt
#   3. tool call started or was about to start
#   4. no successful bridge-window-end update exists
#   5. the corresponding error/failure event exists
#this makes later audit able to distinguish "never attempted","attempted but failed",
#"started but did not finish",and "finished with result".


WHILE main-session active:

    # =====================================================
    # 1. Main leader decides route, not always detailed plan
    # =====================================================

    If main-leader.check_not_need_l2():
        main-leader.simple_plan()

    phase.route.update(main-leader.decide_possible_phase_route_from(frozen_semantics))
        # complex plan belongs to l2, not to bridge-leader
        # l2 is part of the route decided by main-leader.
        # if l2 is needed,main-leader still does not directly call l2 teammates.
        # main-leader builds an l2 bridge packet and invokes bridge through mcp/cc sdk.
        # bridge-leader then creates the l2 team and task inside that bridge window.

    # =====================================================
    # 2. For THIS bridge invocation only, create a new window
    # =====================================================

    IF current next step requires one bridge call:
        #sub_session_id and main_session_id all refers to the session_id ,the built-in object in claude code.but for semantic reasons,we mark it like that.
        sub_session_id = generate_sub_session_id()
        bridge_window_id = generate_bridge_window_id(run_id, main_session_id, sub_session_id)
        # semantic meaning:
        # this sub_session_id names one bridge invocation window
        # bridge_window_id is the runtime-owned durable id for that same invocation window.
        # sub_session_id is tied to the claude-code/sdk session concept.
        # bridge_window_id is tied to state-machine audit and recovery.

        #packet refers to the relavent information pack that main-leader transits to the bridge-leader

        packet = main-leader.build_bridge_instruction_packet_for_this_invoke(
            run_id,
            main_session_id,
            sub_session_id,
            bridge_window_id,
            phase,
            frozen_semantics,
            task_spec_for_this_one_bridge_window,
            team_spec_for_this_one_bridge_window,
            task_team_mapping_for_this_one_bridge_window,
            completion_contract,
            report_contract
        )

        # packet is NOT global for the whole run.
        # it is rebuilt before each bridge call.
        # one bridge invocation window binds exactly one team and one task.
        # that task may have multiple teammates/assignments inside the team,
        # but the window-level task identity is singular.
        # task_team_mapping therefore maps the one task to the one team and its teammates,
        # rather than describing multiple independent tasks in the same bridge window.

    # =================================================
    # 3. PreToolUse for main-leader before call_bridge_sdk
    # =================================================

    EVENT: main-leader about to use tool "call_bridge_sdk"

    Pretooluse.call_bridge_sdk(agent_id,agent_type,tool_input, tool_name,tool_use_id)
        if agent_type is main-leader and tool_name is call_bridge_sdk
            check.id(tool_input.main_session_id,tool_input,tool_use_id)
            check.packet(tool_input.packet,tool_input.notify)
            phase.update(tool_input.packet,tool_input.notify)
            # this update records bridge-call intent/pre-dispatch state.
            # it does not mean the bridge window finished successfully.
            # success/failure/end state must be recorded by later posttooluse/bridge-result events.
        else if agent_type is bridge-leader      #not evoked now
            if tool_name is team_create
                check.id(tool_input.main_session_id,tool_input,tool_use_id)
                check.packet(tool_input.packet.teammates)
                phase.update(tool_input.packet)
            else if tool_name is task_create
                check.id(tool_input.main_session_id,tool_input,tool_use_id)
                check.packet(tool_input.packet)
                phase.update(tool_input.packet)
            else if tool_name is send_messages
                check.id(tool_input.main_session_id,tool_input,tool_use_id)
                check.packet(tool_input.packet)
                phase.update(tool_input.packet)
            else if tool_name is team_delete
                check.id(tool_input.main_session_id,tool_input,tool_use_id)
                check.packet(tool_input.packet)
                phase.update(tool_input.packet)
    

    # =================================================
    # 4. Main leader performs the only active dispatch:
    #    invoke one bridge session
    # =================================================

    bridge_result = main-leader.call_bridge_sdk(packet)
    # main-leader's only active downstream dispatch is this sdk/mcp bridge call.
    # main-leader never directly creates team,task,or teammate messages.
    # if this call fails,phase should retain the attempted call and error,
    # and absence of a normal bridge-window completion update is meaningful.

    # =================================================
    # 5. Bridge session begins
    # =================================================
    #the task,team,and bridge are highly bound together.
    #one bridge window runs exactly one team and one task.
    #the team may contain multiple teammates,but bridge-leader owns their lifecycle.

    bridge-leader.accept_instruction_packet(packet)
    #bridge-leader is about to launch a team according to the task

    Pretooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id)

    bridge-leader.team_create()
    # bridge-leader creates the team according to the packet.
    # team_create start/post events update team lifecycle fields,
    # not the same fields as task_create or taskcompleted hooks.

    Posttooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)
        if agent_type is main-leader and tool_name is call_bridge_sdk   #not evoked now
            check.id(tool_input.main_session_id,tool_input.tool_use_id)
            check.packet(tool_input.packet,tool_input.notify)
            phase.update(tool_input.packet,tool_input.notify)
            phase.notify(tool_input.packet,tool_input.notify)
        else if agent_type is bridge-leader   
            if tool_name is team_create
                check.id(tool_input.main_session_id,tool_input.tool_use_id)
                check.packet(tool_input.packet.teammates)
                phase.update(tool_input.packet)
            else if tool_name is task_create
                check.id(tool_input.main_session_id,tool_input.tool_use_id)
                check.packet(tool_input.packet)
                phase.update(tool_input.packet)
            else if tool_name is send_messages
                check.id(tool_input.main_session_id,tool_input.tool_use_id)
                check.packet(tool_input.packet)
                phase.update(tool_input.packet)
            else if tool_name is team_delete
                check.id(tool_input.main_session_id,tool_input.tool_use_id)
                check.packet(tool_input.packet)
                phase.update(tool_input.packet)

    
    # ---------------------------------------------
    # 5A. PreToolUse before task creation activity
    # ---------------------------------------------
    EVENT: bridge-leader about to create task,bridge-leader own the task

    Pretooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id)
    
    # ---------------------------------------------
    # 5B. Bridge-leader emits task object
    #     This triggers TaskCreated hook
    # ---------------------------------------------
    bridge-leader.task_create(tool_input.packet)
    # task_create tool activity and Taskcreated hook record different facts.
    # Posttooluse(task_create) records the tool invocation outcome.
    # Taskcreated records the authoritative task identity/description/team binding.
    # both may update phase,but they must write distinguishable event kinds.

    Posttooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)

    Taskcreated(task_id,task_subject,task_description,teammate_name,team_name,sub_session_id):
        check.id(task_id,session_id)
        check.description(task_description,phase)
        check.teammates(teammate_name,team_name,phase)
    
    #bridge-leader is about to send messages

    Pretooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id)

    bridge-leader.send_message()
    # bridge-leader sends task instructions/messages to teammates.
    # this is still inside the bridge window;main-leader does not contact teammates directly.

    Posttooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)

    task.run()
        teammates.work()
        teammates.use_minimal_tools()
        teammates.report()

    TeamIdle(hook).check_completions()
    # TeamIdle is mainly a long-running-task waiting/suspension signal.
    # For GPU-heavy work such as DPO training,the team may be idle from the orchestrator view
    # while an owned process continues running or waiting for results.
    # TeamIdle should not by itself mean task completion.
    # It should help bridge-leader decide whether to wait,resume polling,collect artifacts,
    # or mark partial/blocked/failed according to the completion contract.
    # ---------------------------------------------
    # 5G. Completion path
    # ---------------------------------------------
    bridge-leader.collect_report_and_artifacts(task_id)
    bridge-leader.task_complete()
    # bridge-leader decides task completion after collecting report/artifacts.
    # Taskcompleted hook records completion facts and completion-contract evidence.

    Taskcompleted(task_id,task_subject,task_description,teammate_name,team_name,sub_session_id):
        check.id(task_id,session_id)
        check.description(task_description,phase)
        check.teammates(teammate_name,team_name,phase)


    EVENT: bridge-leader about to delete the team


    Pretooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id)

    bridge-leader.team_delete()
    # team_delete records closure of the bridge-owned team.
    # team closure is separate from task completion and bridge window return.

    Posttooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)

    # =================================================
    # 6. End of this one bridge window
    # =================================================

    bridge-leader.aggregate_window_result(sub_session_id, task_id, reports)
    bridge-leader.return_bridge_result_to_main()
    # this is the normal bridge-window return path.
    # if any earlier start event lacks its matching end event,
    # audit can detect unfinished sdk/team/task/message lifecycle segments.

    # =====================================================
    # 7. Main leader resumes from runtime truth
    # =====================================================

    Posttooluse.call_bridge_sdk(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)
    #phase first

    if main-leader.decide_if_need_report():
        main-leader.report()


    # =====================================================
    # 8. Failure / recovery path
    # =====================================================
    # failure is recorded as part of the same lifecycle.
    # a failed action must not erase its earlier intent/start/check events.
    # every meaningful lifecycle segment should end with one of:
    #   success/end
    #   failed
    #   denied/blocked
    #   partial
    #   orphaned
    # this makes audit and recovery possible from phase history.

    IF Pretooluse denies call_bridge_sdk:
        phase.update(event="bridge_call_denied", run_id, main_session_id, sub_session_id, tool_use_id, reasons)
        phase.notify(level="blocking", category="bridge_call_denied")
        main-leader.read_runtime_snapshot(run_id)
        main-leader.replan_or_report_blocked()
        CONTINUE WHILE

    IF call_bridge_sdk raises error OR returns no bridge_result:
        phase.update(event="bridge_call_failed", run_id, main_session_id, sub_session_id, tool_use_id, error)
        phase.notify(level="error", category="bridge_call_failed")
        main-leader.read_runtime_snapshot(run_id)
        main-leader.decide_retry_or_report()
        CONTINUE WHILE

    IF bridge-leader rejects packet:
        phase.update(event="bridge_packet_rejected", run_id, main_session_id, sub_session_id, packet_ref, reasons)
        bridge-leader.return_bridge_result_to_main(
            status="rejected",
            failure_stage="packet_accept",
            reasons=reasons
        )
        main-leader.read_runtime_snapshot(run_id)
        main-leader.rebuild_packet_or_report()
        CONTINUE WHILE

    IF bridge-leader process/window starts but never returns:
        phase.update(event="bridge_window_orphaned", run_id, main_session_id, sub_session_id, last_known_event_ref)
        phase.notify(level="blocking", category="bridge_window_orphaned")
        main-leader.read_runtime_snapshot(run_id)
        main-leader.decide_recover_retry_or_report()
        CONTINUE WHILE

    IF team_create fails:
        phase.update(event="team_create_failed", run_id, sub_session_id, team_id_or_null, tool_use_id, error)
        bridge-leader.return_bridge_result_to_main(
            status="failed",
            failure_stage="team_create",
            error=error
        )
        main-leader.read_runtime_snapshot(run_id)
        CONTINUE WHILE

    IF task_create fails OR Taskcreated check denies:
        phase.update(event="task_create_failed", run_id, sub_session_id, team_id_or_null, task_id_or_null, reasons)
        bridge-leader.delete_team_if_created()
        bridge-leader.return_bridge_result_to_main(
            status="failed",
            failure_stage="task_create",
            reasons=reasons
        )
        main-leader.read_runtime_snapshot(run_id)
        CONTINUE WHILE

    IF send_message fails:
        phase.update(event="message_dispatch_failed", run_id, sub_session_id, team_id, task_id, tool_use_id, error)
        bridge-leader.decide_retry_send_or_fail_task()
        # retrying send_message stays inside the same bridge window.
        # failing the task should still preserve team/task creation history.

    IF TeamIdle indicates still waiting:
        phase.update(event="team_waiting", run_id, sub_session_id, team_id, task_id, wait_reason, owned_process_refs)
        bridge-leader.keep_window_pending_or_suspend_polling()
        # this is normal for long GPU jobs.
        # no task completion should be inferred from waiting alone.

    IF TeamIdle timeout OR owned process lost:
        phase.update(event="team_wait_timeout", run_id, sub_session_id, team_id, task_id, owned_process_refs, evidence)
        bridge-leader.collect_partial_evidence()
        bridge-leader.mark_task_partial_or_failed()
        bridge-leader.return_bridge_result_to_main(
            status="partial_or_failed",
            failure_stage="team_wait",
            evidence=evidence
        )
        main-leader.read_runtime_snapshot(run_id)
        CONTINUE WHILE

    IF task completion contract not satisfied:
        phase.update(event="task_completion_rejected", run_id, sub_session_id, team_id, task_id, missing_contract_items)
        bridge-leader.decide_wait_retry_followup_or_fail()
        # completion rejection does not automatically delete the team.
        # bridge-leader may keep waiting,retry collection,or fail the task.

    IF team_delete fails:
        phase.update(event="team_delete_failed", run_id, sub_session_id, team_id, task_id, error)
        bridge-leader.return_bridge_result_to_main(
            status="partial",
            cleanup_required=true,
            failure_stage="team_delete",
            error=error
        )
        main-leader.read_runtime_snapshot(run_id)
        main-leader.decide_cleanup_followup_or_report()
        CONTINUE WHILE

    IF bridge_result.status is "partial":
        phase.update(event="bridge_window_partial_return", run_id, main_session_id, sub_session_id, bridge_result)
        phase.notify(level="warn", category="partial_bridge_result")
        main-leader.read_runtime_snapshot(run_id)
        main-leader.decide_retry_followup_or_report()
        CONTINUE WHILE


END WHILE
END MAIN_SESSION
