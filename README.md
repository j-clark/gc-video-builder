# GameChanger Video Builder

Tools for fetching GameChanger baseball metadata and building short-form videos from archived game streams:

- player reels
- team highlight reels
- condensed games
- optional burned-in scorebug and play captions
- optional YouTube uploads

The scripts are designed for youth baseball scoring data, where timestamps are useful but imperfect. Render commands include buffers, targeted timing overrides, and a reusable segment cache so repeated test renders do not keep rereading the GameChanger stream.

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` on `PATH`
- A GameChanger token with access to the team/game data

Install Python dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-gc.txt
```

Set credentials and IDs through environment variables:

```bash
export GC_TOKEN='...'
export GC_TEAM_ID='...'
export GC_PUBLIC_TEAM_ID='...'
```

Do not commit tokens, CloudFront cookies, YouTube OAuth files, HAR captures, fetched game data, rendered videos, or segment caches. `.gitignore` is set up for these.

## Fetch Game Data

Fetch a specific game:

```bash
.venv/bin/python gc_fetch_game.py \
  --event-id GAME_EVENT_ID \
  --out-dir gc_output
```

If `--event-id` is omitted, the script picks the latest completed game for `GC_TEAM_ID`.

The fetch creates a per-game folder containing:

- `game.json`
- raw stream events
- raw clip search results
- play indexes as CSV/Markdown

Most render commands take the fetched `game.json` path as their first argument.

## Condensed Game

Basic render:

```bash
.venv/bin/python gc_make_condensed_game.py gc_output/GAME_EVENT_ID/game.json \
  --output gc_render/condensed_game.mp4 \
  --reencode
```

With scorebug, reviewed captions, and targeted pre-roll for known-late plays:

```bash
.venv/bin/python gc_make_condensed_game.py gc_output/GAME_EVENT_ID/game.json \
  --output gc_render/condensed_game.mp4 \
  --plays-output gc_render/condensed_game.plays.txt \
  --reencode \
  --scorebug \
  --description-overrides gc_render/condensed_game_description_review_cleaned.md \
  --extra-start-play-indexes 21,22 \
  --extra-start-buffer 18
```

The condensed-game default selection includes:

- every plate appearance outcome
- steal-home plays
- caught stealing
- lower-priority steals only when target runtime has room

Scorebug renders include inning, score, occupied bases, and simplified play captions. The default team label is `TIG`; override labels with `--team-label` and `--opponent-label`.

## Caption Review

Before burning captions into a full render, generate a review file:

```bash
.venv/bin/python gc_make_condensed_game.py gc_output/GAME_EVENT_ID/game.json \
  --output gc_render/condensed_game.mp4 \
  --descriptions-only \
  --descriptions-output gc_render/condensed_game_description_review.md
```

Edit the `Proposed:` lines, then pass the file back with `--description-overrides`.

## Highlight Reel

```bash
.venv/bin/python gc_make_highlight_reel.py gc_output/GAME_EVENT_ID/game.json \
  --output gc_render/highlight_reel.mp4 \
  --reencode \
  --scorebug
```

The default highlight selection is curated rather than a raw play dump. It prioritizes extra-base hits, run-scoring plate appearances, steals of home, pitching strikeouts, defensive outs, double plays, caught stealing, and any GameChanger exceptional-play clips.

## Player Reels

Render all players:

```bash
.venv/bin/python gc_make_player_reels.py gc_output/GAME_EVENT_ID/game.json \
  --players all \
  --out-dir gc_render/player_reels \
  --reencode \
  --scorebug
```

Render selected players:

```bash
.venv/bin/python gc_make_player_reels.py gc_output/GAME_EVENT_ID/game.json \
  --players 9 11 \
  --out-dir gc_render/player_reels \
  --reencode \
  --scorebug
```

Player reels are intentionally condensed:

- batters: last pitch/play only when they reach base
- runners: plays where they advance
- pitchers: strikeout pitches or pitches where they record an out
- fielders: out-producing defensive plays involving that player

Player reels use the same `plays_to_segments` timing path as the highlight reel. Shared plays should cut the same way in both outputs; the default long-clip pre-roll is `18s`.

## Render Cache

Render scripts use `gc_render_cache/segments` by default. The cache key includes the source, start/end times, and re-encode setting. Re-running with the same timing should reuse local segment files instead of rereading the GameChanger HLS stream.

Disable cache only when debugging cache behavior:

```bash
--no-cache
```

## YouTube Upload

The upload script supports unlisted uploads using the YouTube Data API:

```bash
.venv/bin/python gc_upload_youtube.py gc_render/player_reels/*.mp4 \
  --client-secrets client_secret.json \
  --token-file youtube_token.json \
  --privacy-status unlisted
```

Keep `client_secret.json` and `youtube_token.json` local.

## Verification

Compile all scripts:

```bash
.venv/bin/python -m py_compile \
  gc_common.py \
  gc_fetch_game.py \
  gc_make_player_reels.py \
  gc_make_highlight_reel.py \
  gc_make_condensed_game.py \
  gc_upload_youtube.py
```

Probe a render:

```bash
ffprobe -v error -show_entries format=duration,size \
  -of default=noprint_wrappers=1:nokey=1 gc_render/condensed_game.mp4
```

Check for active render processes:

```bash
ps -eo pid,ppid,command | rg 'gc_make_.*reel|gc_make_condensed_game|ffmpeg'
```
