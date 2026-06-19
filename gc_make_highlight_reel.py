#!/usr/bin/env python3
"""Create a short team highlight reel from GameChanger play metadata."""

from __future__ import annotations

import argparse
import re
import tempfile
from pathlib import Path
from typing import Any

from gc_common import load_json, make_reel, plays_to_segments, video_source_from_game
from gc_make_condensed_game import (
    burn_in_png_overlays,
    opponent_label,
    read_description_overrides,
    write_scorebug_pngs,
)
from gc_make_player_reels import (
    events_by_pbp_id,
    load_stream_events_for_game,
    pitcher_for_play,
    player_fielded_out,
    team_ids,
)


EXTRA_BASE_TYPES = {"double", "triple", "home_run"}
RUN_SCORING_PA_TYPES = {"single", "double", "triple", "home_run", "batter_out", "batter_out_advance_runners"}
DEFENSIVE_OUT_TYPES = {"batter_out", "batter_out_advance_runners", "double_play", "caught_stealing"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_json")
    parser.add_argument("--types", default=None, help="Optional comma-separated play types. Overrides curated team-highlight selection.")
    parser.add_argument("--video", help="Optional local video path or HLS URL override.")
    parser.add_argument("--output", default="gc_output/highlight_reel.mp4")
    parser.add_argument("--include-exceptional", action="store_true")
    parser.add_argument("--reencode", action="store_true")
    parser.add_argument("--target-duration", type=float, default=180.0, help="Approximate target runtime in seconds.")
    parser.add_argument("--max-merge-gap", type=float, default=1.0)
    parser.add_argument("--start-buffer", type=float, default=4.0, help="Additional seconds to subtract from every segment start.")
    parser.add_argument("--end-buffer", type=float, default=2.0, help="Additional seconds to add to every segment end.")
    parser.add_argument("--min-segment-length", type=float, default=12.0, help="Minimum seconds to keep for each selected play.")
    parser.add_argument("--time-shift", type=float, default=0.0, help="Shift every play timestamp by this many seconds before cutting.")
    parser.add_argument("--long-clip-start-buffer", type=float, default=18.0, help="Pre-roll to use for longer GameChanger clips.")
    parser.add_argument("--cache-dir", default="gc_render_cache/segments", help="Directory for reusable local clip segments.")
    parser.add_argument("--no-cache", action="store_true", help="Disable reusable local clip segment cache.")
    parser.add_argument("--plays-output", help="Optional text file listing selected plays. Defaults to OUTPUT.plays.txt.")
    parser.add_argument("--description-overrides", help="Optional markdown review file whose Proposed lines override generated overlay descriptions.")
    parser.add_argument("--scorebug", action="store_true", help="Burn in a centered scorebug and play description.")
    parser.add_argument("--team-label", default="TIG", help="Scorebug label for the fetched team.")
    parser.add_argument("--opponent-label", help="Scorebug label for the opponent. Defaults to an abbreviation from the opponent name.")
    return parser.parse_args()


def team_player_ids(game: dict[str, Any]) -> set[str]:
    ids = set(game.get("team_player_ids") or [])
    if ids:
        return ids
    return set((game.get("players") or {}).keys())


def player_name_patterns(player: dict[str, Any]) -> list[str]:
    display = str(player.get("display") or "").lower()
    first = str(player.get("first_name") or "").lower()
    last = str(player.get("last_name") or "").lower()
    full_name = f"{first} {last}".strip()
    return [value for value in {display, full_name} if value]


def team_player_scores(play: dict[str, Any], players: dict[str, Any], team_ids_: set[str]) -> bool:
    summary = str(play.get("play_summary") or "").lower()
    for player_id in team_ids_:
        for name in player_name_patterns(players.get(player_id, {})):
            if re.search(rf"\b{re.escape(name)}(?:\s+#?\d+)?\s+scores\b", summary):
                return True
    return False


def team_pa(play: dict[str, Any], team_ids_: set[str]) -> bool:
    mentioned = play.get("mentioned_player_ids") or []
    return bool(mentioned and mentioned[0] in team_ids_)


def team_steal_home(play: dict[str, Any], team_ids_: set[str]) -> bool:
    summary = str(play.get("play_summary") or "").lower()
    return (
        play.get("play_type") == "stole_base"
        and "scores on steal of home" in summary
        and bool(set(play.get("mentioned_player_ids") or []).intersection(team_ids_))
    )


def team_pitcher_strikeout(
    play: dict[str, Any],
    events: list[dict[str, Any]],
    home_team_id: str | None,
    away_team_id: str | None,
    team_ids_: set[str],
) -> bool:
    return play.get("play_type") == "strikeout" and pitcher_for_play(play, events, home_team_id, away_team_id) in team_ids_


def team_defensive_out(
    play: dict[str, Any],
    players: dict[str, Any],
    events: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    home_team_id: str | None,
    away_team_id: str | None,
    team_ids_: set[str],
) -> bool:
    if play.get("play_type") not in DEFENSIVE_OUT_TYPES:
        return False
    raw_event = events_by_id.get(str(play.get("pbp_id")).lower())
    return any(
        player_fielded_out(play, player_id, players.get(player_id, {}), raw_event, events, home_team_id, away_team_id)
        for player_id in team_ids_
    )


def highlight_candidate(
    play: dict[str, Any],
    players: dict[str, Any],
    events: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    home_team_id: str | None,
    away_team_id: str | None,
    team_ids_: set[str],
) -> tuple[int, str] | None:
    play_type = play.get("play_type")
    if play.get("exceptional_play"):
        return 0, "exceptional play"
    if not team_pa(play, team_ids_) and not team_pitcher_strikeout(play, events, home_team_id, away_team_id, team_ids_) and not team_defensive_out(
        play, players, events, events_by_id, home_team_id, away_team_id, team_ids_
    ):
        return None
    if play_type == "home_run":
        return 5, "team home run"
    if play_type == "triple":
        return 10, "team triple"
    if play_type in {"double_play", "caught_stealing"} and team_defensive_out(
        play, players, events, events_by_id, home_team_id, away_team_id, team_ids_
    ):
        return 8, "defensive highlight"
    if play_type == "double":
        return 15, "team double"
    if play_type in RUN_SCORING_PA_TYPES and team_pa(play, team_ids_) and team_player_scores(play, players, team_ids_):
        return 12, "run-scoring plate appearance"
    if team_steal_home(play, team_ids_):
        return 14, "steal of home"
    if team_defensive_out(play, players, events, events_by_id, home_team_id, away_team_id, team_ids_):
        return 45, "fielding out"
    if team_pitcher_strikeout(play, events, home_team_id, away_team_id, team_ids_):
        return 55, "pitcher strikeout"
    return None


def estimated_runtime(
    plays: list[dict[str, Any]],
    *,
    start_buffer: float,
    end_buffer: float,
    min_segment_length: float,
    time_shift: float = 0.0,
    long_clip_start_buffer: float = 18.0,
) -> float:
    return sum(
        segment.end - segment.start
        for segment in plays_to_segments(
            plays,
            start_buffer=start_buffer,
            end_buffer=end_buffer,
            min_duration=min_segment_length,
            time_shift=time_shift,
            long_clip_start_buffer=long_clip_start_buffer,
        )
    )


def select_by_target(
    candidates: list[tuple[int, str, dict[str, Any]]],
    *,
    target_duration: float,
    start_buffer: float,
    end_buffer: float,
    min_segment_length: float,
    time_shift: float,
    long_clip_start_buffer: float,
) -> list[tuple[str, dict[str, Any]]]:
    if target_duration <= 0:
        return [(reason, play) for _, reason, play in sorted(candidates, key=lambda item: item[2].get("index") or 0)]

    selected: list[tuple[int, str, dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], item[2].get("index") or 0)):
        selected.append(candidate)
        plays = [play for _, _, play in selected]
        if estimated_runtime(
            plays,
            start_buffer=start_buffer,
            end_buffer=end_buffer,
            min_segment_length=min_segment_length,
            time_shift=time_shift,
            long_clip_start_buffer=long_clip_start_buffer,
        ) >= target_duration:
            break
    return [(reason, play) for _, reason, play in sorted(selected, key=lambda item: item[2].get("index") or 0)]


def select_curated_highlights(
    game: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    target_duration: float = 180.0,
    start_buffer: float = 4.0,
    end_buffer: float = 2.0,
    min_segment_length: float = 12.0,
    time_shift: float = 0.0,
    long_clip_start_buffer: float = 18.0,
) -> list[dict[str, Any]]:
    return [
        play
        for _, play in select_curated_highlight_details(
            game,
            events,
            target_duration=target_duration,
            start_buffer=start_buffer,
            end_buffer=end_buffer,
            min_segment_length=min_segment_length,
            time_shift=time_shift,
            long_clip_start_buffer=long_clip_start_buffer,
        )
    ]


def select_curated_highlight_details(
    game: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    target_duration: float,
    start_buffer: float,
    end_buffer: float,
    min_segment_length: float,
    time_shift: float,
    long_clip_start_buffer: float,
) -> list[tuple[str, dict[str, Any]]]:
    players = game.get("players") or {}
    team_ids_ = team_player_ids(game)
    home_team_id, away_team_id = team_ids(game, events)
    events_by_id = events_by_pbp_id(events)

    candidates = []
    for play in game.get("plays") or []:
        candidate = highlight_candidate(play, players, events, events_by_id, home_team_id, away_team_id, team_ids_)
        if candidate:
            priority, reason = candidate
            candidates.append((priority, reason, play))
    return select_by_target(
        candidates,
        target_duration=target_duration,
        start_buffer=start_buffer,
        end_buffer=end_buffer,
        min_segment_length=min_segment_length,
        time_shift=time_shift,
        long_clip_start_buffer=long_clip_start_buffer,
    )


def write_selected_plays(path: Path, selected: list[tuple[str, dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for index, (reason, play) in enumerate(selected, start=1):
            f.write(
                f"{index:02d}. {play.get('video_timestamp') or ''} "
                f"{play.get('play_type') or ''} [{reason}] - {play.get('play_summary') or ''}\n"
            )


def main() -> None:
    args = parse_args()
    game = load_json(args.game_json)
    events = load_stream_events_for_game(args.game_json)
    source, cookie = video_source_from_game(game, args.video)

    if args.types:
        wanted = {item.strip() for item in args.types.split(",") if item.strip()}
        selected = [
            ("requested type", play)
            for play in game.get("plays") or []
            if play.get("play_type") in wanted or (args.include_exceptional and play.get("exceptional_play"))
        ]
    else:
        selected = select_curated_highlight_details(
            game,
            events,
            target_duration=args.target_duration,
            start_buffer=args.start_buffer,
            end_buffer=args.end_buffer,
            min_segment_length=args.min_segment_length,
            time_shift=args.time_shift,
            long_clip_start_buffer=args.long_clip_start_buffer,
        )
    plays = [play for _, play in selected]

    output = Path(args.output)
    segments = plays_to_segments(
        plays,
        start_buffer=args.start_buffer,
        end_buffer=args.end_buffer,
        min_duration=args.min_segment_length,
        time_shift=args.time_shift,
        long_clip_start_buffer=args.long_clip_start_buffer,
    )
    render_output = output
    temp_dir = None
    if args.scorebug:
        temp_dir = tempfile.TemporaryDirectory(prefix="gc-highlight-overlay-")
        render_output = Path(temp_dir.name) / "clean.mp4"
    make_reel(
        source,
        render_output,
        segments,
        cookie=cookie,
        reencode=args.reencode,
        max_merge_gap=args.max_merge_gap,
        cache_dir=None if args.no_cache else args.cache_dir,
    )
    if args.scorebug:
        assert temp_dir is not None
        overlays = write_scorebug_pngs(
            Path(temp_dir.name),
            clean_video=render_output,
            game=game,
            selected=selected,
            segments=segments,
            max_merge_gap=args.max_merge_gap,
            team_label=args.team_label,
            opp_label=args.opponent_label or opponent_label(game),
            description_overrides=read_description_overrides(Path(args.description_overrides) if args.description_overrides else None),
        )
        burn_in_png_overlays(render_output, output, overlays)
        temp_dir.cleanup()
    plays_output = Path(args.plays_output) if args.plays_output else output.with_suffix(".plays.txt")
    write_selected_plays(plays_output, selected)
    print(f"Wrote {args.output} ({len(plays)} plays)")
    print(f"Wrote {plays_output}")


if __name__ == "__main__":
    main()
