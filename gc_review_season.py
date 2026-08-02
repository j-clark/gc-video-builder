#!/usr/bin/env python3
"""Review GameChanger clips and build end-of-season player highlight reels."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from gc_common import GCClient, GCError
from gc_download_full_game import sort_teams_for_picker
from gc_season import (
    ROLE_NAMES,
    SeasonProject,
    default_project_path,
    team_id_from_payload,
    team_label,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="Season project directory. A new project is created if needed.")
    parser.add_argument("--team-id", help="GameChanger team/season ID for a new project.")
    parser.add_argument("--token", help="GameChanger token. Prefer GC_TOKEN.")
    parser.add_argument("--refresh", action="store_true", help="Refresh an existing project before opening the TUI.")
    return parser.parse_args()


def textual_types():
    try:
        from textual import work
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.screen import ModalScreen
        from textual.widgets import (
            Button,
            DataTable,
            Footer,
            Header,
            Input,
            Label,
            SelectionList,
            Select,
            Static,
            TabbedContent,
            TabPane,
        )
        from rich.text import Text
    except ImportError as exc:
        raise GCError("Install dependencies with: pip install -r requirements-gc.txt") from exc
    return {
        "App": App,
        "ComposeResult": ComposeResult,
        "Horizontal": Horizontal,
        "Vertical": Vertical,
        "ModalScreen": ModalScreen,
        "Button": Button,
        "DataTable": DataTable,
        "Footer": Footer,
        "Header": Header,
        "Input": Input,
        "Label": Label,
        "SelectionList": SelectionList,
        "Select": Select,
        "Static": Static,
        "TabbedContent": TabbedContent,
        "TabPane": TabPane,
        "work": work,
        "Text": Text,
    }


T = textual_types()
App = T["App"]
ComposeResult = T["ComposeResult"]
Horizontal = T["Horizontal"]
Vertical = T["Vertical"]
ModalScreen = T["ModalScreen"]
Button = T["Button"]
DataTable = T["DataTable"]
Footer = T["Footer"]
Header = T["Header"]
Input = T["Input"]
Label = T["Label"]
SelectionList = T["SelectionList"]
Select = T["Select"]
Static = T["Static"]
TabbedContent = T["TabbedContent"]
TabPane = T["TabPane"]
work = T["work"]
Text = T["Text"]


class TeamPickerApp(App[dict[str, Any] | None]):
    CSS = """
    Screen { align: center middle; }
    #picker { width: 90%; height: 80%; border: solid $accent; padding: 1 2; }
    #team-table { height: 1fr; margin-top: 1; }
    """
    BINDINGS = [("enter", "choose", "Choose"), ("escape", "cancel", "Cancel")]

    def __init__(self, teams: list[dict[str, Any]]) -> None:
        super().__init__()
        self.teams = teams

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label("Select GameChanger team and season")
            yield DataTable(id="team-table", cursor_type="row")
            yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#team-table", DataTable)
        table.add_columns("Team / season", "Team ID")
        for team in self.teams:
            table.add_row(Text(team_label(team)), Text(team_id_from_payload(team) or ""))
        table.focus()

    def action_choose(self) -> None:
        table = self.query_one("#team-table", DataTable)
        if 0 <= table.cursor_row < len(self.teams):
            self.exit(self.teams[table.cursor_row])

    def action_cancel(self) -> None:
        self.exit(None)

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_choose()


class ParticipantScreen(ModalScreen[list[tuple[str, str]] | None]):
    CSS = """
    ParticipantScreen { align: center middle; }
    #participant-dialog { width: 88%; height: 88%; border: solid $accent; background: $surface; padding: 1 2; }
    #participant-list { height: 1fr; margin: 1 0; }
    #participant-buttons { height: auto; align-horizontal: right; }
    """

    def __init__(self, players: list[dict[str, Any]], selected: set[tuple[str, str]]) -> None:
        super().__init__()
        self.players = players
        self.selected_values = selected

    def compose(self) -> ComposeResult:
        with Vertical(id="participant-dialog"):
            yield Label("Players and roles")
            options = []
            for player in self.players:
                for role in ROLE_NAMES:
                    value = f"{player['player_id']}|{role}"
                    label = Text(f"#{player.get('number') or '-':>2}  {player['display']}  |  {role.title()}")
                    options.append((label, value, (player["player_id"], role) in self.selected_values))
            yield SelectionList(*options, id="participant-list")
            with Horizontal(id="participant-buttons"):
                yield Button("Cancel", id="participant-cancel")
                yield Button("Save", id="participant-save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "participant-cancel":
            self.dismiss(None)
            return
        values = self.query_one("#participant-list", SelectionList).selected
        self.dismiss([tuple(str(value).split("|", 1)) for value in values])


class TimingScreen(ModalScreen[tuple[float, float] | None]):
    CSS = """
    TimingScreen { align: center middle; }
    #timing-dialog { width: 52; height: auto; border: solid $accent; background: $surface; padding: 1 2; }
    .timing-row { height: auto; margin: 1 0; }
    .timing-label { width: 10; padding-top: 1; }
    .timing-input { width: 1fr; }
    #timing-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    """

    def __init__(self, start: float, end: float) -> None:
        super().__init__()
        self.start = start
        self.end = end

    def compose(self) -> ComposeResult:
        with Vertical(id="timing-dialog"):
            yield Label("Final source window (seconds)")
            with Horizontal(classes="timing-row"):
                yield Static("Start", classes="timing-label")
                yield Button("-1", id="start-minus")
                yield Input(str(round(self.start)), id="timing-start", type="number", classes="timing-input")
                yield Button("+1", id="start-plus")
            with Horizontal(classes="timing-row"):
                yield Static("End", classes="timing-label")
                yield Button("-1", id="end-minus")
                yield Input(str(round(self.end)), id="timing-end", type="number", classes="timing-input")
                yield Button("+1", id="end-plus")
            with Horizontal(id="timing-buttons"):
                yield Button("Cancel", id="timing-cancel")
                yield Button("Save", id="timing-save", variant="primary")

    def _adjust(self, input_id: str, amount: int) -> None:
        widget = self.query_one(input_id, Input)
        try:
            widget.value = str(round(float(widget.value) + amount))
        except ValueError:
            widget.value = "0"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "start-minus":
            self._adjust("#timing-start", -1)
        elif button_id == "start-plus":
            self._adjust("#timing-start", 1)
        elif button_id == "end-minus":
            self._adjust("#timing-end", -1)
        elif button_id == "end-plus":
            self._adjust("#timing-end", 1)
        elif button_id == "timing-cancel":
            self.dismiss(None)
        elif button_id == "timing-save":
            try:
                value = (
                    float(self.query_one("#timing-start", Input).value),
                    float(self.query_one("#timing-end", Input).value),
                )
            except ValueError:
                self.notify("Enter numeric start and end values.", severity="error")
                return
            self.dismiss(value)


class SeasonReviewApp(App[None]):
    TITLE = "GameChanger Season Highlights"
    CSS = """
    Screen { layout: vertical; }
    #project-title { height: 1; padding: 0 1; color: $text-muted; }
    #selectors { height: auto; padding: 0 1; }
    #player-select { width: 35; }
    #role-select, #status-select { width: 20; }
    .actions { height: auto; padding: 0 1; }
    .actions Button { margin-right: 1; min-width: 9; }
    #project-actions { padding-bottom: 1; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        ("p", "preview", "Preview"),
        ("c", "confirm", "Confirm clip"),
        ("a", "accept", "Accept"),
        ("s", "skip", "Skip"),
        ("d", "defer", "Defer"),
        ("t", "timing", "Timing"),
        ("m", "participants", "Players"),
        ("r", "refresh_data", "Refresh"),
    ]

    def __init__(self, project: SeasonProject, client: GCClient | None) -> None:
        super().__init__()
        self.project = project
        self.client = client
        self.table_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            Text(f"{self.project.team_name}  |  {len(self.project.games())} completed games  |  {self.project.root}"),
            id="project-title",
        )
        players = self.project.players()
        player_options = [(Text(player["display"]), player["player_id"]) for player in players]
        role_options = [("All roles", "all"), *((role.title(), role) for role in ROLE_NAMES)]
        with Horizontal(id="selectors"):
            yield Select(player_options, value=player_options[0][1] if player_options else Select.BLANK, id="player-select")
            yield Select(role_options, value="all", id="role-select")
            yield Select(
                [("Pending", "pending"), ("Deferred", "deferred"), ("Skipped", "skipped")],
                value="pending",
                id="status-select",
            )
        with Horizontal(id="clip-actions", classes="actions"):
            yield Button("Preview", id="preview")
            yield Button("Players", id="participants")
            yield Button("Timing", id="timing")
            yield Button("Confirm", id="confirm", variant="primary")
            yield Button("Accept", id="accept", variant="success")
            yield Button("Skip", id="skip", variant="error")
            yield Button("Defer", id="defer")
            yield Button("Move up", id="move-up")
            yield Button("Move down", id="move-down")
        with Horizontal(id="project-actions", classes="actions"):
            yield Button("Refresh API", id="refresh")
            yield Button("Render player", id="render-player")
            yield Button("Render all", id="render-all")
        with TabbedContent(initial="all-tab", id="tabs"):
            with TabPane("Dashboard", id="dashboard-tab"):
                yield DataTable(id="dashboard-table", cursor_type="row")
            with TabPane("All Queue", id="all-tab"):
                yield DataTable(id="all-table", cursor_type="row")
            with TabPane("Player Queue", id="player-tab"):
                yield DataTable(id="player-table", cursor_type="row")
            with TabPane("Accepted Order", id="accepted-tab"):
                yield DataTable(id="accepted-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#dashboard-table", DataTable).add_columns(
            "Player", "Accepted", "Pending", "Deferred", "Skipped", "Unconfirmed"
        )
        clip_columns = (
            "Score",
            "Why",
            "State",
            "Date",
            "Opponent",
            "Type",
            "Timing",
            "Participants",
            "Summary",
        )
        for table_id in ("#all-table", "#player-table", "#accepted-table"):
            self.query_one(table_id, DataTable).add_columns(*clip_columns)
        self.refresh_views()
        self.query_one("#all-table", DataTable).focus()

    def selected_player_id(self) -> str | None:
        value = self.query_one("#player-select", Select).value
        return str(value) if value not in (None, Select.BLANK) else None

    def role_filter(self) -> str | None:
        value = self.query_one("#role-select", Select).value
        return None if value in (None, Select.BLANK, "all") else str(value)

    def refresh_views(self) -> None:
        self.query_one("#project-title", Static).update(
            Text(f"{self.project.team_name}  |  {len(self.project.games())} completed games  |  {self.project.root}")
        )
        dashboard = self.query_one("#dashboard-table", DataTable)
        dashboard.clear(columns=False)
        for player in self.project.dashboard():
            dashboard.add_row(
                Text(player["display"]),
                Text(str(player.get("accepted", 0))),
                Text(str(player.get("pending", 0))),
                Text(str(player.get("deferred", 0))),
                Text(str(player.get("skipped", 0))),
                Text(str(player.get("unconfirmed", 0))),
            )

        self._fill_clip_table("#all-table", self.project.all_queue(role=self.role_filter()), default_state="unreviewed")
        player_id = self.selected_player_id()
        if player_id:
            status_value = self.query_one("#status-select", Select).value
            status = str(status_value) if status_value not in (None, Select.BLANK) else "pending"
            rows = self.project.player_queue(player_id, status=status, role=self.role_filter())
            if status == "pending":
                rows.extend(self.project.inferred_player_queue(player_id, role=self.role_filter()))
                rows.sort(key=lambda row: (-row["score"], row.get("game_date") or ""))
            self._fill_clip_table("#player-table", rows, default_state=status)
            self._fill_clip_table(
                "#accepted-table",
                self.project.player_queue(player_id, status="accepted", role=self.role_filter()),
                default_state="accepted",
            )
        else:
            self._fill_clip_table("#player-table", [], default_state="")
            self._fill_clip_table("#accepted-table", [], default_state="")

    def refresh_player_options(self) -> None:
        select = self.query_one("#player-select", Select)
        current = select.value
        players = self.project.players()
        options = [(Text(player["display"]), player["player_id"]) for player in players]
        select.set_options(options)
        player_ids = {player["player_id"] for player in players}
        if current in player_ids:
            select.value = current
        elif options:
            select.value = options[0][1]
        self.refresh_views()

    def _fill_clip_table(self, selector: str, rows: list[dict[str, Any]], *, default_state: str) -> None:
        table = self.query_one(selector, DataTable)
        table.clear(columns=False)
        self.table_rows[selector] = rows
        for row in rows:
            table.add_row(
                Text(str(row["score"])),
                Text(str(row.get("score_reason") or "")),
                Text(f"{row.get('status') or default_state}{' *' if row.get('source_changed') else ''}"),
                Text(str(row.get("game_date") or "")[:10]),
                Text(str(row.get("opponent") or "")),
                Text(str(row.get("play_type") or "")),
                Text(str(row.get("timing_text") or "")),
                Text(str(row.get("participant_text") or "")),
                Text(str(row.get("play_summary") or "")),
                key=row["clip_key"],
            )

    def active_table_selector(self) -> str | None:
        active = self.query_one("#tabs", TabbedContent).active
        return {
            "all-tab": "#all-table",
            "player-tab": "#player-table",
            "accepted-tab": "#accepted-table",
        }.get(active)

    def selected_clip_key(self) -> str | None:
        selector = self.active_table_selector()
        if not selector:
            return None
        table = self.query_one(selector, DataTable)
        rows = self.table_rows.get(selector, [])
        if 0 <= table.cursor_row < len(rows):
            return str(rows[table.cursor_row]["clip_key"])
        return None

    def require_clip(self) -> str | None:
        clip_key = self.selected_clip_key()
        if not clip_key:
            self.notify("Select a clip first.", severity="warning")
        return clip_key

    def require_client(self) -> GCClient | None:
        if not self.client:
            self.notify("Set GC_TOKEN and restart to access GameChanger media.", severity="error")
        return self.client

    def safe_notify(self, message: str, severity: str = "information") -> None:
        self.notify(message, severity=severity, markup=False)

    def on_select_changed(self, _event: Select.Changed) -> None:
        if self.is_mounted:
            self.refresh_views()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "preview": self.action_preview,
            "participants": self.action_participants,
            "timing": self.action_timing,
            "confirm": self.action_confirm,
            "accept": self.action_accept,
            "skip": self.action_skip,
            "defer": self.action_defer,
            "move-up": lambda: self._move(-1),
            "move-down": lambda: self._move(1),
            "refresh": self.action_refresh_data,
            "render-player": self._render_selected,
            "render-all": self._render_all,
        }
        action = actions.get(event.button.id or "")
        if action:
            action()

    def action_participants(self) -> None:
        clip_key = self.require_clip()
        if not clip_key:
            return
        clip = self.project.clip(clip_key)
        selected = {(tag["player_id"], tag["role"]) for tag in clip["participants"]}

        def save(result: list[tuple[str, str]] | None) -> None:
            if result is not None:
                self.project.replace_draft_participants(clip_key, result)
                if clip["review_state"] == "reviewed":
                    self.project.confirm_clip(clip_key)
                self.refresh_views()

        self.push_screen(ParticipantScreen(self.project.players(), selected), save)

    def action_timing(self) -> None:
        clip_key = self.require_clip()
        if not clip_key:
            return
        clip = self.project.clip(clip_key)
        start = clip["display_start"] if clip["display_start"] is not None else 0.0
        end = clip["display_end"] if clip["display_end"] is not None else 12.0

        def save(result: tuple[float, float] | None) -> None:
            if result is None:
                return
            try:
                self.project.set_timing(clip_key, *result)
            except GCError as exc:
                self.safe_notify(str(exc), "error")
                return
            self.refresh_views()

        self.push_screen(TimingScreen(start, end), save)

    def action_confirm(self) -> None:
        clip_key = self.require_clip()
        if not clip_key:
            return
        try:
            self.project.confirm_clip(clip_key)
        except GCError as exc:
            self.safe_notify(str(exc), "error")
            return
        self.notify("Clip metadata and timing confirmed.")
        self.refresh_views()

    def _set_decision(self, status: str) -> None:
        clip_key = self.require_clip()
        player_id = self.selected_player_id()
        if not clip_key or not player_id:
            return
        clip = self.project.clip(clip_key)
        if clip["review_state"] != "reviewed":
            self.notify("Confirm the clip metadata and timing before making a reel decision.", severity="warning")
            return
        try:
            self.project.set_decision(clip_key, player_id, status)
        except GCError as exc:
            self.safe_notify(str(exc), "error")
            return
        self.refresh_views()

    def action_accept(self) -> None:
        self._set_decision("accepted")

    def action_skip(self) -> None:
        self._set_decision("skipped")

    def action_defer(self) -> None:
        self._set_decision("deferred")

    def _move(self, direction: int) -> None:
        clip_key = self.require_clip()
        player_id = self.selected_player_id()
        if clip_key and player_id:
            self.project.move_accepted(player_id, clip_key, direction)
            self.refresh_views()

    def action_preview(self) -> None:
        clip_key = self.require_clip()
        client = self.require_client()
        if clip_key and client:
            self._preview_worker(clip_key, client)

    @work(thread=True, exclusive=True, group="media")
    def _preview_worker(self, clip_key: str, client: GCClient) -> None:
        try:
            path = self.project.play_preview(client, clip_key)
        except Exception as exc:
            self.call_from_thread(self.safe_notify, str(exc), "error")
            return
        self.call_from_thread(self.safe_notify, f"Opened {path.name}")

    def action_refresh_data(self) -> None:
        client = self.require_client()
        if client:
            self._refresh_worker(client)

    @work(thread=True, exclusive=True, group="api")
    def _refresh_worker(self, client: GCClient) -> None:
        try:
            counts = self.project.refresh(client)
        except Exception as exc:
            self.call_from_thread(self.safe_notify, str(exc), "error")
            return
        self.call_from_thread(self.refresh_player_options)
        self.call_from_thread(
            self.safe_notify,
            f"Imported {counts['games']} games, {counts['clips']} clips, {counts['players']} players.",
        )

    def _render_selected(self) -> None:
        player_id = self.selected_player_id()
        client = self.require_client()
        if player_id and client:
            self._render_worker(client, player_id)

    def _render_all(self) -> None:
        client = self.require_client()
        if client:
            self._render_worker(client, None)

    @work(thread=True, exclusive=True, group="media")
    def _render_worker(self, client: GCClient, player_id: str | None) -> None:
        try:
            if player_id:
                outputs = [self.project.render_player(client, player_id)]
            else:
                outputs = self.project.render_all(client)
        except Exception as exc:
            self.call_from_thread(self.safe_notify, str(exc), "error")
            return
        self.call_from_thread(
            self.safe_notify,
            f"Rendered {len(outputs)} reel(s) in {self.project.root / 'renders'}.",
        )


def choose_team(client: GCClient, requested_team_id: str | None) -> dict[str, Any]:
    teams = sort_teams_for_picker(client.get_my_teams())
    if requested_team_id:
        selected = next((team for team in teams if team_id_from_payload(team) == requested_team_id), None)
        if selected:
            return selected
        raise GCError(f"Team {requested_team_id} was not found in /me/teams.")
    selected = TeamPickerApp(teams).run()
    if not selected:
        raise GCError("No team selected.")
    return selected


def main() -> None:
    args = parse_args()
    requested_path = Path(args.project).expanduser() if args.project else None
    existing = bool(requested_path and (requested_path / "season.db").exists())

    client: GCClient | None
    try:
        client = GCClient(args.token)
    except GCError:
        if not existing:
            raise
        client = None

    if existing:
        assert requested_path is not None
        project = SeasonProject(requested_path)
        if args.refresh:
            if not client:
                raise GCError("Set GC_TOKEN to refresh a season project.")
            project.refresh(client)
    else:
        assert client is not None
        team = choose_team(client, args.team_id)
        project_path = requested_path or default_project_path(team)
        project = SeasonProject.create(project_path, team)
        print(f"Importing completed games into {project_path}...")
        project.refresh(client)

    SeasonReviewApp(project, client).run()


if __name__ == "__main__":
    try:
        main()
    except GCError as exc:
        raise SystemExit(str(exc)) from exc
