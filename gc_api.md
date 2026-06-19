# GameChanger API Notes

This is an unofficial, reverse-engineered API map from the `~/Documents/gc/*.har`
exports and live probes against `https://api.team-manager.gc.com`.

Audience: Codex or another coding agent implementing game video indexing, player
reels, highlight reels, and condensed games.

## Auth

Use the `gc-token` request header.

Recommended runtime config:

```bash
export GC_TOKEN='...'
export GC_TEAM_ID='3944d1b5-9a25-4d9c-bedd-0cb455e3ec51'
export GC_PUBLIC_TEAM_ID='DroRo4lVf3Wm'
```

Do not hard-code tokens. Tokens are short-lived JWTs.

Common headers:

```text
gc-token: $GC_TOKEN
gc-app-name: web
accept: application/json
```

For clip search:

```text
content-type: application/vnd.gc.com.video_clip_search_query+json; version=0.0.0
accept: application/vnd.gc.com.video_clip_search_results+json; version=0.1.0
x-gc-features: lazy-sync
```

For team video asset metadata with `playback_url`:

```text
accept: application/vnd.gc.com.video_stream_asset_metadata:list+json; version=0.0.0
content-type: application/vnd.gc.com.none+json; version=undefined
```

## IDs

Observed current team/game:

```text
team_id:        3944d1b5-9a25-4d9c-bedd-0cb455e3ec51
public_team_id: DroRo4lVf3Wm
event_id:       79e5bb9f-f87e-4d02-96ba-6abb4e7777aa
asset_stream_id bf67c54b-55c1-446d-aaf3-cbb37b1cfe6c
game_stream_id: 4920616b-52e2-429c-91b3-0a08ec2b585a
video_asset_id: c8ef9234-e745-4cca-bb53-928fc476964e
```

`event_id` is the schedule/game id. `game_stream_id` is the scorekeeping event
stream id. `asset_stream_id` is the archived video stream id.

## Discovery Endpoints

### User Teams

```text
GET /me/teams?include=user_team_associations,team_avatar_image,team_player_count,team_public_profile_id
```

Returns teams for the current account. Useful fields:

```text
id
name
sport
season_year
season_name
public_id
url_encoded_name
team_player_count
record
user_team_associations
```

### Schedule

```text
GET /teams/{team_id}/schedule?fetch_place_details=true
```

Returns a list of:

```json
{
  "event": {
    "id": "event uuid",
    "event_type": "game|practice|other",
    "status": "scheduled|completed|canceled",
    "start": {"datetime": "...Z"},
    "end": {"datetime": "...Z"},
    "timezone": "America/New_York",
    "title": "...",
    "location": {"name": "...", "coordinates": {}, "address": []}
  },
  "pregame_data": {
    "game_id": "same as event id",
    "opponent_name": "...",
    "opponent_id": "...",
    "home_away": "home|away"
  }
}
```

### Game Summaries

```text
GET /teams/{team_id}/game-summaries
```

Useful for selecting completed games and finding the scorekeeping stream:

```json
{
  "event_id": "79e5...",
  "game_status": "completed",
  "home_away": "home",
  "owning_team_score": 9,
  "opponent_team_score": 6,
  "last_scoring_update": "2026-06-19T15:39:17.162Z",
  "game_stream": {
    "id": "4920616b-...",
    "game_id": "79e5...",
    "opponent_id": "4b418854-...",
    "sabertooth_major_version": 4,
    "scoring_user_id": "..."
  },
  "sport_specific": {
    "bats": {
      "total_outs": 33,
      "inning_details": {"inning": 6, "half": "bottom"}
    }
  }
}
```

### Public Game Details

```text
GET /public/game-stream-processing/{event_id}/details?include=line_scores
```

Returns public scoreboard metadata:

```text
id
start_ts
end_ts
timezone
game_status
has_live_stream
has_videos_available
home_away
score.team
score.opponent_team
line_score
opponent_team.name
```

## Roster Endpoints

### Private Team Roster

```text
GET /teams/{team_id}/players
```

Fields include:

```text
id
person_id
team_id
status
first_name
last_name
number
bats
```

### Public Team Roster

```text
GET /teams/public/{public_team_id}/players
```

Smaller response:

```text
id
first_name
last_name
number
avatar_url
```

### Opponents

```text
GET /teams/{team_id}/opponents
GET /teams/{team_id}/opponent/{opponent_id}
GET /teams/{team_id}/opponents/{opponent_id}/roster
```

The opponent roster route is exposed by the web app route table but was not part
of the HAR. Treat it as best-effort.

## Video Endpoints

### Video Stream Metadata

```text
GET /teams/{team_id}/schedule/events/{event_id}/video-stream/
```

Returns stream state:

```text
stream_id
team_id
schedule_event_id
status
disabled
is_playable
audience_type
capture_mode
publish_url
viewer_count
```

For the observed completed game, `status` was `ended`.

### Event Video Assets

```text
GET /teams/{team_id}/schedule/events/{event_id}/video-stream/assets
```

Returns assets for that event:

```text
id
stream_id
team_id
schedule_event_id
created_at
duration
ended_at
thumbnail_url
user_id
uploaded
is_processing
```

### Event Playback Assets

```text
GET /teams/{team_id}/schedule/events/{event_id}/video-stream/assets/playback
```

Returns playable archived stream URLs and signed CloudFront cookies:

```text
id
schedule_event_id
url
cookies.CloudFront-Key-Pair-Id
cookies.CloudFront-Signature
cookies.CloudFront-Policy
```

Use the cookies as an HTTP `Cookie:` header when `ffmpeg` reads the HLS URL.

### Team Video Assets

```text
GET /teams/{team_id}/video-stream/assets
```

With the video asset accept header, this can return `playback_url` directly:

```text
id
stream_id
team_id
schedule_event_id
created_at
duration
ended_at
thumbnail_url
playback_url
```

## Clip and Play Endpoints

### Clip Search

```text
POST /clips/search
```

Body:

```json
{
  "match_all": {
    "team_id": "3944d1b5-9a25-4d9c-bedd-0cb455e3ec51",
    "event_id": "79e5bb9f-f87e-4d02-96ba-6abb4e7777aa"
  },
  "sort": [{"by": "timestamp", "order": "asc"}],
  "limit": 500,
  "select": {"kind": "event", "include_totals": true},
  "offset": 0,
  "paging": "page"
}
```

Returns:

```text
hits[]
total_count
next_offset
```

Each hit:

```text
clip_metadata_id
hidden
audience_type
last_updated_at
related_ids.event_id
related_ids.team_id
related_ids.stream_id
sport
play_summary
duration
timestamp
thumbnail_url
play_metadata.type
play_metadata.pbp_id
play_metadata.play_type
sport_metadata.type
sport_metadata.inning
sport_metadata.inning_half
exceptional_play
cv_generated
```

Observed play types:

```text
batter_out
batter_out_advance_runners
caught_stealing
double
double_play
hit_by_pitch
home_run
single
stole_base
strikeout
triple
walk
```

### Simple Clips List

```text
GET /clips?kind=event&teamId={team_id}
```

Returns the same clip objects as an array. Use `POST /clips/search` when you
need event filtering, sorting, paging, or total counts.

## Scorekeeping Event Stream

### Best Game Stream ID

```text
GET /events/{event_id}/best-game-stream-id
```

Returns:

```json
{"game_stream_id": "4920616b-52e2-429c-91b3-0a08ec2b585a"}
```

### Game Stream Metadata

```text
GET /game-streams/{game_stream_id}
```

Returns:

```text
id
game_id
game_status
home_away
is_archived
opponent_id
sabertooth_major_version
scoring_mode
scoring_user_id
```

### Game Stream Events

```text
GET /game-streams/{game_stream_id}/events
GET /game-streams/gamestream-viewer-payload-lite/{event_id}
```

Both return scorekeeping events. The direct `/events` endpoint returns an array.
The viewer payload returns:

```text
stream_id
marker
all_event_data_ids[]
latest_events[]
```

Each event has:

```text
id
stream_id
sequence_number
event_data
created_at
```

`event_data` is a JSON string. Parse it before use.

Observed parsed event codes:

```text
pitch
base_running
ball_in_play
end_at_bat
fill_lineup_index
fill_position
goto_lineup_index
confirm_end_of_lineup
clear_entire_lineup
clear_all_positions
set_teams
undo
transaction
```

`transaction` wraps nested events in `events[]`; flatten those.

Important fields:

```text
event_data.id
event_data.code
event_data.createdAt      # epoch ms, scorer's event time
event_data.attributes
```

Example `pitch`:

```json
{
  "code": "pitch",
  "createdAt": 1781877532785,
  "attributes": {
    "advancesRunners": false,
    "result": "strike_looking",
    "advancesCount": true
  }
}
```

Example `ball_in_play`:

```json
{
  "code": "ball_in_play",
  "attributes": {
    "playResult": "batter_out_advance_runners",
    "playType": "ground_ball",
    "defenders": [
      {"position": "P", "error": false, "location": {"x": 160, "y": 179.78}}
    ]
  }
}
```

Example `base_running`:

```json
{
  "code": "base_running",
  "attributes": {
    "playType": "stole_base",
    "runnerId": "player id",
    "base": 2,
    "defenders": []
  }
}
```

## Player and Season Stats

### Event Player Stats

```text
GET /teams/{team_id}/schedule/events/{event_id}/player-stats
```

Returns:

```text
event_id
team_id
stream_id
player_stats
cumulative_player_stats
spray_chart_data
```

`player_stats` and `cumulative_player_stats` are objects, grouped by team/player.
Stats include offense, defense, and general sections.

### Season Stats

```text
GET /teams/{team_id}/season-stats
```

Returns:

```text
id
team_id
stats_data
```

## Joining Clips, Scorekeeping, and Video

Primary join:

```text
clip.play_metadata.pbp_id == parsed_score_event.id.lower()
```

This is more reliable than parsing `play_summary`.

Video timing:

```text
video_offset_seconds = clip.timestamp - video_asset.created_at
clip_start_seconds = max(0, video_offset_seconds - clip.duration)
clip_end_seconds = min(video_asset.duration, video_offset_seconds)
```

For a little context in reels:

```text
segment_start = max(0, clip_start_seconds - pre_roll)
segment_end = min(video_asset.duration, clip_end_seconds + post_roll)
```

Text placeholders:

```text
${player_uuid}
```

Replace with roster data from team/opponent/player endpoints. Keep the original
IDs too, because player reels should filter by ID rather than by display name.

## Web URLs

Observed public recap route:

```text
https://web.gc.com/teams/{public_team_id}/schedule/{event_id}/recap
```

The web bundle also exposes UI routes such as:

```text
/teams/:teamID/schedule/:eventID/videos/clips/:videoID
/teams/:teamID/schedule/:eventID/recap/reels
```

Live API probes for similarly shaped API paths returned 404; treat these as
front-end routes, not REST endpoints.

## Routes That Failed During Probing

These are not useful with the observed web token/context:

```text
GET /game-stream-processing/{event_id}/plays                         -> 403
GET /game-stream-processing/{event_id}/boxscore                      -> 403
GET /teams/{team_id}/schedule/{event_id}/plays                       -> 404
GET /teams/{team_id}/schedule/{event_id}/box-score                   -> 404
GET /teams/{team_id}/schedule/{event_id}/game-stats                  -> 404
GET /teams/{team_id}/schedule/{event_id}/starting-lineup             -> 404
GET /teams/{team_id}/video-clips/player/{player_id}/clips            -> 404
GET /teams/{team_id}/video-clips/playable-clip/{clip_id}/clip        -> 404
```
