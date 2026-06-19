#!/usr/bin/env python3
"""Upload generated reels to YouTube with OAuth."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gc_common import format_timestamp, load_json

SCOPES = ["https://www.googleapis.com/auth/youtube"]
COLAB_DESCRIPTION_EXCLUDED_TYPES = {"caught_stealing", "stole_base", "wild_pitch"}
AGE_SUFFIX_RE = re.compile(r"\b(?:\d{1,2}U|U\d{1,2})\b", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="*", help="Video files to upload.")
    parser.add_argument("--render-dir", help="Directory containing standard render outputs to upload.")
    parser.add_argument(
        "--include-standard-renders",
        action="store_true",
        help="Include full_game_scorebug.mp4, condensed_game.mp4, highlight_reel.mp4, and player_reels/*.mp4 from --render-dir.",
    )
    parser.add_argument("--game-json", help="Fetched game.json used to generate Colab-style full-game descriptions.")
    parser.add_argument("--client-secrets", default="client_secret.json")
    parser.add_argument("--token-file", default="youtube_token.json")
    parser.add_argument("--title-prefix", default="")
    parser.add_argument("--description", help="Description to use for every uploaded video.")
    parser.add_argument("--description-file", help="File containing a description to use for every uploaded video.")
    parser.add_argument("--playlist-title", help="Playlist title. Defaults to '<team> vs <opponent> — <date>' from --game-json.")
    parser.add_argument("--playlist-team-name", default="Tigers", help="Team name to use when generating a playlist title.")
    parser.add_argument("--no-playlist", action="store_true", help="Upload videos without creating/updating a playlist.")
    parser.add_argument("--tags", default="GameChanger,baseball,9U")
    parser.add_argument("--category-id", default="17", help="17 is Sports.")
    parser.add_argument("--privacy-status", default="unlisted", choices=["private", "unlisted", "public"])
    return parser.parse_args()


def youtube_service(client_secrets: str, token_file: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = Path(token_file)
    if token_path.exists():
        token_info = json.loads(token_path.read_text(encoding="utf-8"))
        token_scopes = token_info.get("scopes")
        granted_scopes = set(token_scopes.split() if isinstance(token_scopes, str) else token_scopes or [])
        if set(SCOPES).issubset(granted_scopes):
            creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def standard_video_paths(render_dir: Path) -> list[Path]:
    candidates = [
        render_dir / "full_game_scorebug.mp4",
        render_dir / "condensed_game.mp4",
        render_dir / "highlight_reel.mp4",
    ]
    candidates.extend(sorted((render_dir / "player_reels").glob("*.mp4")))
    return [path for path in candidates if path.exists()]


def resolve_video_paths(args: argparse.Namespace) -> list[Path]:
    paths = [Path(video) for video in args.videos]
    if args.render_dir and (args.include_standard_renders or not paths):
        paths.extend(standard_video_paths(Path(args.render_dir)))
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser()
        key = resolved.resolve() if resolved.exists() else resolved
        if key in seen:
            continue
        seen.add(key)
        deduped.append(resolved)
    return deduped


def play_seconds(play: dict[str, Any], *, first: bool) -> float:
    if first:
        return 0.0
    for key in ("video_offset_sec", "clip_end_sec", "segment_start_sec", "clip_start_sec"):
        value = play.get(key)
        if value is not None:
            return float(value)
    value = str(play.get("video_timestamp") or "")
    parts = [int(part) for part in value.split(":") if part.isdigit()]
    if len(parts) == 3:
        return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    if len(parts) == 2:
        return float(parts[0] * 60 + parts[1])
    if len(parts) == 1:
        return float(parts[0])
    return float(play.get("index") or 0)


def format_inning_header(play: dict[str, Any]) -> str:
    half_text = "Top" if play.get("inning_half") == "top" else "Bot"
    return f"# {half_text} {play.get('inning')}"


def colab_youtube_description(game: dict[str, Any]) -> str:
    lines: list[str] = []
    last_group: tuple[Any, Any] | None = None
    selected = [
        play
        for play in game.get("plays") or []
        if play.get("play_type") not in COLAB_DESCRIPTION_EXCLUDED_TYPES
    ]
    for index, play in enumerate(selected):
        group = (play.get("inning"), play.get("inning_half"))
        if group != last_group:
            if lines:
                lines.append("")
            lines.append(format_inning_header(play))
            last_group = group
        lines.append(f"{format_timestamp(play_seconds(play, first=index == 0))}: {play.get('play_summary') or ''}")
    return "\n".join(lines).strip()


def clean_team_name(value: str) -> str:
    value = AGE_SUFFIX_RE.sub("", value)
    return " ".join(value.split()).strip()


def opponent_name(game: dict[str, Any]) -> str:
    name = (
        ((game.get("public_details") or {}).get("opponent_team") or {}).get("name")
        or ((game.get("schedule_event") or {}).get("pregame_data") or {}).get("opponent_name")
        or "Opponent"
    )
    return clean_team_name(str(name)) or "Opponent"


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def game_start_datetime(game: dict[str, Any]) -> dt.datetime:
    public_details = game.get("public_details") or {}
    schedule_event = (game.get("schedule_event") or {}).get("event") or {}
    value = (
        public_details.get("start_ts")
        or ((schedule_event.get("start") or {}).get("datetime"))
        or ((schedule_event.get("arrive") or {}).get("datetime"))
        or public_details.get("end_ts")
    )
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError("Could not determine game date for playlist title.")
    timezone = public_details.get("timezone")
    if timezone:
        parsed = parsed.astimezone(ZoneInfo(str(timezone)))
    return parsed


def format_playlist_date(value: dt.datetime) -> str:
    return f"{value.strftime('%B')} {value.day} '{value.strftime('%y')}"


def default_playlist_title(game: dict[str, Any], team_name: str) -> str:
    home_away = (
        (game.get("public_details") or {}).get("home_away")
        or (game.get("game_summary") or {}).get("home_away")
        or "home"
    )
    separator = "@" if str(home_away).lower() == "away" else "vs"
    return f"{clean_team_name(team_name)} {separator} {opponent_name(game)} — {format_playlist_date(game_start_datetime(game))}"


def is_full_game_video(video_path: Path) -> bool:
    return video_path.stem.startswith("full_game")


def common_description(args: argparse.Namespace) -> str | None:
    if args.description_file:
        return Path(args.description_file).read_text(encoding="utf-8")
    return args.description


def description_for_video(video_path: Path, args: argparse.Namespace, game: dict[str, Any] | None) -> str:
    description = common_description(args)
    if description is not None:
        return description
    if game and is_full_game_video(video_path):
        return colab_youtube_description(game)
    return ""


def upload_one(youtube, video_path: Path, args: argparse.Namespace, description: str) -> str:
    from googleapiclient.http import MediaFileUpload

    mime_type = mimetypes.guess_type(video_path)[0] or "video/mp4"
    title_base = video_path.stem.replace("-", " ").replace("_", " ").title()
    body = {
        "snippet": {
            "title": f"{args.title_prefix}{title_base}",
            "description": description,
            "tags": [tag.strip() for tag in args.tags.split(",") if tag.strip()],
            "categoryId": args.category_id,
        },
        "status": {"privacyStatus": args.privacy_status},
    }
    media = MediaFileUpload(str(video_path), mimetype=mime_type, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"{video_path.name}: {int(status.progress() * 100)}%")
    return response["id"]


def iter_youtube_pages(request, list_next):
    while request is not None:
        response = request.execute()
        yield response
        request = list_next(request, response)


def find_playlist(youtube, title: str) -> str | None:
    request = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
    for response in iter_youtube_pages(request, youtube.playlists().list_next):
        for item in response.get("items", []):
            if (item.get("snippet") or {}).get("title") == title:
                return item.get("id")
    return None


def create_playlist(youtube, title: str) -> str:
    response = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {"title": title},
            "status": {"privacyStatus": "unlisted"},
        },
    ).execute()
    return response["id"]


def get_or_create_playlist(youtube, title: str) -> str:
    playlist_id = find_playlist(youtube, title)
    if playlist_id:
        print(f"Using playlist: {title}")
        return playlist_id
    playlist_id = create_playlist(youtube, title)
    print(f"Created playlist: {title}")
    return playlist_id


def playlist_video_ids(youtube, playlist_id: str) -> set[str]:
    video_ids: set[str] = set()
    request = youtube.playlistItems().list(part="contentDetails", playlistId=playlist_id, maxResults=50)
    for response in iter_youtube_pages(request, youtube.playlistItems().list_next):
        for item in response.get("items", []):
            video_id = (item.get("contentDetails") or {}).get("videoId")
            if video_id:
                video_ids.add(video_id)
    return video_ids


def add_video_to_playlist(youtube, playlist_id: str, video_id: str, existing_video_ids: set[str]) -> None:
    if video_id in existing_video_ids:
        return
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                },
            }
        },
    ).execute()
    existing_video_ids.add(video_id)


def resolve_playlist_title(args: argparse.Namespace, game: dict[str, Any] | None) -> str:
    if args.playlist_title:
        return args.playlist_title
    if game:
        return default_playlist_title(game, args.playlist_team_name)
    raise SystemExit("Pass --game-json or --playlist-title, or use --no-playlist.")


def main() -> None:
    args = parse_args()
    videos = resolve_video_paths(args)
    if not videos:
        raise SystemExit("No videos selected. Pass video files, or use --render-dir for standard render outputs.")
    missing = [path for path in videos if not path.exists()]
    if missing:
        raise SystemExit(f"Missing video file: {missing[0]}")
    game = load_json(args.game_json) if args.game_json else None
    playlist_id = None
    playlist_title = None
    existing_playlist_video_ids: set[str] = set()
    if not args.no_playlist:
        playlist_title = resolve_playlist_title(args, game)
    youtube = youtube_service(args.client_secrets, args.token_file)
    if playlist_title:
        playlist_id = get_or_create_playlist(youtube, playlist_title)
        existing_playlist_video_ids = playlist_video_ids(youtube, playlist_id)
    for path in videos:
        video_id = upload_one(youtube, path, args, description_for_video(path, args, game))
        print(f"{path}: https://youtu.be/{video_id}")
        if playlist_id:
            add_video_to_playlist(youtube, playlist_id, video_id, existing_playlist_video_ids)
            print(f"Added {path.name} to playlist.")


if __name__ == "__main__":
    main()
