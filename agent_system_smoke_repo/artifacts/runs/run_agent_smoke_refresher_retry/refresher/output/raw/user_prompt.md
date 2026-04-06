Role: refresher
Run ID: run_agent_smoke_refresher_retry
Repository root: /data03/liang/mjy/agent_system_smoke_repo
Packet path: /data03/liang/mjy/agent_system_smoke_repo/artifacts/runs/run_agent_smoke/context/refresher_packet.md
Phase: refresh
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
# Refresher Packet

## Goal
Refresh the smoke repo spec state for the active run.

## Required Reads
- `/data03/liang/mjy/agent_system_smoke_repo/AGENTS.md`
- `/data03/liang/mjy/agent_system_smoke_repo/CLAUDE.md`
- `/data03/liang/mjy/agent_system_smoke_repo/specs/current_run.md`
- `/data03/liang/mjy/agent_system_smoke_repo/specs/mission.md`
- `/data03/liang/mjy/agent_system_smoke_repo/specs/learned_constraints.md`

## Requested Scope Items
- make `specs/current_run.md` explicitly mention the smoke run id `run_agent_smoke`
- preserve the bounded smoke-test objective
- surface unresolved items honestly if any remain

## Constraints
- do not broaden into project redesign
- keep outputs concise
END PACKET
