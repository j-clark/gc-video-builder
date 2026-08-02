#!/usr/bin/env python3
"""JSON command bridge between the Next.js UI and the season project engine."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from gc_common import GCClient, GCError
from gc_download_full_game import sort_teams_for_picker
from gc_season import SeasonProject, default_project_path, team_id_from_payload, team_label

REPO_ROOT = Path(__file__).resolve().parent
PROJECTS_ROOT = (REPO_ROOT / "gc_seasons").resolve()


def project_id(path: Path) -> str:
    return str(path.resolve().relative_to(PROJECTS_ROOT))


def open_project(value: str) -> SeasonProject:
    path = (PROJECTS_ROOT / value).resolve()
    if path != PROJECTS_ROOT and PROJECTS_ROOT not in path.parents:
        raise GCError("Invalid season project path.")
    return SeasonProject(path)


def project_summary(project: SeasonProject) -> dict[str, Any]:
    with project.connect() as conn:
        clip_counts = {
            row["review_state"]: row["count"]
            for row in conn.execute(
                "SELECT review_state, COUNT(*) AS count FROM clips GROUP BY review_state"
            )
        }
    return {
        "id": project_id(project.root),
        "name": project.team_name,
        "games": len(project.games()),
        "players": len(project.players()),
        "unreviewed": project.queueable_unreviewed_count(),
        "reviewed": clip_counts.get("reviewed", 0),
        "accepted": project.queueable_decision_count("accepted"),
        "refreshedAt": project.config("refreshed_at"),
    }


def list_projects() -> list[dict[str, Any]]:
    if not PROJECTS_ROOT.exists():
        return []
    projects = []
    for database in sorted(PROJECTS_ROOT.glob("*/season.db")):
        projects.append(project_summary(SeasonProject(database.parent)))
    return projects


def require_client() -> GCClient:
    return GCClient(os.environ.get("GC_TOKEN"))


def clean_team(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": team_id_from_payload(team),
        "label": team_label(team),
        "raw": team,
    }

def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cache_job(project: SeasonProject) -> dict[str, Any]:
    path = project.root / "cache-job.json"
    if not path.exists():
        return {"state": "idle"}
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "idle"}
    if job.get("state") == "running" and not process_alive(job.get("pid")):
        job["state"] = "failed"
        job["error"] = "The cache worker exited unexpectedly."
    return job


def start_cache_job(project: SeasonProject, event_ids: list[str]) -> dict[str, Any]:
    current = cache_job(project)
    if current.get("state") == "running":
        return current
    log_path = project.root / "cache-job.log"
    command = [
        sys.executable,
        str(REPO_ROOT / "gc_cache_games.py"),
        "--project",
        str(project.root),
    ]
    for event_id in event_ids:
        command.extend(["--event-id", event_id])
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return {"state": "starting", "pid": process.pid}


def start_slice_job(project: SeasonProject, clip_key: str) -> None:
    log_path = project.root / "slice-jobs.log"
    with open(log_path, "ab") as log:
        subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "gc_slice_clip.py"),
                "--project",
                str(project.root),
                "--clip-key",
                clip_key,
            ],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def page_rows(
    rows: list[dict[str, Any]], payload: dict[str, Any]
) -> dict[str, Any]:
    search = str(payload.get("search") or "").strip().lower()
    if search:
        rows = [
            row
            for row in rows
            if search
            in " ".join(
                str(row.get(key) or "")
                for key in (
                    "play_title",
                    "play_summary",
                    "play_type",
                    "opponent",
                    "participant_text",
                    "score_reason",
                )
            ).lower()
        ]
    offset = max(0, int(payload.get("offset") or 0))
    limit = min(200, max(1, int(payload.get("limit") or 75)))
    return {"total": len(rows), "offset": offset, "rows": rows[offset : offset + limit]}


def dispatch(action: str, payload: dict[str, Any]) -> Any:
    if action == "projects":
        return list_projects()
    if action == "teams":
        return [clean_team(team) for team in sort_teams_for_picker(require_client().get_my_teams())]
    if action == "create_project":
        client = require_client()
        requested_id = str(payload["teamId"])
        team = next(
            (team for team in client.get_my_teams() if team_id_from_payload(team) == requested_id),
            None,
        )
        if not team:
            raise GCError(f"Team {requested_id} was not found.")
        root = default_project_path(team)
        project = SeasonProject(root) if (root / "season.db").exists() else SeasonProject.create(root, team)
        counts = project.refresh(client)
        return {"project": project_summary(project), "imported": counts}

    project = open_project(str(payload["project"]))
    if action == "summary":
        return project_summary(project)
    if action == "dashboard":
        return {
            "project": project_summary(project),
            "players": project.dashboard(),
            "games": project.games(),
        }
    if action == "cache_status":
        return {**project.media_cache_status(), "job": cache_job(project)}
    if action == "cache_start":
        event_ids = [str(value) for value in payload.get("eventIds") or []]
        return start_cache_job(project, event_ids)
    if action == "cache_stop":
        job = cache_job(project)
        pid = job.get("pid")
        if job.get("state") == "running" and process_alive(pid):
            os.killpg(int(pid), signal.SIGTERM)
        return {"ok": True}
    if action == "all_queue":
        role = payload.get("role")
        side = payload.get("side")
        return page_rows(
            project.all_queue(
                role=None if role in (None, "", "all") else str(role),
                side=None if side in (None, "", "all") else str(side),
            ),
            payload,
        )
    if action == "player_queue":
        player_id = str(payload["playerId"])
        role = payload.get("role")
        role_filter = None if role in (None, "", "all") else str(role)
        side = payload.get("side")
        side_filter = None if side in (None, "", "all") else str(side)
        status = str(payload.get("status") or "pending")
        rows = project.player_queue(
            player_id,
            status=status,
            role=role_filter,
            side=side_filter,
        )
        if status == "pending":
            rows.extend(
                project.inferred_player_queue(
                    player_id,
                    role=role_filter,
                    side=side_filter,
                )
            )
            rows.sort(key=lambda row: (-row["score"], row.get("game_date") or ""))
        return page_rows(rows, payload)
    if action == "clip":
        return {"clip": project.clip(str(payload["clipKey"])), "players": project.players()}
    if action == "participants":
        values = [
            (str(item["playerId"]), str(item["role"]))
            for item in payload.get("participants") or []
        ]
        clip_key = str(payload["clipKey"])
        project.replace_draft_participants(clip_key, values)
        clip = project.clip(clip_key)
        if clip["review_state"] == "reviewed":
            project.confirm_clip(clip_key)
        return project.clip(clip_key)
    if action == "timing":
        clip_key = str(payload["clipKey"])
        project.set_timing(clip_key, float(payload["start"]), float(payload["end"]))
        start_slice_job(project, clip_key)
        return project.clip(clip_key)
    if action == "confirm":
        clip_key = str(payload["clipKey"])
        project.confirm_clip(clip_key)
        start_slice_job(project, clip_key)
        return project.clip(clip_key)
    if action == "dismiss":
        clip_key = str(payload["clipKey"])
        project.dismiss_clip(clip_key, str(payload["reason"]))
        return {"ok": True}
    if action == "dismiss_many":
        count = project.dismiss_clips(
            [str(value) for value in payload.get("clipKeys") or []],
            str(payload["reason"]),
        )
        return {"ok": True, "count": count}
    if action == "decision":
        project.set_decision(
            str(payload["clipKey"]),
            str(payload["playerId"]),
            str(payload["status"]),
        )
        return {"ok": True}
    if action == "move":
        project.move_accepted(
            str(payload["playerId"]),
            str(payload["clipKey"]),
            int(payload["direction"]),
        )
        return {"ok": True}
    if action == "refresh":
        imported = project.refresh(require_client())
        return {"project": project_summary(project), "imported": imported}
    if action == "preview":
        clip_key = str(payload["clipKey"])
        path, clip = project.full_game_media(clip_key)
        return {
            "path": str(path.relative_to(project.root)),
            "seek": float(clip["display_start"]),
            "inPoint": float(clip["display_start"]),
            "outPoint": float(clip["display_end"]),
            "sourceStart": 0,
            "sourceEnd": float(clip.get("asset_duration") or clip["display_end"]),
            "fullGame": True,
        }
    if action == "render_player":
        output = project.render_player(require_client(), str(payload["playerId"]))
        return {"path": str(output.relative_to(project.root))}
    if action == "render_all":
        outputs = project.render_all(require_client())
        return {"paths": [str(path.relative_to(project.root)) for path in outputs]}
    raise GCError(f"Unknown bridge action: {action}")


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    try:
        result = dispatch(action, payload)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1) from exc
    print(json.dumps({"ok": True, "data": result}, default=str))


if __name__ == "__main__":
    main()
