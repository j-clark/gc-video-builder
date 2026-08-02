import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gc_common import GCClient, GCError, plays_to_segment_pairs, select_video_asset, write_play_outputs
from gc_download_full_game import (
    build_game_options,
    choose_team,
    concat_parts,
    playable_assets,
    sort_teams_for_picker,
    write_single_asset,
)
from gc_make_condensed_game import after_bases, overlay_timeline
from gc_make_full_game import full_game_overlay_timeline
import gc_upload_youtube
from gc_make_player_reels import all_player_selectors
from gc_upload_youtube import (
    add_video_to_playlist,
    default_playlist_title,
    get_or_create_playlist,
    colab_youtube_description,
    standard_video_paths,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.offsets = []

    def post(self, _url, *, headers, json, timeout):
        del headers, timeout
        offset = json["offset"]
        self.offsets.append(offset)
        if offset == 0:
            return FakeResponse({"hits": [{"id": index} for index in range(500)], "total_count": 501})
        return FakeResponse({"hits": [{"id": 500}], "total_count": 501})


class FakeGetSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, _url, *, headers=None, timeout=45):
        del headers, timeout
        return FakeResponse(self.payload)


class FakeYoutubeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakePlaylistsResource:
    def __init__(self, items):
        self.items = items
        self.insert_body = None

    def list(self, **_kwargs):
        return FakeYoutubeRequest({"items": self.items})

    def list_next(self, _request, _response):
        return None

    def insert(self, *, part, body):
        self.insert_body = {"part": part, "body": body}
        return FakeYoutubeRequest({"id": "created-playlist"})


class FakePlaylistItemsResource:
    def __init__(self, items=None):
        self.items = items or []
        self.insert_bodies = []

    def list(self, **_kwargs):
        return FakeYoutubeRequest({"items": self.items})

    def list_next(self, _request, _response):
        return None

    def insert(self, *, part, body):
        self.insert_bodies.append({"part": part, "body": body})
        return FakeYoutubeRequest({"id": "playlist-item"})


class FakeYoutube:
    def __init__(self, playlist_items):
        self.playlists_resource = FakePlaylistsResource(playlist_items)
        self.playlist_items_resource = FakePlaylistItemsResource()

    def playlists(self):
        return self.playlists_resource

    def playlistItems(self):
        return self.playlist_items_resource


class ReviewFixTests(unittest.TestCase):
    def test_search_clips_paginates_until_total_count(self):
        client = GCClient.__new__(GCClient)
        client.session = FakeSession()

        result = client.search_clips("team-id", "event-id")

        self.assertEqual(501, len(result["hits"]))
        self.assertEqual(501, result["total_count"])
        self.assertEqual([0, 500], client.session.offsets)

    def test_get_my_teams_accepts_wrapped_response(self):
        client = GCClient.__new__(GCClient)
        client.session = FakeGetSession({"teams": [{"id": "team-id", "name": "Tigers"}]})

        self.assertEqual([{"id": "team-id", "name": "Tigers"}], client.get_my_teams())

    def test_select_video_asset_prefers_newer_non_null_values(self):
        old_asset = {
            "id": "old",
            "schedule_event_id": "event-id",
            "created_at": "2024-01-01T00:00:00Z",
            "duration": 100,
            "playback_url": "old-url",
        }
        new_asset = {
            "id": "new",
            "schedule_event_id": "event-id",
            "created_at": "2024-01-02T00:00:00Z",
            "duration": 200,
            "playback_url": "new-url",
        }

        selected = select_video_asset([old_asset, new_asset], [], "event-id")

        self.assertEqual("new", selected["id"])
        self.assertEqual("new-url", selected["playback_url"])

    def test_write_play_outputs_overwrites_stale_csv_for_empty_rows(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            out_dir = Path(tmp_name)
            (out_dir / "plays.csv").write_text("stale,data\n", encoding="utf-8")

            write_play_outputs(out_dir, [])

            self.assertEqual("", (out_dir / "plays.csv").read_text(encoding="utf-8"))
            self.assertIn("# Plays", (out_dir / "plays.md").read_text(encoding="utf-8"))

    def test_plays_to_segment_pairs_skips_untimed_plays_without_losing_play_mapping(self):
        timed = {"index": 1, "video_offset_sec": 20, "duration": 20, "play_summary": "Timed play"}
        untimed = {"index": 2, "play_summary": "Missing timing"}

        pairs = plays_to_segment_pairs([untimed, timed], start_buffer=4, end_buffer=2, min_duration=12)

        self.assertEqual(1, len(pairs))
        self.assertIs(timed, pairs[0][0])
        self.assertEqual(20, pairs[0][1].end - pairs[0][1].start)

    def test_plays_to_segment_pairs_clamps_to_duration_limit(self):
        play = {"index": 1, "video_offset_sec": 95, "duration": 20, "play_summary": "Late play"}

        pairs = plays_to_segment_pairs(
            [play],
            start_buffer=4,
            end_buffer=6,
            min_duration=12,
            long_clip_start_buffer=18,
            duration_limit=100,
        )

        self.assertEqual(77, pairs[0][1].start)
        self.assertEqual(100, pairs[0][1].end)

    def test_overlay_timeline_rejects_misaligned_inputs(self):
        play = {"index": 1}

        with self.assertRaises(ValueError):
            overlay_timeline([("selected", play)], [], max_merge_gap=1)

    def test_full_game_overlay_timeline_truncates_at_next_overlay(self):
        first = {"index": 1}
        second = {"index": 2}

        timeline = full_game_overlay_timeline(
            [("first", first), ("second", second)],
            [
                type("Segment", (), {"start": 10, "end": 25})(),
                type("Segment", (), {"start": 20, "end": 35})(),
            ],
            duration=None,
        )

        self.assertEqual([(10, 20, first), (20, 35, second)], timeline)

    def test_after_bases_removes_non_third_scoring_runner(self):
        play = {"play_type": "single", "play_summary": "Alex scores. Batter singles."}

        self.assertEqual({1}, after_bases(play, {2}))

    def test_after_bases_keeps_explicitly_advanced_runner_after_score(self):
        play = {"play_type": "single", "play_summary": "Alex scores. Ben advances to 2nd. Batter singles."}

        self.assertEqual({1, 2}, after_bases(play, {1, 2}))

    def test_all_player_selectors_excludes_opponent_players(self):
        game = {
            "players": {"team-player": {}, "opponent-player": {}},
            "team_player_ids": ["team-player"],
            "opponent_player_ids": ["opponent-player"],
            "plays": [
                {"mentioned_player_ids": ["team-player"]},
                {"mentioned_player_ids": ["opponent-player"]},
            ],
        }

        self.assertEqual(["team-player"], all_player_selectors(game))

    def test_colab_youtube_description_matches_notebook_shape(self):
        game = {
            "plays": [
                {
                    "inning": 1,
                    "inning_half": "top",
                    "play_type": "single",
                    "video_offset_sec": 13,
                    "play_summary": "Alex singles.",
                },
                {
                    "inning": 1,
                    "inning_half": "top",
                    "play_type": "stole_base",
                    "video_offset_sec": 20,
                    "play_summary": "Alex steals 2nd.",
                },
                {
                    "inning": 1,
                    "inning_half": "bottom",
                    "play_type": "strikeout",
                    "video_offset_sec": 75,
                    "play_summary": "Sam strikes out swinging.",
                },
            ]
        }

        self.assertEqual(
            "# Top 1\n0:00: Alex singles.\n\n# Bot 1\n1:15: Sam strikes out swinging.",
            colab_youtube_description(game),
        )

    def test_standard_video_paths_includes_full_game_first(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            render_dir = Path(tmp_name)
            (render_dir / "player_reels").mkdir()
            for relative in [
                "full_game.mp4",
                "highlight_reel.mp4",
                "condensed_game.mp4",
                "full_game_scorebug.mp4",
                "player_reels/zach.mp4",
                "player_reels/andre.mp4",
            ]:
                (render_dir / relative).write_text("", encoding="utf-8")

            self.assertEqual(
                [
                    render_dir / "full_game.mp4",
                    render_dir / "full_game_scorebug.mp4",
                    render_dir / "condensed_game.mp4",
                    render_dir / "highlight_reel.mp4",
                    render_dir / "player_reels" / "andre.mp4",
                    render_dir / "player_reels" / "zach.mp4",
                ],
                standard_video_paths(render_dir),
            )

    def test_build_game_options_filters_completed_games_and_sorts_newest_first(self):
        schedule = [
            {
                "event": {
                    "id": "old-game",
                    "event_type": "game",
                    "status": "completed",
                    "start": {"datetime": "2026-06-01T12:00:00Z"},
                },
                "pregame_data": {"opponent_name": "Old Opponent"},
            },
            {
                "event": {
                    "id": "practice",
                    "event_type": "practice",
                    "status": "completed",
                    "start": {"datetime": "2026-07-01T12:00:00Z"},
                },
            },
            {
                "event": {
                    "id": "future-game",
                    "event_type": "game",
                    "status": "scheduled",
                    "start": {"datetime": "2026-08-01T12:00:00Z"},
                },
            },
        ]
        summaries = [
            {"event_id": "old-game", "game_status": "completed"},
            {"event_id": "new-game", "game_status": "completed", "last_scoring_update": "2026-06-15T12:00:00Z"},
            {"event_id": "future-game", "game_status": "scheduled"},
        ]

        options = build_game_options(schedule, summaries)

        self.assertEqual(["new-game", "old-game"], [option["event_id"] for option in options])

    def test_sort_teams_for_picker_puts_current_season_first(self):
        teams = [
            {"id": "fall", "name": "Fall Team", "season_name": "fall", "season_year": 2026},
            {"id": "spring", "name": "Spring Team", "season_name": "spring", "season_year": 2026},
            {"id": "summer", "name": "Summer Team", "season_name": "summer", "season_year": 2026},
            {"id": "winter", "name": "Winter Team", "season_name": "winter", "season_year": 2025},
        ]

        sorted_teams = sort_teams_for_picker(teams, today=dt.date(2026, 8, 2))

        self.assertEqual(["summer", "fall", "spring", "winter"], [team["id"] for team in sorted_teams])

    def test_sort_teams_for_picker_supports_nested_team_payloads(self):
        teams = [
            {"team": {"id": "old", "name": "Old Team", "team_season": {"season": "fall", "year": 2025}}},
            {"team": {"id": "active", "name": "Active Team", "team_season": {"season": "summer", "year": 2026}}},
        ]

        sorted_teams = sort_teams_for_picker(teams, today=dt.date(2026, 8, 2))

        self.assertEqual(["active", "old"], [team["team"]["id"] for team in sorted_teams])

    def test_choose_team_sorts_before_showing_picker(self):
        client = mock.Mock()
        client.get_my_teams.return_value = [
            {"id": "old", "name": "Old Team", "season_name": "fall", "season_year": 2025},
            {"id": "active", "name": "Active Team", "season_name": "summer", "season_year": 2026},
        ]

        with mock.patch("gc_download_full_game.current_season", return_value=("summer", 2026)):
            with mock.patch("gc_download_full_game.choose_option", side_effect=lambda options, **_kwargs: options[0]):
                selected = choose_team(client, None)

        self.assertEqual("active", selected["id"])

    def test_playable_assets_merges_metadata_sorts_and_dedupes_urls(self):
        event_assets = [
            {"id": "late", "created_at": "2026-06-01T12:30:00Z", "duration": 300},
            {"id": "early", "created_at": "2026-06-01T12:00:00Z", "duration": 300},
        ]
        playback_assets = [
            {"id": "late", "url": "https://example.test/late.m3u8", "cookies": {"a": "1"}},
            {"id": "early", "url": "https://example.test/early.m3u8", "cookies": {"b": "2"}},
            {"id": "duplicate", "url": "https://example.test/early.m3u8"},
            {"id": "missing-url"},
        ]

        assets = playable_assets(event_assets, playback_assets)

        self.assertEqual(["early", "late"], [asset["id"] for asset in assets])
        self.assertEqual([300, 300], [asset["duration"] for asset in assets])
        self.assertEqual({"b": "2"}, assets[0]["cookies"])

    def test_write_single_asset_downloads_directly_without_parts_dir(self):
        asset = {"url": "https://example.test/game.m3u8"}
        with tempfile.TemporaryDirectory() as tmp_name:
            output = Path(tmp_name) / "full_game.mp4"
            with mock.patch("gc_download_full_game.ffmpeg_download_asset") as download, mock.patch("gc_download_full_game.shutil.copy2") as copy2:
                write_single_asset(asset, output, parts_dir=None, reencode=False, force=False)

        download.assert_called_once_with(asset, output, reencode=False, force=False)
        copy2.assert_not_called()

    def test_write_single_asset_keeps_explicit_part_without_stitching(self):
        asset = {"url": "https://example.test/game.m3u8"}
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            output = root / "full_game.mp4"
            parts_dir = root / "parts"
            with mock.patch("gc_download_full_game.ffmpeg_download_asset") as download, mock.patch("gc_download_full_game.shutil.copy2") as copy2:
                write_single_asset(asset, output, parts_dir=parts_dir, reencode=True, force=True)

        download.assert_called_once_with(asset, parts_dir / "part-001.mp4", reencode=True, force=True, label="part")
        copy2.assert_called_once_with(parts_dir / "part-001.mp4", output)

    def test_concat_parts_rejects_single_part(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            part = Path(tmp_name) / "part-001.mp4"
            output = Path(tmp_name) / "full_game.mp4"

            with self.assertRaises(GCError):
                concat_parts([part], output)

    def test_default_playlist_title_uses_game_metadata(self):
        game = {
            "public_details": {
                "home_away": "home",
                "start_ts": "2026-06-19T14:00:00.000Z",
                "timezone": "America/New_York",
                "opponent_team": {"name": "Cortlandt Nationals 9U"},
            }
        }

        self.assertEqual(
            "Tigers vs Cortlandt Nationals — June 19 '26",
            default_playlist_title(game, "Tigers"),
        )

    def test_default_playlist_title_uses_at_for_away_games(self):
        game = {
            "public_details": {
                "home_away": "away",
                "start_ts": "2026-06-19T14:00:00.000Z",
                "timezone": "America/New_York",
                "opponent_team": {"name": "Cortlandt Nationals 9U"},
            }
        }

        self.assertEqual(
            "Tigers @ Cortlandt Nationals — June 19 '26",
            default_playlist_title(game, "Tigers"),
        )

    def test_get_or_create_playlist_reuses_existing_playlist(self):
        youtube = FakeYoutube(
            [
                {
                    "id": "existing-playlist",
                    "snippet": {"title": "Tigers vs Cortlandt Nationals — June 19 '26"},
                }
            ]
        )

        playlist_id = get_or_create_playlist(youtube, "Tigers vs Cortlandt Nationals — June 19 '26")

        self.assertEqual("existing-playlist", playlist_id)
        self.assertIsNone(youtube.playlists_resource.insert_body)

    def test_get_or_create_playlist_creates_unlisted_playlist(self):
        youtube = FakeYoutube([])

        playlist_id = get_or_create_playlist(youtube, "Tigers vs Cortlandt Nationals — June 19 '26")

        self.assertEqual("created-playlist", playlist_id)
        self.assertEqual(
            {
                "part": "snippet,status",
                "body": {
                    "snippet": {"title": "Tigers vs Cortlandt Nationals — June 19 '26"},
                    "status": {"privacyStatus": "unlisted"},
                },
            },
            youtube.playlists_resource.insert_body,
        )

    def test_add_video_to_playlist_skips_existing_video_id(self):
        youtube = FakeYoutube([])
        existing = {"video-1"}

        add_video_to_playlist(youtube, "playlist-1", "video-1", existing)

        self.assertEqual([], youtube.playlist_items_resource.insert_bodies)

    def test_add_video_to_playlist_inserts_new_video_id(self):
        youtube = FakeYoutube([])
        existing = set()

        add_video_to_playlist(youtube, "playlist-1", "video-1", existing)

        self.assertEqual({"video-1"}, existing)
        self.assertEqual(
            [
                {
                    "part": "snippet",
                    "body": {
                        "snippet": {
                            "playlistId": "playlist-1",
                            "resourceId": {
                                "kind": "youtube#video",
                                "videoId": "video-1",
                            },
                        }
                    },
                }
            ],
            youtube.playlist_items_resource.insert_bodies,
        )

    def test_main_validates_playlist_title_before_oauth(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            video = Path(tmp_name) / "video.mp4"
            video.write_text("", encoding="utf-8")

            with mock.patch(
                "gc_upload_youtube.parse_args",
                return_value=type(
                    "Args",
                    (),
                    {
                        "videos": [str(video)],
                        "render_dir": None,
                        "include_standard_renders": False,
                        "game_json": None,
                        "client_secrets": "client_secret.json",
                        "token_file": "youtube_token.json",
                        "title_prefix": "",
                        "description": None,
                        "description_file": None,
                        "playlist_title": None,
                        "playlist_team_name": "Tigers",
                        "no_playlist": False,
                        "tags": "GameChanger,baseball,9U",
                        "category_id": "17",
                        "privacy_status": "unlisted",
                    },
                )(),
            ), mock.patch("gc_upload_youtube.youtube_service") as youtube_service:
                with self.assertRaises(SystemExit):
                    gc_upload_youtube.main()

            youtube_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
