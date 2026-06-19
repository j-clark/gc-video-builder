#!/usr/bin/env python3
"""Create a condensed game from the fetched GameChanger play index."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from gc_common import Segment, load_json, make_reel, plays_to_segments, run, video_source_from_game


PA_OUTCOME_TYPES = {
    "single",
    "double",
    "triple",
    "home_run",
    "walk",
    "hit_by_pitch",
    "fielders_choice",
    "reached_on_error",
    "error",
    "strikeout",
    "batter_out",
    "batter_out_advance_runners",
    "double_play",
}
BASERUNNING_TYPES = {"stole_base", "caught_stealing"}
BATTED_BALL_TYPES = {
    "single",
    "double",
    "triple",
    "home_run",
    "batter_out",
    "batter_out_advance_runners",
    "double_play",
    "fielders_choice",
    "reached_on_error",
    "error",
}
TIGERS_ORANGE = (234, 117, 38, 255)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_json")
    parser.add_argument("--video", help="Optional local video path or HLS URL override.")
    parser.add_argument("--output", default="gc_output/condensed_game.mp4")
    parser.add_argument("--exclude-types", default="", help="Comma-separated play types to omit.")
    parser.add_argument("--include-all-play-types", action="store_true", help="Use every indexed play instead of PA outcomes plus steals.")
    parser.add_argument("--max-plays", type=int, help="Render only the first N selected plays for QA.")
    parser.add_argument("--reencode", action="store_true")
    parser.add_argument("--target-duration", type=float, default=600.0, help="Approximate target runtime in seconds.")
    parser.add_argument("--max-merge-gap", type=float, default=4.0, help="Merge nearby plays into continuous action.")
    parser.add_argument("--start-buffer", type=float, default=4.0, help="Additional seconds to subtract from every segment start.")
    parser.add_argument("--end-buffer", type=float, default=6.0, help="Additional seconds to add to every segment end.")
    parser.add_argument("--min-segment-length", type=float, default=12.0, help="Minimum seconds to keep for each selected play.")
    parser.add_argument("--time-shift", type=float, default=0.0, help="Shift every play timestamp by this many seconds before cutting.")
    parser.add_argument("--long-clip-start-buffer", type=float, default=6.0, help="Pre-roll to use for longer GameChanger clips.")
    parser.add_argument("--batted-ball-start-buffer", type=float, default=6.0, help="Pre-roll to use for batted-ball plays.")
    parser.add_argument("--extra-start-play-indexes", default="", help="Comma-separated raw play indexes that need extra pre-roll.")
    parser.add_argument("--extra-start-buffer", type=float, default=18.0, help="Pre-roll for plays listed in --extra-start-play-indexes.")
    parser.add_argument("--cache-dir", default="gc_render_cache/segments", help="Directory for reusable local clip segments.")
    parser.add_argument("--no-cache", action="store_true", help="Disable reusable local clip segment cache.")
    parser.add_argument("--plays-output", help="Optional text file listing selected plays. Defaults to OUTPUT.plays.txt.")
    parser.add_argument("--descriptions-output", help="Optional markdown file listing proposed overlay descriptions.")
    parser.add_argument("--description-overrides", help="Optional markdown review file whose Proposed lines override generated overlay descriptions.")
    parser.add_argument("--descriptions-only", action="store_true", help="Write selected plays and proposed descriptions without rendering video.")
    parser.add_argument("--scorebug", action="store_true", help="Burn in a centered scorebug and play description.")
    parser.add_argument("--team-label", help="Scorebug label for the fetched team. Defaults to HOME or AWAY.")
    parser.add_argument("--opponent-label", help="Scorebug label for the opponent. Defaults to an abbreviation from the opponent name.")
    return parser.parse_args()


def estimated_runtime(
    plays: list[dict[str, Any]],
    *,
    start_buffer: float,
    end_buffer: float,
    min_segment_length: float,
    time_shift: float = 0.0,
    long_clip_start_buffer: float = 10.0,
    batted_ball_start_buffer: float = 6.0,
    extra_start_play_indexes: set[int] | None = None,
    extra_start_buffer: float = 18.0,
) -> float:
    return sum(
        segment.end - segment.start
        for segment in condensed_plays_to_segments(
            plays,
            start_buffer=start_buffer,
            end_buffer=end_buffer,
            min_duration=min_segment_length,
            time_shift=time_shift,
            long_clip_start_buffer=long_clip_start_buffer,
            batted_ball_start_buffer=batted_ball_start_buffer,
            extra_start_play_indexes=extra_start_play_indexes or set(),
            extra_start_buffer=extra_start_buffer,
        )
    )


def condensed_plays_to_segments(
    plays: list[dict[str, Any]],
    *,
    start_buffer: float,
    end_buffer: float,
    min_duration: float,
    time_shift: float,
    long_clip_start_buffer: float,
    batted_ball_start_buffer: float,
    extra_start_play_indexes: set[int],
    extra_start_buffer: float,
) -> list[Segment]:
    segments = []
    for play in plays:
        play_type_buffer = (
            batted_ball_start_buffer
            if play.get("play_type") in BATTED_BALL_TYPES
            else long_clip_start_buffer
        )
        if int(play.get("index") or -1) in extra_start_play_indexes:
            play_type_buffer = max(play_type_buffer, extra_start_buffer)
        segments.extend(
            plays_to_segments(
                [play],
                start_buffer=start_buffer,
                end_buffer=end_buffer,
                min_duration=min_duration,
                time_shift=time_shift,
                long_clip_start_buffer=play_type_buffer,
            )
        )
    return segments


def stolen_base_priority(play: dict[str, Any]) -> tuple[int, str]:
    summary = str(play.get("play_summary") or "").lower()
    if play.get("play_type") == "caught_stealing":
        return 5, "caught stealing"
    if "scores on steal of home" in summary:
        return 10, "steal of home"
    if "steals 3rd" in summary:
        return 20, "steal of third"
    return 40, "steal of second"


def select_condensed_plays(
    game: dict[str, Any],
    *,
    include_all_play_types: bool,
    excluded: set[str],
    target_duration: float,
    start_buffer: float,
    end_buffer: float,
    min_segment_length: float,
    time_shift: float,
    long_clip_start_buffer: float,
    batted_ball_start_buffer: float,
    extra_start_play_indexes: set[int],
    extra_start_buffer: float,
) -> list[tuple[str, dict[str, Any]]]:
    if include_all_play_types:
        return [
            ("all indexed plays", play)
            for play in game.get("plays") or []
            if play.get("play_type") not in excluded
        ]

    required = [
        ("plate appearance outcome", play)
        for play in game.get("plays") or []
        if play.get("play_type") in PA_OUTCOME_TYPES and play.get("play_type") not in excluded
    ]
    selected = list(required)
    selected_ids = {id(play) for _, play in selected}
    steals = [
        (*stolen_base_priority(play), play)
        for play in game.get("plays") or []
        if play.get("play_type") in BASERUNNING_TYPES and play.get("play_type") not in excluded
    ]
    for priority, reason, play in sorted(steals, key=lambda item: (item[0], item[2].get("index") or 0)):
        if priority > 10 or id(play) in selected_ids:
            continue
        selected.append((reason, play))
        selected_ids.add(id(play))

    for priority, reason, play in sorted(steals, key=lambda item: (item[0], item[2].get("index") or 0)):
        if id(play) in selected_ids:
            continue
        if target_duration > 0 and estimated_runtime(
            [selected_play for _, selected_play in selected],
            start_buffer=start_buffer,
            end_buffer=end_buffer,
            min_segment_length=min_segment_length,
            time_shift=time_shift,
            long_clip_start_buffer=long_clip_start_buffer,
            batted_ball_start_buffer=batted_ball_start_buffer,
            extra_start_play_indexes=extra_start_play_indexes,
            extra_start_buffer=extra_start_buffer,
        ) >= target_duration:
            break
        selected.append((reason, play))
        selected_ids.add(id(play))
        if target_duration > 0 and estimated_runtime(
            [selected_play for _, selected_play in selected],
            start_buffer=start_buffer,
            end_buffer=end_buffer,
            min_segment_length=min_segment_length,
            time_shift=time_shift,
            long_clip_start_buffer=long_clip_start_buffer,
            batted_ball_start_buffer=batted_ball_start_buffer,
            extra_start_play_indexes=extra_start_play_indexes,
            extra_start_buffer=extra_start_buffer,
        ) >= target_duration:
            break

    return sorted(selected, key=lambda item: item[1].get("index") or 0)


def write_selected_plays(path: Path, selected: list[tuple[str, dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for index, (reason, play) in enumerate(selected, start=1):
            f.write(
                f"{index:02d}. {play.get('video_timestamp') or ''} "
                f"{play.get('play_type') or ''} [{reason}] - {play.get('play_summary') or ''}\n"
            )


def parse_index_set(value: str) -> set[int]:
    indexes = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            indexes.add(int(item))
    return indexes


def parse_play_seconds(play: dict[str, Any]) -> float:
    value = str(play.get("video_timestamp") or "")
    if not value:
        return float(play.get("index") or 0)
    parts = [int(part) for part in value.split(":") if part.isdigit()]
    if len(parts) == 3:
        return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    if len(parts) == 2:
        return float(parts[0] * 60 + parts[1])
    if len(parts) == 1:
        return float(parts[0])
    return float(play.get("index") or 0)


def opponent_label(game: dict[str, Any]) -> str:
    name = (
        ((game.get("public_details") or {}).get("opponent_team") or {}).get("name")
        or ((game.get("schedule_event") or {}).get("pregame_data") or {}).get("opponent_name")
        or "OPP"
    )
    words = [re.sub(r"[^A-Za-z0-9]", "", word).upper() for word in str(name).split()]
    words = [word for word in words if word and not word.isdigit()]
    if not words:
        return "OPP"
    return words[0][:4]


def own_player_prefixes(game: dict[str, Any]) -> list[str]:
    prefixes = []
    for player in (game.get("players") or {}).values():
        display = str(player.get("display") or "").strip()
        if display:
            prefixes.append(display)
            prefixes.append(display.split(" #", 1)[0])
    return sorted(set(prefixes), key=len, reverse=True)


def infer_own_batting(play: dict[str, Any], prefixes: list[str], fallback: bool = True) -> bool:
    summary = str(play.get("play_summary") or "")
    return next((True for prefix in prefixes if summary.startswith(prefix)), fallback)


def infer_half(play: dict[str, Any], own_batting: bool, home_away: str) -> str:
    own_is_home = home_away != "away"
    if own_batting == own_is_home:
        return "bottom"
    return "top"


def run_count(play: dict[str, Any]) -> int:
    summary = str(play.get("play_summary") or "").lower()
    runs = summary.count(" scores")
    if play.get("play_type") == "home_run":
        runs += 1
    return runs


def strip_player_numbers(value: str) -> str:
    return re.sub(r"\s+#\d+\b", "", value).strip()


def clean_player_name(value: str, *, opponent_value: str = "Opponent") -> str:
    value = strip_player_numbers(value)
    value = re.sub(r"\bPlayer [a-f0-9]{8}\b", opponent_value, value)
    return " ".join(value.split())


def possessive_name(value: str) -> str:
    return f"{value}'" if value.endswith("s") else f"{value}'s"


def base_word(value: str) -> str:
    return {
        "1": "first",
        "1st": "first",
        "2": "second",
        "2nd": "second",
        "3": "third",
        "3rd": "third",
        "4": "home",
        "home": "home",
    }.get(value, value)


def primary_actor(summary: str, prefixes: list[str]) -> str:
    stripped = strip_player_numbers(summary)
    for prefix in prefixes:
        if stripped.startswith(strip_player_numbers(prefix)):
            return strip_player_numbers(prefix)
    match = re.match(r"(Player [a-f0-9]{8}|.+?)\s+(?:is hit by pitch|walks|strikes out|singles|doubles|triples|hits|grounds|flies|pops|lines|steals|scores|caught)", stripped)
    if match:
        return clean_player_name(match.group(1), opponent_value="Opponent")
    return "Opponent" if stripped.startswith("Player ") else "Play"


def pitcher_from_summary(summary: str) -> str | None:
    match = re.search(r",\s*([^.,]+?)(?:\s+#?\d+)?\s+pitching\b", strip_player_numbers(summary))
    if not match:
        return None
    pitcher = clean_player_name(match.group(1), opponent_value="")
    return pitcher or None


def run_scorers(summary: str, prefixes: list[str]) -> list[str]:
    scorers = []
    stripped = strip_player_numbers(summary)
    for prefix in prefixes:
        name = strip_player_numbers(prefix)
        if name not in scorers and re.search(rf"\b{re.escape(name)}\b\s+scores\b", stripped):
            scorers.append(name)
    if "Opponent" not in scorers and re.search(r"\bPlayer [a-f0-9]{8}\s+scores\b", stripped):
        scorers.append("Opponent")
    return scorers


def occupied_bases(summary: str) -> set[str]:
    bases = set()
    for match in re.finditer(r"(?:remains at|held up at|advances to)\s+([123](?:st|nd|rd))", summary):
        bases.add(match.group(1))
    return bases


def base_context_phrase(summary: str) -> str:
    bases = occupied_bases(summary)
    if len(bases) >= 2:
        return "to load the bases"
    if not bases:
        return ""
    return f"with a runner on {base_word(next(iter(bases)))}"


def remove_base_context(value: str) -> str:
    value = re.sub(r"\s+to load the bases\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+with (?:a runner|runners?|two runners?|two|three) on (?:base|bases|first|second|third)\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+with (?:one|two|three) on\b", "", value, flags=re.IGNORECASE)
    return " ".join(value.split()).strip()


def action_from_out_summary(summary: str) -> str:
    lowered = summary.lower()
    for verb in ["grounds out", "flies out", "pops out", "lines out"]:
        if verb in lowered:
            return verb
    return "is retired"


def defender_names(summary: str, prefixes: list[str]) -> list[str]:
    names: list[tuple[int, str]] = []
    stripped = strip_player_numbers(summary)
    for prefix in prefixes:
        name = strip_player_numbers(prefix)
        match = re.search(rf"\b{re.escape(name)}\b", stripped)
        if match and not stripped.startswith(name):
            names.append((match.start(), name))
    ordered = []
    for _, name in sorted(names):
        if name not in ordered:
            ordered.append(name)
    return ordered


def format_runs_text(scorers: list[str]) -> str:
    if not scorers:
        return ""
    if len(scorers) == 1:
        if scorers[0] == "Opponent":
            return " to score a run"
        return f" to score {scorers[0]}"
    return f" to drive in {len(scorers)}"


def simplified_play_description(play: dict[str, Any], prefixes: list[str] | None = None) -> str:
    prefixes = prefixes or []
    summary = str(play.get("play_summary") or "")
    play_type = str(play.get("play_type") or "")
    actor = primary_actor(summary, prefixes)
    pitcher = pitcher_from_summary(summary)
    scorers = run_scorers(summary, prefixes)
    run_text = format_runs_text(scorers)
    own_actor = actor != "Opponent"

    if play_type == "hit_by_pitch":
        return f"{actor} is hit by pitch"
    if play_type == "walk":
        return f"{actor} walks"
    if play_type == "strikeout":
        if own_actor:
            return f"{actor} strikes out"
        if pitcher:
            return f"{pitcher} gets a strikeout"
        return "Opponent batter strikes out"
    if play_type in {"single", "double", "triple"}:
        verb = {"single": "singles", "double": "doubles", "triple": "triples"}[play_type]
        return f"{actor} {verb}{run_text}"
    if play_type == "home_run":
        inside = "inside-the-park " if "inside the park" in summary.lower() else ""
        return f"{actor} hits an {inside}home run"
    if play_type == "stole_base":
        if "scores on steal of home" in summary:
            return f"{actor} steals home"
        match = re.search(r"steals\s+([123](?:st|nd|rd))", summary)
        base = base_word(match.group(1)) if match else "a base"
        return f"{actor} steals {base}"
    if play_type == "caught_stealing":
        base_match = re.search(r"caught stealing\s+([123](?:st|nd|rd))", summary)
        base = base_word(base_match.group(1)) if base_match else "a base"
        defenders = defender_names(summary, prefixes)
        if defenders:
            if len(defenders) >= 2:
                return f"{defenders[0]} and {defenders[1]} catch runner stealing {base}"
            return f"{defenders[0]} catches runner stealing {base}"
        return f"{actor} caught stealing {base}"
    if play_type == "double_play":
        defenders = defender_names(summary, prefixes)
        if len(defenders) >= 2:
            return f"{defenders[0]} and {defenders[1]} turn a double play"
        if defenders:
            return f"{defenders[0]} helps turn a double play"
        return "Double play"
    if play_type in {"batter_out", "batter_out_advance_runners"}:
        action = action_from_out_summary(summary)
        if own_actor:
            if scorers:
                return f"{actor} {action}; {', '.join(scorers)} scores"
            return f"{actor} {action}"
        defenders = defender_names(summary, prefixes)
        if len(defenders) >= 2:
            return f"{defenders[0]} and {defenders[1]} record the out"
        if defenders:
            return f"{defenders[0]} records the out"
        return f"Opponent batter {action}"
    if play_type in {"fielders_choice", "reached_on_error", "error"}:
        return f"{actor} reaches{run_text}"
    return remove_base_context(clean_player_name(summary))


def play_override_key(play: dict[str, Any]) -> str:
    return f"{play.get('video_timestamp') or ''}|{play.get('play_type') or ''}"


def read_description_overrides(path: Path | None) -> dict[int | str, str]:
    if path is None or not path.exists():
        return {}
    overrides: dict[int | str, str] = {}
    current_index: int | None = None
    current_key: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        header = re.match(r"^##\s+(\d+)\.\s+([^`]+?)\s+`([^`]+)`", line)
        if header:
            current_index = int(header.group(1))
            current_key = f"{header.group(2).strip()}|{header.group(3).strip()}"
            continue
        legacy_header = re.match(r"^##\s+(\d+)\.", line)
        if legacy_header:
            current_index = int(legacy_header.group(1))
            current_key = None
            continue
        if current_index is not None and line.startswith("Proposed:"):
            value = remove_base_context(line.split(":", 1)[1].strip())
            if value:
                if current_key:
                    overrides[current_key] = value
                else:
                    overrides[current_index] = value
    return overrides


def selected_description(
    selected_index: int,
    play: dict[str, Any],
    prefixes: list[str],
    overrides: dict[int | str, str],
) -> str:
    return overrides.get(play_override_key(play)) or overrides.get(selected_index) or remove_base_context(simplified_play_description(play, prefixes))


def write_description_review(
    path: Path,
    selected: list[tuple[str, dict[str, Any]]],
    game: dict[str, Any],
    overrides: dict[int, str] | None = None,
) -> None:
    prefixes = own_player_prefixes(game)
    overrides = overrides or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Condensed Game Overlay Description Review\n\n")
        f.write("Edit or comment on the `Proposed` lines before rerendering with `--scorebug`.\n\n")
        for index, (reason, play) in enumerate(selected, start=1):
            f.write(f"## {index:02d}. {play.get('video_timestamp') or ''} `{play.get('play_type') or ''}` [{reason}]\n\n")
            f.write(f"Proposed: {selected_description(index, play, prefixes, overrides)}\n\n")
            f.write(f"Original: {play.get('play_summary') or ''}\n\n")


def scorebug_contexts(game: dict[str, Any], team_label: str, opp_label: str) -> dict[int, str]:
    public_details = game.get("public_details") or {}
    line_score = public_details.get("line_score") or {}
    team_scores = list(((line_score.get("team") or {}).get("scores")) or [])
    opp_scores = list(((line_score.get("opponent_team") or {}).get("scores")) or [])
    home_away = str(public_details.get("home_away") or (game.get("game_summary") or {}).get("home_away") or "home")
    own_is_home = home_away != "away"
    prefixes = own_player_prefixes(game)
    contexts: dict[int, str] = {}
    current_half_runs: dict[tuple[int, str], int] = {}
    previous_own_batting = True

    ordered = sorted(game.get("plays") or [], key=lambda play: (parse_play_seconds(play), play.get("index") or 0))
    for play in ordered:
        inning = int(play.get("inning") or 1)
        own_batting = infer_own_batting(play, prefixes, previous_own_batting)
        previous_own_batting = own_batting
        half = infer_half(play, own_batting, home_away)
        before_inning = max(0, inning - 1)

        own_score = sum(team_scores[:before_inning])
        opp_score = sum(opp_scores[:before_inning])
        if own_is_home:
            if half == "bottom" and len(opp_scores) >= inning:
                opp_score += int(opp_scores[inning - 1])
        elif half == "bottom" and len(team_scores) >= inning:
            own_score += int(team_scores[inning - 1])

        own_score += current_half_runs.get((inning, "own"), 0)
        opp_score += current_half_runs.get((inning, "opp"), 0)

        half_label = "BOT" if half == "bottom" else "TOP"
        contexts[id(play)] = f"{half_label} {inning}   {team_label} {own_score}  |  {opp_label} {opp_score}"

        runs = run_count(play)
        if runs:
            key = (inning, "own" if own_batting else "opp")
            current_half_runs[key] = current_half_runs.get(key, 0) + runs

    return contexts


def ordinal_base(value: str) -> int | None:
    normalized = value.lower()
    if normalized in {"1", "1st", "first"}:
        return 1
    if normalized in {"2", "2nd", "second"}:
        return 2
    if normalized in {"3", "3rd", "third"}:
        return 3
    if normalized in {"4", "home"}:
        return 4
    return None


def previous_base(base: int) -> int | None:
    if base == 2:
        return 1
    if base == 3:
        return 2
    if base == 4:
        return 3
    return None


def inferred_before_bases(play: dict[str, Any]) -> set[int]:
    summary = str(play.get("play_summary") or "")
    bases: set[int] = set()
    for match in re.finditer(r"(?:remains at|held up at)\s+([123](?:st|nd|rd))", summary):
        base = ordinal_base(match.group(1))
        if base:
            bases.add(base)
    for match in re.finditer(r"advances to\s+([23](?:nd|rd))", summary):
        base = ordinal_base(match.group(1))
        previous = previous_base(base) if base else None
        if previous:
            bases.add(previous)
    if "scores on steal of home" in summary:
        bases.add(3)
    for match in re.finditer(r"steals\s+([23](?:nd|rd))", summary):
        base = ordinal_base(match.group(1))
        previous = previous_base(base) if base else None
        if previous:
            bases.add(previous)
    for match in re.finditer(r"caught stealing\s+([23](?:nd|rd))", summary):
        base = ordinal_base(match.group(1))
        previous = previous_base(base) if base else None
        if previous:
            bases.add(previous)
    return bases


def after_bases(play: dict[str, Any], before: set[int]) -> set[int]:
    summary = str(play.get("play_summary") or "")
    play_type = str(play.get("play_type") or "")
    after = set(before)

    for match in re.finditer(r"(?:remains at|held up at|advances to)\s+([123](?:st|nd|rd))", summary):
        base = ordinal_base(match.group(1))
        if base:
            after.add(base)

    if play_type in {"single", "walk", "hit_by_pitch", "fielders_choice", "reached_on_error", "error"}:
        after.add(1)
    elif play_type == "double":
        after.add(2)
    elif play_type == "triple":
        after.add(3)
    elif play_type == "home_run":
        after.clear()

    for match in re.finditer(r"steals\s+([23](?:nd|rd))", summary):
        base = ordinal_base(match.group(1))
        previous = previous_base(base) if base else None
        if previous:
            after.discard(previous)
        if base:
            after.add(base)
    if "scores on steal of home" in summary:
        after.discard(3)

    for match in re.finditer(r"caught stealing\s+([23](?:nd|rd))", summary):
        base = ordinal_base(match.group(1))
        previous = previous_base(base) if base else None
        if previous:
            after.discard(previous)

    if " scores" in summary:
        after.discard(3)

    return {base for base in after if base in {1, 2, 3}}


def base_state_contexts(game: dict[str, Any]) -> dict[int, set[int]]:
    public_details = game.get("public_details") or {}
    home_away = str(public_details.get("home_away") or (game.get("game_summary") or {}).get("home_away") or "home")
    prefixes = own_player_prefixes(game)
    contexts: dict[int, set[int]] = {}
    current_bases: set[int] = set()
    current_half: tuple[int, str] | None = None
    previous_own_batting = True

    ordered = sorted(game.get("plays") or [], key=lambda play: (parse_play_seconds(play), play.get("index") or 0))
    for play in ordered:
        inning = int(play.get("inning") or 1)
        own_batting = infer_own_batting(play, prefixes, previous_own_batting)
        previous_own_batting = own_batting
        half = infer_half(play, own_batting, home_away)
        half_key = (inning, half)
        if half_key != current_half:
            current_bases = set()
            current_half = half_key

        before = set(current_bases)
        before.update(inferred_before_bases(play))
        before = {base for base in before if base in {1, 2, 3}}
        contexts[id(play)] = before
        current_bases = after_bases(play, before)

    return contexts


def overlay_timeline(
    selected: list[tuple[str, dict[str, Any]]],
    segments: list[Segment],
    *,
    max_merge_gap: float,
) -> list[tuple[float, float, dict[str, Any]]]:
    paired = sorted(zip(segments, [play for _, play in selected]), key=lambda item: (item[0].start, item[0].end))
    if not paired:
        return []

    groups: list[list[tuple[Segment, dict[str, Any]]]] = [[paired[0]]]
    group_end = paired[0][0].end
    for segment, play in paired[1:]:
        if segment.start <= group_end + max_merge_gap:
            groups[-1].append((segment, play))
            group_end = max(group_end, segment.end)
        else:
            groups.append([(segment, play)])
            group_end = segment.end

    timeline = []
    output_cursor = 0.0
    for group in groups:
        group_start = min(segment.start for segment, _ in group)
        group_end = max(segment.end for segment, _ in group)
        event_starts = [output_cursor + max(0.0, segment.start - group_start) for segment, _ in group]
        for index, ((segment, play), event_start) in enumerate(zip(group, event_starts)):
            next_start = event_starts[index + 1] if index + 1 < len(event_starts) else output_cursor + group_end - group_start
            event_end = max(event_start + 0.5, next_start)
            timeline.append((event_start, event_end, play))
        output_cursor += group_end - group_start
    return timeline


def video_dimensions(path: Path) -> tuple[int, int]:
    output = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
    ).strip()
    width, height = output.split(",", 1)
    return int(width), int(height)


def font(size: int):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def text_width(draw: Any, text: str, selected_font: Any) -> float:
    return float(draw.textlength(text, font=selected_font))


def wrap_text_pixels(draw: Any, text: str, selected_font: Any, max_width: int, max_lines: int) -> list[str]:
    words = " ".join(text.split()).split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and text_width(draw, candidate, selected_font) > max_width:
            lines.append(" ".join(current))
            current = [word]
            if len(lines) == max_lines:
                break
        else:
            current.append(word)
    if current and len(lines) < max_lines:
        lines.append(" ".join(current))
    if len(lines) == max_lines and len(" ".join(lines).split()) < len(words):
        lines[-1] = lines[-1].rstrip(" .") + "..."
    return lines or [""]


def draw_centered_text(
    draw: Any,
    xy: tuple[float, float],
    text: str,
    selected_font: Any,
    fill: tuple[int, int, int, int],
    *,
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int, int] | None = None,
) -> None:
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=selected_font)
    draw.text(
        (x - (bbox[2] - bbox[0]) / 2, y),
        text,
        font=selected_font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )


def draw_diamond_base(draw: Any, center: tuple[float, float], size: float, occupied: bool) -> None:
    x, y = center
    points = [(x, y - size), (x + size, y), (x, y + size), (x - size, y)]
    fill = TIGERS_ORANGE if occupied else (80, 88, 98, 245)
    outline = (255, 255, 255, 190) if occupied else (190, 198, 208, 170)
    draw.polygon(points, fill=fill, outline=outline)


def draw_base_diamond(draw: Any, center: tuple[float, float], bases: set[int], scale: float) -> None:
    x, y = center
    gap = scale * 1.45
    draw_diamond_base(draw, (x, y - gap), scale, 2 in bases)
    draw_diamond_base(draw, (x - gap, y), scale, 3 in bases)
    draw_diamond_base(draw, (x + gap, y), scale, 1 in bases)


def render_overlay_png(
    path: Path,
    *,
    width: int,
    height: int,
    score_text: str,
    bases: set[int],
    description: str,
) -> None:
    from PIL import Image, ImageDraw

    output_size = (width, height)
    render_scale = 3
    width *= render_scale
    height *= render_scale
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    score_font = font(max(24, round(height * 0.045)))
    desc_font = font(max(18, round(height * 0.030)))

    score_padding_x = round(width * 0.02)
    score_padding_y = round(height * 0.012)
    base_box_w = round(width * 0.09)
    score_bbox = draw.textbbox((0, 0), score_text, font=score_font)
    score_text_w = score_bbox[2] - score_bbox[0]
    score_w = score_text_w + score_padding_x * 3 + base_box_w
    score_h = max(score_bbox[3] - score_bbox[1] + score_padding_y * 2, round(height * 0.07))
    score_x0 = (width - score_w) / 2
    score_y0 = round(height * 0.026)
    draw.rounded_rectangle(
        (score_x0, score_y0, score_x0 + score_w, score_y0 + score_h),
        radius=round(score_h * 0.18),
        fill=(8, 12, 18, 232),
        outline=(255, 255, 255, 70),
        width=1,
    )
    draw.rectangle(
        (score_x0, score_y0, score_x0 + score_w, score_y0 + max(3, round(score_h * 0.08))),
        fill=TIGERS_ORANGE,
    )
    divider_x = score_x0 + score_padding_x * 2 + score_text_w
    draw.line(
        (divider_x, score_y0 + round(score_h * 0.18), divider_x, score_y0 + score_h - round(score_h * 0.16)),
        fill=(255, 255, 255, 75),
        width=1,
    )
    draw.text(
        (score_x0 + score_padding_x, score_y0 + (score_h - (score_bbox[3] - score_bbox[1])) / 2 - score_bbox[1]),
        score_text,
        font=score_font,
        fill=(255, 255, 255, 255),
    )
    draw_base_diamond(
        draw,
        (score_x0 + score_w - base_box_w / 2 - score_padding_x * 0.45, score_y0 + score_h * 0.55),
        bases,
        max(5, height * 0.009),
    )

    desc_max_width = round(width * 0.88)
    lines = wrap_text_pixels(draw, description, desc_font, desc_max_width, max_lines=2)
    line_heights = [draw.textbbox((0, 0), line, font=desc_font)[3] - draw.textbbox((0, 0), line, font=desc_font)[1] for line in lines]
    line_height = max(line_heights or [round(height * 0.046)])
    line_gap = round(height * 0.008)
    desc_h = line_height * len(lines) + line_gap * (len(lines) - 1)
    text_y = height - desc_h - round(height * 0.07)
    stroke_width = max(5, round(height * 0.004))
    for line in lines:
        draw_centered_text(
            draw,
            (width / 2, text_y),
            line,
            desc_font,
            (255, 255, 255, 255),
            stroke_width=stroke_width,
            stroke_fill=(0, 0, 0, 245),
        )
        text_y += line_height + line_gap

    image = image.resize(output_size, Image.Resampling.LANCZOS)
    image.save(path)


def write_scorebug_pngs(
    directory: Path,
    *,
    clean_video: Path,
    game: dict[str, Any],
    selected: list[tuple[str, dict[str, Any]]],
    segments: list[Segment],
    max_merge_gap: float,
    team_label: str,
    opp_label: str,
    description_overrides: dict[int, str],
) -> list[tuple[float, float, Path]]:
    width, height = video_dimensions(clean_video)
    contexts = scorebug_contexts(game, team_label, opp_label)
    bases_by_play = base_state_contexts(game)
    prefixes = own_player_prefixes(game)
    descriptions_by_play = {
        id(play): selected_description(index, play, prefixes, description_overrides)
        for index, (_, play) in enumerate(selected, start=1)
    }
    overlays = []
    for index, (start, end, play) in enumerate(overlay_timeline(selected, segments, max_merge_gap=max_merge_gap)):
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


def burn_in_png_overlays(source: Path, output: Path, overlays: list[tuple[float, float, Path]]) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(source)]
    for _, _, overlay_path in overlays:
        cmd.extend(["-loop", "1", "-i", str(overlay_path)])

    chain = "[0:v]"
    filters = []
    for index, (start, end, _) in enumerate(overlays, start=1):
        out = f"[v{index}]"
        filters.append(
            f"{chain}[{index}:v]overlay=0:0:enable='between(t\\,{start:.3f}\\,{end:.3f})'{out}"
        )
        chain = out

    cmd.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            chain,
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-af",
            "aresample=async=1:first_pts=0",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output),
        ]
    )
    run(cmd)


def main() -> None:
    args = parse_args()
    game = load_json(args.game_json)
    excluded = {item.strip() for item in args.exclude_types.split(",") if item.strip()}
    extra_start_play_indexes = parse_index_set(args.extra_start_play_indexes)
    selected = select_condensed_plays(
        game,
        include_all_play_types=args.include_all_play_types,
        excluded=excluded,
        target_duration=args.target_duration,
        start_buffer=args.start_buffer,
        end_buffer=args.end_buffer,
        min_segment_length=args.min_segment_length,
        time_shift=args.time_shift,
        long_clip_start_buffer=args.long_clip_start_buffer,
        batted_ball_start_buffer=args.batted_ball_start_buffer,
        extra_start_play_indexes=extra_start_play_indexes,
        extra_start_buffer=args.extra_start_buffer,
    )
    if args.max_plays is not None:
        selected = selected[: args.max_plays]
    description_overrides = read_description_overrides(Path(args.description_overrides) if args.description_overrides else None)
    descriptions_output = Path(args.descriptions_output) if args.descriptions_output else None
    if args.descriptions_only and descriptions_output is None:
        descriptions_output = Path(args.output).with_suffix(".descriptions.md")
    if descriptions_output is not None:
        write_description_review(descriptions_output, selected, game, description_overrides)
    plays_output = Path(args.plays_output) if args.plays_output else Path(args.output).with_suffix(".plays.txt")
    if args.descriptions_only:
        write_selected_plays(plays_output, selected)
        print(f"Wrote {descriptions_output}")
        print(f"Wrote {plays_output}")
        return

    source, cookie = video_source_from_game(game, args.video)
    plays = [play for _, play in selected]
    output = Path(args.output)
    segments = condensed_plays_to_segments(
        plays,
        start_buffer=args.start_buffer,
        end_buffer=args.end_buffer,
        min_duration=args.min_segment_length,
        time_shift=args.time_shift,
        long_clip_start_buffer=args.long_clip_start_buffer,
        batted_ball_start_buffer=args.batted_ball_start_buffer,
        extra_start_play_indexes=extra_start_play_indexes,
        extra_start_buffer=args.extra_start_buffer,
    )
    render_output = output
    temp_dir = None
    if args.scorebug:
        temp_dir = tempfile.TemporaryDirectory(prefix="gc-condensed-overlay-")
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
        home_away = str((game.get("public_details") or {}).get("home_away") or (game.get("game_summary") or {}).get("home_away") or "home")
        team_label = args.team_label or "TIG"
        opp_label = args.opponent_label or opponent_label(game)
        overlays = write_scorebug_pngs(
            Path(temp_dir.name),
            clean_video=render_output,
            game=game,
            selected=selected,
            segments=segments,
            max_merge_gap=args.max_merge_gap,
            team_label=team_label,
            opp_label=opp_label,
            description_overrides=description_overrides,
        )
        burn_in_png_overlays(render_output, output, overlays)
        temp_dir.cleanup()
    write_selected_plays(plays_output, selected)
    print(f"Wrote {args.output} ({len(plays)} plays)")
    print(f"Wrote {plays_output}")
    if descriptions_output is not None:
        print(f"Wrote {descriptions_output}")


if __name__ == "__main__":
    main()
