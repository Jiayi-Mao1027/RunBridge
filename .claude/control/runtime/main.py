from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from dispatch import dispatch_action
from models import ActionRequest
from persist import persist_dispatch_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch a control runtime action request.")
    parser.add_argument("--control-root", required=True, help="Path to control root, e.g. /data03/liang/mjy/.claude/control")
    parser.add_argument("--runtime-runs-root", default=None, help="Optional explicit runtime_state/runs root")
    parser.add_argument("--mode", choices=["authoritative", "recovery"], default="authoritative")
    parser.add_argument("--request-file", default=None, help="Path to JSON file containing an action request")
    parser.add_argument("--request-json", default=None, help="Inline JSON string containing an action request")
    parser.add_argument("--persist", action="store_true", help="Persist dispatch result to runtime state")
    return parser.parse_args()


def load_request(args: argparse.Namespace) -> dict:
    if args.request_file:
        return json.loads(Path(args.request_file).read_text(encoding="utf-8"))
    if args.request_json:
        return json.loads(args.request_json)
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("No action request provided. Use --request-file, --request-json, or stdin.")
    return json.loads(raw)


def build_action_request(payload: dict) -> ActionRequest:
    return ActionRequest(
        run_id=payload["run_id"],
        action=payload["action"],
        task_id=payload.get("task_id"),
        payload=payload.get("payload", {}),
        reason=payload.get("reason", ""),
        requester=payload.get("requester", "system"),
        timestamp=payload.get("timestamp", ""),
        trigger_source=payload.get("trigger_source", "system"),
        hook_name=payload.get("hook_name"),
        event_name=payload.get("event_name"),
        request_id=payload.get("request_id"),
    )


def main() -> None:
    args = parse_args()
    request_payload = load_request(args)
    action_request = build_action_request(request_payload)

    result = dispatch_action(
        args.control_root,
        action_request,
        runtime_runs_root=args.runtime_runs_root,
        mode=args.mode,
    )

    output = {
        "dispatch_result": {
            "ok": result.ok,
            "transition_id": result.transition_id,
            "run_id": result.run_id,
            "task_id": result.task_id,
            "decision": result.decision,
            "run_status": result.run_status,
            "current_phase": result.current_phase,
            "allowed_next_actions": result.allowed_next_actions,
            "integrity_alerts": result.integrity_alerts,
        }
    }

    if args.persist:
        written = persist_dispatch_result(
            args.control_root,
            result,
            runtime_runs_root=args.runtime_runs_root,
        )
        output["written_paths"] = written

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
