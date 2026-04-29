from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bridge_sdk import call_bridge_sdk
from main_leader import decide_next_bridge_packet
from workflow_runtime import dispatch_workflow_event, reconcile_workflow_from_ledger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch a bridge-window workflow runtime event.")
    parser.add_argument("--control-root", required=True, help="Path to .claude/control")
    parser.add_argument("--runtime-runs-root", default=None, help="Optional explicit runtime_state/runs root")
    parser.add_argument("--event-file", default=None, help="Path to JSON file containing a workflow runtime event")
    parser.add_argument("--event-json", default=None, help="Inline JSON string containing a workflow runtime event")
    parser.add_argument("--build-bridge-packet", action="store_true", help="Build one main-leader bridge packet from current runtime truth")
    parser.add_argument("--call-bridge-sdk", action="store_true", help="Run one bridge invocation from a packet JSON")
    parser.add_argument("--packet-file", default=None, help="Path to JSON file containing a BridgePacket")
    parser.add_argument("--packet-json", default=None, help="Inline JSON string containing a BridgePacket")
    parser.add_argument("--run-id", default=None, help="Run id for build/reconcile operations")
    parser.add_argument("--main-session-id", default=None, help="Main session id for generated bridge packets")
    parser.add_argument("--user-instruction", default=None, help="User instruction to place in the generated task description")
    parser.add_argument("--task-spec-json", default=None, help="Inline JSON task_spec override for generated bridge packets")
    parser.add_argument("--team-spec-json", default=None, help="Inline JSON team_spec override for generated bridge packets")
    parser.add_argument("--target-phase", default=None, help="Target phase override for generated bridge packets")
    parser.add_argument("--reconcile-from-ledger", action="store_true", help="Replay event_log.jsonl and rebuild run ledger/snapshot")
    parser.add_argument("--persist", action="store_true", help="Persist event/check/update/notify ledgers and snapshot")
    return parser.parse_args()


def load_event(args: argparse.Namespace) -> dict:
    if args.event_file:
        return json.loads(Path(args.event_file).read_text(encoding="utf-8"))
    if args.event_json:
        return json.loads(args.event_json)
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("No workflow event provided. Use --event-file, --event-json, or stdin.")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Workflow event payload must be a JSON object.")
    return payload


def load_packet(args: argparse.Namespace) -> dict:
    if args.packet_file:
        return json.loads(Path(args.packet_file).read_text(encoding="utf-8"))
    if args.packet_json:
        return json.loads(args.packet_json)
    payload = load_event(args)
    if "packet" in payload and isinstance(payload["packet"], dict):
        return payload["packet"]
    return payload


def load_optional_json(raw: str | None) -> dict | None:
    if raw is None:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("optional JSON override must be an object")
    return payload


def main() -> None:
    args = parse_args()
    if args.reconcile_from_ledger:
        if not args.run_id:
            raise ValueError("--reconcile-from-ledger requires --run-id")
        result = reconcile_workflow_from_ledger(
            args.control_root,
            args.run_id,
            runtime_runs_root=args.runtime_runs_root,
            persist=args.persist,
        )
        print(json.dumps({"reconcile_result": result}, ensure_ascii=False, indent=2))
        return

    if args.build_bridge_packet:
        if not args.run_id:
            raise ValueError("--build-bridge-packet requires --run-id")
        packet = decide_next_bridge_packet(
            args.control_root,
            args.run_id,
            runtime_runs_root=args.runtime_runs_root,
            main_session_id=args.main_session_id,
            user_instruction=args.user_instruction,
            task_spec=load_optional_json(args.task_spec_json),
            team_spec=load_optional_json(args.team_spec_json),
            target_phase=args.target_phase,
        )
        print(json.dumps({"packet": packet}, ensure_ascii=False, indent=2))
        return

    if args.call_bridge_sdk:
        packet = load_packet(args)
        result = call_bridge_sdk(
            args.control_root,
            packet,
            runtime_runs_root=args.runtime_runs_root,
            persist=args.persist,
            record_main_lifecycle=True,
        )
        print(json.dumps({"bridge_result": result}, ensure_ascii=False, indent=2))
        return

    event_payload = load_event(args)
    if "event_kind" not in event_payload:
        raise ValueError("Workflow event requires event_kind. Legacy action requests are not accepted by this runtime entrypoint.")

    result = dispatch_workflow_event(
        args.control_root,
        event_payload,
        runtime_runs_root=args.runtime_runs_root,
        persist=args.persist,
    )
    print(
        json.dumps(
            {
                "workflow_result": {
                    "ok": result.ok,
                    "run_id": result.run_id,
                    "event_id": result.event_id,
                    "event_kind": result.event_kind,
                    "check_result": result.check_result,
                    "update_result": result.update_result,
                    "notify_result": result.notify_result,
                    "runtime_snapshot": result.runtime_snapshot,
                },
                "written_paths": result.written_paths,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
