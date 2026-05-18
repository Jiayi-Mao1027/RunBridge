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


WHILE main-session active:

    # =====================================================
    # 1. Main leader decides route, not always detailed plan
    # =====================================================

    If main-leader.check_not_need_l2():
        main-leader.simple_plan()

    phase.route.update(main-leader.decide_possible_phase_route_from(frozen_semantics))
        # complex plan belongs to l2, not to bridge-leader

    # =====================================================
    # 2. For THIS bridge invocation only, create a new window
    # =====================================================

    IF current next step requires one bridge call:
        #sub_session_id and main_session_id all refers to the session_id ,the built-in object in claude code.but for semantic reasons,we mark it like that.
        sub_session_id = generate_sub_session_id()
        # semantic meaning:
        # this sub_session_id names one bridge invocation window

        #packet refers to the relavent information pack that main-leader transits to the bridge-leader

        packet = main-leader.build_bridge_instruction_packet_for_this_invoke(
            run_id,
            main_session_id,
            sub_session_id,
            phase,
            frozen_semantics,
            task_specs_for_this_one_bridge_window,
            team_specs_for_this_one_bridge_window,
            task_team_mapping_for_this_one_bridge_window,
            completion_contract,
            report_contract
        )

        # packet is NOT global for the whole run.
        # it is rebuilt before each bridge call.

    # =================================================
    # 3. PreToolUse for main-leader before call_bridge_sdk
    # =================================================

    EVENT: main-leader about to use tool "call_bridge_sdk"

    Pretooluse.call_bridge_sdk(agent_id,agent_type,tool_input, tool_name,tool_use_id)
        if agent_type is main-leader and tool_name is call_bridge_sdk
            check.id(tool_input.main_session_id,tool_input,tool_use_id)
            check.packets(tool_input.packets,tool_input.notify)
            phase.update(tool_input.packets,tool_input.notify)
        else if agent_type is bridge-leader      #not evoked now
            if tool_name is team_create
                check.id(tool_input.main_session_id,tool_input,tool_use_id)
                check.packets(tool_input.packets.teammate)
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

    # =================================================
    # 5. Bridge session begins
    # =================================================
    #the task and the bridge are highly bound together,with one bridge window running only one task

    bridge-leader.accept_instruction_packet(packet)
    #bridge-leader is about to launch a team according to the task

    Pretooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id)

    bridge-leader.team_create()

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

    Pretooluse(agent_id,agent_type,tool_input, tool_name,tool_use_i)
    
    # ---------------------------------------------
    # 5B. Bridge-leader emits task object
    #     This triggers TaskCreated hook
    # ---------------------------------------------
    bridge-leader.task_create(tool_input.packets)

    Posttooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)

    Taskcreated(task_id,task_subject,task_description,teammate_name,team_name,sub_session_id):
        check.id(task_id,session_id)
        check.description(task_description,phase)
        check.teammates(teammate_name,team_name,phase)
    
    #bridge-leader is about to send messages

    Pretooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id)

    bridge-leader.send_message()

    Posttooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)

    task.run()
        teammates.work()
        teammates.use_minimal_tools()
        teammates.report()

    TeamIdle(hook).check_completions()
    # ---------------------------------------------
    # 5G. Completion path
    # ---------------------------------------------
    bridge-leader.collect_report_and_artifacts(task_id)
    bridge-leader.task_complete()

    Taskcompleted(task_id,task_subject,task_description,teammate_name,team_name,sub_session_id):
        check.id(task_id,session_id)
        check.description(task_description,phase)
        check.teammates(teammate_name,team_name,phase)


    EVENT: bridge-leader about to delete the team


    Pretooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id)

    bridge-leader.team_delete()

    Posttooluse(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)

    # =================================================
    # 6. End of this one bridge window
    # =================================================

    bridge-leader.aggregate_window_result(sub_session_id, task_ids, reports)
    bridge-leader.return_bridge_result_to_main()

    # =====================================================
    # 7. Main leader resumes from runtime truth
    # =====================================================

    Posttooluse.call_bridge_sdk(agent_id,agent_type,tool_input, tool_name,tool_use_id,tool_response)
    #phase first

    if main-leader.decide_if_need_report():
        main-leader.report()



END WHILE
END MAIN_SESSION