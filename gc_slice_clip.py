#!/usr/bin/env python3
"""Slice one reviewed clip from a locally cached full-game asset."""

from __future__ import annotations

import argparse
import fcntl
from pathlib import Path

from gc_season import SeasonProject, slugify


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--clip-key", required=True)
    return parser.parse_args()


def timing(project: SeasonProject, clip_key: str) -> tuple[float, float]:
    clip = project.clip(clip_key)
    return float(clip["display_start"]), float(clip["display_end"])


def main() -> None:
    args = parse_args()
    project = SeasonProject(args.project)
    lock_dir = project.root / ".slice-locks"
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / f"{slugify(args.clip_key)}.lock"
    with open(lock_path, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            before = timing(project, args.clip_key)
            project.slice_clip(args.clip_key)
            if timing(project, args.clip_key) == before:
                return


if __name__ == "__main__":
    main()
