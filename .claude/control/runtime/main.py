from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workflow_runtime import dispatch_workflow_event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch a bridge-window workflow runtime event.")
    parser.add_argument("--control-root", required=True, help="Path to .claude/control")
    parser.add_argument("--runtime-runs-root", default=None, help="Optional explicit runtime_state/runs root")
    parser.add_argument("--event-file", default=None, help="Path to JSON file containing a workflow runtime event")
    parser.add_argument("--event-json", default=None, help="Inline JSON string containing a workflow runtime event")
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


def main() -> None:
    args = parse_args()
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
