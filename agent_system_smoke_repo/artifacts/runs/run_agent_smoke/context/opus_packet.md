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
