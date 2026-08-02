#!/usr/bin/env python3
"""Persistent season highlight projects, ranking, previews, and reel rendering."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from gc_common import (
    GCClient,
    GCError,
    build_player_map,
    cookie_header,
    dump_json,
    flatten_stream_events,
    format_timestamp,
    parse_iso,
    replace_player_placeholders,
    run,
    safe_get,
    slugify,
)
from gc_download_full_game import asset_identity, playable_assets

ROLE_NAMES = ("batter", "runner", "pitcher", "fielder")
DECISION_STATES = ("pending", "accepted", "skipped", "deferred")
DISMISS_REASONS = ("no_video", "play_not_found", "not_noteworthy")
PA_TYPES = {
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
OUT_TYPES = {
    "strikeout",
    "batter_out",
    "batter_out_advance_runners",
    "double_play",
    "caught_stealing",
}
RUNNER_TYPES = {"stole_base", "advance", "advanced_on_error", "passed_ball", "wild_pitch"}
NOTEWORTHY_SECONDARY_ACTION = re.compile(
    r"\b("
    r"scores?|scored|"
    r"advances?|advanced|"
    r"steals?|stole|"
    r"reaches?|reached|"
    r"caught stealing|"
    r"wild pitch|passed ball|"
    r"error|out advancing"
    r")\b",
    re.IGNORECASE,
)
ROUTINE_STEAL_SUMMARY = re.compile(
    r"^.+?\s+(?:"
    r"steals\s+(?:home|1st|2nd|3rd|first|second|third)|"
    r"scores\s+on\s+steal\s+of\s+home"
    r")\.?$",
    re.IGNORECASE,
)
ROLE_SUMMARY_PATTERNS = {
    "catcher": re.compile(
        r"(?P<label>\bcatcher\s+)"
        r"(?P<player>Player\s+[A-Za-z0-9-]+|[^,.;]+?)"
        r"(?=\s+to\s+[A-Za-z]|[.,;]|$)",
        re.IGNORECASE,
    ),
    "pitcher": re.compile(
        r"(?P<label>\bpitcher\s+)"
        r"(?P<player>Player\s+[A-Za-z0-9-]+|[^,.;]+?)"
        r"(?=\s+to\s+[A-Za-z]|[.,;]|$)",
        re.IGNORECASE,
    ),
}
TITLE_ROLE_ORDER = {"batter": 0, "runner": 1, "pitcher": 2, "fielder": 3}
PREVIEW_CACHE_VERSION = 3
PARTICIPANT_INFERENCE_VERSION = 2

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    event_id TEXT PRIMARY KEY,
    game_date TEXT,
    opponent TEXT,
    home_away TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    stream_id TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES games(event_id) ON DELETE CASCADE,
    stream_id TEXT,
    created_at TEXT,
    ended_at TEXT,
    duration REAL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    number TEXT,
    is_team INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    clip_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES games(event_id) ON DELETE CASCADE,
    clip_metadata_id TEXT,
    pbp_id TEXT,
    asset_id TEXT REFERENCES assets(asset_id),
    play_type TEXT,
    play_summary TEXT NOT NULL,
    inning INTEGER,
    inning_half TEXT,
    exceptional INTEGER NOT NULL DEFAULT 0,
    clip_timestamp TEXT,
    clip_duration REAL,
    video_offset REAL,
    proposal_start REAL,
    proposal_end REAL,
    draft_start REAL,
    draft_end REAL,
    final_start REAL,
    final_end REAL,
    review_state TEXT NOT NULL DEFAULT 'unreviewed',
    source_updated_at TEXT,
    source_changed INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participant_tags (
    clip_key TEXT NOT NULL REFERENCES clips(clip_key) ON DELETE CASCADE,
    player_id TEXT NOT NULL REFERENCES players(player_id),
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT,
    PRIMARY KEY (clip_key, player_id, role, source)
);

CREATE TABLE IF NOT EXISTS player_decisions (
    clip_key TEXT NOT NULL REFERENCES clips(clip_key) ON DELETE CASCADE,
    player_id TEXT NOT NULL REFERENCES players(player_id),
    status TEXT NOT NULL DEFAULT 'pending',
    reel_order INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (clip_key, player_id)
);

CREATE TABLE IF NOT EXISTS previews (
    clip_key TEXT PRIMARY KEY REFERENCES clips(clip_key) ON DELETE CASCADE,
    path TEXT NOT NULL,
    source_start REAL NOT NULL,
    source_end REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS clips_review_idx ON clips(review_state);
CREATE INDEX IF NOT EXISTS clips_event_idx ON clips(event_id);
CREATE INDEX IF NOT EXISTS decisions_player_idx ON player_decisions(player_id, status, active);
CREATE INDEX IF NOT EXISTS participant_player_idx ON participant_tags(player_id, source);
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def team_payload(team: dict[str, Any]) -> dict[str, Any]:
    nested = team.get("team")
    return nested if isinstance(nested, dict) else team


def team_id_from_payload(team: dict[str, Any]) -> str | None:
    payload = team_payload(team)
    return payload.get("id") or payload.get("team_id")


def team_label(team: dict[str, Any]) -> str:
    payload = team_payload(team)
    nested_season = payload.get("team_season") or {}
    season = payload.get("season_name") or nested_season.get("season") or ""
    year = payload.get("season_year") or nested_season.get("year") or ""
    name = payload.get("name") or payload.get("display_name") or team_id_from_payload(team) or "Team"
    suffix = " ".join(str(value).title() for value in (season, year) if value)
    return f"{name} - {suffix}" if suffix else str(name)


def default_project_path(team: dict[str, Any]) -> Path:
    payload = team_payload(team)
    nested_season = payload.get("team_season") or {}
    season = payload.get("season_name") or nested_season.get("season") or "season"
    year = payload.get("season_year") or nested_season.get("year") or "unknown"
    identity = team_id_from_payload(team) or "team"
    return Path("gc_seasons") / f"{slugify(str(payload.get('name') or 'team'))}-{slugify(str(season))}-{year}-{identity[:8]}"


def event_payload(schedule_item: dict[str, Any]) -> dict[str, Any]:
    nested = schedule_item.get("event")
    return nested if isinstance(nested, dict) else schedule_item


def completed_game_options(
    schedule: list[dict[str, Any]], summaries: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
    schedule_by_id = {
        str(event["id"]): item
        for item in schedule
        if (event := event_payload(item)).get("id") and event.get("event_type") in (None, "game")
    }
    summary_by_id = {str(item["event_id"]): item for item in summaries if item.get("event_id")}
    options = []
    for event_id in set(schedule_by_id) | set(summary_by_id):
        schedule_item = schedule_by_id.get(event_id)
        summary = summary_by_id.get(event_id)
        event = event_payload(schedule_item or {})
        status = (summary or {}).get("game_status") or event.get("status")
        if status == "completed":
            options.append((event_id, schedule_item, summary))
    return sorted(
        options,
        key=lambda item: (
            ((event_payload(item[1] or {}).get("start") or {}).get("datetime"))
            or (item[2] or {}).get("last_scoring_update")
            or ""
        ),
    )


def opponent_name(schedule_item: dict[str, Any] | None, public_details: dict[str, Any]) -> str:
    return str(
        ((public_details.get("opponent_team") or {}).get("name"))
        or (((schedule_item or {}).get("pregame_data") or {}).get("opponent_name"))
        or "Opponent"
    )


def game_date(schedule_item: dict[str, Any] | None, public_details: dict[str, Any]) -> str | None:
    event = event_payload(schedule_item or {})
    value = (
        public_details.get("start_ts")
        or ((event.get("start") or {}).get("datetime"))
        or ((event.get("arrive") or {}).get("datetime"))
    )
    return str(value) if value else None


def source_changed(existing: sqlite3.Row, incoming: dict[str, Any]) -> bool:
    fields = ("pbp_id", "play_type", "play_summary", "clip_timestamp", "clip_duration")
    return any(existing[field] != incoming[field] for field in fields)


def source_event_by_id(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(event["id"]).lower(): event for event in events if event.get("id")}


def sorted_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda event: (event.get("createdAt") or 0, event.get("_stream_sequence_number") or 0))


def positions_at(
    events: list[dict[str, Any]], *, at_ms: int | float | None, team_id: str | None
) -> dict[str, str]:
    if at_ms is None or not team_id:
        return {}
    positions: dict[str, str] = {}
    for event in sorted_events(events):
        created_at = event.get("createdAt")
        if created_at is not None and created_at > at_ms:
            break
        if event.get("code") != "fill_position":
            continue
        attrs = event.get("attributes") or {}
        if attrs.get("teamId") != team_id:
            continue
        position = attrs.get("position")
        player_id = attrs.get("playerId")
        if position and player_id:
            positions[str(position)] = str(player_id)
    return positions


def participant_inference(
    *,
    play_type: str,
    summary: str,
    mentioned_ids: list[str],
    raw_event: dict[str, Any] | None,
    events: list[dict[str, Any]],
    event_ms: int | float | None,
    own_team_id: str,
    own_player_ids: set[str],
    own_batting: bool,
) -> list[tuple[str, str, str]]:
    inferred: dict[tuple[str, str], str] = {}
    if own_batting and play_type in PA_TYPES and mentioned_ids and mentioned_ids[0] in own_player_ids:
        inferred[(mentioned_ids[0], "batter")] = "high"

    attrs = (raw_event or {}).get("attributes") or {}
    runner_id = attrs.get("runnerId")
    if runner_id in own_player_ids and (
        attrs.get("playType") in RUNNER_TYPES or play_type in RUNNER_TYPES or "scores" in summary.lower()
    ):
        inferred[(str(runner_id), "runner")] = "high"

    if own_batting:
        lower_summary = summary.lower()
        for player_id in mentioned_ids:
            if player_id not in own_player_ids:
                continue
            if any(word in lower_summary for word in (" steals ", " advances ", " scores", " scored")):
                inferred[(player_id, "runner")] = "medium"

    positions = positions_at(events, at_ms=event_ms, team_id=own_team_id)
    if not own_batting:
        pitcher_id = positions.get("P")
        if pitcher_id in own_player_ids:
            inferred[(pitcher_id, "pitcher")] = "high"
        for defender in attrs.get("defenders") or []:
            player_id = positions.get(str(defender.get("position") or ""))
            if player_id in own_player_ids:
                inferred[(player_id, "fielder")] = "medium"

    return [(player_id, role, confidence) for (player_id, role), confidence in inferred.items()]


def display_play_summary(summary: str, tags: Iterable[dict[str, Any]]) -> str:
    """Apply unambiguous reviewed role corrections to a source play summary."""
    tags_by_role: dict[str, set[str]] = defaultdict(set)
    for tag in tags:
        display = str(tag.get("display") or "").strip()
        if display:
            tags_by_role[str(tag.get("role") or "")].add(display)

    result = summary
    replacements = {
        "catcher": tags_by_role["fielder"],
        "pitcher": tags_by_role["pitcher"],
    }
    for label, displays in replacements.items():
        if len(displays) != 1:
            continue
        display = next(iter(displays))
        result = ROLE_SUMMARY_PATTERNS[label].sub(
            lambda match: f"{match.group('label')}{display}",
            result,
        )
    return result


def metadata_play_title(
    play_type: str,
    source_summary: str,
    tags: Iterable[dict[str, Any]],
) -> str:
    """Build a title whose player names and roles come only from effective tags."""
    play_label = (play_type or "play").replace("_", " ").capitalize()
    if play_type == "caught_stealing":
        caught = re.search(
            r"\bcaught stealing\s+(home|1st|2nd|3rd)\b",
            source_summary,
            re.IGNORECASE,
        )
        if caught:
            play_label = f"Caught stealing {caught.group(1).lower()}"

    roles_by_player: dict[str, set[str]] = defaultdict(set)
    for tag in tags:
        display = str(tag.get("display") or "").strip()
        role = str(tag.get("role") or "").strip()
        if display and role in ROLE_NAMES:
            roles_by_player[display].add(role)

    def player_order(item: tuple[str, set[str]]) -> tuple[int, str]:
        display, roles = item
        return min(TITLE_ROLE_ORDER[role] for role in roles), display

    fielder_count = sum("fielder" in roles for roles in roles_by_player.values())
    participant_labels = []
    for display, roles in sorted(roles_by_player.items(), key=player_order):
        ordered_roles = sorted(roles, key=TITLE_ROLE_ORDER.__getitem__)
        if play_type == "caught_stealing" and fielder_count == 1:
            ordered_roles = ["catcher" if role == "fielder" else role for role in ordered_roles]
        participant_labels.append(f"{display} ({', '.join(ordered_roles)})")

    return f"{play_label} - {', '.join(participant_labels)}" if participant_labels else play_label


def team_side(home_away: str | None, inning_half: str | None) -> str:
    if home_away not in {"home", "away"} or inning_half not in {"top", "bottom"}:
        return "unknown"
    own_team_batting = (home_away == "home" and inning_half == "bottom") or (
        home_away == "away" and inning_half == "top"
    )
    return "offense" if own_team_batting else "defense"


def is_routine_out(play_type: str, summary: str, exceptional: bool) -> bool:
    routine_type = (
        play_type == "batter_out"
        or play_type == "strikeout"
        or play_type.startswith("strikeout_")
    )
    return bool(
        routine_type
        and not exceptional
        and not NOTEWORTHY_SECONDARY_ACTION.search(summary)
    )


def is_routine_opponent_steal(
    play_type: str,
    summary: str,
    exceptional: bool,
    side: str,
) -> bool:
    return bool(
        side == "defense"
        and play_type == "stole_base"
        and not exceptional
        and ROUTINE_STEAL_SUMMARY.fullmatch(summary.strip())
    )


def is_routine_defensive_free_pass(
    play_type: str,
    summary: str,
    exceptional: bool,
    side: str,
) -> bool:
    return bool(
        side == "defense"
        and play_type in {"walk", "hit_by_pitch"}
        and not exceptional
        and not NOTEWORTHY_SECONDARY_ACTION.search(summary)
    )


def is_queueable_play(
    play_type: str,
    summary: str,
    exceptional: bool,
    side: str,
) -> bool:
    return not (
        is_routine_out(play_type, summary, exceptional)
        or is_routine_opponent_steal(play_type, summary, exceptional, side)
        or is_routine_defensive_free_pass(play_type, summary, exceptional, side)
    )


def score_for_role(play_type: str, summary: str, role: str, exceptional: bool) -> tuple[int, str]:
    lower = summary.lower()
    base = 0
    reason = ""
    if role == "batter":
        base, reason = {
            "home_run": (100, "home run"),
            "triple": (90, "triple"),
            "double": (80, "double"),
            "single": (60, "single"),
            "fielders_choice": (35, "reached base"),
            "reached_on_error": (35, "reached base"),
            "error": (35, "reached base"),
            "walk": (25, "walk"),
            "hit_by_pitch": (25, "hit by pitch"),
            "strikeout": (0, "batter struck out"),
        }.get(play_type, (0, ""))
    elif role == "runner":
        if "steal of home" in lower or ("steals home" in lower):
            base, reason = 95, "steal of home"
        elif " scores" in lower or " scored" in lower:
            base, reason = 70, "run scored"
        elif "steals 3rd" in lower or "steals third" in lower:
            base, reason = 60, "steal of third"
        elif "steals 2nd" in lower or "steals second" in lower:
            base, reason = 50, "steal of second"
        elif any(word in lower for word in (" advances ", " stole ")):
            base, reason = 25, "runner advance"
    elif role == "pitcher":
        if play_type == "strikeout":
            base, reason = 75, "strikeout"
        elif play_type in OUT_TYPES:
            base, reason = 25, "recorded out"
    elif role == "fielder":
        if play_type == "double_play":
            base, reason = 95, "double play"
        elif play_type == "caught_stealing":
            base, reason = 90, "caught stealing"
        elif play_type in OUT_TYPES:
            base, reason = 55, "defensive out"

    bonuses = []
    if exceptional:
        base += 30
        bonuses.append("exceptional")
    if any(token in lower for token in (" scores", " scored", "run scores")):
        base += 15
        bonuses.append("run-scoring")
    if bonuses:
        reason = ", ".join(filter(None, [reason, *bonuses]))
    return base, reason or "indexed play"


def fallback_clip_score(play_type: str, summary: str, exceptional: bool) -> tuple[int, str]:
    role = "fielder" if play_type in OUT_TYPES else "batter"
    score, reason = score_for_role(play_type, summary, role, exceptional)
    return score, reason


def timing_for_clip(
    clip: dict[str, Any], asset: dict[str, Any] | None
) -> tuple[float | None, float | None, float | None]:
    if not asset or not asset.get("created_at") or not clip.get("timestamp"):
        return None, None, None
    offset = max(0.0, (parse_iso(str(clip["timestamp"])) - parse_iso(str(asset["created_at"]))).total_seconds())
    duration = float(clip.get("duration") or 0)
    if duration >= 12:
        start = max(0.0, offset - 18.0)
    else:
        start = max(0.0, offset - duration - 7.0)
    end = max(offset + 2.0, start + 12.0)
    asset_duration = asset.get("duration")
    if asset_duration is not None and offset <= float(asset_duration):
        end = min(float(asset_duration), end)
    return round(offset, 3), round(start, 3), round(end, 3)


def has_video_stream(path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        output = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(output)


def choose_asset_for_clip(clip: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    stream_id = (clip.get("related_ids") or {}).get("stream_id")
    matching = [asset for asset in assets if asset.get("stream_id") == stream_id] if stream_id else []
    candidates = matching or assets
    timestamp = clip.get("timestamp")
    if timestamp:
        clip_time = parse_iso(str(timestamp))
        distances: list[tuple[float, dict[str, Any]]] = []
        for asset in candidates:
            if not asset.get("created_at"):
                continue
            start = parse_iso(str(asset["created_at"]))
            duration = float(asset.get("duration") or 0)
            duration_end = start + dt.timedelta(seconds=duration)
            reported_end = (
                parse_iso(str(asset["ended_at"]))
                if asset.get("ended_at")
                else duration_end
            )
            end = max(reported_end, duration_end)
            if start <= clip_time <= end + dt.timedelta(seconds=10):
                return asset
            if clip_time < start:
                distance = (start - clip_time).total_seconds()
            else:
                distance = (clip_time - end).total_seconds()
            distances.append((distance, asset))
        if distances:
            return min(distances, key=lambda item: item[0])[1]
    return candidates[0] if candidates else None


class SeasonProject:
    """SQLite-backed season curation project."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "season.db"
        if not self.db_path.exists():
            raise GCError(f"Season project does not exist: {self.root}")
        self._ensure_participant_inference_version()

    @classmethod
    def create(cls, root: str | Path, team: dict[str, Any]) -> "SeasonProject":
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        for child in ("raw", "previews", "renders", "full_games"):
            (root_path / child).mkdir(exist_ok=True)
        db_path = root_path / "season.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(SCHEMA)
            payload = team_payload(team)
            values = {
                "team_id": team_id_from_payload(team) or "",
                "team_name": str(payload.get("name") or payload.get("display_name") or "Team"),
                "team_json": json_text(team),
                "created_at": utc_now(),
                "participant_inference_version": str(PARTICIPANT_INFERENCE_VERSION),
            }
            conn.executemany("INSERT OR REPLACE INTO project(key, value) VALUES (?, ?)", values.items())
            conn.commit()
        finally:
            conn.close()
        return cls(root_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def config(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM project WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _ensure_participant_inference_version(self) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM project WHERE key = 'participant_inference_version'"
            ).fetchone()
        current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
        if current >= PARTICIPANT_INFERENCE_VERSION:
            return
        self.rebuild_inferred_participants()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO project(key, value)
                VALUES ('participant_inference_version', ?)
                """,
                (str(PARTICIPANT_INFERENCE_VERSION),),
            )

    def rebuild_inferred_participants(self) -> int:
        with self.connect() as conn:
            player_rows = list(conn.execute("SELECT * FROM players"))
            team_row = conn.execute(
                "SELECT value FROM project WHERE key = 'team_id'"
            ).fetchone()
            own_team_id = str(team_row["value"]) if team_row else ""
            players = {
                str(row["player_id"]): {"display": row["display"]}
                for row in player_rows
            }
            own_player_ids = {
                str(row["player_id"]) for row in player_rows if row["is_team"]
            }
            games = [dict(row) for row in conn.execute("SELECT * FROM games")]

        rebuilt = 0
        for game in games:
            event_id = str(game["event_id"])
            events_path = self.root / "raw" / event_id / "stream_events_raw.json"
            try:
                raw_stream_events = json.loads(events_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw_stream_events = []
            events = flatten_stream_events(raw_stream_events)
            events_by_id = source_event_by_id(events)

            with self.connect() as conn:
                clips = list(
                    conn.execute(
                        "SELECT * FROM clips WHERE event_id = ?",
                        (event_id,),
                    )
                )
                for row in clips:
                    clip = dict(row)
                    try:
                        raw_clip = json.loads(clip["raw_json"])
                    except (TypeError, json.JSONDecodeError):
                        raw_clip = {}
                    _, mentioned_ids = replace_player_placeholders(
                        raw_clip.get("play_summary") or clip["play_summary"],
                        players,
                    )
                    pbp_id = clip.get("pbp_id")
                    raw_event = events_by_id.get(str(pbp_id).lower()) if pbp_id else None
                    event_ms = raw_event.get("createdAt") if raw_event else None
                    own_batting = (
                        game["home_away"] == "home" and clip["inning_half"] == "bottom"
                    ) or (
                        game["home_away"] == "away" and clip["inning_half"] == "top"
                    )

                    conn.execute(
                        "DELETE FROM participant_tags WHERE clip_key = ? AND source = 'inferred'",
                        (clip["clip_key"],),
                    )
                    for player_id, role, confidence in participant_inference(
                        play_type=str(clip.get("play_type") or ""),
                        summary=str(clip["play_summary"]),
                        mentioned_ids=mentioned_ids,
                        raw_event=raw_event,
                        events=events,
                        event_ms=event_ms,
                        own_team_id=own_team_id,
                        own_player_ids=own_player_ids,
                        own_batting=own_batting,
                    ):
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO participant_tags(
                                clip_key, player_id, role, source, confidence
                            ) VALUES (?, ?, ?, 'inferred', ?)
                            """,
                            (clip["clip_key"], player_id, role, confidence),
                        )
                    rebuilt += 1
        return rebuilt

    @property
    def team_id(self) -> str:
        return self.config("team_id", "") or ""

    @property
    def team_name(self) -> str:
        return self.config("team_name", "Team") or "Team"

    def refresh(self, client: GCClient) -> dict[str, int]:
        schedule = client.get_schedule(self.team_id)
        summaries = client.get_game_summaries(self.team_id)
        team_players = client.get_players(self.team_id)
        self._upsert_players(team_players, is_team=True, mark_team_inactive=True)

        imported = 0
        clip_count = 0
        for event_id, schedule_item, summary in completed_game_options(schedule, summaries):
            clip_count += self._import_game(client, event_id, schedule_item, summary, team_players)
            imported += 1
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO project(key, value) VALUES ('refreshed_at', ?)", (utc_now(),))
        return {"games": imported, "clips": clip_count, "players": len(team_players)}

    def _upsert_players(
        self, roster: Iterable[dict[str, Any]], *, is_team: bool, mark_team_inactive: bool = False
    ) -> dict[str, dict[str, Any]]:
        players = build_player_map(roster)
        with self.connect() as conn:
            if mark_team_inactive:
                conn.execute("UPDATE players SET active = 0 WHERE is_team = 1")
            for player_id, player in players.items():
                conn.execute(
                    """
                    INSERT INTO players(
                        player_id, display, first_name, last_name, number, is_team, active, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(player_id) DO UPDATE SET
                        display=excluded.display,
                        first_name=excluded.first_name,
                        last_name=excluded.last_name,
                        number=excluded.number,
                        is_team=MAX(players.is_team, excluded.is_team),
                        active=CASE WHEN excluded.is_team = 1 THEN 1 ELSE players.active END,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        player_id,
                        player.get("display") or player_id[:8],
                        player.get("first_name"),
                        player.get("last_name"),
                        str(player.get("number") or ""),
                        int(is_team),
                        json_text(player),
                    ),
                )
        return players

    def _import_game(
        self,
        client: GCClient,
        event_id: str,
        schedule_item: dict[str, Any] | None,
        summary: dict[str, Any] | None,
        team_roster: list[dict[str, Any]],
    ) -> int:
        public_details = safe_get(
            lambda: client.get_public_game_details(event_id), {}, label=f"public game details for {event_id}"
        )
        clips_response = client.search_clips(self.team_id, event_id)
        clips = clips_response.get("hits") or []
        expected = clips_response.get("total_count")
        if expected is not None and len(clips) < int(expected):
            raise GCError(f"Fetched {len(clips)} of {expected} clips for {event_id}.")

        stream_id = ((summary or {}).get("game_stream") or {}).get("id")
        stream_id = stream_id or safe_get(
            lambda: client.get_best_game_stream_id(event_id),
            None,
            label=f"best game stream for {event_id}",
        )
        raw_stream_events = (
            safe_get(
                lambda: client.get_game_stream_events(stream_id),
                [],
                label=f"game stream events for {event_id}",
            )
            if stream_id
            else []
        )
        events = flatten_stream_events(raw_stream_events)
        events_by_id = source_event_by_id(events)

        team_players = self._upsert_players(team_roster, is_team=True)
        players = team_players
        own_player_ids = set(team_players)

        event_assets = safe_get(
            lambda: client.get_event_assets(self.team_id, event_id),
            [],
            label=f"event assets for {event_id}",
        )
        event_assets = sorted(event_assets, key=lambda item: item.get("created_at") or "")
        imported_at = utc_now()
        home_away = str((summary or {}).get("home_away") or public_details.get("home_away") or "home")

        raw_dir = self.root / "raw" / event_id
        raw_dir.mkdir(parents=True, exist_ok=True)
        dump_json(raw_dir / "clips_raw.json", clips_response)
        dump_json(raw_dir / "stream_events_raw.json", raw_stream_events)
        dump_json(raw_dir / "event_assets.json", event_assets)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO games(event_id, game_date, opponent, home_away, status, stream_id, imported_at)
                VALUES (?, ?, ?, ?, 'completed', ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    game_date=excluded.game_date,
                    opponent=excluded.opponent,
                    home_away=excluded.home_away,
                    status=excluded.status,
                    stream_id=excluded.stream_id,
                    imported_at=excluded.imported_at
                """,
                (
                    event_id,
                    game_date(schedule_item, public_details),
                    opponent_name(schedule_item, public_details),
                    home_away,
                    stream_id,
                    imported_at,
                ),
            )
            for asset in event_assets:
                asset_id = asset_identity(asset)
                if not asset_id:
                    continue
                sanitized = {key: value for key, value in asset.items() if key not in {"url", "playback_url", "cookies"}}
                conn.execute(
                    """
                    INSERT INTO assets(asset_id, event_id, stream_id, created_at, ended_at, duration, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asset_id) DO UPDATE SET
                        event_id=excluded.event_id,
                        stream_id=excluded.stream_id,
                        created_at=excluded.created_at,
                        ended_at=excluded.ended_at,
                        duration=excluded.duration,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        asset_id,
                        event_id,
                        asset.get("stream_id"),
                        asset.get("created_at"),
                        asset.get("ended_at"),
                        asset.get("duration"),
                        json_text(sanitized),
                    ),
                )

            for index, clip in enumerate(clips):
                clip_id = str(clip.get("clip_metadata_id") or clip.get("id") or index)
                clip_key = f"{event_id}:{clip_id}"
                play_metadata = clip.get("play_metadata") or {}
                sport_metadata = clip.get("sport_metadata") or {}
                play_type = str(play_metadata.get("play_type") or "")
                summary_text, mentioned_ids = replace_player_placeholders(clip.get("play_summary") or "", players)
                pbp_id = play_metadata.get("pbp_id")
                raw_event = events_by_id.get(str(pbp_id).lower()) if pbp_id else None
                event_ms = raw_event.get("createdAt") if raw_event else None
                asset = choose_asset_for_clip(clip, event_assets)
                offset, proposal_start, proposal_end = timing_for_clip(clip, asset)
                incoming = {
                    "pbp_id": pbp_id,
                    "play_type": play_type,
                    "play_summary": summary_text,
                    "clip_timestamp": clip.get("timestamp"),
                    "clip_duration": float(clip.get("duration") or 0),
                }
                existing = conn.execute("SELECT * FROM clips WHERE clip_key = ?", (clip_key,)).fetchone()
                changed = bool(existing and source_changed(existing, incoming) and existing["review_state"] == "reviewed")
                conn.execute(
                    """
                    INSERT INTO clips(
                        clip_key, event_id, clip_metadata_id, pbp_id, asset_id, play_type,
                        play_summary, inning, inning_half, exceptional, clip_timestamp,
                        clip_duration, video_offset, proposal_start, proposal_end,
                        source_updated_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(clip_key) DO UPDATE SET
                        pbp_id=excluded.pbp_id,
                        asset_id=excluded.asset_id,
                        play_type=excluded.play_type,
                        play_summary=excluded.play_summary,
                        inning=excluded.inning,
                        inning_half=excluded.inning_half,
                        exceptional=excluded.exceptional,
                        clip_timestamp=excluded.clip_timestamp,
                        clip_duration=excluded.clip_duration,
                        video_offset=excluded.video_offset,
                        proposal_start=excluded.proposal_start,
                        proposal_end=excluded.proposal_end,
                        source_updated_at=excluded.source_updated_at,
                        source_changed=MAX(clips.source_changed, ?),
                        raw_json=excluded.raw_json
                    """,
                    (
                        clip_key,
                        event_id,
                        clip_id,
                        pbp_id,
                        asset_identity(asset or {}),
                        play_type,
                        summary_text,
                        sport_metadata.get("inning"),
                        sport_metadata.get("inning_half"),
                        int(bool(clip.get("exceptional_play"))),
                        clip.get("timestamp"),
                        incoming["clip_duration"],
                        offset,
                        proposal_start,
                        proposal_end,
                        clip.get("last_updated_at"),
                        json_text(clip),
                        int(changed),
                    ),
                )
                conn.execute("DELETE FROM participant_tags WHERE clip_key = ? AND source = 'inferred'", (clip_key,))
                own_batting = (home_away == "home" and sport_metadata.get("inning_half") == "bottom") or (
                    home_away == "away" and sport_metadata.get("inning_half") == "top"
                )
                for player_id, role, confidence in participant_inference(
                    play_type=play_type,
                    summary=summary_text,
                    mentioned_ids=mentioned_ids,
                    raw_event=raw_event,
                    events=events,
                    event_ms=event_ms,
                    own_team_id=self.team_id,
                    own_player_ids=own_player_ids,
                    own_batting=own_batting,
                ):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO participant_tags(
                            clip_key, player_id, role, source, confidence
                        ) VALUES (?, ?, ?, 'inferred', ?)
                        """,
                        (clip_key, player_id, role, confidence),
                    )
        return len(clips)

    def players(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM players WHERE is_team = 1"
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY CASE WHEN number GLOB '[0-9]*' THEN CAST(number AS INTEGER) ELSE 999 END, display"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql)]

    def games(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM games ORDER BY game_date")]

    def _tags_for_clip(self, conn: sqlite3.Connection, clip_key: str, *, effective: bool = True) -> list[sqlite3.Row]:
        clip = conn.execute("SELECT review_state FROM clips WHERE clip_key = ?", (clip_key,)).fetchone()
        if not clip:
            return []
        has_draft = bool(
            conn.execute(
                "SELECT 1 FROM project WHERE key = ?",
                (f"draft_participants:{clip_key}",),
            ).fetchone()
        )
        source = "draft" if (clip["review_state"] == "reviewed" or has_draft) and effective else "inferred"
        return list(
            conn.execute(
                """
                SELECT participant_tags.*, players.display, players.number
                FROM participant_tags JOIN players USING(player_id)
                WHERE clip_key = ? AND source = ?
                ORDER BY players.display, role
                """,
                (clip_key, source),
            )
        )

    def _repair_clip_asset(self, conn: sqlite3.Connection, clip: sqlite3.Row) -> None:
        raw_clip = json.loads(clip["raw_json"])
        assets = [
            {**json.loads(row["metadata_json"]), **dict(row)}
            for row in conn.execute(
                "SELECT * FROM assets WHERE event_id = ?",
                (clip["event_id"],),
            )
        ]
        selected = choose_asset_for_clip(raw_clip, assets)
        selected_id = asset_identity(selected or {})
        if not selected or not selected_id:
            return

        offset, proposal_start, proposal_end = timing_for_clip(raw_clip, selected)
        proposal_invalid = (
            clip["proposal_start"] is None
            or clip["proposal_end"] is None
            or clip["proposal_end"] <= clip["proposal_start"]
        )
        if selected_id == clip["asset_id"] and not proposal_invalid:
            return

        updates: dict[str, Any] = {
            "asset_id": selected_id,
            "video_offset": offset,
            "proposal_start": proposal_start,
            "proposal_end": proposal_end,
        }
        old_asset = next(
            (asset for asset in assets if asset_identity(asset) == clip["asset_id"]),
            None,
        )
        selected_duration = (
            float(selected["duration"])
            if selected.get("duration") is not None
            else None
        )
        selected_duration_is_reliable = (
            selected_duration is not None
            and offset is not None
            and offset <= selected_duration
        )
        if (
            old_asset
            and old_asset.get("created_at")
            and selected.get("created_at")
            and selected_id != clip["asset_id"]
        ):
            delta = (
                parse_iso(str(old_asset["created_at"]))
                - parse_iso(str(selected["created_at"]))
            ).total_seconds()
            for prefix in ("draft", "final"):
                old_start = clip[f"{prefix}_start"]
                old_end = clip[f"{prefix}_end"]
                if old_start is None or old_end is None:
                    continue
                rebased_start = float(old_start) + delta
                rebased_end = float(old_end) + delta
                valid = (
                    rebased_start >= 0
                    and rebased_end > rebased_start
                    and (
                        not selected_duration_is_reliable
                        or rebased_end <= selected_duration + 0.001
                    )
                )
                updates[f"{prefix}_start"] = rebased_start if valid else None
                updates[f"{prefix}_end"] = rebased_end if valid else None

        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE clips SET {assignments} WHERE clip_key = ?",
            (*updates.values(), clip["clip_key"]),
        )

    def clip(self, clip_key: str) -> dict[str, Any]:
        with self.connect() as conn:
            base_row = conn.execute(
                "SELECT * FROM clips WHERE clip_key = ?",
                (clip_key,),
            ).fetchone()
            if not base_row:
                raise GCError(f"Unknown clip: {clip_key}")
            self._repair_clip_asset(conn, base_row)
            row = conn.execute(
                """
                SELECT clips.*, games.game_date, games.opponent, games.home_away,
                       assets.duration AS asset_duration
                FROM clips JOIN games USING(event_id)
                LEFT JOIN assets USING(asset_id)
                WHERE clip_key = ?
                """,
                (clip_key,),
            ).fetchone()
            assert row is not None
            result = dict(row)
            result["participants"] = [dict(tag) for tag in self._tags_for_clip(conn, clip_key)]
            result["source_play_summary"] = result["play_summary"]
            result["play_summary"] = display_play_summary(
                result["source_play_summary"],
                result["participants"],
            )
            result["play_title"] = metadata_play_title(
                result["play_type"],
                result["source_play_summary"],
                result["participants"],
            )
            result["side"] = team_side(result["home_away"], result["inning_half"])
            result["display_start"] = (
                result["final_start"]
                if result["review_state"] == "reviewed"
                else result["draft_start"] if result["draft_start"] is not None else result["proposal_start"]
            )
            result["display_end"] = (
                result["final_end"]
                if result["review_state"] == "reviewed"
                else result["draft_end"] if result["draft_end"] is not None else result["proposal_end"]
            )
            return result

    def replace_draft_participants(self, clip_key: str, values: Iterable[tuple[str, str]]) -> None:
        normalized = {(player_id, role) for player_id, role in values if role in ROLE_NAMES}
        with self.connect() as conn:
            conn.execute("DELETE FROM participant_tags WHERE clip_key = ? AND source = 'draft'", (clip_key,))
            for player_id, role in normalized:
                conn.execute(
                    """
                    INSERT INTO participant_tags(clip_key, player_id, role, source, confidence)
                    VALUES (?, ?, ?, 'draft', 'reviewed')
                    """,
                    (clip_key, player_id, role),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO project(key, value)
                VALUES (?, '1')
                """,
                (f"draft_participants:{clip_key}",),
            )

    def set_timing(self, clip_key: str, start: float, end: float) -> None:
        clip = self.clip(clip_key)
        duration = clip.get("asset_duration")
        if start < 0 or end <= start:
            raise GCError("Clip timing must satisfy 0 <= start < end.")
        duration_is_reliable = (
            duration is not None
            and clip.get("video_offset") is not None
            and float(clip["video_offset"]) <= float(duration)
        )
        if duration_is_reliable and end > float(duration) + 0.001:
            raise GCError(f"Clip end exceeds the asset duration ({duration:.1f}s).")
        start = float(round(start))
        end = float(round(end))
        with self.connect() as conn:
            if clip["review_state"] == "reviewed":
                conn.execute(
                    "UPDATE clips SET final_start = ?, final_end = ? WHERE clip_key = ?",
                    (start, end, clip_key),
                )
            else:
                conn.execute(
                    "UPDATE clips SET draft_start = ?, draft_end = ? WHERE clip_key = ?",
                    (start, end, clip_key),
                )

    def confirm_clip(self, clip_key: str) -> None:
        with self.connect() as conn:
            clip = conn.execute("SELECT * FROM clips WHERE clip_key = ?", (clip_key,)).fetchone()
            if not clip:
                raise GCError(f"Unknown clip: {clip_key}")
            draft_marker = conn.execute(
                "SELECT 1 FROM project WHERE key = ?", (f"draft_participants:{clip_key}",)
            ).fetchone()
            if not draft_marker:
                conn.execute(
                    """
                    INSERT INTO participant_tags(clip_key, player_id, role, source, confidence)
                    SELECT clip_key, player_id, role, 'draft', confidence
                    FROM participant_tags
                    WHERE clip_key = ? AND source = 'inferred'
                    """,
                    (clip_key,),
                )
            start = clip["draft_start"] if clip["draft_start"] is not None else clip["proposal_start"]
            end = clip["draft_end"] if clip["draft_end"] is not None else clip["proposal_end"]
            if start is None or end is None:
                raise GCError("Clip has no usable timing; set start and end before confirming.")
            conn.execute(
                """
                UPDATE clips SET
                    review_state='reviewed',
                    final_start=?,
                    final_end=?,
                    source_changed=0
                WHERE clip_key=?
                """,
                (start, end, clip_key),
            )
            active_players = {
                row["player_id"]
                for row in conn.execute(
                    "SELECT DISTINCT player_id FROM participant_tags WHERE clip_key = ? AND source = 'draft'",
                    (clip_key,),
                )
            }
            conn.execute("UPDATE player_decisions SET active = 0 WHERE clip_key = ?", (clip_key,))
            for player_id in active_players:
                conn.execute(
                    """
                    INSERT INTO player_decisions(clip_key, player_id, status, active, updated_at)
                    VALUES (?, ?, 'pending', 1, ?)
                    ON CONFLICT(clip_key, player_id) DO UPDATE SET active=1, updated_at=excluded.updated_at
                    """,
                    (clip_key, player_id, utc_now()),
                )

    def dismiss_clip(self, clip_key: str, reason: str) -> None:
        self.dismiss_clips([clip_key], reason)

    def dismiss_clips(self, clip_keys: Iterable[str], reason: str) -> int:
        if reason not in DISMISS_REASONS:
            raise GCError(f"Unknown dismissal reason: {reason}")
        keys = list(dict.fromkeys(str(key) for key in clip_keys if key))
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        with self.connect() as conn:
            found = {
                row["clip_key"]
                for row in conn.execute(
                    f"SELECT clip_key FROM clips WHERE clip_key IN ({placeholders})",
                    keys,
                )
            }
            missing = [key for key in keys if key not in found]
            if missing:
                raise GCError(f"Unknown clip: {missing[0]}")
            conn.execute(
                f"""
                UPDATE clips SET review_state = 'dismissed'
                WHERE clip_key IN ({placeholders})
                """,
                keys,
            )
            conn.execute(
                f"""
                UPDATE player_decisions SET active = 0
                WHERE clip_key IN ({placeholders})
                """,
                keys,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO project(key, value) VALUES (?, ?)",
                [(f"dismiss_reason:{key}", reason) for key in keys],
            )
        return len(keys)

    def _scored_clip(
        self,
        row: dict[str, Any],
        tags: list[dict[str, Any]],
        *,
        display_tags: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        source_summary = row["play_summary"]
        scored = [
            (*score_for_role(row["play_type"], source_summary, tag["role"], bool(row["exceptional"])), tag)
            for tag in tags
        ]
        if scored:
            score, reason, _ = max(scored, key=lambda item: item[0])
        else:
            score, reason = fallback_clip_score(row["play_type"], source_summary, bool(row["exceptional"]))
        row["source_play_summary"] = source_summary
        row["play_summary"] = display_play_summary(source_summary, display_tags or tags)
        row["play_title"] = metadata_play_title(
            row["play_type"],
            source_summary,
            display_tags or tags,
        )
        row["score"] = score
        row["score_reason"] = reason
        row["participants"] = tags
        participant_labels = []
        for tag in tags:
            confidence = f", {tag['confidence']}" if tag.get("confidence") else ""
            participant_labels.append(f"{tag['display']} ({tag['role']}{confidence})")
        row["participant_text"] = ", ".join(participant_labels) or "None"
        row["timing_text"] = (
            f"{format_timestamp(row['display_start'])}-{format_timestamp(row['display_end'])}"
            if row.get("display_start") is not None and row.get("display_end") is not None
            else "untimed"
        )
        return row

    def all_queue(
        self,
        *,
        role: str | None = None,
        side: str | None = None,
    ) -> list[dict[str, Any]]:
        cached_events = self.cached_event_ids()
        if not cached_events:
            return []
        with self.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT clips.*, games.game_date, games.opponent, games.home_away
                    FROM clips JOIN games USING(event_id)
                    WHERE review_state = 'unreviewed'
                    """
                )
            )
            results = []
            for raw_row in rows:
                row = dict(raw_row)
                if row["event_id"] not in cached_events:
                    continue
                row["side"] = team_side(row["home_away"], row["inning_half"])
                if not is_queueable_play(
                    row["play_type"],
                    row["play_summary"],
                    bool(row["exceptional"]),
                    row["side"],
                ):
                    continue
                if side and row["side"] != side:
                    continue
                tags = [dict(tag) for tag in self._tags_for_clip(conn, row["clip_key"])]
                if role and not any(tag["role"] == role for tag in tags):
                    continue
                row["display_start"] = row["draft_start"] if row["draft_start"] is not None else row["proposal_start"]
                row["display_end"] = row["draft_end"] if row["draft_end"] is not None else row["proposal_end"]
                results.append(self._scored_clip(row, tags))
        return sorted(results, key=lambda item: (-item["score"], item["game_date"] or "", item["video_offset"] or 0))

    def player_queue(
        self,
        player_id: str,
        *,
        status: str = "pending",
        role: str | None = None,
        side: str | None = None,
    ) -> list[dict[str, Any]]:
        cached_events = self.cached_event_ids()
        if not cached_events:
            return []
        with self.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT clips.*, games.game_date, games.opponent, games.home_away,
                           player_decisions.status, player_decisions.reel_order
                    FROM player_decisions
                    JOIN clips USING(clip_key)
                    JOIN games USING(event_id)
                    WHERE player_decisions.player_id = ?
                      AND player_decisions.active = 1
                      AND player_decisions.status = ?
                      AND clips.review_state IN ('unreviewed', 'reviewed')
                    """,
                    (player_id, status),
                )
            )
            results = []
            for raw_row in rows:
                row = dict(raw_row)
                if row["event_id"] not in cached_events:
                    continue
                row["side"] = team_side(row["home_away"], row["inning_half"])
                if not is_queueable_play(
                    row["play_type"],
                    row["play_summary"],
                    bool(row["exceptional"]),
                    row["side"],
                ):
                    continue
                if side and row["side"] != side:
                    continue
                tags = [dict(tag) for tag in self._tags_for_clip(conn, row["clip_key"])]
                player_tags = [tag for tag in tags if tag["player_id"] == player_id]
                if not player_tags:
                    continue
                if role and not any(tag["role"] == role for tag in player_tags):
                    continue
                if row["review_state"] == "reviewed":
                    row["display_start"] = row["final_start"]
                    row["display_end"] = row["final_end"]
                else:
                    row["display_start"] = (
                        row["draft_start"]
                        if row["draft_start"] is not None
                        else row["proposal_start"]
                    )
                    row["display_end"] = (
                        row["draft_end"]
                        if row["draft_end"] is not None
                        else row["proposal_end"]
                    )
                scored = self._scored_clip(row, player_tags, display_tags=tags)
                scored["all_participants"] = tags
                results.append(scored)
        if status == "accepted":
            return sorted(
                results,
                key=lambda item: (
                    item["reel_order"] is None,
                    item["reel_order"] if item["reel_order"] is not None else -item["score"],
                ),
            )
        return sorted(results, key=lambda item: (-item["score"], item["game_date"] or "", item["video_offset"] or 0))

    def inferred_player_queue(
        self,
        player_id: str,
        *,
        role: str | None = None,
        side: str | None = None,
    ) -> list[dict[str, Any]]:
        cached_events = self.cached_event_ids()
        if not cached_events:
            return []
        with self.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT clips.*, games.game_date, games.opponent, games.home_away
                    FROM clips
                    JOIN games USING(event_id)
                    WHERE clips.review_state = 'unreviewed'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM player_decisions
                          WHERE player_decisions.clip_key = clips.clip_key
                            AND player_decisions.player_id = ?
                            AND player_decisions.active = 1
                      )
                    """,
                    (player_id,),
                )
            )
            results = []
            for raw_row in rows:
                row = dict(raw_row)
                if row["event_id"] not in cached_events:
                    continue
                row["side"] = team_side(row["home_away"], row["inning_half"])
                if not is_queueable_play(
                    row["play_type"],
                    row["play_summary"],
                    bool(row["exceptional"]),
                    row["side"],
                ):
                    continue
                if side and row["side"] != side:
                    continue
                tags = [dict(tag) for tag in self._tags_for_clip(conn, row["clip_key"])]
                player_tags = [tag for tag in tags if tag["player_id"] == player_id]
                if not player_tags:
                    continue
                if role and not any(tag["role"] == role for tag in player_tags):
                    continue
                row["display_start"] = row["draft_start"] if row["draft_start"] is not None else row["proposal_start"]
                row["display_end"] = row["draft_end"] if row["draft_end"] is not None else row["proposal_end"]
                scored = self._scored_clip(row, player_tags, display_tags=tags)
                scored["status"] = "unconfirmed"
                results.append(scored)
        return sorted(results, key=lambda item: (-item["score"], item["game_date"] or "", item["video_offset"] or 0))

    def set_decision(self, clip_key: str, player_id: str, status: str) -> None:
        if status not in DECISION_STATES:
            raise GCError(f"Unknown player decision: {status}")
        with self.connect() as conn:
            clip = conn.execute(
                "SELECT review_state FROM clips WHERE clip_key = ?",
                (clip_key,),
            ).fetchone()
            if not clip:
                raise GCError(f"Unknown clip: {clip_key}")
            if clip["review_state"] != "reviewed" and status != "skipped":
                raise GCError("Only Skip is available before clip timing is confirmed.")
            decision = conn.execute(
                "SELECT * FROM player_decisions WHERE clip_key = ? AND player_id = ? AND active = 1",
                (clip_key, player_id),
            ).fetchone()
            if not decision:
                participant = next(
                    (
                        tag
                        for tag in self._tags_for_clip(conn, clip_key)
                        if tag["player_id"] == player_id
                    ),
                    None,
                )
                if clip["review_state"] == "reviewed" or not participant:
                    raise GCError("The player is not an active participant in this clip.")
                conn.execute(
                    """
                    INSERT INTO player_decisions(
                        clip_key, player_id, status, active, updated_at
                    ) VALUES (?, ?, 'skipped', 1, ?)
                    """,
                    (clip_key, player_id, utc_now()),
                )
                return
            order_value = decision["reel_order"]
            if status == "accepted" and order_value is None:
                maximum = conn.execute(
                    "SELECT MAX(reel_order) FROM player_decisions WHERE player_id = ? AND status = 'accepted'",
                    (player_id,),
                ).fetchone()[0]
                order_value = (maximum if maximum is not None else -1) + 1
            conn.execute(
                """
                UPDATE player_decisions SET status = ?, reel_order = ?, updated_at = ?
                WHERE clip_key = ? AND player_id = ?
                """,
                (status, order_value, utc_now(), clip_key, player_id),
            )

    def move_accepted(self, player_id: str, clip_key: str, direction: int) -> None:
        accepted = self.player_queue(player_id, status="accepted")
        keys = [item["clip_key"] for item in accepted]
        if clip_key not in keys:
            return
        index = keys.index(clip_key)
        target = max(0, min(len(keys) - 1, index + direction))
        if target == index:
            return
        keys[index], keys[target] = keys[target], keys[index]
        with self.connect() as conn:
            for order, key in enumerate(keys):
                conn.execute(
                    "UPDATE player_decisions SET reel_order = ? WHERE clip_key = ? AND player_id = ?",
                    (order, key, player_id),
                )

    def dashboard(self) -> list[dict[str, Any]]:
        result = []
        cached_events = self.cached_event_ids()
        with self.connect() as conn:
            unconfirmed_by_player: defaultdict[str, set[str]] = defaultdict(set)
            decided_pairs = {
                (row["clip_key"], row["player_id"])
                for row in conn.execute(
                    """
                    SELECT clip_key, player_id
                    FROM player_decisions
                    WHERE active = 1
                    """
                )
            }
            for row in conn.execute(
                """
                SELECT clips.clip_key, clips.event_id, clips.play_type,
                       clips.play_summary, clips.exceptional, clips.inning_half,
                       games.home_away
                FROM clips JOIN games USING(event_id)
                WHERE review_state = 'unreviewed'
                """
            ):
                if row["event_id"] not in cached_events:
                    continue
                if not is_queueable_play(
                    row["play_type"],
                    row["play_summary"],
                    bool(row["exceptional"]),
                    team_side(row["home_away"], row["inning_half"]),
                ):
                    continue
                for tag in self._tags_for_clip(conn, row["clip_key"]):
                    if (row["clip_key"], tag["player_id"]) in decided_pairs:
                        continue
                    unconfirmed_by_player[tag["player_id"]].add(row["clip_key"])
            for player in self.players():
                counts = defaultdict(int)
                for row in conn.execute(
                    """
                    SELECT player_decisions.status, clips.event_id,
                           clips.play_type, clips.play_summary, clips.exceptional,
                           clips.inning_half, games.home_away
                    FROM player_decisions
                    JOIN clips USING(clip_key)
                    JOIN games USING(event_id)
                    WHERE player_decisions.player_id = ?
                      AND player_decisions.active = 1
                    """,
                    (player["player_id"],),
                ):
                    if row["event_id"] not in cached_events:
                        continue
                    if not is_queueable_play(
                        row["play_type"],
                        row["play_summary"],
                        bool(row["exceptional"]),
                        team_side(row["home_away"], row["inning_half"]),
                    ):
                        continue
                    counts[row["status"]] += 1
                result.append(
                    {
                        **player,
                        **counts,
                        "unconfirmed": len(unconfirmed_by_player[player["player_id"]]),
                    }
                )
        return result

    def preview_record(self, clip_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM previews WHERE clip_key = ?", (clip_key,)).fetchone()
        return dict(row) if row else None

    def full_game_asset_path(self, event_id: str, asset_id: str) -> Path:
        return self.root / "full_games" / event_id / f"{asset_id}.mp4"

    def media_cache_status(self) -> dict[str, Any]:
        games = []
        total_assets = 0
        cached_assets = 0
        cached_bytes = 0
        with self.connect() as conn:
            for game in conn.execute("SELECT * FROM games ORDER BY game_date"):
                assets = []
                for asset in conn.execute(
                    "SELECT * FROM assets WHERE event_id = ? ORDER BY created_at",
                    (game["event_id"],),
                ):
                    path = self.full_game_asset_path(game["event_id"], asset["asset_id"])
                    size = path.stat().st_size if path.exists() else 0
                    cached = size > 0
                    assets.append(
                        {
                            "assetId": asset["asset_id"],
                            "duration": asset["duration"],
                            "cached": cached,
                            "bytes": size,
                        }
                    )
                    total_assets += 1
                    cached_assets += int(cached)
                    cached_bytes += size
                games.append(
                    {
                        "eventId": game["event_id"],
                        "gameDate": game["game_date"],
                        "opponent": game["opponent"],
                        "assets": assets,
                        "assetCount": len(assets),
                        "cachedAssets": sum(int(asset["cached"]) for asset in assets),
                        "duration": sum(float(asset["duration"] or 0) for asset in assets),
                        "bytes": sum(int(asset["bytes"]) for asset in assets),
                    }
                )
        return {
            "games": games,
            "totalAssets": total_assets,
            "cachedAssets": cached_assets,
            "cachedBytes": cached_bytes,
        }

    def cached_event_ids(self) -> set[str]:
        cached: set[str] = set()
        with self.connect() as conn:
            for game in conn.execute("SELECT event_id FROM games"):
                assets = list(
                    conn.execute(
                        "SELECT asset_id FROM assets WHERE event_id = ?",
                        (game["event_id"],),
                    )
                )
                if assets and all(
                    (
                        path := self.full_game_asset_path(
                            game["event_id"],
                            asset["asset_id"],
                        )
                    ).exists()
                    and path.stat().st_size > 0
                    for asset in assets
                ):
                    cached.add(game["event_id"])
        return cached

    def queueable_unreviewed_count(self) -> int:
        event_ids = self.cached_event_ids()
        if not event_ids:
            return 0
        placeholders = ",".join("?" for _ in event_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT clips.play_type, clips.play_summary, clips.exceptional,
                       clips.inning_half, games.home_away
                FROM clips JOIN games USING(event_id)
                WHERE review_state = 'unreviewed'
                  AND clips.event_id IN ({placeholders})
                """,
                tuple(event_ids),
            )
            return sum(
                is_queueable_play(
                    row["play_type"],
                    row["play_summary"],
                    bool(row["exceptional"]),
                    team_side(row["home_away"], row["inning_half"]),
                )
                for row in rows
            )

    def queueable_decision_count(self, status: str) -> int:
        event_ids = self.cached_event_ids()
        if not event_ids:
            return 0
        placeholders = ",".join("?" for _ in event_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT clips.play_type, clips.play_summary, clips.exceptional,
                       clips.inning_half, games.home_away
                FROM player_decisions
                JOIN clips USING(clip_key)
                JOIN games USING(event_id)
                WHERE player_decisions.active = 1
                  AND player_decisions.status = ?
                  AND clips.event_id IN ({placeholders})
                """,
                (status, *event_ids),
            )
            return sum(
                is_queueable_play(
                    row["play_type"],
                    row["play_summary"],
                    bool(row["exceptional"]),
                    team_side(row["home_away"], row["inning_half"]),
                )
                for row in rows
            )

    def _asset_metadata(self, asset_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        if not row:
            raise GCError(f"No video asset metadata for {asset_id}.")
        return {**json.loads(row["metadata_json"]), **dict(row)}

    def resolve_playback_asset(self, client: GCClient, clip: dict[str, Any]) -> dict[str, Any]:
        event_id = clip["event_id"]
        with self.connect() as conn:
            metadata = [
                {**json.loads(row["metadata_json"]), **dict(row)}
                for row in conn.execute("SELECT * FROM assets WHERE event_id = ?", (event_id,))
            ]
        playback = client.get_event_playback_assets(self.team_id, event_id)
        assets = playable_assets(metadata, playback)
        for asset in assets:
            if asset_identity(asset) == clip.get("asset_id"):
                return asset
        if len(assets) == 1:
            return assets[0]
        raise GCError(f"Could not resolve playback asset for {clip['clip_key']}.")

    def full_game_media(self, clip_key: str) -> tuple[Path, dict[str, Any]]:
        clip = self.clip(clip_key)
        asset_id = str(clip.get("asset_id") or "")
        path = self.full_game_asset_path(str(clip["event_id"]), asset_id)
        if not asset_id or not path.exists():
            raise GCError(
                "This game's full video is not cached. Open Game cache and download it first."
            )
        return path, clip

    def slice_clip(self, clip_key: str) -> Path:
        source_path, clip = self.full_game_media(clip_key)
        start = clip.get("display_start")
        end = clip.get("display_end")
        if start is None or end is None:
            raise GCError("Set clip timing before slicing the clip.")
        start = float(start)
        end = float(end)
        preview = self.preview_record(clip_key)
        if preview:
            path = self.root / preview["path"]
            if (
                path.name.endswith(f"-v{PREVIEW_CACHE_VERSION}.mp4")
                and has_video_stream(path)
                and abs(float(preview["source_start"]) - start) < 0.001
                and abs(float(preview["source_end"]) - end) < 0.001
            ):
                return path

        path = (
            self.root
            / "previews"
            / f"{slugify(clip_key)}-v{PREVIEW_CACHE_VERSION}.mp4"
        )
        temp_path = path.with_suffix(f".tmp-{os.getpid()}.mp4")
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{end - start:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
        try:
            try:
                run(command)
            except subprocess.CalledProcessError as exc:
                raise GCError(
                    f"ffmpeg could not slice the local full-game video (exit status {exc.returncode})."
                ) from None
            if not has_video_stream(temp_path):
                raise GCError("ffmpeg created a preview with no playable video stream.")
            temp_path.replace(path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO previews(clip_key, path, source_start, source_end, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(clip_key) DO UPDATE SET
                    path=excluded.path,
                    source_start=excluded.source_start,
                    source_end=excluded.source_end,
                    updated_at=excluded.updated_at
                """,
                (clip_key, str(path.relative_to(self.root)), start, end, utc_now()),
            )
        return path

    def ensure_preview(self, client: GCClient, clip_key: str, *, handles: float = 20.0) -> Path:
        del client, handles
        return self.slice_clip(clip_key)

    def play_preview(self, client: GCClient, clip_key: str) -> Path:
        del client
        path, clip = self.full_game_media(clip_key)
        seek = max(0.0, float(clip["display_start"]))
        process = subprocess.Popen(
            ["ffplay", "-hide_banner", "-loglevel", "error", "-ss", f"{seek:.3f}", "-autoexit", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        time.sleep(0.5)
        if process.poll() is not None:
            detail = (process.stderr.read() if process.stderr else "").strip()
            raise GCError(f"ffplay could not open the preview{f': {detail}' if detail else '.'}")
        return path

    def _normalized_part(
        self, client: GCClient, clip: dict[str, Any], output: Path
    ) -> None:
        start = float(clip["final_start"])
        end = float(clip["final_end"])
        source_path = self.ensure_preview(client, clip["clip_key"])
        preview = self.preview_record(clip["clip_key"])
        if (
            not preview
            or preview["source_start"] > start
            or preview["source_end"] < end
        ):
            raise GCError(
                f"The bounded GameChanger clip does not cover the final timing for {clip['clip_key']}."
            )
        source = str(source_path)
        start -= float(preview["source_start"])
        end -= float(preview["source_start"])

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
        cmd.extend(
            [
                "-ss",
                f"{start:.3f}",
                "-i",
                source,
                "-t",
                f"{end - start:.3f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        run(cmd)

    def render_player(self, client: GCClient, player_id: str, output: str | Path | None = None) -> Path:
        accepted = self.player_queue(player_id, status="accepted")
        if not accepted:
            raise GCError("This player has no accepted clips.")
        player = next((item for item in self.players(active_only=False) if item["player_id"] == player_id), None)
        if not player:
            raise GCError(f"Unknown player: {player_id}")
        output_path = Path(output) if output else self.root / "renders" / f"{slugify(player['display'])}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="gc-season-render-") as temp_name:
            temp_dir = Path(temp_name)
            parts = []
            for index, clip in enumerate(accepted):
                part = temp_dir / f"part-{index:04d}.mp4"
                self._normalized_part(client, clip, part)
                parts.append(part)
            concat_path = temp_dir / "concat.txt"
            concat_path.write_text(
                "".join(f"file '{part.resolve().as_posix()}'\n" for part in parts),
                encoding="utf-8",
            )
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
        return output_path

    def render_all(self, client: GCClient) -> list[Path]:
        outputs = []
        for player in self.players():
            if self.player_queue(player["player_id"], status="accepted"):
                outputs.append(self.render_player(client, player["player_id"]))
        return outputs
