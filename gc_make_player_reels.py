#!/usr/bin/env python3
"""Create one or more player reels from a fetched GameChanger game.json."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from gc_common import (
    GCError,
    Segment,
    flatten_stream_events,
    load_json,
    make_reel,
    plays_to_segments,
    slugify,
    video_source_from_game,
)
from gc_make_condensed_game import burn_in_png_overlays, opponent_label, write_scorebug_pngs


ON_BASE_TYPES = {
    "single",
    "double",
    "triple",
    "home_run",
    "walk",
    "hit_by_pitch",
    "fielders_choice",
    "reached_on_error",
    "error",
}
OUT_TYPES = {
    "strikeout",
    "batter_out",
    "batter_out_advance_runners",
    "double_play",
    "caught_stealing",
}
RUNNER_ADVANCE_TYPES = {
    "stole_base",
    "advance",
    "advanced_on_error",
    "passed_ball",
    "wild_pitch",
}
ADVANCE_WORDS = (" steals ", " advances ", " scores", " scored")
ADVANCE_VERBS = ("steals", "advances", "scores", "scored")
POSITION_NAMES = {
    "P": ("pitcher",),
    "C": ("catcher",),
    "1B": ("first baseman",),
    "2B": ("second baseman",),
    "3B": ("third baseman",),
    "SS": ("shortstop",),
    "LF": ("left fielder",),
    "CF": ("center fielder",),
    "RF": ("right fielder",),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_json")
    parser.add_argument("--players", nargs="+", required=True, help="Player ids, numbers, names, or 'all'.")
    parser.add_argument("--video", help="Optional local video path or HLS URL override.")
    parser.add_argument("--out-dir", default="gc_output/player_reels")
    parser.add_argument("--reencode", action="store_true", help="Re-encode instead of stream-copying clips.")
    parser.add_argument("--max-merge-gap", type=float, default=1.0)
    parser.add_argument("--batter-pre-roll", type=float, default=6.0)
    parser.add_argument("--runner-pre-roll", type=float, default=4.0)
    parser.add_argument("--pitcher-pre-roll", type=float, default=5.0)
    parser.add_argument("--fielder-pre-roll", type=float, default=5.0)
    parser.add_argument("--post-roll", type=float, default=4.0)
    parser.add_argument("--start-buffer", type=float, default=4.0, help="Additional seconds to subtract from every segment start.")
    parser.add_argument("--end-buffer", type=float, default=2.0, help="Additional seconds to add to every segment end.")
    parser.add_argument("--min-segment-length", type=float, default=12.0, help="Minimum seconds to keep for each selected moment.")
    parser.add_argument("--time-shift", type=float, default=0.0, help="Shift every play timestamp by this many seconds before cutting.")
    parser.add_argument("--long-clip-start-buffer", type=float, default=18.0, help="Pre-roll to use for longer GameChanger clips.")
    parser.add_argument("--cache-dir", default="gc_render_cache/segments", help="Directory for reusable local clip segments.")
    parser.add_argument("--no-cache", action="store_true", help="Disable reusable local clip segment cache.")
    parser.add_argument("--scorebug", action="store_true", help="Burn in a centered scorebug and play description.")
    parser.add_argument("--team-label", default="TIG", help="Scorebug label for the fetched team.")
    parser.add_argument("--opponent-label", help="Scorebug label for the opponent. Defaults to an abbreviation from the opponent name.")
    return parser.parse_args()


def player_label(game: dict, selector: str) -> str:
    players = game.get("players") or {}
    selector_l = selector.lower()
    for player_id, player in players.items():
        if selector_l in {
            player_id.lower(),
            str(player.get("number") or "").lower(),
            str(player.get("display") or "").lower(),
        } or selector_l in str(player.get("display") or "").lower():
            return str(player.get("display") or selector)
    return selector


def all_player_selectors(game: dict) -> list[str]:
    ids: set[str] = set()
    known_player_ids = set((game.get("players") or {}).keys())
    for play in game.get("plays") or []:
        ids.update(play.get("mentioned_player_ids") or [])
    if known_player_ids:
        ids = ids.intersection(known_player_ids)
    return sorted(ids)


def resolve_player_ids(game: dict[str, Any], selector: str) -> set[str]:
    players = game.get("players") or {}
    selector_l = selector.strip().lower()
    selected_ids: set[str] = set()
    for player_id, player in players.items():
        values = {
            player_id.lower(),
            str(player.get("number") or "").lower(),
            str(player.get("display") or "").lower(),
            f"{player.get('first_name', '')} {player.get('last_name', '')}".strip().lower(),
        }
        if selector_l in values or selector_l in str(player.get("display") or "").lower():
            selected_ids.add(player_id)
    if not selected_ids:
        selected_ids.add(selector)
    return selected_ids


def load_stream_events_for_game(game_json_path: str) -> list[dict[str, Any]]:
    path = Path(game_json_path).with_name("stream_events_raw.json")
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return flatten_stream_events(json.load(f))


def events_by_pbp_id(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(event.get("id")).lower(): event for event in events if event.get("id")}


def team_ids(game: dict[str, Any], events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    team_id = game.get("team_id")
    opponent_id = ((game.get("game_summary") or {}).get("game_stream") or {}).get("opponent_id")
    home_away = (game.get("game_summary") or {}).get("home_away") or (game.get("public_details") or {}).get("home_away")
    if team_id and opponent_id and home_away:
        return (team_id, opponent_id) if home_away == "home" else (opponent_id, team_id)

    for event in events:
        if event.get("code") == "set_teams":
            attrs = event.get("attributes") or {}
            return attrs.get("homeId"), attrs.get("awayId")
    return team_id, opponent_id


def pitcher_for_play(
    play: dict[str, Any],
    events: list[dict[str, Any]],
    home_team_id: str | None,
    away_team_id: str | None,
) -> str | None:
    if not home_team_id or not away_team_id:
        return None
    inning_half = str(play.get("inning_half") or "").lower()
    pitching_team_id = home_team_id if inning_half == "top" else away_team_id if inning_half == "bottom" else None
    if not pitching_team_id:
        return None
    at_ms = play.get("raw_event_created_at_ms")
    if at_ms is None:
        return None

    pitcher_id = None
    for event in sorted(events, key=lambda e: (e.get("createdAt") or 0, e.get("_stream_sequence_number") or 0)):
        created_at = event.get("createdAt")
        if created_at is not None and created_at > at_ms:
            break
        if event.get("code") != "fill_position":
            continue
        attrs = event.get("attributes") or {}
        if attrs.get("teamId") == pitching_team_id and attrs.get("position") == "P":
            pitcher_id = attrs.get("playerId")
    return pitcher_id


def fielding_team_for_play(
    play: dict[str, Any],
    home_team_id: str | None,
    away_team_id: str | None,
) -> str | None:
    if not home_team_id or not away_team_id:
        return None
    inning_half = str(play.get("inning_half") or "").lower()
    return home_team_id if inning_half == "top" else away_team_id if inning_half == "bottom" else None


def position_for_player(
    play: dict[str, Any],
    events: list[dict[str, Any]],
    home_team_id: str | None,
    away_team_id: str | None,
    player_id: str,
) -> str | None:
    fielding_team_id = fielding_team_for_play(play, home_team_id, away_team_id)
    if not fielding_team_id:
        return None
    at_ms = play.get("raw_event_created_at_ms")
    if at_ms is None:
        return None

    current_position = None
    for event in sorted(events, key=lambda e: (e.get("createdAt") or 0, e.get("_stream_sequence_number") or 0)):
        created_at = event.get("createdAt")
        if created_at is not None and created_at > at_ms:
            break
        if event.get("code") != "fill_position":
            continue
        attrs = event.get("attributes") or {}
        if attrs.get("teamId") == fielding_team_id and attrs.get("playerId") == player_id:
            current_position = attrs.get("position")
    return str(current_position) if current_position else None


def player_name_in_summary(summary: str, player: dict[str, Any]) -> bool:
    display = str(player.get("display") or "").lower()
    first = str(player.get("first_name") or "").lower()
    last = str(player.get("last_name") or "").lower()
    names = [value for value in {display, first, f"{first} {last}".strip()} if value]
    return any(name in summary for name in names)


def player_fielded_out(
    play: dict[str, Any],
    player_id: str,
    player: dict[str, Any],
    raw_event: dict[str, Any] | None,
    events: list[dict[str, Any]],
    home_team_id: str | None,
    away_team_id: str | None,
) -> bool:
    if play.get("play_type") not in OUT_TYPES:
        return False

    position = position_for_player(play, events, home_team_id, away_team_id, player_id)
    defender_positions = {
        str(defender.get("position"))
        for defender in ((raw_event or {}).get("attributes") or {}).get("defenders") or []
        if defender.get("position")
    }
    if position and position in defender_positions:
        return True

    summary = f" {play.get('play_summary') or ''} ".lower()
    if not position or not player_name_in_summary(summary, player):
        return False
    return any(position_name in summary for position_name in POSITION_NAMES.get(position, ()))


def player_advanced(play: dict[str, Any], player_id: str, raw_event: dict[str, Any] | None) -> bool:
    attrs = (raw_event or {}).get("attributes") or {}
    if attrs.get("runnerId") == player_id and attrs.get("playType") in RUNNER_ADVANCE_TYPES:
        return True
    summary = f" {play.get('play_summary') or ''} ".lower()
    player = player_id.lower()
    if player in summary and any(word in summary for word in ADVANCE_WORDS):
        return True
    return False


def player_name_advanced(play: dict[str, Any], player: dict[str, Any]) -> bool:
    summary = f" {play.get('play_summary') or ''} ".lower()
    display = str(player.get("display") or "").lower()
    first = str(player.get("first_name") or "").lower()
    last = str(player.get("last_name") or "").lower()
    full_name = f"{first} {last}".strip()
    names = [value for value in {display, full_name} if value]
    for name in names:
        pattern = rf"\b{re.escape(name)}(?:\s+#?\d+)?\s+({'|'.join(ADVANCE_VERBS)})\b"
        if re.search(pattern, summary):
            return True
    return False


def video_duration(game: dict[str, Any]) -> float | None:
    for asset in [game.get("video_asset") or {}, *((game.get("playback_assets") or []))]:
        duration = asset.get("duration")
        if duration is not None:
            return float(duration)
    return None


def moment_segment(
    play: dict[str, Any],
    pre_roll: float,
    post_roll: float,
    *,
    start_buffer: float,
    end_buffer: float,
    min_segment_length: float,
    time_shift: float,
    long_clip_start_buffer: float,
) -> Segment | None:
    del pre_roll, post_roll
    segments = plays_to_segments(
        [play],
        start_buffer=start_buffer,
        end_buffer=end_buffer,
        min_duration=min_segment_length,
        time_shift=time_shift,
        long_clip_start_buffer=long_clip_start_buffer,
    )
    return segments[0] if segments else None


def select_player_moment_details(
    game: dict[str, Any],
    selector: str,
    events: list[dict[str, Any]],
    *,
    batter_pre_roll: float,
    runner_pre_roll: float,
    pitcher_pre_roll: float,
    fielder_pre_roll: float,
    post_roll: float,
    start_buffer: float,
    end_buffer: float,
    min_segment_length: float,
    time_shift: float,
    long_clip_start_buffer: float,
) -> list[tuple[str, dict[str, Any], Segment]]:
    selected_ids = resolve_player_ids(game, selector)
    players = game.get("players") or {}
    by_id = events_by_pbp_id(events)
    home_team_id, away_team_id = team_ids(game, events)

    details: list[tuple[str, dict[str, Any], Segment]] = []
    seen: set[tuple[str, str]] = set()
    for play in game.get("plays") or []:
        play_type = play.get("play_type")
        mentioned = play.get("mentioned_player_ids") or []
        raw_event = by_id.get(str(play.get("pbp_id")).lower())

        for player_id in selected_ids:
            role = None
            pre_roll = batter_pre_roll
            if play_type in ON_BASE_TYPES and mentioned and mentioned[0] == player_id:
                role = "batter"
                pre_roll = batter_pre_roll
            elif player_advanced(play, player_id, raw_event) or player_name_advanced(play, players.get(player_id, {})):
                role = "runner"
                pre_roll = runner_pre_roll
            elif play_type in OUT_TYPES and pitcher_for_play(play, events, home_team_id, away_team_id) == player_id:
                role = "pitcher"
                pre_roll = pitcher_pre_roll
            elif player_fielded_out(play, player_id, players.get(player_id, {}), raw_event, events, home_team_id, away_team_id):
                role = "fielder"
                pre_roll = fielder_pre_roll

            if not role:
                continue
            key = (str(play.get("clip_metadata_id") or play.get("pbp_id") or play.get("index")), role)
            if key in seen:
                continue
            segment = moment_segment(
                play,
                pre_roll,
                post_roll,
                start_buffer=start_buffer,
                end_buffer=end_buffer,
                min_segment_length=min_segment_length,
                time_shift=time_shift,
                long_clip_start_buffer=long_clip_start_buffer,
            )
            if segment:
                details.append((role, play, segment))
                seen.add(key)
    return details


def select_player_moments(
    game: dict[str, Any],
    selector: str,
    events: list[dict[str, Any]],
    *,
    batter_pre_roll: float,
    runner_pre_roll: float,
    pitcher_pre_roll: float,
    fielder_pre_roll: float,
    post_roll: float,
    start_buffer: float,
    end_buffer: float,
    min_segment_length: float,
    time_shift: float = 0.0,
    long_clip_start_buffer: float = 18.0,
) -> list[Segment]:
    details = select_player_moment_details(
        game,
        selector,
        events,
        batter_pre_roll=batter_pre_roll,
        runner_pre_roll=runner_pre_roll,
        pitcher_pre_roll=pitcher_pre_roll,
        fielder_pre_roll=fielder_pre_roll,
        post_roll=post_roll,
        start_buffer=start_buffer,
        end_buffer=end_buffer,
        min_segment_length=min_segment_length,
        time_shift=time_shift,
        long_clip_start_buffer=long_clip_start_buffer,
    )
    return [segment for _, _, segment in details]


def main() -> None:
    args = parse_args()
    game = load_json(args.game_json)
    events = load_stream_events_for_game(args.game_json)
    source, cookie = video_source_from_game(game, args.video)
    selectors = all_player_selectors(game) if args.players == ["all"] else args.players
    out_dir = Path(args.out_dir)

    made = 0
    for selector in selectors:
        details = select_player_moment_details(
            game,
            selector,
            events,
            batter_pre_roll=args.batter_pre_roll,
            runner_pre_roll=args.runner_pre_roll,
            pitcher_pre_roll=args.pitcher_pre_roll,
            fielder_pre_roll=args.fielder_pre_roll,
            post_roll=args.post_roll,
            start_buffer=args.start_buffer,
            end_buffer=args.end_buffer,
            min_segment_length=args.min_segment_length,
            time_shift=args.time_shift,
            long_clip_start_buffer=args.long_clip_start_buffer,
        )
        segments = [segment for _, _, segment in details]
        if not segments:
            print(f"Skipping {selector}: no matching plays")
            continue
        label = player_label(game, selector)
        output = out_dir / f"{slugify(label)}.mp4"
        render_output = output
        temp_dir = None
        if args.scorebug:
            temp_dir = tempfile.TemporaryDirectory(prefix="gc-player-overlay-")
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
            selected = [(role, play) for role, play, _ in details]
            overlays = write_scorebug_pngs(
                Path(temp_dir.name),
                clean_video=render_output,
                game=game,
                selected=selected,
                segments=segments,
                max_merge_gap=args.max_merge_gap,
                team_label=args.team_label,
                opp_label=args.opponent_label or opponent_label(game),
                description_overrides={},
            )
            burn_in_png_overlays(render_output, output, overlays)
            temp_dir.cleanup()
        print(f"Wrote {output} ({len(segments)} moments)")
        made += 1
    if not made:
        raise GCError("No player reels created.")


if __name__ == "__main__":
    main()
