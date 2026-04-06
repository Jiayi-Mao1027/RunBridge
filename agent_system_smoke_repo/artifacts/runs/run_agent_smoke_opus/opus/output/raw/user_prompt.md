Role: opus-coder
Run ID: run_agent_smoke_opus
Repository root: /data03/liang/mjy/agent_system_smoke_repo
Packet path: /data03/liang/mjy/agent_system_smoke_repo/artifacts/runs/run_agent_smoke/context/opus_packet.md
Phase: implement
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
# Opus Packet

## Role
Opus Coder

## Phase
implement

## Approved Change Set
- `/data03/liang/mjy/agent_system_smoke_repo/example_task.txt`

## Goal
Make one tiny approved edit proving the Opus bridge can modify files in the smoke repo only.

## Required Action
- update `status: original` to `status: smoke-tested`
- append a short line `agent: opus-coder`

## Constraints
- do not modify any other file
- do not create extra files unless the runner itself writes artifacts
- if you think more changes are needed, report an additional change request instead of widening scope
END PACKET
