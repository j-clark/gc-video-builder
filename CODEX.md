# GameChanger Reel Tools

This is a standalone workspace for pulling GameChanger baseball metadata and rendering video reels. It is not part of the Treehouse repo.

## Guardrails

- Do not print or commit GameChanger tokens, CloudFront cookies, YouTube OAuth tokens, or client secrets.
- Prefer `GC_TOKEN`, `GC_TEAM_ID`, and `GC_PUBLIC_TEAM_ID` environment variables over inline token arguments.
- Avoid unnecessary network/video reads. Render scripts use `gc_render_cache/segments` by default; keep that enabled unless debugging cache behavior.
- Do not delete `gc_render_cache/segments` unless the user explicitly wants a cold render.
- Use `apply_patch` for source edits.

## Key Files

- `gc_api.md`: API notes from HAR/network exploration.
- `gc_common.py`: shared API, timing, video-source, segment cache, and ffmpeg helpers.
- `gc_fetch_game.py`: fetches GameChanger game metadata into a local `game.json`.
- `gc_make_player_reels.py`: creates per-player reels.
- `gc_make_highlight_reel.py`: creates selected play-type highlight reel.
- `gc_make_condensed_game.py`: creates a condensed game.
- `gc_make_full_game.py`: burns scorebug/caption overlays into the full-length game stream.
- `gc_upload_youtube.py`: uploads rendered MP4s to YouTube; not fully tested yet.
- `requirements-gc.txt`: Python dependencies.

## Current Test Data

- Main fetched game JSON:
  `gc_output_test/79e5bb9f-f87e-4d02-96ba-6abb4e7777aa/game.json`
- Raw sibling files expected by player reels:
  `stream_events_raw.json`, `clips_raw.json`, `plays.csv`, `plays.md`
- Render outputs:
  `gc_render_test/player_reels/`
- Segment cache:
  `gc_render_cache/segments/`

## Setup

```bash
cd /Users/josh_clark/Documents/gc
python3 -m venv .venv
.venv/bin/pip install -r requirements-gc.txt
export GC_TOKEN='...'
export GC_TEAM_ID='...'
export GC_PUBLIC_TEAM_ID='...'
```

`ffmpeg` and `ffprobe` must be available on `PATH`.

## Fetch Metadata

```bash
.venv/bin/python gc_fetch_game.py \
  --event-id 79e5bb9f-f87e-4d02-96ba-6abb4e7777aa \
  --out-dir gc_output
```

If `--event-id` is omitted, the script picks the latest completed game for `GC_TEAM_ID`.

The fetch script writes a game folder containing `game.json`, raw clips, raw stream events, and play indexes. The render scripts expect `stream_events_raw.json` next to `game.json` for richer player-role selection.

## Render Player Reels

Default player-reel behavior is intentionally condensed:

- Batter: include only the last pitch/play when the batter reaches base.
- Runner: include only plays where the player advances.
- Pitcher: include strikeout pitches or pitches where the pitcher records an out.
- Fielder: include out-producing plays where the player is the current defender at the recorded position.

Current timing defaults use the same clip-boundary logic as the highlight reel (`gc_common.plays_to_segments` with `anchor="auto"`):

- Start buffer: `4s`
- End buffer: `2s`
- Minimum segment length: `12s`
- Long GameChanger clip pre-roll: `18s`

Player reels intentionally use the same timing knobs as highlight and condensed reels, so shared plays use the same anchor logic across outputs. This is important for plays like the Andre Leon / Connor Dolginko double play at `9:34`.

Render all:

```bash
.venv/bin/python gc_make_player_reels.py \
  gc_output_test/79e5bb9f-f87e-4d02-96ba-6abb4e7777aa/game.json \
  --players all \
  --out-dir gc_render_test/player_reels \
  --reencode \
  --scorebug
```

Render one player:

```bash
.venv/bin/python gc_make_player_reels.py \
  gc_output_test/79e5bb9f-f87e-4d02-96ba-6abb4e7777aa/game.json \
  --players 11 \
  --out-dir gc_render_test/player_reels \
  --reencode \
  --scorebug
```

Adjust timing if clips start late or include too much dead time:

```bash
.venv/bin/python gc_make_player_reels.py GAME_JSON \
  --players all \
  --start-buffer 4 \
  --end-buffer 3 \
  --min-segment-length 16
```

## Render Highlight Or Condensed Game

```bash
.venv/bin/python gc_make_highlight_reel.py GAME_JSON \
  --output gc_render_test/highlight_reel.mp4

.venv/bin/python gc_make_condensed_game.py GAME_JSON \
  --output gc_render_test/condensed_game.mp4

.venv/bin/python gc_make_full_game.py GAME_JSON \
  --output gc_render_test/full_game_scorebug.mp4 \
  --description-overrides gc_render_test/condensed_game_description_review_cleaned.md
```

Both scripts support the same render cache and timing knobs:
`--start-buffer`, `--end-buffer`, `--min-segment-length`, `--cache-dir`, and `--no-cache`.
Highlight, player, and condensed reels all use the same auto-anchor timing helper, `gc_common.plays_to_segments`:

- clips shorter than `12s` anchor from `segment_start_sec`,
- clips `12s+` anchor near `clip_end_sec`/`video_offset_sec`,
- default long-clip pre-roll is `18s`,
- condensed can still use targeted `--extra-start-play-indexes` overrides, but those are passed into the shared helper rather than using a separate batted-ball timing path,
- final review renders should usually use `--reencode`; stream-copy cuts can snap to HLS/keyframe boundaries and make timing changes look ineffective.

Highlight also supports `--time-shift`; use negative values such as `--time-shift -5` when play windows start late and the whole clip should move earlier without getting longer.

Highlight reel default selection is curated for the team, not a raw play-type dump. It includes:

- team extra-base hits,
- team run-scoring plate appearances,
- team steals of home,
- strikeouts by team pitchers,
- defensive outs involving team fielders, including double plays and caught stealing,
- GameChanger `exceptional_play` clips if present.

Passing `--types single,double,...` overrides curated selection.

Condensed game default selection is explicit:

- every plate appearance outcome,
- all steal-home plays,
- caught stealing,
- lower-priority steals only when the target duration has room.

Use `--include-all-play-types` only if you want every indexed GameChanger play. Render scripts anchor timing to `video_offset_sec` when available so they keep the outcome pitch/play instead of using long GameChanger clip durations.

If individual clips start too late, use targeted pre-roll overrides instead of globally lengthening every play:

```bash
.venv/bin/python gc_make_condensed_game.py GAME_JSON \
  --output gc_render_test/condensed_game.mp4 \
  --scorebug \
  --description-overrides gc_render_test/condensed_game_description_review_cleaned.md \
  --extra-start-play-indexes 21,22 \
  --extra-start-buffer 18
```

The full condensed render re-encodes audio in the overlay pass (`aac`, `160k`, `aresample=async=1:first_pts=0`). Do not post-process with global fade filters; that can accidentally mute the whole output.

Condensed game also supports `--scorebug`, which burns in centered top and bottom overlays. The implementation generates transparent PNG overlays with Pillow, then composites them with ffmpeg's `overlay` filter because this local ffmpeg build does not include `drawtext`, `subtitles`, or `ass`. Defaults are:

- top center scorebug with inferred half inning, cumulative score, and occupied-base diamond,
- scorebug accent and active bases use Tigers orange `rgb(234, 117, 38)`,
- bottom center simplified play text uses smaller white letters with black stroke and no caption box,
- fetched team label defaults to `TIG`,
- opponent label defaults to a 4-character abbreviation from the opponent name,
- override labels with `--team-label` and `--opponent-label`.
- override generated captions with `--description-overrides REVIEW.md`; the script reads `Proposed:` lines by selected-play number.

Before rerendering overlays, generate caption review text without rendering video:

```bash
.venv/bin/python gc_make_condensed_game.py GAME_JSON \
  --output gc_render_test/condensed_game.mp4 \
  --descriptions-only \
  --descriptions-output gc_render_test/condensed_game_description_review.md
```

## Segment Cache

`gc_common.make_reel` caches each cut segment by:

- source URL or local file identity,
- exact start/end times,
- reencode setting.

The cache is used before ffmpeg reads the source. If a matching cached MP4 exists, the script hard-links or copies it into the temporary concat directory. A repeated render with identical timing should be fast and should not re-read the GameChanger HLS stream.

To inspect cache size:

```bash
find gc_render_cache/segments -type f -name '*.mp4' | wc -l
du -sh gc_render_cache/segments
```

## Verification

Compile scripts:

```bash
.venv/bin/python -m py_compile \
  gc_common.py \
  gc_fetch_game.py \
  gc_make_player_reels.py \
  gc_make_highlight_reel.py \
  gc_make_condensed_game.py \
  gc_make_full_game.py \
  gc_upload_youtube.py
```

Probe outputs:

```bash
for f in gc_render_test/player_reels/*.mp4; do
  printf '%s\t' "$f"
  ffprobe -v error -show_entries format=duration,size \
    -of default=noprint_wrappers=1:nokey=1 "$f" | paste -sd '\t' -
done
```

Check for active render processes after interruptions:

```bash
ps -eo pid,ppid,command | rg 'gc_make_.*reel|gc_make_condensed_game|ffmpeg|tee gc_render_test/logs' || true
```

## Known Game Notes

- Oliver Clark #9 should include a first-base putout on the play:
  `Player b966a7ae grounds out, pitcher Marko Burger #1 to first baseman Oliver Clark #9.`
- Charlie Bellingham #11 should include his walk and steal, but not defensive mentions where another runner advances.
- Walks count as batter on-base moments.

## YouTube Upload

Upload script exists but still needs an end-to-end test. Standard render uploads include
`full_game_scorebug.mp4`, `condensed_game.mp4`, `highlight_reel.mp4`, and
`player_reels/*.mp4` when present:

```bash
.venv/bin/python gc_upload_youtube.py \
  --render-dir gc_render_test \
  --include-standard-renders \
  --game-json gc_output_test/79e5bb9f-f87e-4d02-96ba-6abb4e7777aa/game.json \
  --client-secrets client_secret.json \
  --token-file youtube_token.json \
  --privacy-status unlisted
```

With `--game-json` and no explicit `--description`/`--description-file`, the full-game
upload description follows the original Colab shape: `# Top/Bot inning` headers and
timestamped play summaries. Keep this as the default for the full-game upload.

Uploads create or reuse an unlisted playlist by default and add every uploaded video
to it. The generated title format is:
`Tigers vs Cortlandt Nationals — June 19 '26` for home games and
`Tigers @ Cortlandt Nationals — June 19 '26` for away games.

Use `--playlist-title` to override the whole name, `--playlist-team-name` to change
the first team in the generated name, or `--no-playlist` to skip playlist updates.
The OAuth scope is `https://www.googleapis.com/auth/youtube`; old upload-only
`youtube_token.json` files will need a browser reauthorization.

Keep `client_secret.json` and `youtube_token.json` out of version control and out of logs.
