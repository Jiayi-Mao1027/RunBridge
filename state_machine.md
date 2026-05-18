FUNCTION reconcile_authoritative(state):
    run = deepcopy(run_ledger)
    tasks = deepcopy(task_ledgers)

    phase_names = phase_graph.phases
    current_phase = run.current_phase

    task_index = build_task_index(tasks)
    phase_task_index = build_phase_task_index(tasks)

    integrity_alerts += validate_task_membership(tasks, phase_names)
    integrity_alerts += validate_dependencies(tasks)
    integrity_alerts += validate_l3_before_l4(run, tasks)

    approval_state = derive_approval_state(run.approval_state)
    hard_stop = run.hard_stop
    hard_stop_active = hard_stop.active

    phase_exit_readiness = derive_phase_exit_readiness(
        current_phase,
        tasks
    )
        # all phase_gate tasks in current phase
        # must be completed or noop
        # otherwise exit_ready = false

    completion_summary = derive_completion_summary(
        current_phase,
        tasks,
        approval_state,
        hard_stop,
        phase_graph
    )
        # completion allowed only from completion_policy phases
        # and only if:
        #   no open required phase-gate tasks
        #   no pending required approval
        #   no active hard stop

    derived_run_status = derive_run_status(
        current_run_status,
        approval_state,
        hard_stop
    )
        # hard stop => blocked
        # approval pending => awaiting_approval
        # terminal stays terminal
        # else in_progress

    allowed_next_phases = derive_allowed_next_phases(
        current_phase,
        phase_exit_readiness,
        phase_graph,
        hard_stop_active,
        approval_pending
    )
        # no hard stop
        # no pending approval
        # current phase exit_ready
        # then use phase_graph[current_phase].allowed_next_phases

    allowed_next_actions = derive_allowed_next_actions(
        phase_exit_readiness,
        completion_summary,
        hard_stop_active,
        approval_pending
    )
        IF hard_stop_active:
            ["clear_hard_stop", "request_approval", "resolve_approval", "abort_run"]
        ELIF approval_pending:
            ["resolve_approval", "abort_run"]
        ELIF phase not exit_ready:
            [
              "create_task", "retry_task", "complete_task",
              "noop_task", "fail_task", "abort_task",
              "request_approval", "pause_run", "abort_run"
            ]
        ELIF completion_eligible:
            [
              "advance_phase", "reroute_phase", "create_task",
              "request_approval", "pause_run", "complete_run", "abort_run"
            ]
        ELSE:
            [
              "advance_phase", "reroute_phase", "create_task",
              "request_approval", "pause_run", "abort_run"
            ]

    write back authoritative derived fields to run_ledger
    RETURN reconcile_output