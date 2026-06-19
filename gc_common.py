#!/usr/bin/env python3
"""Shared GameChanger helpers for fetching metadata and building videos."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

BASE_URL = "https://api.team-manager.gc.com"
WEB_BASE_URL = "https://web.gc.com"
PLAYER_RE = re.compile(r"\$\{([^}]+)\}")


class GCError(RuntimeError):
    pass


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_timestamp(seconds: float) -> str:
    seconds_i = max(0, int(seconds))
    hours, rem = divmod(seconds_i, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "untitled"


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: str | Path, data: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


class GCClient:
    def __init__(self, token: str | None = None) -> None:
        token = token or os.environ.get("GC_TOKEN")
        if not token:
            raise GCError("Set GC_TOKEN or pass --token.")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "gc-token": token,
                "gc-app-name": "web",
                "accept": "application/json",
                "user-agent": "gc-video-tools/0.1",
            }
        )

    def get(self, path: str, *, headers: dict[str, str] | None = None) -> Any:
        response = self.session.get(f"{BASE_URL}{path}", headers=headers, timeout=45)
        if response.status_code >= 400:
            raise GCError(f"GET {path} failed: {response.status_code} {response.text[:300]}")
        return response.json()

    def get_best_game_stream_id(self, event_id: str) -> str | None:
        data = self.get(f"/events/{event_id}/best-game-stream-id")
        return data.get("game_stream_id")

    def get_game_stream_events(self, game_stream_id: str) -> list[dict[str, Any]]:
        return self.get(f"/game-streams/{game_stream_id}/events")

    def get_viewer_payload(self, event_id: str) -> dict[str, Any]:
        return self.get(f"/game-streams/gamestream-viewer-payload-lite/{event_id}")

    def get_team_assets(self, team_id: str) -> list[dict[str, Any]]:
        headers = {
            "accept": "application/vnd.gc.com.video_stream_asset_metadata:list+json; version=0.0.0",
            "content-type": "application/vnd.gc.com.none+json; version=undefined",
        }
        return self.get(f"/teams/{team_id}/video-stream/assets", headers=headers)

    def get_event_assets(self, team_id: str, event_id: str) -> list[dict[str, Any]]:
        return self.get(f"/teams/{team_id}/schedule/events/{event_id}/video-stream/assets")

    def get_event_playback_assets(self, team_id: str, event_id: str) -> list[dict[str, Any]]:
        return self.get(f"/teams/{team_id}/schedule/events/{event_id}/video-stream/assets/playback")

    def get_players(self, team_id: str) -> list[dict[str, Any]]:
        return self.get(f"/teams/{team_id}/players")

    def get_public_players(self, public_team_id: str) -> list[dict[str, Any]]:
        return self.get(f"/teams/public/{public_team_id}/players")

    def get_opponent_roster(self, team_id: str, opponent_id: str) -> list[dict[str, Any]]:
        return self.get(f"/teams/{team_id}/opponents/{opponent_id}/roster")

    def get_schedule(self, team_id: str) -> list[dict[str, Any]]:
        return self.get(f"/teams/{team_id}/schedule?fetch_place_details=true")

    def get_game_summaries(self, team_id: str) -> list[dict[str, Any]]:
        return self.get(f"/teams/{team_id}/game-summaries")

    def get_public_game_details(self, event_id: str) -> dict[str, Any]:
        return self.get(f"/public/game-stream-processing/{event_id}/details?include=line_scores")

    def get_player_stats(self, team_id: str, event_id: str) -> dict[str, Any]:
        return self.get(f"/teams/{team_id}/schedule/events/{event_id}/player-stats")

    def get_season_stats(self, team_id: str) -> dict[str, Any]:
        return self.get(f"/teams/{team_id}/season-stats")

    def search_clips(self, team_id: str, event_id: str | None = None, limit: int = 500) -> dict[str, Any]:
        body: dict[str, Any] = {
            "match_all": {"team_id": team_id},
            "sort": [{"by": "timestamp", "order": "asc"}],
            "limit": limit,
            "select": {"kind": "event", "include_totals": True},
            "offset": 0,
            "paging": "page",
        }
        if event_id:
            body["match_all"]["event_id"] = event_id
        response = self.session.post(
            f"{BASE_URL}/clips/search",
            headers={
                "content-type": "application/vnd.gc.com.video_clip_search_query+json; version=0.0.0",
                "accept": "application/vnd.gc.com.video_clip_search_results+json; version=0.1.0",
                "x-gc-features": "lazy-sync",
            },
            json=body,
            timeout=45,
        )
        if response.status_code >= 400:
            raise GCError(f"POST /clips/search failed: {response.status_code} {response.text[:300]}")
        return response.json()


def safe_get(call, default):
    try:
        return call()
    except Exception:
        return default


def player_display(player: dict[str, Any]) -> str:
    first = player.get("first_name") or ""
    last = player.get("last_name") or ""
    number = str(player.get("number") or "").strip()
    name = " ".join(part for part in [first, last] if part).strip()
    if not name:
        name = player.get("id", "unknown")[:8]
    return f"{name} #{number}" if number else name


def build_player_map(*rosters: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    players: dict[str, dict[str, Any]] = {}
    for roster in rosters:
        for player in roster or []:
            player_id = player.get("id") or player.get("player_id")
            if not player_id:
                continue
            normalized = dict(player)
            normalized["display"] = player_display(player)
            players[player_id] = normalized
    return players


def replace_player_placeholders(summary: str, players: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    mentioned: list[str] = []

    def repl(match: re.Match[str]) -> str:
        player_id = match.group(1)
        mentioned.append(player_id)
        return players.get(player_id, {}).get("display") or f"Player {player_id[:8]}"

    return PLAYER_RE.sub(repl, summary or ""), mentioned


def flatten_stream_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for wrapper in events:
        raw = wrapper.get("event_data")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        nested = parsed.get("events") if parsed.get("code") == "transaction" else None
        source_events = nested if isinstance(nested, list) else [parsed]
        for event in source_events:
            if not isinstance(event, dict):
                continue
            event_copy = dict(event)
            event_copy["_stream_sequence_number"] = wrapper.get("sequence_number")
            event_copy["_stream_event_id"] = wrapper.get("id")
            flattened.append(event_copy)
    return flattened


def stream_events_by_pbp_id(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(event["id"]).lower(): event
        for event in flatten_stream_events(events)
        if event.get("id")
    }


def choose_event_id(client: GCClient, team_id: str, requested_event_id: str | None = None) -> str:
    if requested_event_id:
        return requested_event_id
    summaries = client.get_game_summaries(team_id)
    completed = [s for s in summaries if s.get("game_status") == "completed"]
    if not completed:
        raise GCError("No completed games found. Pass --event-id explicitly.")
    completed.sort(key=lambda item: item.get("last_scoring_update") or "", reverse=True)
    return completed[0]["event_id"]


def select_video_asset(
    team_assets: list[dict[str, Any]],
    event_assets: list[dict[str, Any]],
    event_id: str,
) -> dict[str, Any] | None:
    candidates = [a for a in team_assets if a.get("schedule_event_id") == event_id]
    candidates.extend(a for a in event_assets if a.get("schedule_event_id") == event_id)
    if not candidates:
        return None
    candidates.sort(key=lambda a: (a.get("created_at") or "", a.get("duration") or 0), reverse=True)
    merged = dict(candidates[-1])
    for candidate in candidates:
        merged.update({k: v for k, v in candidate.items() if v is not None})
    return merged


def clip_window(clip: dict[str, Any], asset: dict[str, Any], pre_roll: float, post_roll: float) -> dict[str, float]:
    asset_start = parse_iso(asset["created_at"])
    timestamp = parse_iso(clip["timestamp"])
    duration = float(clip.get("duration") or 0)
    video_offset = max(0.0, (timestamp - asset_start).total_seconds())
    clip_start = max(0.0, video_offset - duration)
    clip_end = video_offset
    asset_duration = float(asset.get("duration") or clip_end + post_roll)
    return {
        "video_offset_sec": round(video_offset, 3),
        "clip_start_sec": round(clip_start, 3),
        "clip_end_sec": round(clip_end, 3),
        "segment_start_sec": round(max(0.0, clip_start - pre_roll), 3),
        "segment_end_sec": round(min(asset_duration, clip_end + post_roll), 3),
    }


def youtube_time_url(youtube_url: str | None, seconds: float) -> str | None:
    if not youtube_url:
        return None
    sep = "&" if "?" in youtube_url else "?"
    return f"{youtube_url}{sep}t={int(max(0, seconds))}s"


def write_play_outputs(out_dir: Path, plays: list[dict[str, Any]]) -> None:
    rows = plays
    csv_path = out_dir / "plays.csv"
    md_path = out_dir / "plays.md"
    if rows:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Plays\n\n")
        last_group = None
        for row in rows:
            group = (row.get("inning"), row.get("inning_half"))
            if group != last_group:
                f.write(f"\n## {str(row.get('inning_half', '')).title()} {row.get('inning')}\n\n")
                last_group = group
            ts = row.get("video_timestamp") or ""
            f.write(f"- `{ts}` `{row.get('play_type')}` {row.get('play_summary')}\n")


def cookie_header(playback_assets: list[dict[str, Any]]) -> str | None:
    if not playback_assets:
        return None
    cookies = playback_assets[0].get("cookies") or {}
    if not cookies:
        return None
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def video_source_from_game(game: dict[str, Any], override: str | None = None) -> tuple[str, str | None]:
    if override:
        return override, None
    playback_assets = game.get("playback_assets") or []
    if playback_assets and playback_assets[0].get("url"):
        return playback_assets[0]["url"], cookie_header(playback_assets)
    asset = game.get("video_asset") or {}
    if asset.get("playback_url"):
        return asset["playback_url"], None
    raise GCError("No video source found. Pass --video with a local file or HLS URL.")


@dataclass
class Segment:
    start: float
    end: float
    title: str


def merge_segments(segments: list[Segment], max_gap: float = 1.0) -> list[Segment]:
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    merged = [ordered[0]]
    for seg in ordered[1:]:
        cur = merged[-1]
        if seg.start <= cur.end + max_gap:
            cur.end = max(cur.end, seg.end)
            cur.title = f"{cur.title}; {seg.title}"
        else:
            merged.append(seg)
    return merged


def adjusted_segment_window(
    start: float,
    end: float,
    *,
    start_buffer: float = 0.0,
    end_buffer: float = 0.0,
    min_duration: float = 0.0,
    duration_limit: float | None = None,
) -> tuple[float, float] | None:
    start = max(0.0, start - max(0.0, start_buffer))
    end = end + max(0.0, end_buffer)
    if min_duration > 0:
        end = max(end, start + min_duration)
    if duration_limit is not None:
        end = min(float(duration_limit), end)
    if end <= start:
        return None
    return start, end


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def segment_cache_key(source: str, start: float, end: float, reencode: bool) -> str:
    source_identity: dict[str, Any] = {"source": source}
    source_path = Path(source)
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", source) and source_path.exists():
        stat = source_path.stat()
        source_identity = {
            "source": str(source_path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    payload = json.dumps(
        {
            **source_identity,
            "start": f"{start:.3f}",
            "end": f"{end:.3f}",
            "reencode": reencode,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def make_reel(
    source: str,
    output: str | Path,
    segments: list[Segment],
    *,
    cookie: str | None = None,
    reencode: bool = False,
    max_merge_gap: float = 1.0,
    cache_dir: str | Path | None = "gc_render_cache/segments",
) -> None:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    cache_path = Path(cache_dir) if cache_dir else None
    if cache_path:
        cache_path.mkdir(parents=True, exist_ok=True)
    segments = merge_segments(segments, max_gap=max_merge_gap)
    if not segments:
        raise GCError("No segments selected.")

    with tempfile.TemporaryDirectory(prefix="gc-reel-") as tmp_name:
        tmp = Path(tmp_name)
        parts: list[Path] = []
        for index, seg in enumerate(segments):
            part = tmp / f"part-{index:04d}.mp4"
            cached_part = None
            if cache_path:
                cached_part = cache_path / f"{segment_cache_key(source, seg.start, seg.end, reencode)}.mp4"
                if cached_part.exists() and cached_part.stat().st_size > 0:
                    link_or_copy(cached_part, part)
                    parts.append(part)
                    continue

            target = cached_part.with_suffix(f".tmp-{os.getpid()}-{index}.mp4") if cached_part else part
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
            if cookie:
                cmd.extend(["-headers", f"Cookie: {cookie}\r\n"])
            cmd.extend(["-ss", f"{seg.start:.3f}", "-to", f"{seg.end:.3f}", "-i", source])
            if reencode:
                cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart"])
            else:
                cmd.extend(["-c", "copy", "-avoid_negative_ts", "make_zero"])
            cmd.append(str(target))
            try:
                run(cmd)
                if cached_part:
                    target.replace(cached_part)
                    link_or_copy(cached_part, part)
            finally:
                if cached_part and target.exists():
                    target.unlink()
            parts.append(part)

        concat_file = tmp / "concat.txt"
        with open(concat_file, "w", encoding="utf-8") as f:
            for part in parts:
                f.write(f"file '{part.as_posix()}'\n")

        concat_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file)]
        if reencode:
            concat_cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart"])
        else:
            concat_cmd.extend(["-c", "copy", "-movflags", "+faststart"])
        concat_cmd.append(str(output))
        run(concat_cmd)


def plays_to_segments(
    plays: list[dict[str, Any]],
    *,
    start_buffer: float = 0.0,
    end_buffer: float = 0.0,
    min_duration: float = 0.0,
    time_shift: float = 0.0,
    anchor: str = "auto",
    long_clip_threshold: float = 12.0,
    long_clip_start_buffer: float = 18.0,
) -> list[Segment]:
    segments = []
    for play in plays:
        anchor_value = None
        effective_start_buffer = start_buffer
        duration = float(play.get("duration") or 0)
        resolved_anchor = anchor
        if anchor == "auto":
            if duration >= long_clip_threshold and (play.get("video_offset_sec") or play.get("clip_end_sec")) is not None:
                resolved_anchor = "offset"
                effective_start_buffer = max(start_buffer, long_clip_start_buffer)
            else:
                resolved_anchor = "segment_start"
        if resolved_anchor == "segment_start":
            anchor_value = play.get("segment_start_sec") or play.get("clip_start_sec")
        elif resolved_anchor == "clip_start":
            anchor_value = play.get("clip_start_sec") or play.get("segment_start_sec")
        elif resolved_anchor == "offset":
            anchor_value = play.get("video_offset_sec") or play.get("clip_end_sec")
        else:
            raise GCError(f"Unknown segment anchor: {anchor}")
        anchor_value = anchor_value or play.get("video_offset_sec") or play.get("clip_end_sec")
        if anchor_value is None:
            continue
        start = float(anchor_value) + time_shift
        end = float(anchor_value) + time_shift
        window = adjusted_segment_window(
            float(start),
            float(end),
            start_buffer=effective_start_buffer,
            end_buffer=end_buffer,
            min_duration=min_duration,
        )
        if not window:
            continue
        segments.append(Segment(window[0], window[1], str(play.get("play_summary") or play.get("play_type") or "")))
    return segments


def select_player_plays(game: dict[str, Any], player_selector: str) -> list[dict[str, Any]]:
    plays = game.get("plays") or []
    players = game.get("players") or {}
    selector = player_selector.strip().lower()
    selected_ids: set[str] = set()
    for player_id, player in players.items():
        values = {
            player_id.lower(),
            str(player.get("number") or "").lower(),
            str(player.get("display") or "").lower(),
            f"{player.get('first_name', '')} {player.get('last_name', '')}".strip().lower(),
        }
        if selector in values or selector in str(player.get("display") or "").lower():
            selected_ids.add(player_id)
    if not selected_ids:
        selected_ids.add(player_selector)
    return [
        play
        for play in plays
        if selected_ids.intersection(set(play.get("mentioned_player_ids") or []))
    ]


def default_highlight_types() -> set[str]:
    return {
        "home_run",
        "triple",
        "double",
        "single",
        "strikeout",
        "double_play",
        "caught_stealing",
        "hit_by_pitch",
        "walk",
    }
