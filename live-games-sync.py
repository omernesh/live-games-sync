#!/usr/bin/env python3
"""
Live Games Calendar Sync — fetch sports broadcasts from Telesport API,
filter for male adult soccer & basketball, and add missing events to
Google Calendar "Live Games".

Idempotent: checks existing calendar events before creating new ones.
Safe: never duplicates, never overwrites.

Usage:
  python3 live-games-sync.py          # sync next 7 days
  python3 live-games-sync.py --days 3 # sync next 3 days
  python3 live-games-sync.py --dry-run  # preview only, no creates
  python3 live-games-sync.py --cleanup-only --since 2026-08-15 --until 2026-10-31
                                        # one-off duplicate sweep (no creates)
"""

import json, os, subprocess, sys, time
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────
# Resolution order: environment variable → config file → placeholder.
#
# Config file: ~/.config/live-games-sync/config.json
#   {
#     "calendar_id": "abc123@group.calendar.google.com",
#     "state_path": "/path/to/state.json"
#   }
# Env vars: LIVE_GAMES_CALENDAR_ID, LIVE_GAMES_STATE_PATH

CONFIG_PATH = os.path.expanduser("~/.config/live-games-sync/config.json")

def _load_config() -> dict:
    cfg = {}
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    return cfg

_CONFIG = _load_config()

# Target Google Calendar ID (see README: "Create a calendar")
LIVE_GAMES_CALENDAR_ID = (
    os.environ.get("LIVE_GAMES_CALENDAR_ID")
    or _CONFIG.get("calendar_id")
    or "YOUR_CALENDAR_ID@group.calendar.google.com"
)

# Local state file path — prevents duplicate creation across runs
LOCAL_STATE_PATH = os.path.expanduser(
    os.environ.get("LIVE_GAMES_STATE_PATH")
    or _CONFIG.get("state_path")
    or "~/.local/share/live-games-sync/state.json"
)

TELESPORT_API = "https://m.telesport.co.il/api/broadcasts?date={date}"
LOOKAHEAD_DAYS = 7
EVENT_DURATION = timedelta(hours=2)

# Telesport media_name → Calendar location (yes channel display name)
# Keys are checked left-to-right, both exact and substring matches.
CHANNEL_MAP = {
    "1":      "yes 1 (yes #1)",
    "11":     "כאן 11 (yes #11)",
    "12":     "קשת 12 (yes #12)",
    "13":     "רשת 13 (yes #13)",
    "14":     "ערוץ 14 (yes #14)",
    "51":     "ספורט 1 HD (yes #51)",
    "52":     "ספורט 2 HD (yes #52)",
    "53":     "ספורט 3 HD (yes #53)",
    "54":     "ספורט 4 HD (yes #54)",
    "55":     "5SPORT (yes #55)",
    "56":     "5PLUS HD (yes #56)",
    "57":     "5GOLD (yes #57)",
    "58":     "5LIVE HD (yes #58)",
    "59":     "5STARS (yes #59)",
    "61":     "Eurosport 1 (yes #61)",
    "62":     "Eurosport 2 (yes #62)",
    "80":     "כאן חינוכית (yes #80)",
}

# Fallback name-based channel mapping (for channels that don't have yes numbers)
NAME_CHANNEL_MAP = {
    "ספורט 1":    "ספורט 1 HD (yes #51)",
    "ספורט 2":    "ספורט 2 HD (yes #52)",
    "ספורט 3":    "ספורט 3 HD (yes #53)",
    "ספורט 4":    "ספורט 4 HD (yes #54)",
    "ספורט 5":    "5SPORT (yes #55)",
    "ספורט 5+":   "5LIVE HD (yes #58)",
    "5PLUS":       "5PLUS HD (yes #56)",
    "5GOLD":       "5GOLD (yes #57)",
    "5LIVE":       "5LIVE HD (yes #58)",
    "5STARS":      "5STARS (yes #59)",
    "5SPORT":      "5SPORT (yes #55)",
    "ספורט 6":     "ספורט 6",
    "ONE":         "ONE",
    "5 סטארס":    "5STARS (yes #59)",
    "5 גולד":     "5GOLD (yes #57)",
    "5 ספורט":    "5SPORT (yes #55)",
    "ספורט 4K":   "5SPORT 4K (yes #55)",
    "כאן 11":      "כאן 11 (yes #11)",
    "קשת 12":     "קשת 12 (yes #12)",
    "רשת 13":     "רשת 13 (yes #13)",
    "ערוץ 14":    "ערוץ 14 (yes #14)",
    "ספורט 5 מקס": "5SPORT 4K (yes #55)",
}

# Keywords that indicate women's or youth sports → exclude
EXCLUDE_KEYWORDS = [
    "נשים", "נקבה", "נוער", "צעירות", "בנות", "ילדות",
    "עד גיל", "גיל 16", "גיל 18", "גיל 20",
    "עד 19", "עד 20", "עד 21",
    "בית ספר", "תלמידות", "תלמידים",
    "wnba", "u19", "u20", "u21", "מכביה",
    # Israeli 3rd division (ליגה א') — not interesting (Omer, 2026-09-05).
    # Both quote variants since Telesport mixes ' and ׳.
    "ליגה א'", "ליגה א׳",
    # Israeli 2nd division (ליגה לאומית) — not interesting (Omer, 2026-09-06).
    # Covers "ערוץ הקיבוץ" multi-game multiplexes with no team names.
    "ליגה לאומית",
    # Israeli High School Basketball League (ליגת התיכונים) — not interesting
    # (Omer, 2026-09-06). Multiplex "ישיר!" broadcasts with no team names.
    "ליגת התיכונים",
]

# WNBA-specific team nicknames — basketball only (branch_id=2). These are
# uniquely WNBA team names; no NBA or Euroleague team shares them.
WNBA_TEAM_NICKNAMES = [
    "אייסז", "פיבר", "ליברטי", "לינקס", "מרקורי",
    "סקיי", "סאן", "וינגס", "דרים", "סטורם",
    "מיסטיקס", "ספארקס",
]

# Australian league team cities — exclude games between two Australian teams
AUSTRALIAN_TEAM_CITIES = [
    "סידני", "מלבורן", "אדלייד", "בריזביין", "בריזבן",
    "פרת", "קנברה", "וולינגטון", "אוקלנד", "גולד קוסט",
    "ווסטרן", "ניוקאסל", "סנטרל קוסט", "וסטרן",
    "מקארתור", "וולינגטון",
    # NBL (Australian basketball) spellings — added 2026-09-21 (Omer:
    # "Skip: Australian basketball"). Telesport spells Brisbane "בריסביין"
    # and Illawarra "אילוורה" — neither matched the soccer-spelled entries.
    "בריסביין", "אילוורה", "קאירנס", "קיירנס", "טסמניה",
    "טזמניה", "ברייקרס", "טאיפאנס", "ג'קג'מפרס", "ג׳קג׳מפרס",
]

# Australian league indicators in title
AUSTRALIAN_LEAGUE_KEYWORDS = [
    "איי-ליג", "ליגה אוסטרלית", "אוסטרליה",
    "A-League", "ALeague", "a league",
]

# Known Australian team full names (not just city-based)
AUSTRALIAN_TEAM_NAMES = [
    "סטירלינג",  # Stirling in Western Australia or other Australian context
    "היידלברג",  # Heidelberg United (Australian)
    "סאות מלבורן",  # South Melbourne
    "וולונגונג",
    "וולפס",
    # NBL teams — single-side name matches (added 2026-09-21)
    "בריסביין בולטס", "אילוורה הוקס", "ניו זילנד ברייקרס",
]

# ── Omer's soccer content filters (added 2026-08-30) ─────────────────
# NOTE: the live team lists are loaded at startup from the editable file
# ~/.config/live-games-sync/lists.json — edit THAT file to add/remove
# teams (one line per team; no script changes needed). The literals in
# this file are fallback defaults, used only when the lists file is
# missing or unreadable.
#
# Teams to always skip — if EITHER team in a title matches, the game is
# skipped (Omer's rule: these teams aren't interesting in any fixture).
SKIP_TEAMS = [
    "שטרסבורג", "לאנס", "פאלרמו", "מאנטובה",
    "היברניאן", "הארטס", "קליארי", "ורונה",
    "סטוק", "נוריץ'", "ססואולו", "פרוסינונה",
    "אודינזה", "ונציה", "זלצבורג", "ראפיד וינה",
    "ברנלי", "מידלסברו", "פלקירק", "ריינג'רס",
    "דיז'ון", "סט. אטיין", "בנפיקה", "אשטוריל",
    "פארמה", "קרמונזה", "טורינו", "מונזה",
    "לה האבר", "ברסט", "לינגבי",  # added 2026-09-04 (Le Havre, Brest, Lyngby)
    "קובנטרי", "אוסנאברוק",  # added 2026-09-13 (Coventry, Osnabrück)
    "ליל", "טרואה", "שפילד יונייטד", "ברייטון",  # added 2026-09-13 (Lille, Troyes, Sheffield Utd, Brighton)
    "פמאליקאו", "ספורטינג ליסבון",  # added 2026-09-13 (Famalicão, Sporting Lisbon)
    "מידטיילנד", "ברונדבי", "מץ", "גנואה", "סודטירול", "אספניול", "ראיו ואייקאנו",  # added 2026-09-13 (Midtjylland, Brøndby, Metz, Genoa, Südtirol, Espanyol, Rayo Vallecano)
]

# Protected teams — games involving these are NEVER filtered out (they
# bypass SKIP_TEAMS / region exclusions / the Israeli restriction).
# Omer, 2026-09-13: "Never remove Manchester united games" (after
# 'מנצ'סטר יונייטד - ברייטון' was removed by the Brighton skip).
PROTECTED_TEAMS = [
    "מנצ'סטר יונייטד", "מנצ'סטר יוניטד",
    "ברצלונה", "ריאל מדריד", "באיירן מינכן",
    "מכבי תל אביב", "הפועל פתח תקווה",
]

# South American soccer — entire region excluded (team names in Hebrew).
# Matched per team-half of the title to avoid cross-matches.
SOUTH_AMERICAN_TEAMS = [
    # Brazil
    "פלמנגו", "פלמיירס", "פלמייראס", "קורינתיאנס", "סאו פאולו",
    "סנטוס", "סאנטוס", "פלומיננזה", "פלומיננסה", "ואסקו",
    "גרמיו", "אינטרנסיונל", "בוטאפוגו", "קרוזיירו", "אתלטיקו מיניירו",
    "אתלטיקו פארננסה", "באיה", "פורטלזה", "קויאבה", "ספורט רסיפה",
    "נאוטיקו", "סיארה", "ויטוריה", "ז'ובנטודה", "ברגנטינו",
    "קוריטיבה", "גויאס", "אמריקה מיניירו", "צ'אפקואנסה", "מיראסול",
    "רד בול ברגנטינו", "אתלטיקו גויאניינסה",
    # Argentina
    "בוקה ג'וניורס", "ריבר פלייט", "ראסינג קלוב", "אינדפנדיינטה",
    "סן לורנסו", "ניואל'ס", "אסטודיאנטס", "רוסאריו סנטראל",
    "לנוס", "באנפילד", "ארחנטינוס ג'וניורס", "טאלרס", "גודוי קרוז",
    "דפנסה וחוסטיסיה", "טיגרה", "הורקאן", "חימנסיה", "אוניון",
    "סנטראל קורדובה", "בלגראנו", "פלטנסה", "אינסטיטוטו",
    # Uruguay / Colombia / Chile / Paraguay / Ecuador / Peru / Bolivia / Venezuela
    "פניארול", "נסיונל מונטווידאו", "דאנוביו", "דפנסור ספורטינג",
    "אתלטיקו נסיונל", "מיונאריוס", "אמריקה קאלי", "ג'וניור ברנקייה",
    "קולו-קולו", "אוניברסידד דה צ'ילה", "אוניברסידד קאתוליקה",
    "סרו פורטניו", "אולימפיה אסונסיון", "ליברטד",
    "ליגה דה קיטו", "ברצלונה גואיאקיל", "אמלק", "אינדפנדיינטה דל ואיה",
    "אליאנסה לימה", "אוניברסיטריו", "ספורטינג קריסטל",
    "בוליבר", "סטרונגסט", "קראקאס", "דפורטיבו טאצ'ירה",
]

# MLS — entire league excluded (team names in Hebrew).
MLS_TEAMS = [
    "אינטר מיאמי", "מיאמי", "לוס אנג'לס", "גלאקסי", "ניו יורק",
    "אטלנטה יונייטד", "סיאטל סאונדרס", "פורטלנד טימברס", "דאלאס",
    "סט. לואיס סיטי", "שארלוט", "אוסטין", "נאשוויל", "אורלנדו סיטי",
    "טורונטו", "ונקובר", "מונטריאול", "שיקגו פייר", "קולומבוס",
    "סינסינטי", "פילדלפיה יוניון", "ניו אינגלנד", "יוסטון",
    "קנזס סיטי", "מינסוטה", "סולט לייק", "קולורדו רפידס", "סן חוזה",
    "סן דייגו",
]

# Turkish Süper Lig — entire league excluded (team names in Hebrew).
TURKISH_TEAMS = [
    "גלאטסראיי", "פנרבחצ'ה", "בשיקטש", "טרבזונספור",
    "איסטנבול בשאקשהיר", "בשאקשהיר", "אדנה דמירספור", "אנטליאספור",
    "אלניאספור", "סיוואספור", "קסימפאשה", "גזיאנטפ", "קוניאספור",
    "צ'איקור ריזספור", "קייסריספור", "סמסונספור", "האטייספור",
    "איופספור", "גוזטפה", "פאטיח קרגומרוק", "אנקרגוצ'ו",
    "בורסאספור", "דניזליספור", "ארזורומספור", "אמד SK",
    "גנצ'לרבירליגי", "אוסמנליספור", "אקהיסאר",
]

# Dutch Eredivisie — entire league excluded (team names in Hebrew).
# Added 2026-09-13 (Omer: "skip dutch soccer games like ווילם"). Telesport's
# daily template repeats the same Eredivisie fixtures, so they surface
# constantly; all Dutch clubs are treated the same.
DUTCH_TEAMS = [
    "אייאקס", "פ.ס.וו", "איינדהובן", "פיינורד", "זוולה",
    "ספרטה רוטרדם", "רוטרדם", "ווילם", "וילם",
    "אוטרכט", "טוונטה", "חרונינגן", "הירנביין", "נמחן", "ניימכן", "ניימיכן",
    "הרקלס", "אלקמאר", "גו אהד", "פורטונה סיטארד",
    "ואלוויק", "ואלבייק", "נאק ברדה", "אקסלסיור", "טלסטאר",
    "פולנדם", "פולינדם", "דן האג", "קמבור",
]

# Saudi/Gulf soccer — entire group excluded (team names in Hebrew).
# Added 2026-09-13 (Omer: "saudi soccer games, like אל עין, אל נאסר").
# Covers Saudi Pro League, UAE, Qatar and Kuwait clubs. Hyphen variants
# ("אל-עין") are matched via hyphen-normalized team halves.
GULF_TEAMS = [
    # Saudi Pro League
    "אל הילאל", "אל נאסר", "אל איתיחאד", "אל אהלי", "אל שאבאב",
    "אל פתח", "אל טאוון", "אל איתיפאק", "אל ריאד", "אל ח'ליג'",
    "אל רائد", "אל ווחדה", "אל חזם", "אל קדסיה", "דמאק",
    # UAE
    "אל עין", "אל ג'זירה", "אל שארג'ה", "עג'מאן", "אל בטאה",
    "חור פקאן", "בני יאס",
    # Qatar
    "אל סאד", "אל דוחיל", "אל ריאן", "אל רייאן", "אל גרפא",
    "אל ערבי", "אום סלאל",
    # Kuwait
    "אל כווית", "אל סלמיה", "כזמא",
]

# Israeli soccer: only keep games involving these clubs (quote chars
# normalized away — Telesport mixes " and ״).
ALLOWED_ISRAELI_TEAMS = [
    "מכבי תל אביב", "הפועל תל אביב", "מכבי חיפה",
    "ביתר ירושלים", "הפועל פתח תקווה", "מכבי פתח תקווה",
]

# Indicators that a game is Israeli (clubs/prefixes unique to Israel).
# Titles are checked with quote chars removed.
ISRAELI_TEAM_KEYWORDS = [
    "מכבי", "הפועל", "ביתר", "בני סכנין", "בני יהודה",
    "מ.ס", "עירוני", "נס ציונה", "אשדוד", "חדרה", "נתניה",
    "קרית שמונה", "קריית שמונה", "עכו", "רעננה", "עפולה",
    "כפר סבא", "ראשון לציון", "הרצליה", "כפר קאסם", "טבריה",
    "סכנין", "ריינה", "אשקלון", "לוד", "אום אל פאחם",
]


# ── Editable team lists loader ───────────────────────────────────────
# Live team lists come from ~/.config/live-games-sync/lists.json (the
# "skip list + protected teams in one editable file" consolidation,
# 2026-09-13). Edit that file for team tweaks; the literals above are
# fallback defaults only. A missing or corrupt lists file never crashes
# the sync — it warns to stderr and keeps the defaults.

LISTS_FILE = os.path.expanduser("~/.config/live-games-sync/lists.json")

_EDITABLE_LIST_KEYS = {
    "skip_teams": "SKIP_TEAMS",
    "protected_teams": "PROTECTED_TEAMS",
    "south_american_teams": "SOUTH_AMERICAN_TEAMS",
    "mls_teams": "MLS_TEAMS",
    "turkish_teams": "TURKISH_TEAMS",
    "dutch_teams": "DUTCH_TEAMS",
    "gulf_teams": "GULF_TEAMS",
    "allowed_israeli_teams": "ALLOWED_ISRAELI_TEAMS",
    "australian_cities": "AUSTRALIAN_TEAM_CITIES",
    "australian_teams": "AUSTRALIAN_TEAM_NAMES",
}


def _load_editable_lists() -> None:
    """Override team lists from the editable JSON file (if present)."""
    try:
        with open(LISTS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print(f"[lists] {LISTS_FILE} missing — using built-in defaults",
              file=sys.stderr)
        return
    except Exception as exc:
        print(f"[lists] WARNING: bad {LISTS_FILE} ({exc}) — using built-in "
              f"defaults", file=sys.stderr)
        return
    for key, gname in _EDITABLE_LIST_KEYS.items():
        value = data.get(key)
        if isinstance(value, list):
            globals()[gname] = [str(x).strip() for x in value if str(x).strip()]


_load_editable_lists()


def is_australian_game(title: str) -> bool:
    """Check if a title represents an Australian league game."""
    # Check for explicit league keywords
    title_lower = title.lower()
    for kw in AUSTRALIAN_LEAGUE_KEYWORDS:
        if kw in title_lower or kw in title:
            return True
    # Check for known Australian team names (singular match is enough)
    for name in AUSTRALIAN_TEAM_NAMES:
        if name in title:
            return True
    # Check if both team names contain Australian city names
    parts = re.split(r'\s*-\s*', title, maxsplit=1)
    if len(parts) == 2:
        team_a, team_b = parts[0].strip(), parts[1].strip()
        city_a = any(city in team_a for city in AUSTRALIAN_TEAM_CITIES)
        city_b = any(city in team_b for city in AUSTRALIAN_TEAM_CITIES)
        if city_a and city_b:
            return True
    return False


def _normalize_quotes(s: str) -> str:
    """Normalize Hebrew geresh/quote variants (Telesport mixes \" and ״)."""
    return s.replace("״", "").replace('"', "").strip()


def _team_halves(title: str) -> list:
    """Split a normalized title into its two team halves."""
    parts = re.split(r"\s*-\s*", normalize_title(title), maxsplit=1)
    if len(parts) == 2:
        return [parts[0].strip(), parts[1].strip()]
    return [normalize_title(title)]


def is_skipped_team(title: str) -> bool:
    """True if EITHER team in the title is on Omer's skip list.

    Omer's rule: skip any game involving one of these teams, regardless
    of opponent (2026-08-30 clarification). Short entries (<=4 chars, no
    spaces — e.g. "האל", "מץ", "ראן", "ליון") match only as whole words,
    so they never trip on longer phrases like "ליגת האלופות", "עליון"
    or "איראן".
    """
    for team in _team_halves(title):
        for skip in SKIP_TEAMS:
            if len(skip) <= 4 and " " not in skip:
                if re.search(rf"(?<![א-ת]){re.escape(skip)}(?![א-ת])", team):
                    return True
            elif skip in team:
                return True
    return False


def has_protected_team(title: str) -> bool:
    """True if EITHER team in the title is on the protected list.

    Protected teams are never filtered out — they bypass SKIP_TEAMS,
    region exclusions and the Israeli restriction (Omer 2026-09-13:
    "Never remove Manchester united games"; extended same day to
    Barcelona, Real Madrid, Bayern Munich, Maccabi TLV, Hapoel PT).
    """
    for team in _team_halves(title):
        for name in PROTECTED_TEAMS:
            if name in team:
                # 'ברצלונה' must not protect Ecuador's Barcelona SC —
                # 'ברצלונה גואיאקיל' stays region-excluded.
                if "גואיאקיל" in team:
                    continue
                return True
    return False


def is_region_excluded(title: str) -> bool:
    """Check per team-half of the title against excluded league/region lists.

    Covers South America / MLS / Turkish Süper Lig (2026-08-30), Dutch
    Eredivisie and the Saudi/Gulf leagues (2026-09-13). Hyphen variants
    ("אל-עין" vs "אל עין") are handled by also matching a hyphen-normalized
    copy of the team half and the list entry.
    Only applies to soccer (caller gates on branch_id == 1).
    """
    for team in _team_halves(title):
        team_alt = team.replace("-", " ")
        for name in (SOUTH_AMERICAN_TEAMS + MLS_TEAMS + TURKISH_TEAMS
                     + DUTCH_TEAMS + GULF_TEAMS):
            if name in team or name.replace("-", " ") in team_alt:
                return True
    return False


def is_israeli_game(title: str) -> bool:
    """True if the title contains any Israeli club indicator."""
    t = _normalize_quotes(title)
    return any(kw in t for kw in ISRAELI_TEAM_KEYWORDS)


def has_allowed_israeli_team(title: str) -> bool:
    """True if the title contains one of Omer's allowed Israeli clubs."""
    t = _normalize_quotes(title)
    return any(team in t for team in ALLOWED_ISRAELI_TEAMS)


# Non-game events (draws, lotteries, press conferences) to always skip
# מסע"ת = מסיבת עיתונאים (pre-game presser show, not a game). Both quote
# variants included since Telesport mixes " and ״ (added 2026-09-02).
NON_GAME_KEYWORDS = [
    "הגרלת", "הגרלה", "גרלה",
    "מסע\"ת", "מסע״ת",
]

# Team name normalization: map known sponsor prefixes to empty (strip them)
TEAM_ALIASES = {
    "ארמני":       "",
    "אולימפיה":     "",
    "איברוסטאר":    "",
    "לנובו":        "",
    "חובנטוד":      "",
    "קלוב":         "",
}

# ── Helpers ───────────────────────────────────────────────────────────

IL_TZ = timezone(timedelta(hours=3))  # Asia/Jerusalem (UTC+3 during summer)


def log(msg):
    print(f"[live-games-sync] {msg}", file=sys.stderr)


def is_wnba(title: str) -> bool:
    """Check if a basketball game title matches a WNBA team nickname."""
    for nick in WNBA_TEAM_NICKNAMES:
        if nick in title:
            return True
    return False


def is_male_adult(title: str) -> bool:
    title_lower = title.lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in title_lower:
            return False
    return True


def is_game_event(title: str) -> bool:
    for kw in NON_GAME_KEYWORDS:
        if kw in title:
            return False
    return True


def normalize_title(title: str) -> str:
    t = title.strip()
    # Tournament-program prefixes (e.g. "טורניר הכנה בכדורסל: ") are also
    # stripped: Telesport rewrites listings between revisions (team matchup
    # with/without the tournament wrapper, times shifted by ~30 min), and
    # without this the same broadcast yields different fingerprints.
    # Loop until stable to handle stacked prefixes
    # ("כדורסל : טורניר הכנה בכדורסל: X - Y" → "X - Y").
    prefixes = ["גמר: ", "משחק ", "שידור חוזר: ",
                "כדורסל : ", "כדורסל: ",
                "כדורגל : ", "כדורגל: ",
                "טורניר הכנה בכדורסל: ", "טורניר הכנה: "]
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if t.startswith(prefix):
                t = t[len(prefix):]
                changed = True
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def strip_team_sponsors(team: str) -> str:
    words = team.split()
    filtered = [w for w in words if w not in TEAM_ALIASES]
    return " ".join(filtered) if filtered else team


def sort_teams(title: str) -> str:
    parts = re.split(r'\s*-\s*', title, maxsplit=1)
    if len(parts) == 2:
        a, b = parts[0].strip(), parts[1].strip()
        if a > b:
            return f"{b} - {a}"
    return title


def game_fingerprint(title: str) -> str:
    """Canonical dedup key for a game title.

    Strips: prefixes (גמר:), sponsor names, game numbers. Sorts teams.
    """
    t = normalize_title(title)
    # Strip sponsor names from each team side
    parts = re.split(r'\s*-\s*', t, maxsplit=1)
    if len(parts) == 2:
        a = strip_team_sponsors(parts[0].strip())
        b = strip_team_sponsors(parts[1].strip())
        t = f"{a} - {b}"
    t = sort_teams(t)
    t = re.sub(r',\s*מ\.\s*\d+', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def normalize_location_for_dedup(location: str) -> str:
    """
    Normalize a location string to a canonical channel identifier for dedup.

    Strip the descriptive part so '5SPORT (yes #55)' and 'ספורט 5'
    and 'ספורט 5 HD (yes #55)' all resolve to 'yes#55'.
    Falls back to running through telesport_to_location if no yes# found,
    then to the raw string.
    """
    m = re.search(r'\(yes #(\d+)\)', location)
    if m:
        return f"yes#{m.group(1)}"
    mapped = telesport_to_location(location)
    if mapped != location:
        m = re.search(r'\(yes #(\d+)\)', mapped)
        if m:
            return f"yes#{m.group(1)}"
    return location


def time_fingerprint(start_iso: str, location: str) -> str:
    """
    Create a time+location dedup key with ±15 min tolerance.

    Uses: date (YYYY-MM-DD) + rounded-to-15min time + normalized location.
    """
    dt = datetime.fromisoformat(start_iso)
    date_part = dt.strftime("%Y-%m-%d")
    minutes = (dt.minute // 15) * 15
    rounded = dt.replace(minute=minutes, second=0, microsecond=0)
    loc_normalized = normalize_location_for_dedup(location)
    return f"{date_part}T{rounded.strftime('%H:%M')}|{loc_normalized}"


def telesport_to_location(media_name: str) -> str:
    """Map Telesport channel name to calendar location string.

    Strategy:
    1. Try exact NAME_CHANNEL_MAP match
    2. Try NAME_CHANNEL_MAP substring match (longest key first)
    3. Fallback to raw media_name
    """
    raw = media_name.strip()

    # 1. Exact match
    if raw in NAME_CHANNEL_MAP:
        return NAME_CHANNEL_MAP[raw]

    # 2. Substring match — sort keys by length DESC to match "ספורט 5+ לייב" 
    #    before "ספורט 5", "Here" before "He", etc.
    for key in sorted(NAME_CHANNEL_MAP, key=len, reverse=True):
        if key in raw:
            return NAME_CHANNEL_MAP[key]

    # 3. Fallback
    return raw


def load_local_state() -> set:
    """Load the set of known event keys from local state file.
    Also prunes entries older than LOOKAHEAD_DAYS days.
    """
    import json
    state = set()
    if not os.path.exists(LOCAL_STATE_PATH):
        return state
    try:
        with open(LOCAL_STATE_PATH, "r") as f:
            data = json.load(f)
        now = datetime.now(IL_TZ)
        cutoff = (now - timedelta(days=LOOKAHEAD_DAYS + 1)).isoformat()
        for key, created_at in data.items():
            if created_at >= cutoff:
                state.add(key)
        return state
    except (json.JSONDecodeError, OSError) as e:
        log(f"Warning: cannot read local state: {e}")
        return state


def save_local_state(tfp: str):
    """Append a new time+location fingerprint to the local state file.
    Uses a helper file and atomic rename to prevent corruption.
    """
    import json
    now = datetime.now(IL_TZ)
    state = {}
    # Read existing
    if os.path.exists(LOCAL_STATE_PATH):
        try:
            with open(LOCAL_STATE_PATH, "r") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}
    # Add new entry
    state[tfp] = now.isoformat()
    # Atomic write
    tmp = LOCAL_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LOCAL_STATE_PATH)


def fetch_telesport(date_str: str) -> list:
    """Fetch broadcasts from Telesport API for a given date (YYYY-MM-DD)."""
    url = TELESPORT_API.format(date=date_str)
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "12",
             "-A", "Mozilla/5.0", "--compressed", url],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0 or not r.stdout.strip():
            log(f"  Telesport empty for {date_str}")
            return []
        data = json.loads(r.stdout)
        return data
    except Exception as e:
        log(f"  Error fetching {date_str}: {e}")
        return []


# ── Calendar Operations ───────────────────────────────────────────────

def get_existing_events(days: int = 7, since: str = "", until: str = "") -> list:
    """Fetch existing events from Live Games calendar.

    Optional `since`/`until` (YYYY-MM-DD) override the default
    (now-2d .. now+lookahead) window — used by --cleanup-only sweeps.
    """
    now = datetime.now(IL_TZ)
    lookahead = max(days, LOOKAHEAD_DAYS + 1)
    time_min = (f"{since}T00:00:00Z" if since
                else (now - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z"))
    time_max = (f"{until}T23:59:59Z" if until
                else (now + timedelta(days=lookahead)).strftime("%Y-%m-%dT23:59:59Z"))

    base_params = {
        "calendarId": LIVE_GAMES_CALENDAR_ID,
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": 2500,
    }

    try:
        # Fetch with bounded pagination — a single page may be silently
        # truncated (observed 2026-09-11: 32 of 162 events returned with a
        # nextPageToken that was never followed), which caused the cross-
        # check to miss existing events.
        items = []
        next_token = None
        for _ in range(10):
            params = dict(base_params)
            if next_token:
                params["pageToken"] = next_token
            r = subprocess.run(
                ["gws", "calendar", "events", "list",
                 "--params", json.dumps(params)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                log(f"gws events list failed: {r.stderr[:200]}")
                return []
            data = json.loads(r.stdout)
            items.extend(data.get("items", []))
            next_token = data.get("nextPageToken")
            if not next_token:
                break

        events = []
        for ev in items:
            summary = ev.get("summary", "")
            start = ev.get("start", {}).get("dateTime", "")
            end = ev.get("end", {}).get("dateTime", "")
            location = ev.get("location", "")
            events.append({
                "id": ev["id"],
                "summary": summary,
                "start": start,
                "end": end,
                "location": location,
                "description": (ev.get("description") or "").strip(),
                "created": ev.get("created", ""),
            })

        # ── Stale local state cleanup ──
        # Two-tier purge:
        #   1. Entry's event time is within the scan window but NOT in the calendar → always purge
        #   2. Entry is >2h old and NOT in the calendar → purge (catches entries for past events)
        try:
            import json as _json
            now = datetime.now(IL_TZ)
            if os.path.exists(LOCAL_STATE_PATH):
                with open(LOCAL_STATE_PATH) as f:
                    raw_state = _json.load(f)
                # Build set of actual calendar tfps
                actual_tfps = set()
                for ev in events:
                    actual_tfps.add(time_fingerprint(ev["start"], ev["location"]))
                # Determine scan window bounds from the timeMin/timeMax used in this fetch
                try:
                    scan_start = datetime.fromisoformat(time_min.replace("Z", "+00:00"))
                    scan_end = datetime.fromisoformat(time_max.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    scan_start = scan_end = None
                purged = []
                for tfp_key, created_at_str in list(raw_state.items()):
                    if tfp_key in actual_tfps:
                        continue
                    # Try to extract event start time from the tfp key (format: "YYYY-MM-DDTHH:MM|...")
                    try:
                        event_time_str = tfp_key.split("|")[0]
                        # Keys are IL local time — make aware before
                        # comparing with the (UTC-aware) scan window.
                        event_dt = datetime.fromisoformat(event_time_str).replace(tzinfo=IL_TZ)
                        in_scan_window = scan_start and scan_end and scan_start <= event_dt <= scan_end
                    except (ValueError, IndexError):
                        event_dt = None
                        in_scan_window = False

                    if in_scan_window:
                        # Event should be in this fetch but isn't — clearly a phantom
                        purged.append(tfp_key)
                        del raw_state[tfp_key]
                    else:
                        # Outside scan window: use age-based check
                        try:
                            created_dt = datetime.fromisoformat(created_at_str)
                            age_hours = (now - created_dt).total_seconds() / 3600
                        except (ValueError, TypeError):
                            age_hours = 999
                        if age_hours > 2 and tfp_key not in actual_tfps:
                            purged.append(tfp_key)
                            del raw_state[tfp_key]
                if purged:
                    # Atomic write back
                    tmp = LOCAL_STATE_PATH + ".tmp"
                    with open(tmp, "w") as f:
                        _json.dump(raw_state, f, indent=2)
                    os.replace(tmp, LOCAL_STATE_PATH)
                    log(f"  Purged {len(purged)} stale local state entries (not in calendar)")
                    for p in purged:
                        log(f"    - {p}")
        except Exception as e:
            log(f"  Note: stale cleanup skipped ({e})")

        return events
    except Exception as e:
        log(f"Error fetching calendar events: {e}")
        return []


def create_calendar_event(summary: str, start_iso: str, end_iso: str,
                          location: str, tfp: str) -> bool:
    """Create a new event in the Live Games calendar. Saves to local state on success."""
    cmd = [
        "gws", "calendar", "+insert",
        "--calendar", LIVE_GAMES_CALENDAR_ID,
        "--summary", summary,
        "--start", start_iso,
        "--end", end_iso,
        "--location", location,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            log(f"  ✓ Created: {summary}")
            save_local_state(tfp)
            return True
        else:
            log(f"  ✗ Failed to create '{summary}': {r.stderr[:200]}")
            return False
    except Exception as e:
        log(f"  ✗ Error creating '{summary}': {e}")
        return False


def verify_event_in_calendar(start_iso: str, location: str) -> bool:
    """Verify a newly created event actually exists in the calendar.

    Queries a narrow window around the event time and checks the
    time+location fingerprint. Prevents local state from getting out of
    sync when gws returns success but the event isn't actually persisted.
    """
    try:
        dt = datetime.fromisoformat(start_iso)
        window_start = (dt - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        window_end = (dt + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

        params = json.dumps({
            "calendarId": LIVE_GAMES_CALENDAR_ID,
            "timeMin": window_start,
            "timeMax": window_end,
        })

        r = subprocess.run(
            ["gws", "calendar", "events", "list", "--params", params],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            log(f"  ⚠️  Verification query failed (rc={r.returncode})")
            return False

        data = json.loads(r.stdout)
        items = data.get("items", [])
        target_tfp = time_fingerprint(start_iso, location)
        for ev in items:
            ev_start = ev.get("start", {}).get("dateTime", "")
            ev_loc = ev.get("location", "")
            if time_fingerprint(ev_start, ev_loc) == target_tfp:
                return True

        log(f"  ⚠️  Event not found after creation (tfp={target_tfp})")
        return False
    except Exception as e:
        log(f"  ⚠️  Verification error: {e}")
        return False


# ── Duplicate Cleanup ─────────────────────────────────────────────────

def _cleanup_fingerprint(summary: str) -> str:
    """Strict fingerprint for duplicate collapse.

    Same as game_fingerprint but also removes backslashes — some
    external writers (e.g. the legacy n8n "calendar updater") escape
    quotes as \\\" in event summaries.
    """
    s = summary.replace("\\", "").replace('"', "").replace("״", "")
    return game_fingerprint(s)


def delete_calendar_event(event_id: str) -> bool:
    """Delete a single event from the Live Games calendar by ID."""
    params = json.dumps({
        "calendarId": LIVE_GAMES_CALENDAR_ID,
        "eventId": event_id,
    })
    try:
        r = subprocess.run(
            ["gws", "calendar", "events", "delete", "--params", params],
            capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return True
        log(f"  ✗ Failed to delete {event_id}: {(r.stderr or r.stdout)[:200]}")
        return False
    except Exception as e:
        log(f"  ✗ Error deleting {event_id}: {e}")
        return False


# Max start-time drift between copies of the same game that still counts
# as a duplicate. Telesport rewrites listings between revisions and can
# shift the start time by up to ~30 min; keep a margin above that.
CLEANUP_TIME_TOLERANCE = timedelta(minutes=60)


def cleanup_duplicates(events: list, dry_run: bool = False) -> int:
    """Collapse near-duplicate calendar events.

    A duplicate = same normalized channel + same game fingerprint (title
    order / sponsor / quote / tournament-prefix-insensitive) + start times
    within CLEANUP_TIME_TOLERANCE of each other — the same broadcast gets
    re-listed by Telesport with title rewrites and time shifts, and legacy
    writers (e.g. the retired n8n "calendar updater") mirrored older
    revisions, so copies can differ on both. Keeps the canonical copy —
    preference: no description (Hermes-created) → mapped location
    ("(yes #NN)") → oldest. Deletes the rest.

    Only time-clusters of >=2 members sharing channel+title keys are
    touched, so genuinely different games in the same slot are never
    affected.
    Returns number removed (or that would be removed in dry-run).
    """
    groups = {}
    for ev in events:
        start = ev.get("start", "")
        loc = ev.get("location", "")
        summary = ev.get("summary", "")
        if not start or not summary:
            continue
        try:
            start_dt = datetime.fromisoformat(start)
        except ValueError:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=IL_TZ)
        key = (normalize_location_for_dedup(loc),
               _cleanup_fingerprint(summary))
        groups.setdefault(key, []).append((start_dt, ev))

    removed = 0

    def collapse(cluster):
        nonlocal removed
        if len(cluster) < 2:
            return

        def keep_rank(item):
            ev = item[1]
            has_desc = 1 if (ev.get("description") or "").strip() else 0
            not_mapped = 0 if "(yes #" in ev.get("location", "") else 1
            return (has_desc, not_mapped, ev.get("created", ""))

        ordered = sorted(cluster, key=keep_rank)
        keeper = ordered[0][1]
        for _, ev in ordered[1:]:
            label = (f"{ev.get('summary')} @ {ev.get('start')} "
                     f"({ev.get('location')})")
            if dry_run:
                log(f"  [DRY] Would delete duplicate: {label} "
                    f"[keep {keeper['id']}]")
                removed += 1
                continue
            if delete_calendar_event(ev["id"]):
                log(f"  🗑 Deleted duplicate: {label} [kept {keeper['id']}]")
                removed += 1
                time.sleep(0.4)

    for (nloc, fp), members in sorted(groups.items()):
        members.sort(key=lambda m: m[0])
        cluster = [members[0]]
        for m in members[1:]:
            if m[0] - cluster[0][0] <= CLEANUP_TIME_TOLERANCE:
                cluster.append(m)
            else:
                collapse(cluster)
                cluster = [m]
        collapse(cluster)
    return removed


# ── Main ──────────────────────────────────────────────────────────────

def main():
    if "YOUR_CALENDAR_ID" in LIVE_GAMES_CALENDAR_ID:
        print(
            "❌ LIVE_GAMES_CALENDAR_ID is not configured.\n"
            "   Set the env var or create ~/.config/live-games-sync/config.json\n"
            "   with {\"calendar_id\": \"your-calendar-id@group.calendar.google.com\"}.\n"
            "   See README.md for instructions.",
            file=sys.stderr,
        )
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv

    # Parse --days flag
    days = LOOKAHEAD_DAYS
    for i, arg in enumerate(sys.argv):
        if arg == "--days" and i + 1 < len(sys.argv):
            try:
                days = int(sys.argv[i + 1])
            except ValueError:
                pass

    # Parse --cleanup-only / --since / --until (one-off duplicate sweeps)
    cleanup_only = "--cleanup-only" in sys.argv
    since = until = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--since" and i + 1 < len(sys.argv):
            since = sys.argv[i + 1]
        elif arg == "--until" and i + 1 < len(sys.argv):
            until = sys.argv[i + 1]

    if dry_run:
        log("⚠️  DRY RUN — no events will be created")

    today = datetime.now(IL_TZ)
    log(f"Scanning {days} days from {today.strftime('%Y-%m-%d')}")

    # ── Cleanup-only sweep (one-off duplicate collapse, no creates) ──
    if cleanup_only:
        log("CLEANUP-ONLY mode — collapsing duplicates, no creates")
        existing = get_existing_events(days, since=since, until=until)
        log(f"  Found {len(existing)} events")
        removed = cleanup_duplicates(existing, dry_run=dry_run)
        tag = "[DRY RUN] would remove" if dry_run else "removed"
        print(f"🧹 Live Games Duplicate Sweep — {tag}: {removed}")
        return

    # ── Step 1: Collect existing calendar events ──
    log("Fetching existing calendar events...")
    existing = get_existing_events(days)
    existing_fingerprints = set()
    existing_tfps = set()
    for ev in existing:
        fp = game_fingerprint(ev["summary"])
        existing_fingerprints.add(fp)
        tfp = time_fingerprint(ev["start"], ev["location"])
        existing_tfps.add(tfp)
    log(f"  Found {len(existing)} existing events")

    # ── Load local state as additional dedup layer ──
    local_state_tfps = load_local_state()
    log(f"  Local state has {len(local_state_tfps)} known event tfps")

    # ── Collapse duplicate events (idempotent safety net) ──
    # Catches copies written by other systems (e.g. the legacy n8n
    # "calendar updater") and any historical duplication.
    duplicates_removed = cleanup_duplicates(existing, dry_run=dry_run)
    if duplicates_removed:
        log(f"  Removed {duplicates_removed} duplicate events")

    # ── Step 2: Fetch Telesport data ──
    candidates = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        date_str = day.strftime("%Y-%m-%d")
        broadcasts = fetch_telesport(date_str)
        if not broadcasts:
            continue

        for b in broadcasts:
            bid = b.get("branch_id")
            if bid not in (1, 2):
                continue  # only soccer (1) and basketball (2)

            title = b.get("title", "").strip()
            if not title:
                continue

            if not is_male_adult(title):
                log(f"  Skip (not male adult): {title}")
                continue

            if bid == 2 and is_wnba(title):
                log(f"  Skip (WNBA): {title}")
                continue

            if bid == 2 and "ב'" in title:
                log(f"  Skip (Summer League / B team): {title}")
                continue

            if is_australian_game(title):
                log(f"  Skip (Australian league): {title}")
                continue

            # Omer's soccer content filters (branch_id == 1 only).
            # Protected teams (e.g. Manchester United) bypass ALL of them.
            if bid == 1:
                protected = has_protected_team(title)
                if not protected and is_skipped_team(title):
                    log(f"  Skip (team on Omer's skip list): {title}")
                    continue
                if not protected and is_region_excluded(title):
                    log(f"  Skip (excluded league/region): {title}")
                    continue
                if not protected and is_israeli_game(title) and not has_allowed_israeli_team(title):
                    log(f"  Skip (Israeli league, no allowed team): {title}")
                    continue
                if protected:
                    log(f"  Keep (protected team): {title}")

            if not is_game_event(title):
                log(f"  Skip (not a game): {title}")
                continue

            time_str = b.get("timeStr", "")
            media_name = b.get("media_name", "")
            date_key = b.get("dateKey", date_str)
            if not time_str:
                continue

            try:
                start_dt = datetime.strptime(
                    f"{date_key}T{time_str}:00", "%Y-%m-%dT%H:%M:%S"
                )
                start_dt = start_dt.replace(tzinfo=IL_TZ)
                end_dt = start_dt + EVENT_DURATION
            except ValueError as e:
                log(f"  Skip '{title}' — bad time '{time_str}': {e}")
                continue

            location = telesport_to_location(media_name)

            # Add sport prefix to title
            sport_prefix = "כדורסל : " if bid == 2 else "כדורגל : "
            title = sport_prefix + title

            candidates.append({
                "title": title,
                "date_key": date_key,
                "time_str": time_str,
                "start_iso": start_dt.isoformat(),
                "end_iso": end_dt.isoformat(),
                "location": location,
                "media_name": media_name,
                "branch_id": bid,
            })

    log(f"Found {len(candidates)} candidate games from Telesport")

    # ── Step 3: Deduplicate within Telesport data ──
    seen_fps = {}
    seen_tfps = {}
    deduped = []

    for c in candidates:
        fp = game_fingerprint(c["title"])
        tfp = time_fingerprint(c["start_iso"], c["location"])

        # Same game name already seen
        if fp in seen_fps:
            existing_entry = seen_fps[fp]
            if c["location"] != c["media_name"] and existing_entry["location"] == existing_entry["media_name"]:
                seen_fps[fp] = c
                deduped = [d for d in deduped if d is not existing_entry]
                deduped.append(c)
            continue

        # Same time+channel (different name variant)
        if tfp in seen_tfps:
            existing_title = seen_tfps[tfp]["title"]
            log(f"  Dup (time+loc): '{c['title']}' ← already have '{existing_title}'")
            continue

        seen_fps[fp] = c
        seen_tfps[tfp] = c
        deduped.append(c)

    candidates = deduped
    log(f"After Telesport dedup: {len(candidates)} unique games")

    # ── Step 4: Cross-check against calendar ──
    created = 0
    skipped = 0
    created_events = []  # (date, time, title, channel) for the report

    for c in candidates:
        fp = game_fingerprint(c["title"])
        tfp = time_fingerprint(c["start_iso"], c["location"])

        if fp in existing_fingerprints:
            log(f"  Skip (in calendar): {c['title']}")
            skipped += 1
            continue

        if tfp in existing_tfps:
            log(f"  Skip (time+loc match calendar): {c['title']}")
            skipped += 1
            continue

        # Local state check — catches duplicates when Google API hasn't caught up
        if tfp in local_state_tfps:
            log(f"  Skip (local state): {c['title']}")
            skipped += 1
            continue

        if dry_run:
            log(f"  [DRY] Would create: {c['title']} @ {c['start_iso']} → {c['end_iso']} | {c['location']}")
            created += 1
            created_events.append((c["date_key"], c["time_str"], c["title"], c["location"]))
        else:
            ok = create_calendar_event(
                summary=c["title"],
                start_iso=c["start_iso"],
                end_iso=c["end_iso"],
                location=c["location"],
                tfp=tfp,
            )
            if ok:
                created += 1
                created_events.append((c["date_key"], c["time_str"], c["title"], c["location"]))
            time.sleep(0.5)

    # ── Report ──
    action_label = "Created" if not dry_run else "Would create"
    print(f"\n📋 Live Games Sync Report")
    print(f"━━━━━━━━━━━━━━━━━━━")
    print(f"• Scanned: {days} days")
    print(f"• Existing in calendar: {len(existing)}")
    print(f"• Telesport candidates: {len(candidates)} unique")
    print(f"  → {action_label}: {created}")
    for ev_date, ev_time, ev_title, ev_loc in sorted(created_events):
        print(f"    • {ev_date} {ev_time} — {ev_title} ({ev_loc})")
    print(f"  → Skipped: {skipped}")
    if duplicates_removed:
        print(f"  → Duplicates cleaned: {duplicates_removed}")


if __name__ == "__main__":
    main()
