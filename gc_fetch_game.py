#!/usr/bin/env python3
"""Fetch and index a GameChanger game more robustly than the original Colab."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from gc_common import (
    GCClient,
    GCError,
    build_player_map,
    choose_event_id,
    clip_window,
    dump_json,
    format_timestamp,
    replace_player_placeholders,
    safe_get,
    select_video_asset,
    stream_events_by_pbp_id,
    write_play_outputs,
    youtube_time_url,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", help="GC token. Prefer GC_TOKEN env var.")
    parser.add_argument("--team-id", default=None, help="Team UUID. Defaults to GC_TEAM_ID.")
    parser.add_argument("--public-team-id", default=None, help="Public team id. Defaults to GC_PUBLIC_TEAM_ID.")
    parser.add_argument("--event-id", default=None, help="Schedule event/game UUID. Defaults to latest completed game.")
    parser.add_argument("--youtube-url", default=None, help="Optional YouTube video URL for timestamp links.")
    parser.add_argument("--pre-roll", type=float, default=3.0)
    parser.add_argument("--post-roll", type=float, default=4.0)
    parser.add_argument("--out-dir", default="gc_output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    import os

    team_id = args.team_id or os.environ.get("GC_TEAM_ID")
    public_team_id = args.public_team_id or os.environ.get("GC_PUBLIC_TEAM_ID")
    if not team_id:
        raise GCError("Pass --team-id or set GC_TEAM_ID.")

    client = GCClient(args.token)
    event_id = choose_event_id(client, team_id, args.event_id)
    out_dir = Path(args.out_dir) / event_id
    out_dir.mkdir(parents=True, exist_ok=True)

    schedule = safe_get(lambda: client.get_schedule(team_id), [], label="schedule")
    game_summaries = safe_get(lambda: client.get_game_summaries(team_id), [], label="game summaries")
    public_details = safe_get(lambda: client.get_public_game_details(event_id), {}, label="public game details")
    clips_response = client.search_clips(team_id, event_id)
    clips = clips_response.get("hits") or []
    clips_total_count = clips_response.get("total_count", len(clips))
    if clips_total_count is not None and len(clips) < int(clips_total_count):
        raise GCError(f"Fetched {len(clips)} clips but GameChanger reported {clips_total_count}.")

    game_stream_id = None
    matching_summary = next((s for s in game_summaries if s.get("event_id") == event_id), None)
    if matching_summary:
        game_stream_id = (matching_summary.get("game_stream") or {}).get("id")
    game_stream_id = game_stream_id or safe_get(lambda: client.get_best_game_stream_id(event_id), None, label="best game stream id")

    stream_events = safe_get(lambda: client.get_game_stream_events(game_stream_id), [], label="game stream events") if game_stream_id else []
    events_by_pbp_id = stream_events_by_pbp_id(stream_events)

    team_players = safe_get(lambda: client.get_players(team_id), [], label="team players")
    public_players = safe_get(lambda: client.get_public_players(public_team_id), [], label="public players") if public_team_id else []
    opponent_id = (matching_summary or {}).get("game_stream", {}).get("opponent_id")
    opponent_players = safe_get(lambda: client.get_opponent_roster(team_id, opponent_id), [], label="opponent roster") if opponent_id else []
    players = build_player_map(team_players, public_players, opponent_players)
    team_player_ids = sorted(
        {
            player.get("id") or player.get("player_id")
            for roster in (team_players, public_players)
            for player in (roster or [])
            if player.get("id") or player.get("player_id")
        }
    )
    opponent_player_ids = sorted(
        {
            player.get("id") or player.get("player_id")
            for player in (opponent_players or [])
            if player.get("id") or player.get("player_id")
        }
    )

    team_assets = safe_get(lambda: client.get_team_assets(team_id), [], label="team video assets")
    event_assets = safe_get(lambda: client.get_event_assets(team_id, event_id), [], label="event video assets")
    playback_assets = safe_get(lambda: client.get_event_playback_assets(team_id, event_id), [], label="event playback assets")
    video_asset = select_video_asset(team_assets, event_assets, event_id)

    plays = []
    for index, clip in enumerate(clips):
        summary, mentioned_ids = replace_player_placeholders(clip.get("play_summary") or "", players)
        play_metadata = clip.get("play_metadata") or {}
        sport_metadata = clip.get("sport_metadata") or {}
        pbp_id = play_metadata.get("pbp_id")
        raw_event = events_by_pbp_id.get(str(pbp_id).lower()) if pbp_id else None
        timing = clip_window(clip, video_asset, args.pre_roll, args.post_roll) if video_asset else {}
        segment_start = timing.get("segment_start_sec")
        plays.append(
            {
                "index": index,
                "event_id": event_id,
                "clip_metadata_id": clip.get("clip_metadata_id"),
                "pbp_id": pbp_id,
                "timestamp": clip.get("timestamp"),
                "duration": clip.get("duration"),
                "play_type": play_metadata.get("play_type"),
                "exceptional_play": clip.get("exceptional_play"),
                "cv_generated": clip.get("cv_generated"),
                "hidden": clip.get("hidden"),
                "related_stream_id": (clip.get("related_ids") or {}).get("stream_id"),
                "inning": sport_metadata.get("inning"),
                "inning_half": sport_metadata.get("inning_half"),
                "play_summary": summary,
                "mentioned_player_ids": mentioned_ids,
                "raw_event_code": raw_event.get("code") if raw_event else None,
                "raw_event_created_at_ms": raw_event.get("createdAt") if raw_event else None,
                "thumbnail_url": clip.get("thumbnail_url"),
                "video_offset_sec": timing.get("video_offset_sec"),
                "clip_start_sec": timing.get("clip_start_sec"),
                "clip_end_sec": timing.get("clip_end_sec"),
                "segment_start_sec": timing.get("segment_start_sec"),
                "segment_end_sec": timing.get("segment_end_sec"),
                "video_timestamp": format_timestamp(segment_start or 0),
                "youtube_url": youtube_time_url(args.youtube_url, segment_start or 0),
            }
        )

    public_recap_url = f"https://web.gc.com/teams/{public_team_id}/schedule/{event_id}/recap" if public_team_id else None
    game = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "team_id": team_id,
        "public_team_id": public_team_id,
        "event_id": event_id,
        "public_recap_url": public_recap_url,
        "youtube_url": args.youtube_url,
        "game_stream_id": game_stream_id,
        "clips_total_count": clips_total_count,
        "schedule_event": next((e for e in schedule if (e.get("event") or {}).get("id") == event_id), None),
        "game_summary": matching_summary,
        "public_details": public_details,
        "video_asset": video_asset,
        "playback_assets": playback_assets,
        "players": players,
        "team_player_ids": team_player_ids,
        "opponent_player_ids": opponent_player_ids,
        "plays": plays,
        "raw_stream_events_count": len(stream_events),
    }

    dump_json(out_dir / "game.json", game)
    dump_json(out_dir / "clips_raw.json", clips_response)
    dump_json(out_dir / "stream_events_raw.json", stream_events)
    write_play_outputs(out_dir, plays)

    print(f"Wrote {out_dir / 'game.json'}")
    print(f"Wrote {out_dir / 'plays.csv'}")
    print(f"Wrote {out_dir / 'plays.md'}")
    print(f"Clips: {len(plays)}")
    if video_asset:
        print(f"Video asset: {video_asset.get('id')} duration={video_asset.get('duration')}s")
    if public_recap_url:
        print(f"Recap: {public_recap_url}")


if __name__ == "__main__":
    main()
