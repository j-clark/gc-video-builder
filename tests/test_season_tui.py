import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gc_common import GCError
from gc_season import SeasonProject

try:
    from tests.test_season_project import ASSET_ID, EVENT_ID, FakeSeasonClient, TEAM_ID
except ModuleNotFoundError:
    from test_season_project import ASSET_ID, EVENT_ID, FakeSeasonClient, TEAM_ID


TEXTUAL_AVAILABLE = importlib.util.find_spec("textual") is not None


@unittest.skipUnless(TEXTUAL_AVAILABLE, "Textual is not installed")
class SeasonTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_clip_leaves_all_queue(self):
        from gc_review_season import ParticipantScreen, SeasonReviewApp, TimingScreen

        with tempfile.TemporaryDirectory() as tmp_name:
            project = SeasonProject.create(
                Path(tmp_name),
                {
                    "id": TEAM_ID,
                    "name": "Tigers",
                    "season_name": "summer",
                    "season_year": 2026,
                },
            )
            project.refresh(FakeSeasonClient())
            source = project.full_game_asset_path(EVENT_ID, ASSET_ID)
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"cached game")
            with project.connect() as conn:
                conn.execute(
                    "UPDATE clips SET play_summary = 'Bracketed diagnostic []' WHERE clip_metadata_id = 'clip-double'"
                )
            app = SeasonReviewApp(project, FakeSeasonClient())

            async with app.run_test(size=(120, 50)) as pilot:
                await pilot.pause()
                clip_key = app.selected_clip_key()
                before = project.clip(clip_key)["display_start"]

                with mock.patch.object(
                    project,
                    "play_preview",
                    side_effect=GCError("ffmpeg [Parsed_scale_0 @ 0x1] failed []"),
                ):
                    await pilot.click("#preview")
                    await pilot.pause(0.2)
                self.assertEqual(2, app.query_one("#all-table").row_count)

                await pilot.click("#timing")
                await pilot.pause()
                self.assertIsInstance(app.screen, TimingScreen)
                await pilot.click("#start-plus")
                await pilot.click("#timing-save")
                await pilot.pause()
                self.assertEqual(round(before) + 1, project.clip(clip_key)["display_start"])

                await pilot.click("#participants")
                await pilot.pause()
                self.assertIsInstance(app.screen, ParticipantScreen)
                await pilot.click("#participant-save")
                await pilot.pause()
                await pilot.click("#confirm")
                await pilot.pause()

                self.assertEqual("reviewed", project.clip(clip_key)["review_state"])
                self.assertEqual(1, app.query_one("#all-table").row_count)


if __name__ == "__main__":
    unittest.main()
