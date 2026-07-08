#!/usr/bin/env python3
"""Download and stitch a no-overlay full GameChanger game video."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gc_common import GCClient, GCError, cookie_header, parse_iso, run, safe_get, slugify


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", help="GC token. Prefer GC_TOKEN env var.")
    parser.add_argument("--team-id", default=None, help="Team UUID. If omitted, choose from your GameChanger teams.")
    parser.add_argument("--event-id", default=None, help="Schedule event/game UUID. If omitted, choose from completed games.")
    parser.add_argument("--output", help="Output MP4 path. Defaults to gc_render/full_game_<date>_<opponent>.mp4.")
    parser.add_argument("--parts-dir", help="Directory for downloaded source parts. Defaults to OUTPUT stem plus _parts.")
    parser.add_argument("--max-games", type=int, default=25, help="Maximum games to show in the interactive picker.")
    parser.add_argument("--include-incomplete", action="store_true", help="Include games that are not marked completed.")
    parser.add_argument("--include-non-games", action="store_true", help="Include practices and other scheduled events.")
    parser.add_argument("--reencode", action="store_true", help="Re-encode downloaded parts before concatenating.")
    parser.add_argument("--force", action="store_true", help="Redownload source parts even if they already exist.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve team, game, and playback assets without downloading video.")
    return parser.parse_args()


def team_payload(team: dict[str, Any]) -> dict[str, Any]:
    nested = team.get("team")
    return nested if isinstance(nested, dict) else team


def team_id(team: dict[str, Any]) -> str | None:
    payload = team_payload(team)
    return payload.get("id") or payload.get("team_id")


def team_name(team: dict[str, Any] | None) -> str:
    if not team:
        return "Team"
    payload = team_payload(team)
    return str(payload.get("name") or payload.get("display_name") or team_id(payload) or "Team")


def find_team(teams: list[dict[str, Any]], requested_team_id: str) -> dict[str, Any] | None:
    return next((team for team in teams if team_id(team) == requested_team_id), None)


def choose_option(options: list[Any], *, prompt: str, required_arg: str, labeler) -> Any:
    if not options:
        raise GCError(f"No options available for {prompt.lower()}.")
    if len(options) == 1:
        print(f"Using only {prompt.lower()}: {labeler(options[0])}")
        return options[0]
    if not sys.stdin.isatty():
        raise GCError(f"Pass {required_arg} when running non-interactively.")

    for index, option in enumerate(options, start=1):
        print(f"{index:2d}. {labeler(option)}")
    while True:
        raw = input(f"Select {prompt} [1-{len(options)}]: ").strip()
        try:
            selected = int(raw)
        except ValueError:
            print("Enter a number from the list.")
            continue
        if 1 <= selected <= len(options):
            return options[selected - 1]
        print("Enter a number from the list.")


def choose_team(client: GCClient, requested_team_id: str | None) -> dict[str, Any]:
    if requested_team_id:
        teams = safe_get(lambda: client.get_my_teams(), [], label="teams")
        return find_team(teams, requested_team_id) or {"id": requested_team_id, "name": requested_team_id}
    teams = client.get_my_teams()
    return choose_option(teams, prompt="team", required_arg="--team-id", labeler=team_name)


def event_payload(schedule_item: dict[str, Any]) -> dict[str, Any]:
    event = schedule_item.get("event")
    return event if isinstance(event, dict) else schedule_item


def schedule_event_id(schedule_item: dict[str, Any]) -> str | None:
    event = event_payload(schedule_item)
    return event.get("id") or event.get("event_id")


def event_datetime_text(schedule_item: dict[str, Any] | None, summary: dict[str, Any] | None = None) -> str:
    event = event_payload(schedule_item or {})
    value = (
        ((event.get("start") or {}).get("datetime"))
        or ((event.get("arrive") or {}).get("datetime"))
        or (summary or {}).get("last_scoring_update")
        or ""
    )
    if not value:
        return "unknown date"
    try:
        parsed = parse_iso(value)
    except ValueError:
        return str(value)
    timezone = event.get("timezone")
    if timezone:
        parsed = parsed.astimezone(ZoneInfo(str(timezone)))
    return parsed.strftime("%Y-%m-%d %I:%M %p").lstrip("0")


def event_sort_value(option: dict[str, Any]) -> str:
    schedule_item = option.get("schedule") or {}
    summary = option.get("summary") or {}
    event = event_payload(schedule_item)
    return (
        ((event.get("start") or {}).get("datetime"))
        or ((event.get("arrive") or {}).get("datetime"))
        or summary.get("last_scoring_update")
        or ""
    )


def opponent_name(option: dict[str, Any]) -> str:
    schedule_item = option.get("schedule") or {}
    pregame = schedule_item.get("pregame_data") or {}
    event = event_payload(schedule_item)
    return str(pregame.get("opponent_name") or event.get("title") or "game")


def game_status(option: dict[str, Any]) -> str:
    summary = option.get("summary") or {}
    event = event_payload(option.get("schedule") or {})
    return str(summary.get("game_status") or event.get("status") or "unknown")


def is_game(schedule_item: dict[str, Any] | None) -> bool:
    if not schedule_item:
        return True
    event_type = event_payload(schedule_item).get("event_type")
    return event_type in (None, "game")


def is_completed(option: dict[str, Any]) -> bool:
    return game_status(option) == "completed"


def build_game_options(
    schedule: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    *,
    include_incomplete: bool = False,
    include_non_games: bool = False,
) -> list[dict[str, Any]]:
    schedule_by_id = {
        event_id: item
        for item in schedule
        if (event_id := schedule_event_id(item))
    }
    summaries_by_id = {
        summary["event_id"]: summary
        for summary in summaries
        if summary.get("event_id")
    }
    event_ids = set(schedule_by_id) | set(summaries_by_id)

    options = []
    for event_id in event_ids:
        option = {
            "event_id": event_id,
            "schedule": schedule_by_id.get(event_id),
            "summary": summaries_by_id.get(event_id),
        }
        if not include_non_games and not is_game(option.get("schedule")):
            continue
        if not include_incomplete and not is_completed(option):
            continue
        options.append(option)

    return sorted(options, key=event_sort_value, reverse=True)


def score_text(option: dict[str, Any]) -> str:
    summary = option.get("summary") or {}
    own = summary.get("owning_team_score")
    opp = summary.get("opponent_team_score")
    if own is None or opp is None:
        return ""
    return f"score {own}-{opp}"


def game_label(option: dict[str, Any]) -> str:
    parts = [
        event_datetime_text(option.get("schedule"), option.get("summary")),
        opponent_name(option),
    ]
    score = score_text(option)
    if score:
        parts.append(score)
    status = game_status(option)
    if status != "completed":
        parts.append(status)
    parts.append(option["event_id"])
    return " - ".join(parts)


def choose_game(
    client: GCClient,
    selected_team_id: str,
    requested_event_id: str | None,
    *,
    max_games: int,
    include_incomplete: bool,
    include_non_games: bool,
) -> dict[str, Any]:
    schedule = safe_get(lambda: client.get_schedule(selected_team_id), [], label="schedule")
    summaries = safe_get(lambda: client.get_game_summaries(selected_team_id), [], label="game summaries")
    options = build_game_options(
        schedule,
        summaries,
        include_incomplete=include_incomplete,
        include_non_games=include_non_games,
    )
    if requested_event_id:
        return next(
            (option for option in options if option["event_id"] == requested_event_id),
            {"event_id": requested_event_id, "schedule": None, "summary": None},
        )
    return choose_option(options[:max_games], prompt="game", required_arg="--event-id", labeler=game_label)


def asset_identity(asset: dict[str, Any]) -> str | None:
    return asset.get("id") or asset.get("asset_id") or asset.get("video_stream_asset_id")


def playable_assets(event_assets: list[dict[str, Any]], playback_assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metadata_by_id = {
        asset_id: asset
        for asset in event_assets
        if (asset_id := asset_identity(asset))
    }
    order_by_id = {
        asset_id: index
        for index, asset in enumerate(event_assets)
        if (asset_id := asset_identity(asset))
    }

    merged_assets: list[tuple[int, dict[str, Any]]] = []
    for index, playback in enumerate(playback_assets):
        asset_id = asset_identity(playback)
        merged = dict(metadata_by_id.get(asset_id, {}))
        merged.update(playback)
        if not merged.get("url"):
            continue
        merged_assets.append((order_by_id.get(asset_id, index), merged))

    deduped = []
    seen_urls: set[str] = set()
    for _, asset in sorted(
        merged_assets,
        key=lambda item: (
            not bool(item[1].get("created_at")),
            item[1].get("created_at") or "",
            item[1].get("ended_at") or "",
            item[0],
        ),
    ):
        url = str(asset.get("url"))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(asset)
    return deduped


def ffmpeg_download_asset(asset: dict[str, Any], output: Path, *, reencode: bool, force: bool, label: str = "video") -> None:
    if output.exists() and output.stat().st_size > 0 and not force:
        print(f"Using existing {label}: {output}")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output.with_suffix(f".tmp-{os.getpid()}.mp4")
    if temp_output.exists():
        temp_output.unlink()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    cookie = cookie_header([asset])
    if cookie:
        cmd.extend(["-headers", f"Cookie: {cookie}\r\n"])
    cmd.extend(["-i", str(asset["url"])])
    if reencode:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac"])
    else:
        cmd.extend(["-c", "copy"])
    cmd.extend(["-movflags", "+faststart", str(temp_output)])
    try:
        run(cmd)
        temp_output.replace(output)
    finally:
        if temp_output.exists():
            temp_output.unlink()


def concat_file_line(path: Path) -> str:
    escaped = path.resolve().as_posix().replace("'", "\\'")
    return f"file '{escaped}'\n"


def concat_parts(parts: list[Path], output: Path) -> None:
    if not parts:
        raise GCError("No downloaded parts to concatenate.")
    if len(parts) < 2:
        raise GCError("Need at least two downloaded parts to concatenate.")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gc-full-game-concat-") as temp_name:
        concat_file = Path(temp_name) / "concat.txt"
        with open(concat_file, "w", encoding="utf-8") as f:
            for part in parts:
                f.write(concat_file_line(part))
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
                str(concat_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )


def asset_label(index: int, asset: dict[str, Any]) -> str:
    values = [f"part {index}"]
    if asset.get("id"):
        values.append(str(asset["id"]))
    if asset.get("created_at"):
        values.append(str(asset["created_at"]))
    if asset.get("duration") is not None:
        values.append(f"{asset['duration']}s")
    return " - ".join(values)


def default_output_path(team: dict[str, Any], game: dict[str, Any]) -> Path:
    date_slug = event_datetime_text(game.get("schedule"), game.get("summary")).split()[0]
    opponent_slug = slugify(opponent_name(game))
    team_slug = slugify(team_name(team))
    return Path("gc_render") / f"full_game_{date_slug}_{team_slug}_{opponent_slug}.mp4"


def write_single_asset(
    asset: dict[str, Any],
    output: Path,
    *,
    parts_dir: Path | None,
    reencode: bool,
    force: bool,
) -> None:
    if parts_dir is None:
        print(f"Single video asset; downloading directly to {output}")
        ffmpeg_download_asset(asset, output, reencode=reencode, force=force)
        return

    part = parts_dir / "part-001.mp4"
    print(f"Single video asset; downloading source part to {part}")
    ffmpeg_download_asset(asset, part, reencode=reencode, force=force, label="part")
    if part.resolve() != output.resolve():
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(part, output)


def main() -> None:
    args = parse_args()
    client = GCClient(args.token)
    team = choose_team(client, args.team_id)
    selected_team_id = team_id(team)
    if not selected_team_id:
        raise GCError("Selected team did not include a team id.")

    game = choose_game(
        client,
        selected_team_id,
        args.event_id,
        max_games=args.max_games,
        include_incomplete=args.include_incomplete,
        include_non_games=args.include_non_games,
    )
    selected_event_id = game["event_id"]
    output = Path(args.output) if args.output else default_output_path(team, game)
    explicit_parts_dir = Path(args.parts_dir) if args.parts_dir else None
    parts_dir = explicit_parts_dir or output.with_name(f"{output.stem}_parts")

    event_assets = safe_get(lambda: client.get_event_assets(selected_team_id, selected_event_id), [], label="event video assets")
    playback_assets = client.get_event_playback_assets(selected_team_id, selected_event_id)
    assets = playable_assets(event_assets, playback_assets)
    if not assets:
        raise GCError("No playable video assets found for selected game.")

    print(f"Team: {team_name(team)}")
    print(f"Game: {game_label(game)}")
    print(f"Video assets: {len(assets)}")
    if args.dry_run:
        for index, asset in enumerate(assets, start=1):
            print(asset_label(index, asset))
        print("Dry run only; no video downloaded.")
        return

    if len(assets) == 1:
        write_single_asset(
            assets[0],
            output,
            parts_dir=explicit_parts_dir,
            reencode=args.reencode,
            force=args.force,
        )
        print(f"Wrote {output}")
        if explicit_parts_dir:
            print(f"Kept downloaded part in {explicit_parts_dir}")
        return

    parts = []
    for index, asset in enumerate(assets, start=1):
        part = parts_dir / f"part-{index:03d}.mp4"
        print(f"Downloading part {index}/{len(assets)} to {part}")
        ffmpeg_download_asset(asset, part, reencode=args.reencode, force=args.force, label="part")
        parts.append(part)

    concat_parts(parts, output)
    print(f"Wrote {output}")
    print(f"Kept downloaded parts in {parts_dir}")


if __name__ == "__main__":
    main()
