# Bridge Companion Architecture

## 1. Product Definition

Bridge Companion is an external, read-only observation layer for the Claude Code Bridge Runtime. It turns runtime snapshots, event ledgers, inbox notifications, reports, and artifact references into a readable foreground experience.

It is not a controller. It does not create bridge windows, create teams, dispatch tasks, approve routes, modify ledgers, or inject context into any runtime agent.

The design boundary is:

```text
视觉可以游戏化，信息不能游戏化。
```

The UI may use a dark fantasy task board, pixel characters, parchment cards, animated routes, themed icons, and companion notes. The authoritative status text must remain factual, precise, and derived from runtime data.

## 2. Isolation Boundary

Bridge Companion lives in its own folder:

```text
bridge-companion/
```

It must not modify or import runtime control code directly as a control dependency. It should consume exported data through one of these read-only mechanisms:

```text
runtime snapshot file reader
read-only HTTP adapter
read-only websocket/SSE event stream
mock data during prototype development
```

The original agent system remains the source of execution truth. Bridge Companion only observes.

## 3. Non-Goals

Bridge Companion must not become a second leader, bridge controller, task dispatcher, approval surface, or agent prompt layer.

It must not write to these areas:

```text
.claude/control/
.claude/agents/
.claude/hooks/
.claude/runtime_state/
```

It must not add context to implementor, bridge-leader, leader-orchestrator, or any teammate agent. If an intelligent explanation layer is added, it should be a UI-side API that receives already-normalized facts and returns constrained explanations only for display.

## 4. Data Flow

Recommended flow:

```text
Bridge Runtime files / read-only API
  -> companion data adapter
  -> normalized runtime view
  -> companion status model
  -> fact copy / explanation copy / companion note
  -> themed UI
```

The UI should not directly invent prose from raw logs. A status model should first determine what is known, what is unknown, what is waiting, what failed, and what lifecycle transitions are possible.

## 5. Three-Layer Copy Model

Every status surface should distinguish three layers.

The fact layer is authoritative and must come from runtime state. Example: current phase, latest event, lifecycle state, wait reason, completion report presence, artifact presence, failure event.

The explanation layer translates facts into user-readable language. It may explain that waiting does not necessarily mean stuck, but it may not claim file-level progress unless a report or event says so.

The atmosphere layer is optional. It may use themed language such as “同伴札记” or “小队还没有带回新卷宗,” but it must remain visibly secondary and must not introduce new facts.

## 6. Optional Intelligence API

If extra intelligence is needed, add it as a UI-side explanation API, not as an agent-system participant.

Safe scope:

```text
input: normalized runtime facts, unknowns, allowed next events, report excerpts
output: concise explanation copy and user-facing summary
```

Forbidden scope:

```text
controlling workflow
calling bridge tools
modifying runtime files
changing frozen semantics
sending instructions to system agents
claiming hidden internal progress
```

The API prompt should include a strict contract: only use provided facts, list unknowns, do not infer file-level or code-level progress unless explicitly present, do not rename agent roles in authoritative copy, and keep themed text limited to the companion note layer.

## 7. Prototype Strategy

The prototype in this folder uses static mock data. This is intentional. The first milestone is to prove the UI grammar, copy boundaries, and status mapping without touching the existing Bridge Runtime.

Later milestones can add a read-only adapter that loads runtime_snapshot.json and event_log.jsonl from a configured location or from a read-only server.