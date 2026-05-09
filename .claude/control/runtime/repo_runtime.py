from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from persist import atomic_write_json


RUNTIME_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class RepoManifest:
    repo_key: str
    repo_root: str
    display_name: str
    git: dict[str, Any]
    created_at: str
    last_seen_at: str
    runtime_schema_version: int = RUNTIME_SCHEMA_VERSION
    is_active: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_key": self.repo_key,
            "repo_root": self.repo_root,
            "display_name": self.display_name,
            "git": dict(self.git),
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "runtime_schema_version": self.runtime_schema_version,
            "is_active": self.is_active,
        }


def resolve_repo_key(repo_root: Path) -> str:
    resolved = Path(repo_root).expanduser().resolve()
    normalized = str(resolved).lower()
    digest = hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{_safe_name(resolved.name)}_{digest}"


def runtime_state_root(control_root: str | Path) -> Path:
    return Path(control_root).expanduser().resolve().parent / "runtime_state"


def get_repo_runtime_root(control_root: str | Path, repo_key: str) -> Path:
    return runtime_state_root(control_root) / "projects" / _safe_name(repo_key) / "runs"


def get_repo_project_root(control_root: str | Path, repo_key: str) -> Path:
    return runtime_state_root(control_root) / "projects" / _safe_name(repo_key)


def registry_root(control_root: str | Path) -> Path:
    return runtime_state_root(control_root) / "registry"


def infer_repo_key_from_runs_root(runtime_runs_root: str | Path | None) -> str | None:
    if runtime_runs_root is None:
        return None
    path = Path(runtime_runs_root).expanduser().resolve()
    parts = list(path.parts)
    for index, part in enumerate(parts):
        if part == "projects" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def repo_key_for_paths(control_root: str | Path, runtime_runs_root: str | Path | None, repo_root: str | Path | None = None) -> str:
    inferred = infer_repo_key_from_runs_root(runtime_runs_root)
    if inferred:
        return inferred
    if repo_root:
        return resolve_repo_key(Path(repo_root))
    return "unscoped_repo"


def ensure_repo_registered(
    control_root: str | Path,
    repo_root: str | Path,
    *,
    run_id: str | None = None,
    status: str = "running",
) -> RepoManifest:
    repo_path = Path(repo_root).expanduser().resolve()
    repo_key = resolve_repo_key(repo_path)
    now = _now_iso()
    project_root = get_repo_project_root(control_root, repo_key)
    manifest_path = project_root / "repo_manifest.json"
    existing = _read_json(manifest_path)
    created_at = str(existing.get("created_at") or now) if isinstance(existing, dict) else now
    manifest = RepoManifest(
        repo_key=repo_key,
        repo_root=str(repo_path),
        display_name=repo_path.name,
        git=_git_summary(repo_path),
        created_at=created_at,
        last_seen_at=now,
        runtime_schema_version=RUNTIME_SCHEMA_VERSION,
        is_active=True,
    )
    atomic_write_json(manifest_path, manifest.as_dict())
    _update_registry(control_root, manifest, run_id=run_id, status=status)
    return manifest


def list_registered_repos(control_root: str | Path) -> list[RepoManifest]:
    payload = _read_json(registry_root(control_root) / "repos.json")
    repos = payload.get("repos") if isinstance(payload.get("repos"), dict) else {}
    result = []
    for repo_key, item in repos.items():
        if not isinstance(item, dict):
            continue
        result.append(
            RepoManifest(
                repo_key=str(item.get("repo_key") or repo_key),
                repo_root=str(item.get("repo_root") or ""),
                display_name=str(item.get("display_name") or repo_key),
                git=item.get("git") if isinstance(item.get("git"), dict) else {},
                created_at=str(item.get("created_at") or ""),
                last_seen_at=str(item.get("last_seen_at") or ""),
                runtime_schema_version=int(item.get("runtime_schema_version") or RUNTIME_SCHEMA_VERSION),
                is_active=bool(item.get("is_active", True)),
            )
        )
    return sorted(result, key=lambda item: item.display_name.casefold())


def list_runs(control_root: str | Path, repo_key: str) -> list[dict[str, Any]]:
    runs_root = get_repo_runtime_root(control_root, repo_key)
    if not runs_root.exists():
        return []
    summaries = []
    for run_dir in sorted((item for item in runs_root.iterdir() if item.is_dir()), key=lambda item: item.name):
        snapshot = _read_json(run_dir / "runtime_snapshot.json")
        ledger = _read_json(run_dir / "run_ledger.json")
        source = snapshot if snapshot else ledger
        summaries.append(
            {
                "repo_key": repo_key,
                "run_id": run_dir.name,
                "current_phase": source.get("current_phase"),
                "run_status": source.get("run_status"),
                "updated_at": source.get("updated_at"),
                "snapshot_path": str(run_dir / "runtime_snapshot.json"),
            }
        )
    return summaries


def read_snapshot(control_root: str | Path, repo_key: str, run_id: str) -> dict[str, Any]:
    path = get_repo_runtime_root(control_root, repo_key) / run_id / "runtime_snapshot.json"
    return _read_json(path)


def append_event(control_root: str | Path, repo_key: str, run_id: str, event: dict[str, Any]) -> None:
    path = get_repo_runtime_root(control_root, repo_key) / run_id / "event_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def update_active_run_registry(
    control_root: str | Path,
    *,
    repo_key: str,
    repo_root: str | None,
    run_id: str,
    status: str,
) -> None:
    manifest_payload = {
        "repo_key": repo_key,
        "repo_root": repo_root or "",
        "display_name": Path(repo_root).name if repo_root else repo_key,
        "git": {},
        "created_at": _now_iso(),
        "last_seen_at": _now_iso(),
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "is_active": True,
    }
    _update_registry(control_root, RepoManifest(**manifest_payload), run_id=run_id, status=status)


def _update_registry(control_root: str | Path, manifest: RepoManifest, *, run_id: str | None, status: str) -> None:
    root = registry_root(control_root)
    repos_path = root / "repos.json"
    active_path = root / "active_runs.json"
    now = _now_iso()

    repos_payload = _read_json(repos_path)
    repos = repos_payload.get("repos") if isinstance(repos_payload.get("repos"), dict) else {}
    repos[manifest.repo_key] = manifest.as_dict()
    atomic_write_json(repos_path, {"updated_at": now, "repos": repos})

    active_payload = _read_json(active_path)
    active_repos = active_payload.get("repos") if isinstance(active_payload.get("repos"), dict) else {}
    existing = active_repos.get(manifest.repo_key) if isinstance(active_repos.get(manifest.repo_key), dict) else {}
    active_ids = [str(item) for item in existing.get("active_run_ids", []) if str(item)]
    if run_id and status == "running" and run_id not in active_ids:
        active_ids.append(run_id)
    if run_id and status in {"idle", "completed", "failed", "aborted"}:
        active_ids = [item for item in active_ids if item != run_id]
    active_repos[manifest.repo_key] = {
        "repo_root": manifest.repo_root,
        "latest_run_id": run_id or existing.get("latest_run_id"),
        "active_run_ids": active_ids,
        "status": "running" if active_ids else status,
        "last_seen_at": now,
    }
    atomic_write_json(active_path, {"updated_at": now, "repos": active_repos})


def _git_summary(repo_root: Path) -> dict[str, Any]:
    return {
        "remote_url_hash": _hash_text(_git(repo_root, "config", "--get", "remote.origin.url")),
        "current_branch": _git(repo_root, "branch", "--show-current"),
        "head_sha": _git(repo_root, "rev-parse", "HEAD"),
    }


def _git(repo_root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _hash_text(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value))[:96] or "repo"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
