#!/usr/bin/env python3
"""Upload generated reels to YouTube with OAuth."""

from __future__ import annotations

import argparse
import mimetypes
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="+", help="Video files to upload.")
    parser.add_argument("--client-secrets", default="client_secret.json")
    parser.add_argument("--token-file", default="youtube_token.json")
    parser.add_argument("--title-prefix", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--tags", default="GameChanger,baseball,9U")
    parser.add_argument("--category-id", default="17", help="17 is Sports.")
    parser.add_argument("--privacy-status", default="unlisted", choices=["private", "unlisted", "public"])
    return parser.parse_args()


def youtube_service(client_secrets: str, token_file: str):
    creds = None
    token_path = Path(token_file)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def upload_one(youtube, video_path: Path, args: argparse.Namespace) -> str:
    mime_type = mimetypes.guess_type(video_path)[0] or "video/mp4"
    title_base = video_path.stem.replace("-", " ").replace("_", " ").title()
    body = {
        "snippet": {
            "title": f"{args.title_prefix}{title_base}",
            "description": args.description,
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


def main() -> None:
    args = parse_args()
    youtube = youtube_service(args.client_secrets, args.token_file)
    for video in args.videos:
        path = Path(video)
        video_id = upload_one(youtube, path, args)
        print(f"{path}: https://youtu.be/{video_id}")


if __name__ == "__main__":
    main()
