#!/usr/bin/env python3
"""Render the full game video with scorebug and play-description overlays."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from gc_common import Segment, load_json, plays_to_segment_pairs, run, video_source_from_game
from gc_make_condensed_game import (
    burn_in_png_overlays,
    base_state_contexts,
    long_clip_start_buffer_override_map,
    opponent_label,
    own_player_prefixes,
    read_description_overrides,
    render_overlay_png,
    scorebug_contexts,
    select_condensed_plays,
    selected_description,
    simplified_play_description,
    write_selected_plays,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_json")
    parser.add_argument("--video", help="Optional local video path or HLS URL override.")
    parser.add_argument("--output", default="gc_output/full_game_scorebug.mp4")
    parser.add_argument("--exclude-types", default="", help="Comma-separated play types to omit.")
    parser.add_argument("--include-all-play-types", action="store_true", help="Overlay every indexed play instead of PA outcomes plus steals.")
    parser.add_argument("--reencode", action="store_true", help="Re-encode the clean full-game source before applying overlays.")
    parser.add_argument("--start-buffer", type=float, default=4.0, help="Additional seconds to subtract from each play overlay start.")
    parser.add_argument("--end-buffer", type=float, default=6.0, help="Additional seconds to add to each play overlay end.")
    parser.add_argument("--min-segment-length", type=float, default=12.0, help="Minimum seconds to show each play overlay.")
    parser.add_argument("--time-shift", type=float, default=0.0, help="Shift every play timestamp by this many seconds.")
    parser.add_argument("--long-clip-start-buffer", type=float, default=18.0, help="Pre-roll to use for longer GameChanger clips.")
    parser.add_argument("--extra-start-play-indexes", default="", help="Comma-separated raw play indexes that need extra pre-roll.")
    parser.add_argument("--extra-start-buffer", type=float, default=18.0, help="Pre-roll for plays listed in --extra-start-play-indexes.")
    parser.add_argument("--description-overrides", help="Optional markdown review file whose Proposed lines override generated overlay descriptions.")
    parser.add_argument("--plays-output", help="Optional text file listing overlaid plays. Defaults to OUTPUT.plays.txt.")
    parser.add_argument("--team-label", default="TIG", help="Scorebug label for the fetched team.")
    parser.add_argument("--opponent-label", help="Scorebug label for the opponent. Defaults to an abbreviation from the opponent name.")
    return parser.parse_args()


def parse_index_set(value: str) -> set[int]:
    indexes = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            indexes.add(int(item))
    return indexes


def copy_full_game_source(source: str, output: Path, *, cookie: str | None, reencode: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    if cookie:
        cmd.extend(["-headers", f"Cookie: {cookie}\r\n"])
    cmd.extend(["-i", source])
    if reencode:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac"])
    else:
        cmd.extend(["-c", "copy"])
    cmd.extend(["-movflags", "+faststart", str(output)])
    run(cmd)


def video_duration(path: Path) -> float | None:
    output = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    ).strip()
    return float(output) if output else None


def full_game_overlay_timeline(
    selected: list[tuple[str, dict[str, Any]]],
    segments: list[Segment],
    *,
    duration: float | None,
) -> list[tuple[float, float, dict[str, Any]]]:
    if len(selected) != len(segments):
        raise ValueError(f"Overlay inputs are misaligned: {len(selected)} selected plays for {len(segments)} segments")
    windows: list[tuple[float, float, dict[str, Any]]] = []
    paired = sorted(zip(segments, [play for _, play in selected]), key=lambda item: (item[0].start, item[0].end))
    for segment, play in paired:
        start = max(0.0, segment.start)
        end = max(start + 0.5, segment.end)
        if duration is not None:
            end = min(end, duration)
        if end > start:
            windows.append((start, end, play))

    timeline: list[tuple[float, float, dict[str, Any]]] = []
    for index, (start, end, play) in enumerate(windows):
        if index + 1 < len(windows):
            end = min(end, windows[index + 1][0])
        if end > start:
            timeline.append((start, end, play))
    return timeline


def write_full_game_scorebug_pngs(
    directory: Path,
    *,
    clean_video: Path,
    game: dict[str, Any],
    selected: list[tuple[str, dict[str, Any]]],
    segments: list[Segment],
    duration: float | None,
    team_label: str,
    opp_label: str,
    description_overrides: dict[int | str, str],
) -> list[tuple[float, float, Path]]:
    from gc_make_condensed_game import video_dimensions

    width, height = video_dimensions(clean_video)
    contexts = scorebug_contexts(game, team_label, opp_label)
    bases_by_play = base_state_contexts(game)
    prefixes = own_player_prefixes(game)
    descriptions_by_play = {
        id(play): selected_description(index, play, prefixes, description_overrides)
        for index, (_, play) in enumerate(selected, start=1)
    }

    overlays = []
    for index, (start, end, play) in enumerate(full_game_overlay_timeline(selected, segments, duration=duration)):
        path = directory / f"overlay-{index:04d}.png"
        render_overlay_png(
            path,
            width=width,
            height=height,
            score_text=contexts.get(id(play)) or f"INNING {play.get('inning') or ''}",
            bases=bases_by_play.get(id(play), set()),
            description=descriptions_by_play.get(id(play)) or simplified_play_description(play, prefixes),
        )
        overlays.append((start, end, path))
    return overlays


def main() -> None:
    args = parse_args()
    game = load_json(args.game_json)
    excluded = {item.strip() for item in args.exclude_types.split(",") if item.strip()}
    extra_start_play_indexes = parse_index_set(args.extra_start_play_indexes)
    selected = select_condensed_plays(
        game,
        include_all_play_types=args.include_all_play_types,
        excluded=excluded,
        target_duration=0,
        start_buffer=args.start_buffer,
        end_buffer=args.end_buffer,
        min_segment_length=args.min_segment_length,
        time_shift=args.time_shift,
        long_clip_start_buffer=args.long_clip_start_buffer,
        extra_start_play_indexes=extra_start_play_indexes,
        extra_start_buffer=args.extra_start_buffer,
    )

    plays = [play for _, play in selected]
    segment_pairs = plays_to_segment_pairs(
        plays,
        start_buffer=args.start_buffer,
        end_buffer=args.end_buffer,
        min_duration=args.min_segment_length,
        time_shift=args.time_shift,
        long_clip_start_buffer=args.long_clip_start_buffer,
        long_clip_start_buffer_overrides=long_clip_start_buffer_override_map(extra_start_play_indexes, args.extra_start_buffer),
    )
    selected_by_play_id = {id(play): (reason, play) for reason, play in selected}
    skipped_count = len(selected) - len(segment_pairs)
    selected = [selected_by_play_id[id(play)] for play, _ in segment_pairs]
    segments = [segment for _, segment in segment_pairs]
    if not segments:
        raise ValueError("No plays with usable timing selected for full-game overlay.")

    source, cookie = video_source_from_game(game, args.video)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    description_overrides = read_description_overrides(Path(args.description_overrides) if args.description_overrides else None)
    plays_output = Path(args.plays_output) if args.plays_output else output.with_suffix(".plays.txt")

    with tempfile.TemporaryDirectory(prefix="gc-full-game-overlay-") as temp_name:
        temp_dir = Path(temp_name)
        clean_video = temp_dir / "clean.mp4"
        copy_full_game_source(source, clean_video, cookie=cookie, reencode=args.reencode)
        overlays = write_full_game_scorebug_pngs(
            temp_dir,
            clean_video=clean_video,
            game=game,
            selected=selected,
            segments=segments,
            duration=video_duration(clean_video),
            team_label=args.team_label,
            opp_label=args.opponent_label or opponent_label(game),
            description_overrides=description_overrides,
        )
        burn_in_png_overlays(clean_video, output, overlays)

    write_selected_plays(plays_output, selected)
    print(f"Wrote {args.output} ({len(selected)} overlays)")
    if skipped_count:
        print(f"Skipped {skipped_count} selected plays with no usable timing")
    print(f"Wrote {plays_output}")


if __name__ == "__main__":
    main()
