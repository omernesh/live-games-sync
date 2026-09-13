# Live Games Calendar Sync 🏀⚽

Automatically syncs Israeli TV sports broadcasts (soccer & basketball) to your Google Calendar — no duplicates, no women's/youth sports, just men's adult games worth watching.

## How It Works

1. **Scrapes the Telesport API** — fetches all sports broadcasts scheduled on Israeli TV (yes, HOT, etc.), no API key needed
2. **Filters** — keeps only men's adult soccer and basketball; excludes women's/youth matches, WNBA, NBA Summer League, Australian leagues, draws, and lotteries
3. **Deduplicates** — two-layer check against your calendar AND a local state file, so the same game never gets created twice even when Telesport lists it under different names
4. **Creates events** — with proper channel names, 2-hour durations, and Israel timezone
5. **Reports** — prints a summary listing every event created in the run

Runs standalone or via cron — your calendar stays fresh automatically.

## Quick Start

### Prerequisites

- Python 3.11+
- `gws` CLI (Google Workspace CLI) configured with access to a Google Calendar
- `curl` (used to fetch the Telesport API)

### Create a calendar

1. Create a Google Calendar (e.g. "Live Games")
2. Copy its **Calendar ID** (Settings → Calendar → "Calendar ID", format like `abc123@group.calendar.google.com`)
3. Configure it (choose one):
   - **Config file** (recommended):
     ```bash
     mkdir -p ~/.config/live-games-sync
     cat > ~/.config/live-games-sync/config.json <<'EOF'
     {
       "calendar_id": "abc123@group.calendar.google.com"
     }
     EOF
     ```
   - **Environment variable**:
     ```bash
     export LIVE_GAMES_CALENDAR_ID="abc123@group.calendar.google.com"
     ```

### Run

```bash
# Clone the repo
git clone https://github.com/omernesh/live-games-sync.git
cd live-games-sync

# Dry run first — shows what WOULD be created, writes nothing
python3 live-games-sync.py --dry-run

# If it looks good, run for real
python3 live-games-sync.py
```

## Usage

```bash
# Sync next 7 days (default)
python3 live-games-sync.py

# Preview changes without writing anything
python3 live-games-sync.py --dry-run

# Custom lookahead window: sync next 3 days
python3 live-games-sync.py --days 3

# One-off duplicate sweep over a wide window (no creates)
python3 live-games-sync.py --cleanup-only --since 2026-08-15 --until 2026-10-31
```

### Duplicate Cleanup

Every run also collapses **exact duplicate events** already on the calendar
(same start minute + same normalized channel + same game fingerprint, e.g.
copies written by another calendar tool). The canonical copy is kept —
preference: no description → mapped location ("(yes #NN)") → oldest — and the
rest are deleted. Genuinely different games sharing a slot are never affected.
Use `--cleanup-only` for a one-off wide-window sweep.

### Output

```
📋 Live Games Sync Report
━━━━━━━━━━━━━━━━━━━
• Scanned: 7 days
• Existing in calendar: 5
• Telesport candidates: 82 unique
  → Created: 2
    • 2026-08-24 21:00 — מכבי תל אביב - הפועל ירושלים (5SPORT (yes #55))
    • 2026-08-25 19:30 — בית"ר ירושלים - מכבי חיפה (ספורט 2 HD (yes #52))
  → Skipped: 80
```

Detailed per-game skip/create reasons go to stderr.

### Daily Automation

```bash
# Run twice daily (10:00 and 22:00 IL — Telesport finalizes listings late morning)
crontab -e
# Add:
0 10,22 * * * cd /path/to/live-games-sync && python3 live-games-sync.py
```

The script is idempotent — running it repeatedly is always safe.

## What Gets Added

| Criteria | Detail |
|----------|--------|
| **Sports** | Soccer (branch_id=1) + Basketball (branch_id=2) |
| **Gender/Age** | Men's adult only — excludes women's/youth keywords |
| **Leagues excluded** | WNBA, NBA Summer League ("ב'" marker), Australian leagues (city pair + keyword checks) |
| **Event type** | Actual games only — excludes draws, lotteries |
| **Duration** | 2 hours per event |
| **Timezone** | Asia/Jerusalem (Israel summer time) |

## Deduplication

Two layers prevent duplicates even when the same game is listed with different names:

1. **Name fingerprint** — strips prefixes (`גמר:`, `משחק`, `כדורגל :`/`כדורסל :`), sponsor names (e.g. "ארמני מילאנו" → "מילאנו"), game numbers ("מ. 2"), and sorts team names so order doesn't matter
2. **Time+location key** — matches games at the same time (±15 min) on the same channel (channels normalized, so `ספורט 5` and `5SPORT (yes #55)` resolve to the same key)

On top of the calendar cross-check, a **local state file** records every created event fingerprint, protecting against Google Calendar API eventual consistency. Stale entries (events no longer in the calendar) are purged automatically.

## Channel Mapping

Telesport channel names → yes channel display names:

| Telesport Name | Calendar Location |
|---------------|-------------------|
| ספורט 1 | ספורט 1 HD (yes #51) |
| ספורט 2 | ספורט 2 HD (yes #52) |
| ספורט 3 | ספורט 3 HD (yes #53) |
| ספורט 4 | ספורט 4 HD (yes #54) |
| ספורט 5 | 5SPORT (yes #55) |
| ספורט 5+ | 5LIVE HD (yes #58) |
| 5PLUS | 5PLUS HD (yes #56) |
| 5STARS | 5STARS (yes #59) |
| 5SPORT 4K | 5SPORT 4K (yes #55) |
| ספורט 6 | ספורט 6 |
| ONE | ONE |
| כאן 11 | כאן 11 (yes #11) |
| קשת 12 | קשת 12 (yes #12) |
| רשת 13 | רשת 13 (yes #13) |
| ערוץ 14 | ערוץ 14 (yes #14) |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVE_GAMES_CALENDAR_ID` (env) / `calendar_id` (config) | — | Google Calendar ID to sync to |
| `LIVE_GAMES_STATE_PATH` (env) / `state_path` (config) | `~/.local/share/live-games-sync/state.json` | Local dedup state file location |
| `~/.config/live-games-sync/lists.json` | built-in defaults | Editable team lists — skip / protect / league exclusions |
| `LOOKAHEAD_DAYS` | 7 | How many days ahead to scan |
| `EVENT_DURATION` | 2 hours | Default event duration |
| `EXCLUDE_KEYWORDS` | Hebrew keywords | Women's/youth sports to skip |
| `NON_GAME_KEYWORDS` | Draw/lottery terms | Non-game events to skip |
| `TEAM_ALIASES` | Sponsor names | Team name variants to normalize |

## Editable Team Lists

Team preferences — which clubs to skip, which are never filtered out, and per-league exclusion lists — live in a single editable file, read at startup:

```
~/.config/live-games-sync/lists.json
```

```json
{
  "skip_teams":            ["Some Club"],
  "protected_teams":       ["A Club to Never Filter"],
  "south_american_teams":  [],
  "mls_teams":             [],
  "turkish_teams":         [],
  "dutch_teams":           [],
  "gulf_teams":            [],
  "allowed_israeli_teams": []
}
```

- One team per line — adding or removing a team is a one-line change, applied on the next run (no script edits or restarts).
- `skip_teams` — skip any game involving these teams. `protected_teams` — never filtered out (bypass all exclusion lists). The `*_teams` keys are league-wide exclusions; `allowed_israeli_teams` is the keep-list for Israeli league games.
- If the file is missing or invalid, the script warns and falls back to its built-in defaults — a bad edit can never crash a sync run.

## Data Source

[Telesport](https://m.telesport.co.il/) provides a free API of Israeli TV sports broadcasts. No API key needed.

```
GET https://m.telesport.co.il/api/broadcasts?date=2026-06-13
```

Returns a JSON array of broadcasts with title, channel, time, sport category, and date.

## License

MIT
