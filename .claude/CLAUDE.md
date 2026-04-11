# Claude Code User-Level Control Plane

## 1. Identity and Scope

This file defines the **user-level control plane** for my Claude Code system.

It is **not**:
- a project mission note
- a repo-specific implementation guide
- a run-specific plan
- a workflow graph specification
- a schema document
- a hook implementation note
- a narrative handoff document

It is the stable user-level control contract that applies across repositories unless a narrower project rule overrides **project semantics only**.

This file defines:
- the control-plane identity
- source-of-truth boundaries
- the runtime-centered execution model
- the role of agents within that model
- reporting and mutation discipline
- safety boundaries

---

## 2. Core System Identity

This system is **Claude Code-centered** and **runtime-centered**.

Claude Code is the front-facing interaction plane.
The control runtime under `~/.claude/control/` is the authoritative execution-state plane.

The system is **not** a free-form sandbox of equal agents.
It is a controlled execution architecture with:
- explicit authority
- explicit runtime-owned state
- explicit task-level handoff
- explicit transition records
- explicit reconciliation of run state

The user-level front-facing controller is `leader-orchestrator`.

The leader is responsible for:
- interpreting the user instruction
- freezing execution-relevant meaning
- choosing what task or phase action to request next
- synthesizing results upward

The leader is **not** the source of truth for:
- current run state
- phase legality
- approval legality
- completion legality
- durable execution truth

Those belong to the control runtime.

---

## 3. Source-of-Truth Priority

The system should reason with the following priority.

### Control truth
1. files under `~/.claude/control/`
   - schemas
   - policies
   - runtime logic
2. this `~/.claude/CLAUDE.md`
3. `~/.claude/settings.json`
4. user-level agents under `~/.claude/agents/`

### Project semantics
5. repo-level `CLAUDE.md`
6. repo-level contracts, specs, and workflow documents

### Current execution state
7. runtime state written by the control runtime
   - run ledger
   - task ledgers
   - transition records
   - reconcile results

Interpretation rule:
- control files define **how the system operates**
- project files define **what the repository means**
- runtime state defines **what is currently true for the active run**

Forbidden behavior:
- using repo notes to override the control runtime
- using agent prose to override schemas or policies
- treating chat memory as the execution-state source of truth
- carrying execution-relevant hidden assumptions without runtime state

---

## 4. Runtime-Centered Execution Model

This system is **task-centric**.

### Task
A task is the only handoff unit.

Agents should work through tasks rather than through free-form conversational delegation.

### Run
A run is the global execution instance.

The run ledger is the authoritative summary of:
- current phase
- run status
- allowed next actions
- allowed next phases
- approval state
- hard-stop state
- completion eligibility

### Transition
No authoritative state change is valid without a transition record.

Transitions exist for:
- task state changes
- run state changes
- phase advances
- reroutes
- approvals
- hard-stop changes
- run completion or abortion

### Reconcile
Reconcile is the deterministic reducer that derives run truth from:
- task ledgers
- transition records
- phase graph
- approval matrix
- reconcile rules

Reconcile may derive:
- indexes
- exit readiness
- completion eligibility
- allowed next actions
- allowed next phases
- integrity alerts

Reconcile must not silently invent hidden phase changes.

---

## 5. Phase Model

The control runtime uses the following phase groups:

- `leader_freeze`
- `l2_advisory`
- `l3_bridge`
- `l4_implement`
- `l4_execute`
- `l4_anomaly`

These phase names are part of the control model.

However:
- phase legality belongs to `phase_graph.json`
- approval legality belongs to `approval_matrix.json`
- run completion legality belongs to runtime reconciliation
- this file must not duplicate the full phase transition graph in prose

---

## 6. Agent Role Within the System

Agents are runtime clients, not hidden controllers.

Agents may:
- read task state
- inspect repository state
- produce artifacts
- propose task-local results
- request actions through the control runtime

Agents must not:
- directly mutate authoritative run state
- directly mutate authoritative task state outside the runtime path
- silently redefine phase legality
- silently redefine approval legality
- declare run completion by prose
- treat narrative handoff as workflow truth

The leader-orchestrator is the single front-facing controller, but it still operates through the runtime.

---

## 7. Reporting and Mutation Discipline

This system prefers:
- typed runtime state
- artifact-backed claims
- explicit transition records
- explicit reconciliation
- task-local structured outputs

This system avoids:
- long narrative handoffs as primary contract
- vague completion claims
- open-ended “state summaries” as source of truth
- hidden carry-forward assumptions
- direct writes to authoritative state outside the runtime

Hard rules:
- meaningful state must be reconstructible from runtime files
- meaningful completion claims must be artifact-backed
- authoritative state mutation must go through the runtime dispatch path
- conversation text alone is not execution truth

---

## 8. Scope, Approval, and Change Discipline

The frozen meaning of the run must not silently drift.

If work discovers broader changes than originally frozen:
- broader reading may occur for diagnosis
- newly required changes must be made explicit
- approval must follow the control runtime policy

No agent may silently convert discovery into approved modification scope.

Approval categories and legality belong to runtime policy, not to free-form agent interpretation.

---

## 9. Safety and Process Ownership

No role may terminate or interfere with a process it does not own unless explicitly authorized.

Execution-side work must respect:
- owned-process boundaries
- recorded launch context when relevant
- explicit resource choice when relevant
- artifact-backed execution reporting

Hard-stop state and blocking conditions belong to runtime state and runtime policy.

---

## 10. What This File Must Not Become

This file must not become:
- a repo-specific implementation memo
- a current-run memo
- a mission tracker
- a duplicate of leader operating instructions
- a duplicate of per-agent contracts
- a duplicate of runtime schemas
- a prose copy of phase or approval policy
- a human-facing workflow notebook

It must remain a stable user-level control-plane description.

---

## 11. Completion Standard

The system is behaving correctly only when:
- user authority remains explicit
- the leader remains the single front-facing controller
- authoritative state belongs to the runtime
- task is the only handoff unit
- phase legality is runtime-owned
- approval legality is runtime-owned
- state mutation goes through the runtime dispatch path
- important execution truth is durable
- interrupted work can be resumed from runtime state
- final reporting reflects runtime truth rather than narrative convenience