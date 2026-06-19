import tempfile
import unittest
from pathlib import Path

from gc_common import GCClient, plays_to_segment_pairs, select_video_asset, write_play_outputs
from gc_make_condensed_game import after_bases, overlay_timeline
from gc_make_full_game import full_game_overlay_timeline
from gc_make_player_reels import all_player_selectors
from gc_upload_youtube import colab_youtube_description, standard_video_paths


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


class ReviewFixTests(unittest.TestCase):
    def test_search_clips_paginates_until_total_count(self):
        client = GCClient.__new__(GCClient)
        client.session = FakeSession()

        result = client.search_clips("team-id", "event-id")

        self.assertEqual(501, len(result["hits"]))
        self.assertEqual(501, result["total_count"])
        self.assertEqual([0, 500], client.session.offsets)

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
                "highlight_reel.mp4",
                "condensed_game.mp4",
                "full_game_scorebug.mp4",
                "player_reels/zach.mp4",
                "player_reels/andre.mp4",
            ]:
                (render_dir / relative).write_text("", encoding="utf-8")

            self.assertEqual(
                [
                    render_dir / "full_game_scorebug.mp4",
                    render_dir / "condensed_game.mp4",
                    render_dir / "highlight_reel.mp4",
                    render_dir / "player_reels" / "andre.mp4",
                    render_dir / "player_reels" / "zach.mp4",
                ],
                standard_video_paths(render_dir),
            )


if __name__ == "__main__":
    unittest.main()
