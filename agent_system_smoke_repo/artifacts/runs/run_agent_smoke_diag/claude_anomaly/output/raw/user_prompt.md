Role: claude-anomaly-analyst
Run ID: run_agent_smoke_diag
Repository root: /data03/liang/mjy/agent_system_smoke_repo
Packet path: /data03/liang/mjy/agent_system_smoke_repo/artifacts/runs/run_agent_smoke/context/claude_anomaly_packet.md
Phase: analyze
Follow the bound worker contract and packet exactly.

You must return outputs that are directly usable by the Codex orchestrator.

Return exactly one JSON object and no surrounding commentary.

Required top-level fields:
- assistant_markdown: string
- report_markdown: string
- handoff_markdown: string
- orchestrator_summary: object
- receipt: object

Required orchestrator_summary fields:
- headline: string
- status: string
- next_action_owner: string
- next_action: string
- key_points: array of strings
- evidence_paths: array of strings

Required receipt fields:
- role: string
- phase: string
- scope_completed: boolean
- issues: array

Each issue object should include when available:
- title
- issue_type
- severity
- evidence_paths
- next_action

Optional top-level field:
- extra_artifacts: object mapping relative paths to either string content or JSON values

Do not wrap the JSON in markdown fences unless unavoidable.

BEGIN PACKET
# Claude Anomaly Packet

## Goal
Diagnose a prepared abnormal-output case in the smoke repo.

## Required Reads
- `/data03/liang/mjy/agent_system_smoke_repo/specs/current_run.md`
- `/data03/liang/mjy/agent_system_smoke_repo/artifacts/runs/run_agent_smoke/outputs/fake_metrics.json`
- `/data03/liang/mjy/agent_system_smoke_repo/artifacts/runs/run_agent_smoke/outputs/fake_log.txt`

## Task
- explain why the prepared result looks abnormal
- keep this strictly read-only
END PACKET
