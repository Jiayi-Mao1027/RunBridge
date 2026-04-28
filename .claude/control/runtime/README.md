# Runtime v2

Active entry point:

`main.py -> workflow_runtime.dispatch_workflow_event`

The runtime accepts `RuntimeEvent` objects only. It does not accept legacy task/run action requests at `main.py`.

Active ledgers per run:

- `run_ledger.json`
- `runtime_snapshot.json`
- `event_log.jsonl`
- `check_ledger.jsonl`
- `update_ledger.jsonl`
- `transitions.jsonl`
- `main_leader_inbox.jsonl`

The older action-dispatch modules in this directory are retained only as historical implementation material. They are not the active execution path for the bridge-window workflow.
