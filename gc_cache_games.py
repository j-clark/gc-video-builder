#!/usr/bin/env python3
"""Background worker that caches full GameChanger video assets locally."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from gc_common import GCClient
from gc_download_full_game import asset_identity, ffmpeg_download_asset, playable_assets
from gc_season import SeasonProject, utc_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--event-id", action="append", default=[])
    return parser.parse_args()


def write_status(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def main() -> None:
    args = parse_args()
    project = SeasonProject(args.project)
    client = GCClient(os.environ.get("GC_TOKEN"))
    status_path = project.root / "cache-job.json"
    requested = set(args.event_id)
    with project.connect() as conn:
        event_ids = [
            row["event_id"]
            for row in conn.execute("SELECT event_id FROM games ORDER BY game_date")
            if not requested or row["event_id"] in requested
        ]
        total_assets = conn.execute(
            f"SELECT COUNT(*) FROM assets WHERE event_id IN ({','.join('?' for _ in event_ids)})",
            event_ids,
        ).fetchone()[0] if event_ids else 0

    state: dict[str, Any] = {
        "state": "running",
        "pid": os.getpid(),
        "startedAt": utc_now(),
        "finishedAt": None,
        "totalAssets": total_assets,
        "processedAssets": 0,
        "currentEventId": None,
        "currentAssetId": None,
        "error": None,
    }
    write_status(status_path, state)

    def stopped(_signum: int, _frame: object) -> None:
        state["state"] = "stopped"
        state["finishedAt"] = utc_now()
        write_status(status_path, state)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stopped)
    try:
        for event_id in event_ids:
            state["currentEventId"] = event_id
            with project.connect() as conn:
                metadata = [
                    {**json.loads(row["metadata_json"]), **dict(row)}
                    for row in conn.execute(
                        "SELECT * FROM assets WHERE event_id = ? ORDER BY created_at",
                        (event_id,),
                    )
                ]
            playback = client.get_event_playback_assets(project.team_id, event_id)
            playable = {
                asset_identity(asset): asset
                for asset in playable_assets(metadata, playback)
            }
            for asset in metadata:
                asset_id = str(asset_identity(asset) or "")
                state["currentAssetId"] = asset_id
                write_status(status_path, state)
                output = project.full_game_asset_path(event_id, asset_id)
                if not output.exists() or output.stat().st_size == 0:
                    source = playable.get(asset_id)
                    if not source:
                        raise RuntimeError(f"No playback URL for asset {asset_id}.")
                    ffmpeg_download_asset(
                        source,
                        output,
                        reencode=False,
                        force=False,
                        label="full-game asset",
                    )
                state["processedAssets"] += 1
                write_status(status_path, state)
        state["state"] = "complete"
    except Exception as exc:
        state["state"] = "failed"
        state["error"] = (
            f"ffmpeg failed with exit status {exc.returncode}."
            if isinstance(exc, subprocess.CalledProcessError)
            else str(exc)
        )
    state["currentEventId"] = None
    state["currentAssetId"] = None
    state["finishedAt"] = utc_now()
    write_status(status_path, state)


if __name__ == "__main__":
    main()
