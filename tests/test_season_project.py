import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gc_season import (
    SeasonProject,
    choose_asset_for_clip,
    display_play_summary,
    is_routine_defensive_free_pass,
    is_routine_opponent_steal,
    is_routine_out,
    participant_inference,
    score_for_role,
    team_side,
)


TEAM_ID = "team-1"
PLAYER_1 = "player-1"
PLAYER_2 = "player-2"
EVENT_ID = "event-1"
ASSET_ID = "asset-1"


class FakeSeasonClient:
    def get_schedule(self, _team_id):
        return [
            {
                "event": {
                    "id": EVENT_ID,
                    "event_type": "game",
                    "status": "completed",
                    "start": {"datetime": "2026-06-01T12:00:00Z"},
                },
                "pregame_data": {"opponent_name": "Visitors"},
            }
        ]

    def get_game_summaries(self, _team_id):
        return [
            {
                "event_id": EVENT_ID,
                "game_status": "completed",
                "home_away": "home",
                "game_stream": {"id": "stream-1", "opponent_id": "opponent-1"},
            }
        ]

    def get_players(self, _team_id):
        return [
            {"id": PLAYER_1, "first_name": "Alex", "last_name": "One", "number": "1"},
            {"id": PLAYER_2, "first_name": "Blair", "last_name": "Two", "number": "2"},
        ]

    def get_public_game_details(self, _event_id):
        return {
            "start_ts": "2026-06-01T12:00:00Z",
            "home_away": "home",
            "opponent_team": {"name": "Visitors"},
        }

    def search_clips(self, _team_id, _event_id):
        return {
            "total_count": 2,
            "hits": [
                {
                    "clip_metadata_id": "clip-double",
                    "timestamp": "2026-06-01T12:01:00Z",
                    "duration": 15,
                    "thumbnail_url": "https://example.invalid/clips/double.jpg",
                    "play_summary": f"${{{PLAYER_1}}} doubles.",
                    "play_metadata": {"pbp_id": "play-double", "play_type": "double"},
                    "sport_metadata": {"inning": 1, "inning_half": "bottom"},
                    "related_ids": {"stream_id": "video-stream-1"},
                },
                {
                    "clip_metadata_id": "clip-single",
                    "timestamp": "2026-06-01T12:02:00Z",
                    "duration": 10,
                    "thumbnail_url": "https://example.invalid/clips/single.jpg",
                    "play_summary": f"${{{PLAYER_2}}} singles.",
                    "play_metadata": {"pbp_id": "play-single", "play_type": "single"},
                    "sport_metadata": {"inning": 1, "inning_half": "bottom"},
                    "related_ids": {"stream_id": "video-stream-1"},
                },
            ],
        }

    def get_best_game_stream_id(self, _event_id):
        return "stream-1"

    def get_game_stream_events(self, _stream_id):
        events = [
            {"id": "play-double", "code": "ball_in_play", "createdAt": 1000, "attributes": {}},
            {"id": "play-single", "code": "ball_in_play", "createdAt": 2000, "attributes": {}},
        ]
        return [
            {
                "id": f"wrapper-{index}",
                "sequence_number": index,
                "event_data": json.dumps(event),
            }
            for index, event in enumerate(events)
        ]

    def get_opponent_roster(self, _team_id, _opponent_id):
        return []

    def get_event_assets(self, _team_id, _event_id):
        return [
            {
                "id": ASSET_ID,
                "stream_id": "video-stream-1",
                "schedule_event_id": EVENT_ID,
                "created_at": "2026-06-01T12:00:00Z",
                "duration": 600,
            }
        ]

    def get_event_playback_assets(self, _team_id, _event_id):
        return [{"id": ASSET_ID, "url": "https://example.invalid/video.m3u8"}]


class SeasonProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = SeasonProject.create(
            self.root,
            {
                "id": TEAM_ID,
                "name": "Tigers",
                "season_name": "summer",
                "season_year": 2026,
            },
        )
        self.client = FakeSeasonClient()
        self.project.refresh(self.client)
        source = self.project.full_game_asset_path(EVENT_ID, ASSET_ID)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"cached game")

    def tearDown(self):
        self.temp.cleanup()

    def test_all_queue_is_sorted_by_excitement(self):
        queue = self.project.all_queue()

        self.assertEqual(["double", "single"], [item["play_type"] for item in queue])
        self.assertEqual([80, 60], [item["score"] for item in queue])

    def test_routine_strikeouts_and_batter_outs_are_not_queueable(self):
        first = f"{EVENT_ID}:clip-double"
        second = f"{EVENT_ID}:clip-single"
        with self.project.connect() as conn:
            conn.execute(
                """
                UPDATE clips
                SET play_type = 'strikeout',
                    play_summary = 'Alex One #1 strikes out swinging.'
                WHERE clip_key = ?
                """,
                (first,),
            )
            conn.execute(
                """
                UPDATE clips
                SET play_type = 'batter_out',
                    play_summary = 'Blair Two #2 flies out to center field.'
                WHERE clip_key = ?
                """,
                (second,),
            )

        self.assertEqual([], self.project.all_queue())
        self.assertEqual([], self.project.inferred_player_queue(PLAYER_1))
        self.assertEqual(0, self.project.queueable_unreviewed_count())

    def test_out_with_secondary_action_or_exception_is_queueable(self):
        self.assertTrue(
            is_routine_out(
                "strikeout",
                "Alex One #1 strikes out swinging.",
                False,
            )
        )
        self.assertFalse(
            is_routine_out(
                "strikeout",
                "Alex One #1 strikes out. Blair Two #2 scores.",
                False,
            )
        )
        self.assertFalse(
            is_routine_out(
                "batter_out",
                "Alex One #1 makes an exceptional diving catch.",
                True,
            )
        )

    def test_routine_opponent_steal_is_not_queueable(self):
        clip_key = f"{EVENT_ID}:clip-single"
        with self.project.connect() as conn:
            conn.execute(
                """
                UPDATE clips
                SET play_type = 'stole_base',
                    play_summary = 'Opponent steals 2nd',
                    inning_half = 'top'
                WHERE clip_key = ?
                """,
                (clip_key,),
            )

        self.assertNotIn(
            clip_key,
            [item["clip_key"] for item in self.project.all_queue()],
        )
        self.assertNotIn(
            clip_key,
            [item["clip_key"] for item in self.project.inferred_player_queue(PLAYER_2)],
        )

    def test_opponent_steal_with_secondary_action_is_queueable(self):
        self.assertTrue(
            is_routine_opponent_steal(
                "stole_base",
                "Opponent steals 3rd",
                False,
                "defense",
            )
        )
        self.assertTrue(
            is_routine_opponent_steal(
                "stole_base",
                "Opponent scores on steal of home",
                False,
                "defense",
            )
        )
        self.assertFalse(
            is_routine_opponent_steal(
                "stole_base",
                "Opponent steals 2nd. Runner advances to 3rd.",
                False,
                "defense",
            )
        )
        self.assertFalse(
            is_routine_opponent_steal(
                "caught_stealing",
                "Opponent caught stealing 2nd",
                False,
                "defense",
            )
        )
        self.assertFalse(
            is_routine_opponent_steal(
                "stole_base",
                "Tigers runner steals 2nd",
                False,
                "offense",
            )
        )

    def test_routine_defensive_free_pass_is_not_queueable(self):
        for play_type, summary in (
            ("walk", "Opponent walks, Alex One #1 pitching."),
            (
                "hit_by_pitch",
                "Opponent is hit by pitch, Alex One #1 pitching. Runner remains at 3rd.",
            ),
        ):
            with self.subTest(play_type=play_type):
                self.assertTrue(
                    is_routine_defensive_free_pass(
                        play_type,
                        summary,
                        False,
                        "defense",
                    )
                )

    def test_defensive_free_pass_with_secondary_action_is_queueable(self):
        for play_type, summary in (
            (
                "walk",
                "Opponent walks, Alex One #1 pitching. Runner advances to 3rd.",
            ),
            (
                "hit_by_pitch",
                "Opponent is hit by pitch, Alex One #1 pitching. Runner scores.",
            ),
        ):
            with self.subTest(play_type=play_type):
                self.assertFalse(
                    is_routine_defensive_free_pass(
                        play_type,
                        summary,
                        False,
                        "defense",
                    )
                )

    def test_offensive_or_exceptional_free_pass_is_queueable(self):
        self.assertFalse(
            is_routine_defensive_free_pass(
                "walk",
                "Alex One #1 walks.",
                False,
                "offense",
            )
        )
        self.assertFalse(
            is_routine_defensive_free_pass(
                "hit_by_pitch",
                "Opponent is hit by pitch during an exceptional play.",
                True,
                "defense",
            )
        )

    def test_defensive_free_pass_is_removed_from_queues(self):
        clip_key = f"{EVENT_ID}:clip-single"
        with self.project.connect() as conn:
            conn.execute(
                """
                UPDATE clips
                SET play_type = 'walk',
                    play_summary = 'Opponent walks, Blair Two #2 pitching.',
                    inning_half = 'top'
                WHERE clip_key = ?
                """,
                (clip_key,),
            )

        self.assertNotIn(
            clip_key,
            [item["clip_key"] for item in self.project.all_queue()],
        )
        self.assertNotIn(
            clip_key,
            [item["clip_key"] for item in self.project.inferred_player_queue(PLAYER_2)],
        )
        self.assertEqual(1, self.project.queueable_unreviewed_count())

    def test_queues_expose_and_filter_offense_and_defense(self):
        defensive_clip = f"{EVENT_ID}:clip-single"
        with self.project.connect() as conn:
            conn.execute(
                "UPDATE clips SET inning_half = 'top' WHERE clip_key = ?",
                (defensive_clip,),
            )

        self.assertEqual("offense", team_side("home", "bottom"))
        self.assertEqual("offense", team_side("away", "top"))
        self.assertEqual("defense", team_side("home", "top"))
        self.assertEqual(
            [defensive_clip],
            [clip["clip_key"] for clip in self.project.all_queue(side="defense")],
        )
        self.assertEqual(
            [f"{EVENT_ID}:clip-double"],
            [clip["clip_key"] for clip in self.project.all_queue(side="offense")],
        )

    def test_media_cache_status_tracks_full_game_assets(self):
        path = self.project.full_game_asset_path(EVENT_ID, ASSET_ID)
        path.unlink()
        status = self.project.media_cache_status()
        self.assertEqual(1, status["totalAssets"])
        self.assertEqual(0, status["cachedAssets"])

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached game")

        status = self.project.media_cache_status()
        self.assertEqual(1, status["cachedAssets"])
        self.assertEqual(len(b"cached game"), status["cachedBytes"])

    def test_uncached_games_do_not_appear_in_queues(self):
        self.project.full_game_asset_path(EVENT_ID, ASSET_ID).unlink()

        self.assertEqual([], self.project.all_queue())
        self.assertEqual([], self.project.inferred_player_queue(PLAYER_1))

    def test_confirm_creates_independent_player_decisions(self):
        clip_key = f"{EVENT_ID}:clip-double"
        self.project.replace_draft_participants(
            clip_key,
            [(PLAYER_1, "batter"), (PLAYER_2, "runner")],
        )
        self.project.confirm_clip(clip_key)

        self.assertNotIn(clip_key, [item["clip_key"] for item in self.project.all_queue()])
        self.assertEqual([clip_key], [item["clip_key"] for item in self.project.player_queue(PLAYER_1)])
        self.assertEqual([clip_key], [item["clip_key"] for item in self.project.player_queue(PLAYER_2)])

        self.project.set_decision(clip_key, PLAYER_1, "accepted")
        self.project.set_decision(clip_key, PLAYER_2, "skipped")

        self.assertEqual([clip_key], [item["clip_key"] for item in self.project.player_queue(PLAYER_1, status="accepted")])
        self.assertEqual([clip_key], [item["clip_key"] for item in self.project.player_queue(PLAYER_2, status="skipped")])

    def test_unconfirmed_player_can_be_skipped_before_timing_review(self):
        clip_key = f"{EVENT_ID}:clip-double"
        self.assertIn(
            clip_key,
            [item["clip_key"] for item in self.project.inferred_player_queue(PLAYER_1)],
        )

        self.project.set_decision(clip_key, PLAYER_1, "skipped")

        self.assertNotIn(
            clip_key,
            [item["clip_key"] for item in self.project.inferred_player_queue(PLAYER_1)],
        )
        skipped = self.project.player_queue(PLAYER_1, status="skipped")
        self.assertEqual([clip_key], [item["clip_key"] for item in skipped])
        self.assertEqual("unreviewed", skipped[0]["review_state"])
        player = next(
            item for item in self.project.dashboard() if item["player_id"] == PLAYER_1
        )
        self.assertEqual(1, player["skipped"])
        self.assertEqual(0, player["unconfirmed"])

        self.project.confirm_clip(clip_key)
        self.assertEqual(
            [clip_key],
            [
                item["clip_key"]
                for item in self.project.player_queue(PLAYER_1, status="skipped")
            ],
        )

    def test_unconfirmed_clip_cannot_be_accepted(self):
        clip_key = f"{EVENT_ID}:clip-double"

        with self.assertRaisesRegex(Exception, "Only Skip"):
            self.project.set_decision(clip_key, PLAYER_1, "accepted")

    def test_explicit_empty_participants_are_reviewable(self):
        clip_key = f"{EVENT_ID}:clip-single"
        self.project.replace_draft_participants(clip_key, [])

        self.assertEqual([], self.project.clip(clip_key)["participants"])
        self.assertEqual([], self.project.inferred_player_queue(PLAYER_2))

        self.project.confirm_clip(clip_key)

        self.assertEqual("reviewed", self.project.clip(clip_key)["review_state"])
        self.assertEqual([], self.project.clip(clip_key)["participants"])
        self.assertEqual([], self.project.player_queue(PLAYER_2))

    def test_dismissed_clip_leaves_queues_without_deleting_source(self):
        clip_key = f"{EVENT_ID}:clip-double"
        self.project.confirm_clip(clip_key)
        self.project.dismiss_clip(clip_key, "play_not_found")

        self.assertEqual("dismissed", self.project.clip(clip_key)["review_state"])
        self.assertEqual([], self.project.player_queue(PLAYER_1))
        with self.project.connect() as conn:
            reason = conn.execute(
                "SELECT value FROM project WHERE key = ?",
                (f"dismiss_reason:{clip_key}",),
            ).fetchone()[0]
        self.assertEqual("play_not_found", reason)

    def test_clips_can_be_bulk_dismissed_as_not_noteworthy(self):
        clip_keys = [f"{EVENT_ID}:clip-double", f"{EVENT_ID}:clip-single"]

        count = self.project.dismiss_clips(clip_keys, "not_noteworthy")

        self.assertEqual(2, count)
        self.assertEqual([], self.project.all_queue())
        with self.project.connect() as conn:
            states = {
                row["clip_key"]: row["review_state"]
                for row in conn.execute(
                    "SELECT clip_key, review_state FROM clips ORDER BY clip_key"
                )
            }
            reasons = {
                row["key"]: row["value"]
                for row in conn.execute(
                    """
                    SELECT key, value FROM project
                    WHERE key LIKE 'dismiss_reason:%'
                    """
                )
            }
        self.assertEqual({key: "dismissed" for key in clip_keys}, states)
        self.assertEqual(
            {
                f"dismiss_reason:{key}": "not_noteworthy"
                for key in clip_keys
            },
            reasons,
        )

    def test_corrected_catcher_updates_display_summary_without_changing_source(self):
        clip_key = f"{EVENT_ID}:clip-double"
        source_summary = "Player 5007c990 caught stealing 2nd, catcher Oliver Clark #9"
        with self.project.connect() as conn:
            conn.execute(
                "UPDATE clips SET play_type = 'caught_stealing', play_summary = ? WHERE clip_key = ?",
                (source_summary, clip_key),
            )
        self.project.replace_draft_participants(
            clip_key,
            [(PLAYER_1, "pitcher"), (PLAYER_2, "fielder")],
        )

        draft_clip = self.project.clip(clip_key)
        self.assertEqual(
            "Player 5007c990 caught stealing 2nd, catcher Blair Two #2",
            draft_clip["play_summary"],
        )
        self.assertEqual(
            "Caught stealing 2nd - Alex One #1 (pitcher), Blair Two #2 (catcher)",
            draft_clip["play_title"],
        )
        self.assertEqual(source_summary, draft_clip["source_play_summary"])

        self.project.confirm_clip(clip_key)
        player_clip = self.project.player_queue(PLAYER_1)[0]
        self.assertEqual(
            "Player 5007c990 caught stealing 2nd, catcher Blair Two #2",
            player_clip["play_summary"],
        )
        self.assertEqual(draft_clip["play_title"], player_clip["play_title"])
        self.assertEqual(source_summary, player_clip["source_play_summary"])

        self.project.replace_draft_participants(clip_key, [(PLAYER_1, "fielder")])
        corrected_again = self.project.clip(clip_key)
        self.assertEqual(
            "Caught stealing 2nd - Alex One #1 (catcher)",
            corrected_again["play_title"],
        )

    def test_display_summary_leaves_ambiguous_fielders_unchanged(self):
        summary = "Runner caught stealing 3rd, catcher Original Player #9"
        tags = [
            {"display": "Alex One #1", "role": "fielder"},
            {"display": "Blair Two #2", "role": "fielder"},
        ]

        self.assertEqual(summary, display_play_summary(summary, tags))

    def test_removing_and_readding_player_restores_decision(self):
        clip_key = f"{EVENT_ID}:clip-double"
        self.project.replace_draft_participants(clip_key, [(PLAYER_1, "batter")])
        self.project.confirm_clip(clip_key)
        self.project.set_decision(clip_key, PLAYER_1, "accepted")

        self.project.replace_draft_participants(clip_key, [])
        self.project.confirm_clip(clip_key)
        self.assertEqual([], self.project.player_queue(PLAYER_1, status="accepted"))

        self.project.replace_draft_participants(clip_key, [(PLAYER_1, "batter")])
        self.project.confirm_clip(clip_key)
        self.assertEqual([clip_key], [item["clip_key"] for item in self.project.player_queue(PLAYER_1, status="accepted")])

    def test_refresh_preserves_review_and_decisions(self):
        clip_key = f"{EVENT_ID}:clip-double"
        self.project.confirm_clip(clip_key)
        self.project.set_decision(clip_key, PLAYER_1, "accepted")

        self.project.refresh(self.client)

        self.assertEqual("reviewed", self.project.clip(clip_key)["review_state"])
        self.assertEqual([clip_key], [item["clip_key"] for item in self.project.player_queue(PLAYER_1, status="accepted")])

    def test_timing_is_whole_seconds_and_validated(self):
        clip_key = f"{EVENT_ID}:clip-double"
        self.project.set_timing(clip_key, 41.6, 63.4)

        clip = self.project.clip(clip_key)
        self.assertEqual(42, clip["display_start"])
        self.assertEqual(63, clip["display_end"])

        with self.assertRaisesRegex(Exception, "0 <= start < end"):
            self.project.set_timing(clip_key, 70, 60)

    def test_manual_accepted_order_can_be_rearranged(self):
        first = f"{EVENT_ID}:clip-double"
        second = f"{EVENT_ID}:clip-single"
        self.project.confirm_clip(first)
        self.project.replace_draft_participants(second, [(PLAYER_1, "batter")])
        self.project.confirm_clip(second)
        self.project.set_decision(first, PLAYER_1, "accepted")
        self.project.set_decision(second, PLAYER_1, "accepted")

        self.project.move_accepted(PLAYER_1, second, -1)

        self.assertEqual(
            [second, first],
            [item["clip_key"] for item in self.project.player_queue(PLAYER_1, status="accepted")],
        )

    def test_missing_preview_file_is_regenerated(self):
        clip_key = f"{EVENT_ID}:clip-double"
        source = self.project.full_game_asset_path(EVENT_ID, ASSET_ID)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"full game")
        relative_path = Path("previews") / "missing.mp4"
        with self.project.connect() as conn:
            conn.execute(
                """
                INSERT INTO previews(clip_key, path, source_start, source_end, updated_at)
                VALUES (?, ?, 0, 100, 'now')
                """,
                (clip_key, str(relative_path)),
            )

        def fake_run(command):
            Path(command[-1]).write_bytes(b"preview")

        with (
            mock.patch("gc_season.run", side_effect=fake_run) as run_mock,
            mock.patch("gc_season.has_video_stream", side_effect=lambda path: path.exists()),
        ):
            path = self.project.ensure_preview(self.client, clip_key)

        self.assertTrue(path.exists())
        run_mock.assert_called_once()
        command = run_mock.call_args.args[0]
        self.assertIn(str(source), command)
        self.assertNotIn("https://example.invalid/video.m3u8", command)

    def test_preview_error_does_not_expose_signed_headers(self):
        clip_key = f"{EVENT_ID}:clip-double"
        source = self.project.full_game_asset_path(EVENT_ID, ASSET_ID)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"full game")
        command = ["ffmpeg", "-headers", "Cookie: secret"]
        with mock.patch(
            "gc_season.run",
            side_effect=subprocess.CalledProcessError(222, command),
        ):
            with self.assertRaisesRegex(Exception, "exit status 222") as raised:
                self.project.ensure_preview(self.client, clip_key)

        self.assertNotIn("secret", str(raised.exception))

    def test_role_scoring_bonuses(self):
        score, reason = score_for_role("double_play", "Alex scores on the play.", "fielder", True)

        self.assertEqual(140, score)
        self.assertIn("double play", reason)
        self.assertIn("exceptional", reason)
        self.assertIn("run-scoring", reason)

    def test_batter_strikeout_scores_below_hits(self):
        strikeout, reason = score_for_role(
            "strikeout",
            "Alex strikes out swinging.",
            "batter",
            False,
        )
        single, _ = score_for_role("single", "Alex singles.", "batter", False)
        double, _ = score_for_role("double", "Alex doubles.", "batter", False)
        pitcher_strikeout, _ = score_for_role(
            "strikeout",
            "Opponent strikes out swinging.",
            "pitcher",
            False,
        )

        self.assertEqual(0, strikeout)
        self.assertEqual("batter struck out", reason)
        self.assertEqual(60, single)
        self.assertEqual(80, double)
        self.assertEqual(75, pitcher_strikeout)

    def test_fielder_inference_maps_defender_position_to_player(self):
        events = [
            {
                "code": "fill_position",
                "createdAt": 100,
                "attributes": {"teamId": TEAM_ID, "position": "SS", "playerId": PLAYER_2},
            }
        ]

        participants = participant_inference(
            play_type="batter_out",
            summary="Batter grounds out to shortstop.",
            mentioned_ids=[],
            raw_event={"attributes": {"defenders": [{"position": "SS"}]}},
            events=events,
            event_ms=200,
            own_team_id=TEAM_ID,
            own_player_ids={PLAYER_1, PLAYER_2},
            own_batting=False,
        )

        self.assertIn((PLAYER_2, "fielder", "medium"), participants)

    def test_defensive_hit_infers_fielder_without_runner(self):
        events = [
            {
                "code": "fill_position",
                "createdAt": 100,
                "attributes": {"teamId": TEAM_ID, "position": "P", "playerId": PLAYER_1},
            },
            {
                "code": "fill_position",
                "createdAt": 100,
                "attributes": {"teamId": TEAM_ID, "position": "RF", "playerId": PLAYER_2},
            },
        ]

        participants = participant_inference(
            play_type="single",
            summary="Opponent singles to right fielder Blair Two #2. Another runner advances.",
            mentioned_ids=[PLAYER_2],
            raw_event={"attributes": {"defenders": [{"position": "RF"}]}},
            events=events,
            event_ms=200,
            own_team_id=TEAM_ID,
            own_player_ids={PLAYER_1, PLAYER_2},
            own_batting=False,
        )

        self.assertIn((PLAYER_1, "pitcher", "high"), participants)
        self.assertIn((PLAYER_2, "fielder", "medium"), participants)
        self.assertNotIn((PLAYER_2, "runner", "medium"), participants)

    def test_asset_selection_uses_timestamp_when_stream_has_multiple_parts(self):
        clip = {
            "timestamp": "2026-06-01T12:11:00Z",
            "related_ids": {"stream_id": "shared-stream"},
        }
        assets = [
            {
                "id": "first",
                "stream_id": "shared-stream",
                "created_at": "2026-06-01T12:00:00Z",
                "duration": 300,
            },
            {
                "id": "second",
                "stream_id": "shared-stream",
                "created_at": "2026-06-01T12:10:00Z",
                "duration": 300,
            },
        ]

        self.assertEqual("second", choose_asset_for_clip(clip, assets)["id"])

    def test_asset_selection_uses_duration_when_reported_end_is_too_early(self):
        clip = {
            "timestamp": "2026-06-01T12:18:00Z",
            "related_ids": {"stream_id": "shared-stream"},
        }
        assets = [
            {
                "id": "first",
                "stream_id": "shared-stream",
                "created_at": "2026-06-01T12:00:00Z",
                "ended_at": "2026-06-01T12:05:00Z",
                "duration": 600,
            },
            {
                "id": "second",
                "stream_id": "shared-stream",
                "created_at": "2026-06-01T12:10:00Z",
                "ended_at": "2026-06-01T12:13:00Z",
                "duration": 1200,
            },
        ]

        self.assertEqual("second", choose_asset_for_clip(clip, assets)["id"])

    def test_asset_selection_falls_back_to_nearest_asset(self):
        clip = {
            "timestamp": "2026-06-01T12:09:40Z",
            "related_ids": {"stream_id": "shared-stream"},
        }
        assets = [
            {
                "id": "first",
                "stream_id": "shared-stream",
                "created_at": "2026-06-01T12:00:00Z",
                "duration": 300,
            },
            {
                "id": "second",
                "stream_id": "shared-stream",
                "created_at": "2026-06-01T12:10:00Z",
                "duration": 300,
            },
        ]

        self.assertEqual("second", choose_asset_for_clip(clip, assets)["id"])


if __name__ == "__main__":
    unittest.main()
