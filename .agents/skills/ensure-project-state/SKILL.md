---
name: ensure-project-state
description: Initialize the repository bootstrap state before first dispatch or before a run that depends on artifacts/specs.
---

# ensure-project-state

Use this skill when entering a repo for the first meaningful run, or when a run depends on repository bootstrap folders that may not exist yet.

This skill is not a semantic planner.
It only ensures that bootstrap state exists so the real workflow can proceed.

## Why this skill exists

The system contract allows first-run bootstrap state to be missing.
Missing `specs/` or `artifacts/` should not by itself be treated as a hard failure if they can be safely created.

## Required action

Run:

```bash
/data03/liang/mjy/.codex/protocol/bin/ensure_project_state.sh "$(pwd)"
```

Then snapshot the pre-run process baseline:

```bash
python /data03/liang/mjy/.codex/protocol/bin/owned_processes.py snapshot
```

## What this should leave behind
- `specs/`
- `artifacts/`
- `artifacts/runs/`
- placeholder bootstrap spec files only when they were missing

## Do not do
- do not invent project semantics
- do not overwrite real spec content when files already exist
- do not treat this as a substitute for Refresher
