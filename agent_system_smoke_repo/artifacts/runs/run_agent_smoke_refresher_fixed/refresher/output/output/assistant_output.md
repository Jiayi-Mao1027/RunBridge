## Refresher — run_agent_smoke_refresher_fixed

**Phase:** refresh  
**Run ID:** run_agent_smoke  
**Date:** 2026-04-06

### What was done

`specs/current_run.md` has been refreshed. The file previously had no explicit run ID field, no active run area declaration, and no inline constraints. All three gaps were resolved in place without altering the bounded smoke-test objective or the done-when criteria.

**Changes made to `specs/current_run.md`:**
- Added `## Run ID` section explicitly naming `run_agent_smoke`
- Added `## Active Run Area` pointing to `artifacts/runs/run_agent_smoke/`
- Added `## Constraints` inline (carried from `specs/learned_constraints.md` and packet) to make the doc self-contained for downstream execution roles
- Added `## Refresh Date` stamped 2026-04-06

**Unchanged files:**
- `specs/mission.md` — already accurate; no update needed
- `specs/learned_constraints.md` — already accurate; no update needed

### Unresolved items after refresh

None blocking. The execution-facing spec is now usable for downstream roles (Curator, Preflight, Opus).
