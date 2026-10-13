"""Phase 1 prototype — Flask + SQLite.

Originally planned as Next.js + Prisma, but this sandbox's network policy
blocks the npm registry (host_not_allowed), so this uses what's actually
available here: Flask + Jinja2 (pre-installed) and SQLite (Python stdlib).
Same schema, same architecture, same seeded data as the mockups — just a
different stack under the hood. See README.md for the full explanation.

This build adds the Game Type/Neutral Host, Time Zone, Record Bar,
Alternative Results Format, Location/Facility split + opponent display
overrides, and Event Group additions from data-model.md §5c-5g, plus the
six school-site-centric Public View Selection schedule views from §12
(Classic Athletics, Matchup Cards, Compact List, Timeline, Calendar,
Season Story) as their own dedicated Schedule page, separate from the
team hub page, with a click-to-open detail card on every game in every
view. Also: All Day/TBA time handling (games.time_type), Game Setup split
into Event Details/Opponent & Venue/Coverage & Tickets/Additional Links &
Files/Results sections, a Today's Games dashboard widget with inline
quick results entry, and a disabled placeholder for the stats-XML import
that's planned next. It does NOT include the Master Schools Database
(§5a), the schedule paste-import wizard (§5b), the fuller Event
Details/Coverage & Tickets/Additional Links & Files fields beyond what's
in the sectioned Game Setup form (§5h-§5j), the standalone Results Center
dashboard (§5k), the Media & File Library (§13), the actual stats-XML
import, or Roster's own multi-view system — all separate, larger pieces
of tooling, scoped out of this pass. See README.md.
"""

import calendar as cal
import json
import os
import re
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, redirect, render_template, request, abort, url_for, Response, jsonify

from db import get_db, DB_PATH, using_postgres

app = Flask(__name__)


# ---------------------------------------------------------------------
# Cold-start auto-seed — for a hosted demo deploy, there's no separate
# "run this once" step available, so if the database simply isn't there
# yet, seed it automatically on process start. Two different ways to
# check that, because "isn't there yet" means something different for
# each backend:
#   - SQLite: the file itself doesn't exist (a brand-new deploy, or a
#     host whose disk got wiped). Safe specifically because the Procfile
#     runs a single gunicorn worker (--workers 1) — only one process ever
#     starts up, so there's no risk of two workers racing to seed the
#     same fresh database at once.
#   - Postgres: the file check is meaningless (DB_PATH never exists
#     regardless of what's in the real database), and — unlike SQLite on
#     a host that sleeps a whole process — a serverless platform like
#     Vercel cold-starts a fresh process on a huge fraction of requests.
#     Re-running this unconditionally would mean sending CREATE TABLE
#     statements at an already-seeded database on nearly every request,
#     which fails loudly (schema.sql has no "IF NOT EXISTS"). So instead:
#     ask Postgres itself whether the schema exists yet (to_regclass
#     returns NULL for a table that isn't there) and only seed once, the
#     first time a fresh empty database answers "no".
# ---------------------------------------------------------------------
def _needs_seed():
    if using_postgres():
        conn = get_db()
        try:
            row = conn.execute("SELECT to_regclass('schools') AS t").fetchone()
            return not row or not row["t"]
        finally:
            conn.close()
    return not os.path.exists(DB_PATH)


if _needs_seed():
    import seed as _seed
    _seed.run()


# ---------------------------------------------------------------------
# Demo password gate — this prototype has no real login/session system
# yet (see CURRENT_USER below), so anyone with the URL is effectively a
# full admin. For a durable hosted demo link shared with people outside
# the team, set the DEMO_PASSWORD environment variable on the host and
# every request will require it via a simple HTTP Basic Auth prompt.
# Leaving DEMO_PASSWORD unset (e.g. for local development) disables the
# gate entirely, so nothing changes for local/dev use.
# ---------------------------------------------------------------------
@app.before_request
def _require_demo_password():
    demo_password = os.environ.get("DEMO_PASSWORD")
    if not demo_password:
        return None  # gate disabled — no env var set
    auth = request.authorization
    if not auth or not secrets.compare_digest(auth.password or "", demo_password):
        return Response(
            "Authentication required to view this demo.", 401,
            {"WWW-Authenticate": 'Basic realm="Athletics CMS Demo"'},
        )
    return None


# ---------------------------------------------------------------------
# Shared helpers — registered as Jinja globals so templates and Python
# call the exact same logic. Centralizing these is a direct lesson from
# the tie-game bug caught earlier in this build: one place, not four.
# ---------------------------------------------------------------------

def result_letter(score_for, score_against):
    """W / L / T — shared everywhere a final score gets summarized, so a tie
    never silently renders as a loss."""
    if score_for is None or score_against is None:
        return ""
    if score_for > score_against:
        return "W"
    if score_for < score_against:
        return "L"
    return "T"


def counts_toward_record(game):
    """The one shared predicate from data-model.md §5c: scrimmages,
    exhibitions, and Neutral Host games never affect a team's record."""
    return game["game_type"] not in ("scrimmage", "exhibition") and not game["is_neutral_host"]


def ordinal(n):
    """1 -> '1st', 3 -> '3rd', 12 -> '12th' — for place-finish results."""
    if n is None:
        return ""
    n = int(n)
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def game_time_info(game, school):
    """Returns the three things the public Game Location / Client School /
    My Location toggle needs: the time formatted in the game's own site
    zone, the same instant formatted in the client school's zone, and a
    UTC ISO instant for client-side JS to convert to the visitor's zone.
    All-Day and TBA games (time_type) show that label everywhere instead
    of a time, with no UTC instant to convert — "My Location" degrades to
    showing the same label, same as any other time-info consumer."""
    time_type = game.get("time_type") or "specific"
    if time_type == "all_day":
        return {"site": "All Day", "school": "All Day", "utc_iso": None, "has_time": True}
    if time_type == "tba":
        return {"site": "TBA", "school": "TBA", "utc_iso": None, "has_time": True}
    if not game["time"]:
        return {"site": None, "school": None, "utc_iso": None, "has_time": False}
    tz_name = game["time_zone"] or (school["default_timezone"] if school else None) or "America/Chicago"
    school_tz_name = (school["default_timezone"] if school else None) or "America/Chicago"
    try:
        naive = datetime.strptime(f"{game['date']} {game['time']}", "%Y-%m-%d %H:%M")
        site_dt = naive.replace(tzinfo=ZoneInfo(tz_name))
        school_dt = site_dt.astimezone(ZoneInfo(school_tz_name))
        utc_dt = site_dt.astimezone(ZoneInfo("UTC"))
        return {
            "site": site_dt.strftime("%I:%M %p %Z").lstrip("0"),
            "school": school_dt.strftime("%I:%M %p %Z").lstrip("0"),
            "utc_iso": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "has_time": True,
        }
    except Exception:
        # Bad/unknown tz id or unparseable time — degrade to showing the
        # raw entered time rather than a 500.
        return {"site": game["time"], "school": game["time"], "utc_iso": None, "has_time": True}


def initials(name):
    """'Ridgeview College' -> 'RC', 'Westcliff' -> 'WE' — the neutral
    monogram a logo-forward schedule view falls back to when an opponent
    has no logo on file, instead of a broken image or empty box
    (data-model.md §12)."""
    if not name:
        return "?"
    words = [w for w in name.replace("'", "").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return name[:2].upper()


def date_parts(date_str):
    """'2026-08-24' -> {'dow': 'MON', 'mon': 'AUG', 'day': '24'} — the small
    calendar-chip pieces the refreshed Classic Athletics/Timeline rows use.
    Reads the game's existing date string; no new stored field needed."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return {"dow": d.strftime("%a").upper(), "mon": d.strftime("%b").upper(), "day": str(d.day)}


app.jinja_env.globals["result_letter"] = result_letter
app.jinja_env.globals["ordinal"] = ordinal
app.jinja_env.globals["game_time_info"] = game_time_info
app.jinja_env.globals["initials"] = initials
app.jinja_env.globals["date_parts"] = date_parts

# The six school-site-centric schedule views from data-model.md §12 — replaces
# the earlier four-view set (list_view/calendar_view/results_grid/compact_view),
# which was still shaped like a neutral, both-sides-equal conference scoreboard.
SCHEDULE_VIEWS = [
    ("classic", "Classic Athletics"),
    ("matchup_cards", "Matchup Cards"),
    ("compact_list", "Compact List"),
    ("timeline", "Timeline"),
    ("calendar", "Calendar"),
    ("season_story", "Season Story"),
]
# No per-view logo toggle: Compact List is always text-only (that's the
# point of it — the plain, dense option) and every other view always shows
# the opponent logo, falling back to initials, never optional.

# Registered as a Jinja global (not just passed per-route) so the public
# site_page.html template can render the view switcher without every call
# site that renders a page having to thread it through explicitly.
app.jinja_env.globals["SCHEDULE_VIEWS"] = SCHEDULE_VIEWS

GAME_TYPES = [
    ("scrimmage", "Scrimmage"),
    ("exhibition", "Exhibition"),
    ("non_conference", "Non-Conference"),
    ("conference", "Conference"),
    ("postseason", "Postseason"),
]
RESULT_TYPES = [
    ("standard", "Standard"),
    ("no_team_score", "No Team Score"),
    ("no_result", "No Result"),
]
# The Story System (data-model.md §7 / core-systems.md §1) — `articles` is
# an existing table that was already fully specced (school/team scoping,
# category, season tagging, linked_game_id for auto-recaps, linked_gallery_id
# for a photo set) but had no admin screen or public archive built against
# it yet; this is that build. "Classification" (renamed from the original
# recap/feature/press_release/announcement set per direct feedback) is the
# same underlying `category` column with a different value set — a
# Sidearm-style story typing, not a second field.
ARTICLE_CATEGORIES = [
    ("game_preview", "Game Preview"),
    ("game_recap", "Game Recap"),
    ("general_article", "General Article"),
    ("feature_story", "Feature Story"),
]
ARTICLE_CATEGORY_LABELS = dict(ARTICLE_CATEGORIES)
app.jinja_env.globals["ARTICLE_CATEGORY_LABELS"] = ARTICLE_CATEGORY_LABELS
ARTICLE_STATUSES = [
    ("draft", "Draft"),
    ("pending_review", "Pending Review"),
    ("published", "Published"),
]
# Only 'standard' actually renders anything different today — this is here
# so more layouts can be added later ("something we will build along the
# way") without a schema change, not a claim that multiple templates exist.
ARTICLE_TEMPLATE_TYPES = [
    ("standard", "Standard Article"),
]
COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Phoenix", "America/Anchorage", "Pacific/Honolulu",
]
CLASS_YEARS = ["Fr", "So", "Jr", "Sr", "Grad"]
SOCIAL_PLATFORMS = [
    ("x", "X (Twitter)"),
    ("instagram", "Instagram"),
    ("tiktok", "TikTok"),
    ("facebook", "Facebook"),
    ("youtube", "YouTube"),
    ("threads", "Threads"),
    ("snapchat", "Snapchat"),
    ("other", "Other"),
]
SOCIAL_PLATFORM_LABELS = dict(SOCIAL_PLATFORMS)
app.jinja_env.globals["SOCIAL_PLATFORM_LABELS"] = SOCIAL_PLATFORM_LABELS
# The roster display varieties (`teams.roster_template`) — this column
# already existed but nothing actually read it before now, so every team's
# public roster rendered identically regardless of what it was set to.
ROSTER_TEMPLATES = [
    ("card", "Card"),
    ("photo", "Photo"),
    ("table", "Table"),
]

# The roster fields an admin can choose to show/hide, reorder (drag-and-drop,
# on their own dedicated page — see /admin/rosters/fields), and relabel, on
# the public Table display (Card/Photo keep their fixed visual layout — this
# is aimed at Table's column-based shape). Jersey # and Name are part of this
# same configurable set now, not pinned — a school can reorder or even hide
# them, same as any other field.
ROSTER_FIELD_LABELS = {
    "jersey_number": "Jersey #", "name": "Name", "position": "Position", "class_year": "Class",
    "height": "Height", "weight": "Weight", "hometown": "Hometown", "high_school": "High School",
    "previous_school": "Previous School", "bats_throws": "Bats/Throws",
    "custom_field_1": "Custom Field 1", "custom_field_2": "Custom Field 2", "custom_field_3": "Custom Field 3",
}
DEFAULT_ROSTER_FIELDS_CONFIG = [
    {"key": "jersey_number", "enabled": True, "label": None}, {"key": "name", "enabled": True, "label": None},
    {"key": "position", "enabled": True, "label": None}, {"key": "class_year", "enabled": True, "label": None},
    {"key": "height", "enabled": True, "label": None}, {"key": "weight", "enabled": True, "label": None},
    {"key": "hometown", "enabled": True, "label": None}, {"key": "high_school", "enabled": False, "label": None},
    {"key": "previous_school", "enabled": False, "label": None}, {"key": "bats_throws", "enabled": False, "label": None},
    {"key": "custom_field_1", "enabled": False, "label": None}, {"key": "custom_field_2", "enabled": False, "label": None},
    {"key": "custom_field_3", "enabled": False, "label": None},
]
SORT_MODES = [("jersey", "Jersey Number, then Last Name"), ("alpha", "Alphabetical (Last Name)")]
STAFF_COACH_DEFAULT_TITLE = "Assistant Coach"

# --- Staff Directory -----------------------------------------------------
# Same {key, enabled, label} JSON shape and same defaulting/backfill pattern
# as ROSTER_FIELD_LABELS/DEFAULT_ROSTER_FIELDS_CONFIG, one row per school
# this time (staff_directory_settings) rather than per team+season — this
# governs which of these show on the public Staff Directory's cards, and
# under what header text.
STAFF_FIELD_LABELS = {
    "name": "Name", "title": "Title", "email": "Email", "phone": "Phone", "social": "Social",
}
DEFAULT_STAFF_FIELDS_CONFIG = [
    {"key": "name", "enabled": True, "label": None},
    {"key": "title", "enabled": True, "label": None},
    {"key": "email", "enabled": True, "label": None},
    {"key": "phone", "enabled": True, "label": None},
    {"key": "social", "enabled": True, "label": None},
]
# Seeded once per school as a starting point — "every category but the
# sports should be custom with option to create more custom" — these three
# are just the first custom ones on file, not a fixed/required set; a school
# can rename or delete any of them like any other custom category.
DEFAULT_STAFF_CATEGORIES = ["Administration", "Athletic Trainers", "Athletic Communications"]

# The full name a computed SQL alias builds wherever a query used to just
# select staff_members.name, before it split into first_name/last_name —
# keeps every existing `c['name']`-style template reference working.
STAFF_NAME_SQL = "(staff_members.first_name || ' ' || staff_members.last_name)"

# --- Live/box-score team stats ------------------------------------------
# Which team-level box score categories exist, per sport, and what order/
# header text they display with — same pattern as ROSTER_FIELD_LABELS.
# Only a sport with an entry here gets the stats-feed import UI surfaced
# at all (see admin_game_form.html's Results section and _team_stat_categories()
# below); teams.sport is the lookup key, independent of the team's own
# display name. Values are stored and shown exactly as the feed sends them
# (see game_team_stats in schema.sql) — e.g. "6-14" for third-down
# conversions, "31:12" for time of possession — never reformatted here.
# Team totals only, on purpose (direct feedback: start with what a game's
# final stat sheet actually reports team-wide; individual player stat
# lines are a deliberately separate, later pass on top of this same table).
TEAM_STAT_CATEGORIES = {
    "football": [
        {"key": "total_yards", "label": "Total Yards"},
        {"key": "passing_yards", "label": "Passing Yards"},
        {"key": "rushing_yards", "label": "Rushing Yards"},
        {"key": "first_downs", "label": "First Downs"},
        {"key": "third_down", "label": "3rd Down Conversions"},
        {"key": "turnovers", "label": "Turnovers"},
        {"key": "penalties", "label": "Penalties-Yards"},
        {"key": "time_of_possession", "label": "Time of Possession"},
    ],
}


def _team_stat_categories(team_row):
    """None when this team's sport has no stat categories defined yet — the
    signal every stats-feed UI point (Results section, Schedule Settings'
    Live Scoring control, the public game card's box score) uses to stay
    hidden rather than show an empty/broken feature for that sport."""
    if not team_row or not team_row["sport"]:
        return None
    return TEAM_STAT_CATEGORIES.get(team_row["sport"])


# Individual player box score lines, grouped into categories (each with its
# own column set) — a real fan-facing live stats page leads with "who's
# doing what," not just team totals. Same registry pattern as
# TEAM_STAT_CATEGORIES/ROSTER_FIELD_LABELS: only a sport listed here gets
# player stat tables at all. Deliberately compact column sets (not every
# category a real box score has — no defense/kicking/punting yet) since
# this is the fan-facing live page's first pass, not a full stats service.
PLAYER_STAT_CATEGORIES = {
    "football": [
        {"category": "passing", "label": "Passing", "leader_stat": "yards", "leader_label": "YDS", "columns": [
            {"key": "comp_att", "label": "C/ATT"}, {"key": "yards", "label": "YDS"},
            {"key": "td", "label": "TD"}, {"key": "int", "label": "INT"},
        ]},
        {"category": "rushing", "label": "Rushing", "leader_stat": "yards", "leader_label": "YDS", "columns": [
            {"key": "car", "label": "CAR"}, {"key": "yards", "label": "YDS"}, {"key": "td", "label": "TD"},
        ]},
        {"category": "receiving", "label": "Receiving", "leader_stat": "yards", "leader_label": "YDS", "columns": [
            {"key": "rec", "label": "REC"}, {"key": "yards", "label": "YDS"}, {"key": "td", "label": "TD"},
        ]},
        # Added once the real PrestoSports feed (see _parse_stats_feed_xml)
        # showed how central defensive stats are to a real box score — every
        # player who recorded a tackle gets a line here, which the compact
        # demo-format placeholder feed never had a shape for.
        {"category": "defense", "label": "Defense", "leader_stat": "tot", "leader_label": "TOT", "columns": [
            {"key": "tot", "label": "TOT"}, {"key": "solo", "label": "SOLO"},
            {"key": "ast", "label": "AST"}, {"key": "tfl", "label": "TFL"},
        ]},
    ],
}


def _player_stat_categories(team_row):
    """Same None-means-not-built-for-this-sport contract as _team_stat_categories."""
    if not team_row or not team_row["sport"]:
        return None
    return PLAYER_STAT_CATEGORIES.get(team_row["sport"])


def _build_player_stat_tables(player_entries, player_categories):
    """Buckets a flat list of {"side", "category", "player_name",
    "jersey_number", "stats"} entries into {"for": [...], "against": [...]}
    category tables ready to display, ordered/labeled by player_categories.
    Every caller already has entries in exactly this per-player shape —
    _parse_stats_feed_xml's own "player_stats" list (the stats-import
    preview) and _enrich_game's collapsed game_player_stats rows (the
    public schedule/game-card box score) — so this is the one place that
    ever decides category order/labels, shared instead of duplicated
    between them."""
    grouped = {"for": {}, "against": {}}
    for entry in player_entries:
        side = entry.get("side")
        if side not in grouped:
            continue
        grouped[side].setdefault(entry["category"], []).append(entry)

    tables = {"for": [], "against": []}
    if not player_categories:
        return tables
    for side in ("for", "against"):
        for cat_def in player_categories:
            players = grouped[side].get(cat_def["category"], [])
            if players:
                tables[side].append({
                    "category": cat_def["category"], "label": cat_def["label"],
                    "columns": cat_def["columns"], "players": players,
                })
    return tables


def _compute_game_leaders(player_entries, player_categories):
    """One 'leader' per stat category per side — whichever player has the
    highest value in that category's own leader_stat column (passing/
    rushing/receiving yards, tackles for defense) — for Game Central's
    Leaders box. Built from the same flat per-player entries
    _build_player_stat_tables groups into full tables; this only ever
    needs the single best row per category, not the whole roster, so it's
    kept separate rather than derived from that function's grouped output.
    Returns empty for/against lists when a category has no numeric-parsable
    entries yet (e.g. before any stats are on file) rather than guessing."""
    leaders = {"for": [], "against": []}
    if not player_categories:
        return leaders
    by_side_cat = {}
    for entry in player_entries:
        side = entry.get("side")
        if side not in leaders:
            continue
        by_side_cat.setdefault(side, {}).setdefault(entry["category"], []).append(entry)
    for side in ("for", "against"):
        for cat_def in player_categories:
            candidates = by_side_cat.get(side, {}).get(cat_def["category"], [])
            best, best_value = None, None
            for entry in candidates:
                raw = entry.get("stats", {}).get(cat_def["leader_stat"])
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    continue
                if best_value is None or value > best_value:
                    best, best_value = entry, value
            if best is not None:
                leaders[side].append({
                    "category": cat_def["category"], "label": cat_def["label"],
                    "player_name": best["player_name"], "jersey_number": best.get("jersey_number"),
                    "value": best_value, "leader_label": cat_def["leader_label"],
                })
    return leaders


class StatsFeedError(ValueError):
    """Raised on any malformed/unparseable stats feed XML — caught by the
    import route to redisplay the paste/upload form with a plain-English
    error instead of a stack trace."""


def _ordinal(n):
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11-13 -> '11th'/'12th'/'13th'."""
    n = int(n)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _presto_down_distance(down, togo, play_type=None):
    """Builds a "2nd & 7" (or "3rd & Goal") style string from a PrestoSports
    <play>'s down/togo attributes. togo="0" is ambiguous in this feed — it
    shows up both on real goal-to-go snaps (down="3" togo="0" for a snap
    from the 2, where "0" means "at the goal line" rather than a real
    yards-to-go) and on plays that aren't a down at all (PAT attempts are
    always stamped down="1" togo="0"). play_type is the <play>'s own
    type=".." attribute; "X" (PrestoSports' PAT-attempt type, confirmed
    against the real sample) is treated as "not a real down" and returns
    None, everything else with togo="0" is treated as goal-to-go."""
    if down in (None, "") or togo in (None, ""):
        return None
    try:
        down, togo = int(down), int(togo)
    except ValueError:
        return None
    if down < 1 or down > 4 or togo < 0:
        return None
    if togo == 0:
        return None if play_type == "X" else f"{_ordinal(down)} & Goal"
    return f"{_ordinal(down)} & {togo}"


def _presto_yard_line(spot, for_id, against_id):
    """Converts a PrestoSports <play>'s spot="ARIZONA35" style field position
    into this app's 0-100 "for"-frame yard_line (see schema.sql's game_plays
    comment). A spot is always TEAMID + a 2-digit yard number, naming
    whichever team's *territory* the ball is sitting in — not necessarily
    the team with the ball. So: spot in the for team's own territory maps
    straight across (its yard number IS the for-frame yard_line, since 0
    there is for's own goal line, matching the number's own meaning); spot
    in the against team's territory is 100 minus its yard number (its 0,
    against's own goal line, is for's frame's 100 — the opponent's goal
    line). Returns None for anything that doesn't parse (missing spot,
    unrecognized team prefix, a bare "50" with no prefix at all — none of
    which showed up in the one real sample seen so far, but a feed
    hiccup shouldn't break the whole import over one field-position value)."""
    if not spot:
        return None
    spot = spot.strip().upper()
    for team_id, is_for in ((for_id, True), (against_id, False)):
        tid = (team_id or "").upper()
        if tid and spot.startswith(tid):
            try:
                n = int(spot[len(tid):])
            except ValueError:
                return None
            n = max(0, min(100, n))
            return n if is_for else 100 - n
    if spot == "50":
        return 50
    return None


def _resolve_presto_sides(team_els, home_away, opponent_name_hint):
    """PrestoSports only ever labels its two <team> elements Visitor/Home
    (vh="V"/"H") — nothing in the feed itself says which one is "us". This
    game's own home_away column (home/away/neutral) is the primary signal,
    since it's already set on every game regardless of stats feeds. For a
    neutral-site game (or if home_away is somehow blank), falls back to
    matching opponent_name_hint (the game's opponent_display_name or
    opponent_name) against each <team name=...> — whichever team ISN'T a
    name match for the opponent is us. Raises StatsFeedError rather than
    guessing wrong if neither signal resolves it, consistent with this
    importer's "surface a clear error, never silently misattribute stats"
    approach elsewhere (see _match_athlete_id's callers).

    Returns (for_team_el, against_team_el)."""
    by_vh = {t.get("vh"): t for t in team_els}
    if "V" not in by_vh or "H" not in by_vh:
        raise StatsFeedError('Expected one <team vh="V"> and one <team vh="H"> element.')
    if home_away == "home":
        return by_vh["H"], by_vh["V"]
    if home_away == "away":
        return by_vh["V"], by_vh["H"]

    hint = (opponent_name_hint or "").strip().lower()
    if hint:
        for vh, other_vh in (("V", "H"), ("H", "V")):
            name = (by_vh[vh].get("name") or "").strip().lower()
            if name and (name == hint or name in hint or hint in name):
                return by_vh[other_vh], by_vh[vh]

    raise StatsFeedError(
        "Couldn't tell which team in this feed is us — this game's Home/Away isn't set to "
        "home or away, and the feed's team names don't match the opponent on the schedule. "
        "Set Home/Away on this game (or check the opponent name) and try again."
    )


def _presto_team_stats(team_el):
    """Maps a PrestoSports <team>'s <totals> block onto TEAM_STAT_CATEGORIES'
    keys. Only the subset that registry defines — the feed reports a lot
    more (kicking, punting, return yardage, redzone splits, quarter-by-
    quarter scoring...) than this prototype's team-stats table has columns
    for; see that registry's own comment for why it's scoped that way."""
    totals = team_el.find("totals")
    stats = {}
    if totals is None:
        return stats

    def to_int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    if totals.get("totoff_yards") is not None:
        stats["total_yards"] = totals.get("totoff_yards")
    pass_el = totals.find("pass")
    if pass_el is not None and pass_el.get("yds") is not None:
        stats["passing_yards"] = pass_el.get("yds")
    rush_el = totals.find("rush")
    if rush_el is not None and rush_el.get("yds") is not None:
        stats["rushing_yards"] = rush_el.get("yds")
    firstdowns = totals.find("firstdowns")
    if firstdowns is not None and firstdowns.get("no") is not None:
        stats["first_downs"] = firstdowns.get("no")
    conversions = totals.find("conversions")
    if conversions is not None:
        stats["third_down"] = f"{conversions.get('thirdconv', '0')}-{conversions.get('thirdatt', '0')}"
    fumbles = totals.find("fumbles")
    turnovers = (to_int(pass_el.get("int")) if pass_el is not None else 0) + \
        (to_int(fumbles.get("lost")) if fumbles is not None else 0)
    if pass_el is not None or fumbles is not None:
        stats["turnovers"] = str(turnovers)
    penalties = totals.find("penalties")
    if penalties is not None:
        stats["penalties"] = f"{penalties.get('no', '0')}-{penalties.get('yds', '0')}"
    misc = totals.find("misc")
    if misc is not None and misc.get("top"):
        stats["time_of_possession"] = misc.get("top")
    return stats


def _presto_player_stats(team_el, side):
    """Maps a PrestoSports <team>'s <player> lines onto
    PLAYER_STAT_CATEGORIES' passing/rushing/receiving/defense shape. A
    player contributes an entry per category only when the feed included
    that sub-element for them at all (e.g. only the QB(s) who threw a pass
    get a <pass> child) — presence, not a zero-check, is the participation
    signal, matching how the feed itself only stamps stat blocks for
    players who took part."""
    entries = []
    for player_el in team_el.findall("player"):
        name = player_el.get("name")
        if not name:
            continue
        jersey = player_el.get("uni") or None

        pass_el = player_el.find("pass")
        if pass_el is not None:
            entries.append({
                "side": side, "category": "passing", "player_name": name, "jersey_number": jersey,
                "stats": {
                    "comp_att": f"{pass_el.get('comp', '0')}-{pass_el.get('att', '0')}",
                    "yards": pass_el.get("yds", "0"), "td": pass_el.get("td", "0"),
                    "int": pass_el.get("int", "0"),
                },
            })
        rush_el = player_el.find("rush")
        if rush_el is not None:
            entries.append({
                "side": side, "category": "rushing", "player_name": name, "jersey_number": jersey,
                "stats": {
                    "car": rush_el.get("att", "0"), "yards": rush_el.get("yds", "0"),
                    "td": rush_el.get("td", "0"),
                },
            })
        rcv_el = player_el.find("rcv")
        if rcv_el is not None:
            entries.append({
                "side": side, "category": "receiving", "player_name": name, "jersey_number": jersey,
                "stats": {
                    "rec": rcv_el.get("no", "0"), "yards": rcv_el.get("yds", "0"),
                    "td": rcv_el.get("td", "0"),
                },
            })
        def_el = player_el.find("defense")
        if def_el is not None:
            entries.append({
                "side": side, "category": "defense", "player_name": name, "jersey_number": jersey,
                "stats": {
                    "tot": def_el.get("tot_tack", "0"), "solo": def_el.get("tacka", "0"),
                    "ast": def_el.get("tackua", "0"), "tfl": def_el.get("tfla", "0"),
                },
            })
    return entries


def _presto_score(team_el):
    """A team's current/final total from its <linescore score="..">, used
    for both a completed game's final score and (best-effort, unverified
    against a real in-progress sample yet) a live game's running score."""
    ls = team_el.find("linescore")
    if ls is None or ls.get("score") is None:
        return None
    try:
        return int(ls.get("score"))
    except ValueError:
        return None


def _presto_plays(plays_el, for_id, against_id, for_vh):
    """Walks a PrestoSports <plays><qtr>...</qtr></plays> block in document
    order (which is already chronological) into this app's flat play list.

    Each <qtr number="N"> becomes period "QN". Within a quarter, <play>,
    <drivestart>, <drivesum>, <score>, and <endqtr> all appear as sibling
    elements rather than any of them nesting the others — <drivestart>/
    <drivesum>/<endqtr> are skipped (this app doesn't track drives at all;
    game_plays exists for the box score's Scoring Summary, not a full
    drive-by-drive breakdown); a <score> is consumed as the running score
    belonging to the <play> it immediately follows, marking that play as a
    scoring play, and a <score final="Y"> (only ever seen at the very last
    play of a completed game so far) is how "final" status is detected.

    Returns (plays, final_seen).
    """
    plays = []
    pending_play = None
    final_seen = False
    last_score = None
    if plays_el is None:
        return plays, final_seen

    for qtr_el in plays_el.findall("qtr"):
        period = f"Q{qtr_el.get('number')}" if qtr_el.get("number") else qtr_el.get("text")
        for child in qtr_el:
            if child.tag == "play":
                hasball = child.get("hasball")
                side = "for" if hasball == for_id else "against" if hasball == against_id else None
                description = (child.get("text") or "").strip()
                play = {
                    "sequence": len(plays) + 1, "period": period, "clock": child.get("clock"),
                    "side": side, "description": description,
                    "score_for": None, "score_against": None,
                    # The TD run/pass itself and the PAT/2pt try that follows
                    # it are always two separate <play>s in this feed, and
                    # the <score> element (below) only ever lands on the
                    # later one (the kick/try) — so the touchdown play would
                    # otherwise never show up in the box score's Scoring
                    # Summary even though it's the play fans came to see.
                    # Flagging it directly off its own "TOUCHDOWN" text
                    # (present on every scoring run/pass in the one real
                    # sample checked) fixes that independent of the <score>
                    # bookkeeping below, which still separately marks the
                    # actual point-scoring play (FG, or the PAT/2pt try).
                    "is_scoring_play": "touchdown" in description.lower(),
                    "yard_line": _presto_yard_line(child.get("spot"), for_id, against_id),
                    "down_distance": _presto_down_distance(child.get("down"), child.get("togo"), child.get("type")),
                }
                plays.append(play)
                pending_play = play
            elif child.tag == "score" and pending_play is not None:
                v, h = child.get("V"), child.get("H")
                if v not in (None, "") and h not in (None, ""):
                    try:
                        v, h = int(v), int(h)
                    except ValueError:
                        v = h = None
                    # A completed game's very last <score> repeats the same
                    # V/H as the previous one (it's an end-of-game summary
                    # marker, not a new score, and lands on the "End of
                    # game" filler play rather than anything that actually
                    # scored) — only attach/flag when the total actually
                    # changed, so that marker doesn't get bolded as a
                    # scoring play with a duplicate score line.
                    if v is not None and (v, h) != last_score:
                        pending_play["score_for"], pending_play["score_against"] = \
                            (v, h) if for_vh == "V" else (h, v)
                        pending_play["is_scoring_play"] = True
                        last_score = (v, h)
                if child.get("final") == "Y":
                    final_seen = True
    return plays, final_seen


def _parse_stats_feed_xml(xml_text, home_away=None, opponent_name_hint=None):
    """Parses a PrestoSports <fbgame> export — the real stats-provider
    format this school actually uses, confirmed against a real completed
    NAIA game file the school sent over (not the earlier placeholder shape
    this function used to target, which was a stand-in invented before any
    real feed sample existed). Simplified structure:

        <fbgame source="PrestoSports" ... generated="10/20/2025">
          <team vh="V" id="ARIZONA" name="Arizona Christian" ...>
            <linescore score="14">...</linescore>
            <totals totoff_yards="207" ...>
              <firstdowns no="13"/> <penalties no="12" yds="66"/>
              <conversions thirdconv="2" thirdatt="8"/> <fumbles lost="0"/>
              <misc top="23:01"/> <pass att="22" comp="11" yds="102" td="1" int="1"/>
              <rush att="20" yds="105" td="1"/> <rcv .../> <defense .../> ...
            </totals>
            <player uni="17" name="Josh Bell" ...>
              <pass .../> <rush .../> <rcv .../> <defense .../>
            </player>
            ...
          </team>
          <team vh="H" id="OUAZ" name="OUAZ" ...>...</team>
          <plays format="summary">
            <qtr number="1" text="1st">
              <play context="..." spot="ARIZONA35" down="1" togo="10"
                    hasball="ARIZONA" text="Josh Bell pass complete to..."
                    clock="14:54" playid="1,2,3">...</play>
              <drivestart .../> <drivesum .../>
              <score V="0" H="3"></score>
              ...
              <endqtr/>
            </qtr>
            ...
          </plays>
        </fbgame>

    Notable feed quirks this parser works around (see the helper functions'
    own docstrings for the full reasoning on each):
      - Nothing in the feed says which <team> is "us" — resolved via this
        game's home_away, with an opponent-name fallback for neutral sites
        (_resolve_presto_sides). Raises StatsFeedError if neither works.
      - A play's field position (spot="TEAMID##") names whichever team's
        territory the ball sits in, not who has it — converted to this
        app's 0-100 for-frame by _presto_yard_line.
      - down/togo are stamped on every <play>, including non-scrimmage-down
        ones (kickoffs, PATs) where they're not meaningful; down_distance
        comes back None for those (_presto_down_distance).
      - clock is only present on some <play>s (drive-start/scoring/notable
        plays) — most plays in the one real sample seen have no clock at
        all, so game_plays.clock is None for those; period (from the
        enclosing <qtr>) is always present, so ordering/grouping is
        unaffected either way.
      - "final" status is inferred from a <score final="Y"> marker (seen
        once, on the last play of the one completed-game sample provided);
        its absence means "live". This is a reasonable inference but
        UNVERIFIED against a real in-progress PrestoSports export — the
        school has only sent a final game so far and said an in-game
        sample would follow; re-check this the moment one arrives.
      - TEAM_STAT_CATEGORIES/PLAYER_STAT_CATEGORIES are both much smaller
        than everything PrestoSports actually reports (no kicking/punting/
        return-yardage categories, no quarter-by-quarter scoring, no
        drive log) — this parser only pulls what those registries define;
        ElementTree still parses the rest of the document, it's just not
        carried through, the same "only what the UI shows" scope the
        previous placeholder parser had.

    home_away/opponent_name_hint are this game's own games.home_away and
    opponent_display_name-or-opponent_name — needed only to resolve which
    <team> is "us"; both may be None (a neutral game with no opponent-name
    match will then raise StatsFeedError rather than guess).

    Returns/raises exactly what the previous parser did — same dict shape
    ({"status", "updated_at", "score_for", "score_against", "for",
    "against", "plays", "player_stats"}), same StatsFeedError contract —
    so nothing downstream of this function (game_team_stats, game_plays,
    game_player_stats, every template that reads parsed data) needed to
    change for this rewrite.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise StatsFeedError(f"That doesn't parse as XML ({e}). Check for a stray tag or unclosed element.")
    if root.tag != "fbgame":
        raise StatsFeedError(f"Expected an <fbgame> root element (a PrestoSports football export), found <{root.tag}>.")

    team_els = root.findall("team")
    if len(team_els) != 2:
        raise StatsFeedError(f"Expected exactly two <team> elements, found {len(team_els)}.")

    for_team_el, against_team_el = _resolve_presto_sides(team_els, home_away, opponent_name_hint)
    for_id, against_id, for_vh = for_team_el.get("id"), against_team_el.get("id"), for_team_el.get("vh")
    if not for_id or not against_id:
        raise StatsFeedError("A <team> element is missing its id attribute.")

    plays, final_seen = _presto_plays(root.find("plays"), for_id, against_id, for_vh)

    return {
        "status": "final" if final_seen else "live",
        "updated_at": root.get("generated"),
        "score_for": _presto_score(for_team_el),
        "score_against": _presto_score(against_team_el),
        "for": _presto_team_stats(for_team_el),
        "against": _presto_team_stats(against_team_el),
        "plays": plays,
        "player_stats": _presto_player_stats(for_team_el, "for") + _presto_player_stats(against_team_el, "against"),
    }


def height_display(ft, inches):
    """5, 11 -> '5-11' — the FT-IN format requested for the public roster,
    everywhere height shows. Blank if either half is missing."""
    if ft is None or inches is None or ft == "" or inches == "":
        return ""
    return f"{ft}-{inches}"


def _parse_height_text(text):
    """Best-effort split of a pasted/typed height like 5-11, 5'11, 5' 11",
    or "5 11" into (ft, inches) ints, for Paste Import — which still takes
    height as one free-text column. Returns (None, None) if it can't parse."""
    import re
    if not text:
        return None, None
    m = re.match(r"\s*(\d+)\s*[\'\-\s]\s*(\d+)", str(text))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


app.jinja_env.globals["height_display"] = height_display

# Hardcoded "logged in as" user for this prototype — real auth is out of
# scope for Phase 1 proof-of-concept; see README.md.
CURRENT_USER = {"name": "Sarah Jennings", "role": "Admin"}
SCHOOL_SLUG = "northfield-college"


def _current_user_permissions(conn, school_id):
    """What CURRENT_USER's role is allowed to do, read for real off
    roles/role_permissions rather than hardcoded — matched by role *name*
    since there's no real login/session tying CURRENT_USER to an actual
    `users` row yet (see "No auth" in README.md). This is the hook the
    Story System's Publish-vs-Submit-for-Review gate (publish_news) reads;
    with CURRENT_USER always Admin today it'll always resolve to "can
    publish," but the check is real, not a stub — swap in a real logged-in
    user later and it starts meaning something."""
    role = conn.execute(
        "SELECT * FROM roles WHERE school_id=? AND name=?", (school_id, CURRENT_USER["role"])
    ).fetchone()
    if not role:
        return set()
    rows = conn.execute(
        "SELECT permission_key FROM role_permissions WHERE role_id=?", (role["id"],)
    ).fetchall()
    return {r["permission_key"] for r in rows}


def _current_user_row(conn, school_id):
    """Same "matched by name, no real session" caveat as
    _current_user_permissions above — the actual `users` row for
    CURRENT_USER, so per-user settings (like the byline default below) have
    somewhere real to read from even without real login."""
    return conn.execute(
        "SELECT * FROM users WHERE school_id=? AND name=?", (school_id, CURRENT_USER["name"])
    ).fetchone()


def _slugify(text):
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "story"


def _unique_article_slug(conn, school_id, base_text, exclude_id=None):
    """Auto-generates a slug from the title (never asked for directly on
    the form — same reasoning as opponent-driven auto-fill elsewhere in
    this app: one less thing to type, one less thing to get wrong) and
    de-dupes it against this school's other articles, matching the
    UNIQUE(school_id, slug) constraint in schema.sql."""
    base = _slugify(base_text)
    slug = base
    n = 2
    while True:
        query = "SELECT id FROM articles WHERE school_id=? AND slug=?"
        params = [school_id, slug]
        if exclude_id:
            query += " AND id!=?"
            params.append(exclude_id)
        if not conn.execute(query, params).fetchone():
            return slug
        slug = f"{base}-{n}"
        n += 1


def get_school(conn):
    return conn.execute("SELECT * FROM schools WHERE slug = ?", (SCHOOL_SLUG,)).fetchone()


def _get_or_create_roster_settings(conn, team_id, season):
    """One settings row per team+season, created on first touch with sane
    defaults — so every existing team/season works without a migration and
    every new one gets a row lazily the first time its settings page loads."""
    if not season:
        return None
    row = conn.execute(
        "SELECT * FROM roster_settings WHERE team_id=? AND season=?", (team_id, season)
    ).fetchone()
    if row:
        return dict(row)
    conn.execute(
        "INSERT INTO roster_settings (team_id, season, fields_config) VALUES (?, ?, ?)",
        (team_id, season, json.dumps(DEFAULT_ROSTER_FIELDS_CONFIG)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM roster_settings WHERE team_id=? AND season=?", (team_id, season)
    ).fetchone()
    return dict(row)


def _get_or_create_schedule_settings(conn, team_id, season):
    if not season:
        return None
    row = conn.execute(
        "SELECT * FROM schedule_settings WHERE team_id=? AND season=?", (team_id, season)
    ).fetchone()
    if row:
        return dict(row)
    conn.execute("INSERT INTO schedule_settings (team_id, season) VALUES (?, ?)", (team_id, season))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM schedule_settings WHERE team_id=? AND season=?", (team_id, season)
    ).fetchone()
    return dict(row)


def _roster_fields_config(settings):
    """Parses roster_settings.fields_config, filling in any field key that
    doesn't exist yet in a saved row (e.g. a field added to the app after the
    row was first created) so old settings rows never crash a new template.
    Jersey #/Name are prepended if missing (they anchor the identity of the
    row); everything else newly-added is appended at the end."""
    try:
        saved = json.loads(settings["fields_config"]) if settings and settings.get("fields_config") else []
    except (ValueError, TypeError):
        saved = []
    for f in saved:
        f.setdefault("label", None)
    seen = {f["key"] for f in saved}
    for d in DEFAULT_ROSTER_FIELDS_CONFIG:
        if d["key"] not in seen:
            if d["key"] in ("jersey_number", "name"):
                saved.insert(0, dict(d))
            else:
                saved.append(dict(d))
    return saved


def _roster_field_label(field_entry):
    """A field's effective header text — its saved custom label if the admin
    set one, else the app's default label for that key."""
    return field_entry.get("label") or ROSTER_FIELD_LABELS.get(field_entry["key"], field_entry["key"])


app.jinja_env.globals["roster_field_label"] = _roster_field_label


def _roster_title(school, team, season, custom_title):
    if custom_title:
        return custom_title
    return f"{season} {school['name']} {team['name']} Roster"


# --- Staff Directory -------------------------------------------------------
# staff_members is the same school-wide people directory the Coaches & Staff
# tab on a roster already used; this section is what gives it its own
# real admin screens (list + profile, same "shared directory, per-context
# assignment" shape as Players/athletes) plus the Staff Categories/Directory
# Settings machinery that's new this round.

def _ensure_sport_staff_categories(conn, school_id):
    """One auto-managed staff category per team, kept in sync lazily (same
    pattern as _get_or_create_stats_feed_token) rather than requiring a
    migration step every time a team is added. These are never
    renamed/deleted from the category management screen — their identity
    IS the team, so display always prefers the live teams.name join over
    whatever's frozen in staff_categories.name (see the queries that read
    them) to avoid the two ever drifting apart."""
    teams = conn.execute(
        "SELECT id, name FROM teams WHERE school_id=? ORDER BY sort_order", (school_id,)
    ).fetchall()
    existing_team_ids = {
        r["team_id"] for r in conn.execute(
            "SELECT team_id FROM staff_categories WHERE school_id=? AND team_id IS NOT NULL", (school_id,)
        ).fetchall()
    }
    missing = [t for t in teams if t["id"] not in existing_team_ids]
    if not missing:
        return
    max_sort = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) as m FROM staff_categories WHERE school_id=?", (school_id,)
    ).fetchone()["m"]
    for i, t in enumerate(missing):
        conn.execute(
            "INSERT INTO staff_categories (school_id, name, team_id, sort_order) VALUES (?, ?, ?, ?)",
            (school_id, t["name"], t["id"], max_sort + 1 + i),
        )
    conn.commit()


def _ensure_default_staff_categories(conn, school_id):
    """The three starter custom categories, created once if this school has
    no custom (team_id IS NULL) category on file yet — a starting point, not
    a fixed/required set; every one of them can be renamed or deleted like
    any other custom category once real ones exist."""
    has_custom = conn.execute(
        "SELECT 1 FROM staff_categories WHERE school_id=? AND team_id IS NULL LIMIT 1", (school_id,)
    ).fetchone()
    if has_custom:
        return
    max_sort = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) as m FROM staff_categories WHERE school_id=?", (school_id,)
    ).fetchone()["m"]
    for i, name in enumerate(DEFAULT_STAFF_CATEGORIES):
        conn.execute(
            "INSERT INTO staff_categories (school_id, name, team_id, sort_order) VALUES (?, ?, NULL, ?)",
            (school_id, name, max_sort + 1 + i),
        )
    conn.commit()


def _ensure_staff_categories(conn, school_id):
    """Called at the top of every staff-category-touching route — cheap
    no-ops once both are already in place."""
    _ensure_sport_staff_categories(conn, school_id)
    _ensure_default_staff_categories(conn, school_id)


def _list_staff_categories(conn, school_id):
    """Every category, sport and custom together in display order — the
    live team name is preferred for a sport category (see
    _ensure_sport_staff_categories) so a category never shows a stale name
    if the team it's tied to were ever renamed."""
    rows = conn.execute(
        """SELECT staff_categories.*, teams.name as team_name FROM staff_categories
           LEFT JOIN teams ON staff_categories.team_id = teams.id
           WHERE staff_categories.school_id=? ORDER BY staff_categories.sort_order""",
        (school_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["display_name"] = d["team_name"] or d["name"]
        d["is_sport"] = d["team_id"] is not None
        out.append(d)
    return out


def _get_or_create_staff_directory_settings(conn, school_id):
    row = conn.execute("SELECT * FROM staff_directory_settings WHERE school_id=?", (school_id,)).fetchone()
    if row:
        return dict(row)
    conn.execute(
        """INSERT INTO staff_directory_settings (school_id, intro_text, fields_config, custom_field_labels,
                                                    directory_template)
           VALUES (?, ?, ?, ?, 'cards')""",
        (school_id, None, json.dumps(DEFAULT_STAFF_FIELDS_CONFIG), json.dumps([])),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM staff_directory_settings WHERE school_id=?", (school_id,)).fetchone())


def _staff_directory_custom_field_labels(settings):
    """The freeform "which custom field labels also show on the public
    directory" list — matched by label text at render time (case/whitespace
    -insensitive) since staff_custom_fields has no fixed per-school schema
    to hang a toggle off of the way fields_config does for the 5 built-in
    fields."""
    try:
        return [s for s in json.loads(settings.get("custom_field_labels") or "[]") if s]
    except (ValueError, TypeError):
        return []


def _staff_fields_config(settings):
    """Same backfill logic as _roster_fields_config, for the Staff
    Directory's Name/Title/Email/Phone/Social display toggles."""
    try:
        saved = json.loads(settings["fields_config"]) if settings and settings.get("fields_config") else []
    except (ValueError, TypeError):
        saved = []
    for f in saved:
        f.setdefault("label", None)
    seen = {f["key"] for f in saved}
    for d in DEFAULT_STAFF_FIELDS_CONFIG:
        if d["key"] not in seen:
            saved.append(dict(d))
    return saved


def _staff_field_label(field_entry):
    return field_entry.get("label") or STAFF_FIELD_LABELS.get(field_entry["key"], field_entry["key"])


app.jinja_env.globals["staff_field_label"] = _staff_field_label


def _parse_staff_form(f):
    return {
        "first_name": (f.get("first_name") or "").strip(),
        "last_name": (f.get("last_name") or "").strip(),
        "title": f.get("title") or None,
        "email": f.get("email") or None,
        "phone": f.get("phone") or None,
        "photo_url": f.get("photo_url") or None,
        "bio_text": f.get("bio_text") or None,
        "hidden_from_directory": f.get("hidden_from_directory") == "on",
    }


def _parse_staff_custom_fields_form(f):
    labels = f.getlist("custom_field_label")
    values = f.getlist("custom_field_value")
    fields = []
    for label, value in zip(labels, values):
        label = label.strip()
        if label:
            fields.append((label, value.strip()))
    return fields


def _save_staff_custom_fields(conn, staff_id, fields):
    """Delete-and-reinsert on every save — same convention as
    _save_article_links; there's no per-field identity worth preserving
    across edits, only the current list."""
    conn.execute("DELETE FROM staff_custom_fields WHERE staff_id=?", (staff_id,))
    conn.executemany(
        "INSERT INTO staff_custom_fields (staff_id, label, value, sort_order) VALUES (?, ?, ?, ?)",
        [(staff_id, label, value, i) for i, (label, value) in enumerate(fields)],
    )


def _save_staff_category_assignments(conn, staff_id, category_ids):
    """Unlike custom fields/links above, this deliberately does NOT reset
    sort_order for a category a person was already in — a routine bio edit
    shouldn't silently bump them to the bottom of a category's carefully
    hand-ranked order. A newly-checked category appends them at the end of
    that category's current order instead of guessing a position."""
    category_ids = set(category_ids)
    existing = {
        r["category_id"]: r["sort_order"] for r in conn.execute(
            "SELECT category_id, sort_order FROM staff_category_assignments WHERE staff_id=?", (staff_id,)
        ).fetchall()
    }
    conn.execute("DELETE FROM staff_category_assignments WHERE staff_id=?", (staff_id,))
    for category_id in category_ids:
        if category_id in existing:
            sort_order = existing[category_id]
        else:
            max_sort = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) as m FROM staff_category_assignments WHERE category_id=?",
                (category_id,),
            ).fetchone()["m"]
            sort_order = max_sort + 1
        conn.execute(
            "INSERT INTO staff_category_assignments (staff_id, category_id, sort_order) VALUES (?, ?, ?)",
            (staff_id, category_id, sort_order),
        )


def _compute_coach_career_record(conn, staff_id):
    """Aggregates every final, record-eligible game across every team/season
    this person is flagged Head Coach for on roster_coaches — the "tracking
    their career record based on results in the schedule" ask — PLUS
    whatever's been hand-entered in staff_manual_records for seasons that
    computation can't see (coached before joining this school; a mid-season
    change here that the single is_head_coach flag can't split correctly).
    Reuses the same counts_toward_record/result_letter predicates the public
    Record Bar does, so a scrimmage/exhibition/Neutral Host game is excluded
    from the computed portion too. IMPORTANT: the computed "stints" below
    only ever reflect games in *this school's* own games table — a coach's
    record from a previous school never shows up on its own; that's exactly
    what a manual entry is for. A team/season can only ever have one
    is_head_coach=1 row at a time (admin_roster_coaches_set_head enforces
    that), so a mid-season coaching change isn't split between two people
    for that season by the computed half alone — a known, pre-existing
    simplification of this schema; a manual entry is the fix for it, not a
    schema change."""
    stints = conn.execute(
        """SELECT roster_coaches.team_id, roster_coaches.season, teams.name as team_name
           FROM roster_coaches JOIN teams ON roster_coaches.team_id = teams.id
           WHERE roster_coaches.staff_id=? AND roster_coaches.is_head_coach=1
           ORDER BY roster_coaches.season DESC, teams.sort_order""",
        (staff_id,),
    ).fetchall()
    breakdown = []
    total_w = total_l = total_t = 0
    for s in stints:
        games = conn.execute(
            "SELECT * FROM games WHERE team_id=? AND season=? AND status='final'",
            (s["team_id"], s["season"]),
        ).fetchall()
        w = l = t = 0
        for g in games:
            if not counts_toward_record(g):
                continue
            r = result_letter(g["score_for"], g["score_against"])
            if r == "W":
                w += 1
            elif r == "L":
                l += 1
            elif r == "T":
                t += 1
        total_w += w
        total_l += l
        total_t += t
        breakdown.append({
            "team_name": s["team_name"], "season": s["season"], "w": w, "l": l, "t": t, "source": "computed",
        })

    manual_rows = conn.execute(
        "SELECT * FROM staff_manual_records WHERE staff_id=? ORDER BY season DESC, sort_order",
        (staff_id,),
    ).fetchall()
    manual_entries = []
    for m in manual_rows:
        total_w += m["wins"]
        total_l += m["losses"]
        total_t += m["ties"]
        manual_entries.append({
            "id": m["id"], "team_name": m["team_name"], "season": m["season"],
            "w": m["wins"], "l": m["losses"], "t": m["ties"], "note": m["note"], "source": "manual",
        })

    total = total_w + total_l + total_t
    return {
        "stints": breakdown,
        "manual_entries": manual_entries,
        "overall": {"w": total_w, "l": total_l, "t": total_t, "total": total},
    }


def _save_staff_social_links(conn, staff_id, links):
    """Only ever called right after a brand-new staff row is inserted (the
    New Staff form's own Social Links repeater), so a plain append is
    correct here — an existing profile's Social Links card still uses its
    own one-at-a-time add/delete forms (admin_staff_social_add/_delete),
    same as a Player's, since by then there's a real staff_id for each
    "+ Add Link" postback to target."""
    conn.executemany(
        "INSERT INTO staff_social_links (staff_id, platform, handle_or_url, sort_order) VALUES (?, ?, ?, ?)",
        [(staff_id, platform, handle, i) for i, (platform, handle) in enumerate(links)],
    )


def _parse_staff_social_form(f):
    platforms = f.getlist("social_platform")
    handles = f.getlist("social_handle_or_url")
    links = []
    for platform, handle in zip(platforms, handles):
        handle = handle.strip()
        if handle:
            links.append((platform or "other", handle))
    return links


def _apply_due_publishes(conn):
    """Lazy stand-in for a real scheduler: this prototype has no background
    cron, so any roster/schedule season with a publish_at in the past that
    hasn't fired yet gets flipped live (hidden=0, and Current too if
    publish_makes_current) the next time any request touches the app.
    publish_applied stops it from re-firing and clobbering a Current an
    admin has since changed by hand. Wired up as a before_request hook so
    every admin and public page benefits without each route remembering to
    call it."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    due_rosters = conn.execute(
        "SELECT * FROM roster_settings WHERE publish_at IS NOT NULL AND publish_at<=? AND publish_applied=0",
        (now,),
    ).fetchall()
    for r in due_rosters:
        conn.execute(
            "UPDATE roster_settings SET hidden=0, publish_applied=1 WHERE team_id=? AND season=?",
            (r["team_id"], r["season"]),
        )
        if r["publish_makes_current"]:
            conn.execute("UPDATE teams SET current_roster_season=? WHERE id=?", (r["season"], r["team_id"]))
    due_schedules = conn.execute(
        "SELECT * FROM schedule_settings WHERE publish_at IS NOT NULL AND publish_at<=? AND publish_applied=0",
        (now,),
    ).fetchall()
    for r in due_schedules:
        conn.execute(
            "UPDATE schedule_settings SET hidden=0, publish_applied=1 WHERE team_id=? AND season=?",
            (r["team_id"], r["season"]),
        )
        if r["publish_makes_current"]:
            conn.execute("UPDATE teams SET current_schedule_season=? WHERE id=?", (r["season"], r["team_id"]))
    # Same mechanism for the Story System's Release Date & Time — see the
    # articles.published_at/hidden/publish_applied comment in schema.sql.
    # Only a 'published' (approved) story is ever eligible; a draft or
    # pending-review story with a release date set just has that date
    # sitting there inert until someone approves it.
    due_articles = conn.execute(
        """SELECT id FROM articles
           WHERE status='published' AND published_at IS NOT NULL AND published_at<=? AND publish_applied=0""",
        (now,),
    ).fetchall()
    for a in due_articles:
        conn.execute("UPDATE articles SET hidden=0, publish_applied=1 WHERE id=?", (a["id"],))
    if due_rosters or due_schedules or due_articles:
        conn.commit()


@app.before_request
def _run_due_publishes():
    conn = get_db()
    try:
        _apply_due_publishes(conn)
    finally:
        conn.close()


def compute_record_bar(conn, team_id):
    """Every Record Bar stat from data-model.md §5e, computed live from
    `games` — nothing stored. All of it respects counts_toward_record."""
    rows = conn.execute(
        "SELECT * FROM games WHERE team_id=? AND status='final' ORDER BY date, id", (team_id,)
    ).fetchall()
    eligible = [g for g in rows if counts_toward_record(g)]

    def tally(games):
        w = l = t = 0
        for g in games:
            r = result_letter(g["score_for"], g["score_against"])
            if r == "W":
                w += 1
            elif r == "L":
                l += 1
            elif r == "T":
                t += 1
        total = w + l + t
        pct = ((w + 0.5 * t) / total) if total else None
        return {"w": w, "l": l, "t": t, "total": total, "pct": pct}

    def fmt(rec):
        if not rec["total"]:
            return "—"
        return f"{rec['w']}-{rec['l']}-{rec['t']}" if rec["t"] else f"{rec['w']}-{rec['l']}"

    def fmt_pct(rec):
        return f"{rec['pct']:.3f}".lstrip("0") if rec["pct"] is not None else "—"

    overall = tally(eligible)
    conference = tally([g for g in eligible if g["game_type"] == "conference"])
    home = tally([g for g in eligible if g["home_away"] == "home"])
    away = tally([g for g in eligible if g["home_away"] == "away"])
    neutral = tally([g for g in eligible if g["home_away"] == "neutral"])
    last10 = tally(eligible[-10:])

    streak_type, streak_count = None, 0
    for g in reversed(eligible):
        r = result_letter(g["score_for"], g["score_against"])
        if not r:
            continue
        if streak_type is None:
            streak_type, streak_count = r, 1
        elif r == streak_type:
            streak_count += 1
        else:
            break
    streak = f"{streak_type}{streak_count}" if streak_type else "—"

    return {
        "overall": fmt(overall), "pct": fmt_pct(overall),
        "conference": fmt(conference), "conference_pct": fmt_pct(conference),
        "home": fmt(home), "away": fmt(away), "neutral": fmt(neutral),
        "streak": streak, "last10": fmt(last10),
    }


@app.route("/")
def index():
    conn = get_db()
    school = get_school(conn)
    conn.close()
    return render_template("index.html", school=school)


# ---------------------------------------------------------------------
# Admin (CMS) routes
# ---------------------------------------------------------------------

@app.route("/admin")
def admin_dashboard():
    conn = get_db()
    school = get_school(conn)

    # "Today's Games" — game-day operations, not a schedule browser: every
    # game across every team on one date, with same-page inline result
    # entry, since that's the moment someone's actually looking at this
    # dashboard for. Defaults to the real current date; ?date= lets an
    # admin step to another day (yesterday's late finals, tomorrow's slate)
    # without that being a whole separate screen.
    date_param = request.args.get("date")
    try:
        selected_date = datetime.strptime(date_param, "%Y-%m-%d").date() if date_param else datetime.now().date()
    except ValueError:
        selected_date = datetime.now().date()
    selected_date_str = selected_date.strftime("%Y-%m-%d")
    prev_date_str = (selected_date - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date_str = (selected_date + timedelta(days=1)).strftime("%Y-%m-%d")
    is_today = selected_date == datetime.now().date()

    todays_games = conn.execute(
        """SELECT games.*, teams.name as team_name, teams.results_format as team_results_format,
                  teams.sport as team_sport, opponents.name as opponent_name
           FROM games JOIN teams ON games.team_id = teams.id
                      JOIN opponents ON games.opponent_id = opponents.id
           WHERE teams.school_id = ? AND games.date = ?
           ORDER BY teams.sort_order, games.time""",
        (school["id"], selected_date_str),
    ).fetchall()

    upcoming_count = conn.execute(
        """SELECT COUNT(*) c FROM games JOIN teams ON games.team_id = teams.id
           WHERE teams.school_id = ? AND games.status = 'upcoming'""",
        (school["id"],),
    ).fetchone()["c"]
    draft_pages = conn.execute(
        "SELECT COUNT(*) c FROM pages WHERE school_id=? AND status='draft'", (school["id"],)
    ).fetchone()["c"]
    pending_news = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE school_id=? AND status='pending_review'", (school["id"],)
    ).fetchone()["c"]
    active_staff = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE school_id=? AND status='active'", (school["id"],)
    ).fetchone()["c"]
    recent_articles = conn.execute(
        "SELECT * FROM articles WHERE school_id=? ORDER BY published_at DESC LIMIT 5", (school["id"],)
    ).fetchall()
    conn.close()

    todays_games_enriched = []
    for g in todays_games:
        row = dict(g)
        row["time_info"] = game_time_info(row, school)
        row["location"] = ", ".join(p for p in (row.get("city"), row.get("state")) if p) or None
        # Same "only a listed sport gets the UI" contract as _team_stat_categories
        # (can't call it directly here — this row's team column is aliased
        # team_sport, not sport, from the join above).
        row["has_stats"] = bool(TEAM_STAT_CATEGORIES.get(row["team_sport"]))
        todays_games_enriched.append(row)

    return render_template(
        "admin_dashboard.html", school=school, todays_games=todays_games_enriched,
        selected_date=selected_date_str, prev_date=prev_date_str, next_date=next_date_str, is_today=is_today,
        upcoming_count=upcoming_count, draft_pages=draft_pages,
        pending_news=pending_news, active_staff=active_staff, recent_articles=recent_articles, user=CURRENT_USER,
    )


@app.route("/admin/schedule/<int:game_id>/quick_result", methods=["POST"])
def admin_schedule_quick_result(game_id):
    """Same-page results entry from the dashboard's Today's Games list —
    the small subset of the full Edit Game form (score, or finish place
    for place_finish teams) someone actually needs mid-game-day, without
    leaving the dashboard for the full form. Always marks the game Final
    with score_finalized_by='manual_entry', same tagging the full form uses."""
    f = request.form
    conn = get_db()
    game = conn.execute(
        "SELECT games.*, teams.results_format as team_results_format FROM games "
        "JOIN teams ON games.team_id = teams.id WHERE games.id=?",
        (game_id,),
    ).fetchone()
    if game:
        if game["team_results_format"] == "place_finish":
            conn.execute(
                """UPDATE games SET status='final', result_type='standard', finish_place=?, total_teams=?,
                                     score_finalized_by='manual_entry' WHERE id=?""",
                (f.get("finish_place", type=int), f.get("total_teams", type=int), game_id),
            )
        else:
            conn.execute(
                """UPDATE games SET status='final', result_type='standard', score_for=?, score_against=?,
                                     score_finalized_by='manual_entry' WHERE id=?""",
                (f.get("score_for", type=int), f.get("score_against", type=int), game_id),
            )
        conn.commit()
    conn.close()
    date_param = f.get("date") or None
    return redirect(url_for("admin_dashboard", date=date_param) if date_param else url_for("admin_dashboard"))


@app.route("/admin/settings")
def admin_settings():
    """A hub page, same shape as the Rosters/Schedule hubs — direct feedback
    that the left nav had grown too many one-off links (Opponents, Users &
    Access) that aren't touched often enough to deserve top billing next to
    Dashboard/News/Schedule/Rosters/Players. More settings screens land as
    cards here as they're built; nothing about Opponents' or Users &
    Access's own routes/behavior changed, just what links to them."""
    conn = get_db()
    school = get_school(conn)
    conn.close()
    return render_template("admin_settings.html", school=school, user=CURRENT_USER)


@app.route("/admin/users")
def admin_users():
    conn = get_db()
    school = get_school(conn)
    users = conn.execute(
        """SELECT users.*, roles.name as role_name FROM users
           JOIN roles ON users.role_id = roles.id
           WHERE users.school_id=? ORDER BY roles.rank""",
        (school["id"],),
    ).fetchall()
    rows = []
    for u in users:
        scopes = conn.execute(
            """SELECT user_team_scopes.scope_type, teams.name as team_name FROM user_team_scopes
               LEFT JOIN teams ON user_team_scopes.team_id = teams.id WHERE user_id=?""",
            (u["id"],),
        ).fetchall()
        rows.append({"user": u, "scopes": scopes})
    conn.close()
    return render_template("admin_users.html", school=school, rows=rows, user=CURRENT_USER)


@app.route("/admin/users/<int:user_id>/toggle_byline_default", methods=["POST"])
def admin_user_toggle_byline_default(user_id):
    conn = get_db()
    school = get_school(conn)
    u = conn.execute("SELECT * FROM users WHERE id=? AND school_id=?", (user_id, school["id"])).fetchone()
    if u:
        conn.execute(
            "UPDATE users SET show_byline_by_default=? WHERE id=?",
            (0 if u["show_byline_by_default"] else 1, user_id),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/rosters")
def admin_rosters():
    conn = get_db()
    school = get_school(conn)
    teams = conn.execute(
        "SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)
    ).fetchall()
    view = request.args.get("view")
    team_id = request.args.get("team", type=int)
    season_param = request.args.get("season")

    # Browse by Sport, step 1 — pick which team's history to look at.
    if view == "by_sport" and not team_id:
        conn.close()
        return render_template(
            "admin_rosters_hub.html", school=school, teams=teams, mode="by_sport_pick_team", user=CURRENT_USER,
        )

    # Browse by Sport, step 2 — every season that team has a roster for.
    if view == "by_sport" and team_id:
        picked_team = next((t for t in teams if t["id"] == team_id), None)
        if not picked_team:
            conn.close()
            abort(404)
        seasons_for_team = [
            r["season"] for r in conn.execute(
                "SELECT DISTINCT season FROM roster_entries WHERE team_id=? ORDER BY season DESC", (team_id,)
            ).fetchall()
        ]
        conn.close()
        return render_template(
            "admin_rosters_hub.html", school=school, teams=teams, mode="by_sport_pick_season",
            picked_team=picked_team, seasons_for_team=seasons_for_team,
            current_roster_season=picked_team["current_roster_season"], user=CURRENT_USER,
        )

    # Browse by Season, step 1 — every season any team has a roster for.
    if view == "by_season" and not season_param:
        all_seasons = [
            r["season"] for r in conn.execute(
                """SELECT DISTINCT roster_entries.season FROM roster_entries
                   JOIN teams ON roster_entries.team_id = teams.id
                   WHERE teams.school_id=? ORDER BY roster_entries.season DESC""",
                (school["id"],),
            ).fetchall()
        ]
        conn.close()
        return render_template(
            "admin_rosters_hub.html", school=school, teams=teams, mode="by_season_pick_season",
            all_seasons=all_seasons, user=CURRENT_USER,
        )

    # Browse by Season, step 2 — every team that has a roster for that season.
    if view == "by_season" and season_param:
        teams_for_season = conn.execute(
            """SELECT DISTINCT teams.* FROM teams JOIN roster_entries ON roster_entries.team_id = teams.id
               WHERE teams.school_id=? AND roster_entries.season=? ORDER BY teams.sort_order""",
            (school["id"], season_param),
        ).fetchall()
        conn.close()
        return render_template(
            "admin_rosters_hub.html", school=school, teams=teams, mode="by_season_pick_team",
            picked_season=season_param, teams_for_season=teams_for_season, user=CURRENT_USER,
        )

    # A specific team's roster — the detail/edit view. Reached only by an
    # explicit ?team=, never as the bare-URL default any more (see below) —
    # landing on a specific team by default was exactly what made it too
    # easy to edit the wrong sport without noticing.
    if team_id:
        current_team = next((t for t in teams if t["id"] == team_id), None)
        if not current_team:
            conn.close()
            abort(404)
        seasons = [
            r["season"] for r in conn.execute(
                "SELECT DISTINCT season FROM roster_entries WHERE team_id=? ORDER BY season DESC", (current_team["id"],)
            ).fetchall()
        ]
        resolved_current = _resolve_current_season(
            conn, current_team["id"], "roster_entries", current_team["current_roster_season"]
        )
        current_season = season_param or resolved_current
        roster = []
        settings = None
        coaches = []
        if current_season:
            settings = _get_or_create_roster_settings(conn, current_team["id"], current_season)
            order_by = "athletes.last_name, athletes.first_name" if settings["sort_mode"] == "alpha" \
                else "roster_entries.sort_order, roster_entries.jersey_number"
            roster = conn.execute(
                f"""SELECT roster_entries.*, athletes.first_name, athletes.last_name, athletes.hometown,
                          athletes.high_school, athletes.previous_school
                   FROM roster_entries JOIN athletes ON roster_entries.athlete_id = athletes.id
                   WHERE roster_entries.team_id=? AND roster_entries.season=?
                   ORDER BY {order_by}""",
                (current_team["id"], current_season),
            ).fetchall()
            coaches = conn.execute(
                """SELECT roster_coaches.*, staff_members.first_name, staff_members.last_name,
                          (staff_members.first_name || ' ' || staff_members.last_name) as name,
                          staff_members.title, staff_members.photo_url
                   FROM roster_coaches JOIN staff_members ON roster_coaches.staff_id = staff_members.id
                   WHERE roster_coaches.team_id=? AND roster_coaches.season=?
                   ORDER BY roster_coaches.is_head_coach DESC, roster_coaches.sort_order""",
                (current_team["id"], current_season),
            ).fetchall()
        conn.close()
        return render_template(
            "admin_rosters.html", school=school, teams=teams, current_team=current_team, roster=roster,
            seasons=seasons, current_season=current_season, user=CURRENT_USER, roster_templates=ROSTER_TEMPLATES,
            is_current_season=(bool(current_season) and current_season == resolved_current),
            settings=settings, roster_field_labels=ROSTER_FIELD_LABELS,
            fields_config=_roster_fields_config(settings) if settings else [],
            sort_modes=SORT_MODES, roster_title=_roster_title(school, current_team, current_season, settings["custom_title"]) if settings else None,
            coaches=coaches,
        )

    # Bare /admin/rosters — the new default landing: one card per team
    # showing whichever season is Current for its roster, so picking a sport
    # to edit is always a deliberate click rather than "whatever the tabs
    # happened to land on."
    cards = []
    for t in teams:
        cs = _resolve_current_season(conn, t["id"], "roster_entries", t["current_roster_season"])
        count = conn.execute(
            "SELECT COUNT(*) c FROM roster_entries WHERE team_id=? AND season=?", (t["id"], cs)
        ).fetchone()["c"] if cs else 0
        cards.append({"team": t, "season": cs, "count": count})
    conn.close()
    return render_template(
        "admin_rosters_hub.html", school=school, teams=teams, mode="current", cards=cards, user=CURRENT_USER,
    )


@app.route("/admin/rosters/current", methods=["POST"])
def admin_rosters_mark_current():
    f = request.form
    team_id = f.get("team_id", type=int)
    season = f.get("season")
    conn = get_db()
    conn.execute("UPDATE teams SET current_roster_season=? WHERE id=?", (season, team_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_rosters", team=team_id, season=season))


@app.route("/admin/rosters/settings", methods=["POST"])
def admin_rosters_settings():
    f = request.form
    team_id = f.get("team_id", type=int)
    season = f.get("season", "")
    conn = get_db()
    if f.get("roster_template"):
        conn.execute("UPDATE teams SET roster_template=? WHERE id=?", (f.get("roster_template"), team_id))

    if season:
        _get_or_create_roster_settings(conn, team_id, season)
        # Fields/order/labels are edited on their own dedicated drag-and-drop
        # page (see /admin/rosters/fields) and saved through its own route —
        # this form never touches fields_config, so it's left out of this
        # UPDATE entirely (a plain column-list UPDATE would otherwise need to
        # carry it along just to leave it unchanged).
        # Re-saving with a publish_at always re-arms it (publish_applied=0) —
        # simplest correct behavior for "I changed the go-live time."
        conn.execute(
            """UPDATE roster_settings SET custom_title=?, team_photo_url=?, show_coaches=?, sort_mode=?,
                                           hidden=?, publish_at=?, publish_makes_current=?, publish_applied=0
               WHERE team_id=? AND season=?""",
            (
                f.get("custom_title") or None, f.get("team_photo_url") or None,
                1 if f.get("show_coaches") == "on" else 0, f.get("sort_mode") or "jersey",
                1 if f.get("hidden") == "on" else 0, f.get("publish_at") or None,
                1 if f.get("publish_makes_current") == "on" else 0,
                team_id, season,
            ),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_rosters", team=team_id, season=season))


@app.route("/admin/rosters/fields")
def admin_roster_fields():
    """Its own page (not a popup, but reached and returned-from like one)
    for reordering/relabeling/toggling roster fields via drag-and-drop —
    split out from the main Roster Settings form because a checkbox+typed-
    order-number list for 13 fields was cramped inline, and drag-and-drop
    needs its own JS-managed list anyway."""
    conn = get_db()
    school = get_school(conn)
    team_id = request.args.get("team", type=int)
    season = request.args.get("season")
    team = conn.execute("SELECT * FROM teams WHERE id=? AND school_id=?", (team_id, school["id"])).fetchone()
    if not team or not season:
        conn.close()
        abort(404)
    settings = _get_or_create_roster_settings(conn, team_id, season)
    fields_config = _roster_fields_config(settings)
    conn.close()
    return render_template(
        "admin_roster_fields.html", school=school, team=team, season=season,
        fields_config=fields_config, roster_field_labels=ROSTER_FIELD_LABELS, user=CURRENT_USER,
    )


@app.route("/admin/rosters/fields/save", methods=["POST"])
def admin_roster_fields_save():
    f = request.form
    team_id = f.get("team_id", type=int)
    season = f.get("season")
    try:
        fields_config = json.loads(f.get("fields_config_json") or "[]")
    except ValueError:
        fields_config = []
    conn = get_db()
    _get_or_create_roster_settings(conn, team_id, season)
    conn.execute(
        "UPDATE roster_settings SET fields_config=? WHERE team_id=? AND season=?",
        (json.dumps(fields_config), team_id, season),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_rosters", team=team_id, season=season))


# ---------------------------------------------------------------------------
# Coaches — a tab on Roster Settings, same "shared directory, per-roster
# assignment" shape as Players/roster_entries: staff_members is the
# school-wide coach/staff directory, roster_coaches is what actually ties
# one of them to one team's specific-season roster (so a coaching change
# between seasons doesn't rewrite history, and letting a coach go mid-season
# can hide them from the public site without deleting their record).
# ---------------------------------------------------------------------------

@app.route("/admin/rosters/coaches")
def admin_roster_coaches():
    conn = get_db()
    school = get_school(conn)
    team_id = request.args.get("team", type=int)
    season = request.args.get("season")
    team = conn.execute("SELECT * FROM teams WHERE id=? AND school_id=?", (team_id, school["id"])).fetchone()
    if not team or not season:
        conn.close()
        abort(404)
    coaches = conn.execute(
        """SELECT roster_coaches.*, staff_members.first_name, staff_members.last_name,
                          (staff_members.first_name || ' ' || staff_members.last_name) as name,
                          staff_members.title, staff_members.photo_url
           FROM roster_coaches JOIN staff_members ON roster_coaches.staff_id = staff_members.id
           WHERE roster_coaches.team_id=? AND roster_coaches.season=?
           ORDER BY roster_coaches.is_head_coach DESC, roster_coaches.sort_order""",
        (team_id, season),
    ).fetchall()
    attached_ids = {c["staff_id"] for c in coaches}
    all_staff = conn.execute(
        "SELECT * FROM staff_members WHERE school_id=? ORDER BY last_name, first_name", (school["id"],)
    ).fetchall()
    available_staff = [s for s in all_staff if s["id"] not in attached_ids]
    conn.close()
    return render_template(
        "admin_roster_coaches.html", school=school, team=team, season=season, coaches=coaches,
        available_staff=available_staff, user=CURRENT_USER,
    )


@app.route("/admin/rosters/coaches/add", methods=["POST"])
def admin_roster_coaches_add():
    f = request.form
    conn = get_db()
    school = get_school(conn)
    team_id = f.get("team_id", type=int)
    season = f.get("season")
    staff_id = f.get("staff_id", type=int)
    if not staff_id:
        # Quick-add: this school has no staff on file yet for this role, so
        # rather than dead-ending here, create the staff_members row inline —
        # same "don't make the admin go somewhere else first" pattern as New
        # Athlete on the roster form. Only first/last/title are collected
        # here; everything else on the full Staff Bio (email, phone, photo,
        # categories, ...) gets filled in later from the Staff Directory.
        new_first = (f.get("new_first_name") or "").strip()
        new_last = (f.get("new_last_name") or "").strip()
        if new_first and new_last:
            cur = conn.execute(
                "INSERT INTO staff_members (school_id, team_id, first_name, last_name, title) VALUES (?, ?, ?, ?, ?)",
                (school["id"], team_id, new_first, new_last, f.get("new_title") or STAFF_COACH_DEFAULT_TITLE),
            )
            staff_id = cur.lastrowid
    if staff_id:
        already = conn.execute(
            "SELECT id FROM roster_coaches WHERE team_id=? AND season=? AND staff_id=?",
            (team_id, season, staff_id),
        ).fetchone()
        if not already:
            max_sort = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) as m FROM roster_coaches WHERE team_id=? AND season=?",
                (team_id, season),
            ).fetchone()["m"]
            conn.execute(
                "INSERT INTO roster_coaches (team_id, season, staff_id, sort_order) VALUES (?, ?, ?, ?)",
                (team_id, season, staff_id, max_sort + 1),
            )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_roster_coaches", team=team_id, season=season))


@app.route("/admin/rosters/coaches/<int:coach_id>/remove", methods=["POST"])
def admin_roster_coaches_remove(coach_id):
    conn = get_db()
    rc = conn.execute("SELECT * FROM roster_coaches WHERE id=?", (coach_id,)).fetchone()
    conn.execute("DELETE FROM roster_coaches WHERE id=?", (coach_id,))
    conn.commit()
    conn.close()
    if rc:
        return redirect(url_for("admin_roster_coaches", team=rc["team_id"], season=rc["season"]))
    return redirect(url_for("admin_rosters"))


@app.route("/admin/rosters/coaches/<int:coach_id>/toggle_hidden", methods=["POST"])
def admin_roster_coaches_toggle_hidden(coach_id):
    conn = get_db()
    rc = conn.execute("SELECT * FROM roster_coaches WHERE id=?", (coach_id,)).fetchone()
    if rc:
        conn.execute("UPDATE roster_coaches SET hidden=? WHERE id=?", (0 if rc["hidden"] else 1, coach_id))
        conn.commit()
    conn.close()
    if rc:
        return redirect(url_for("admin_roster_coaches", team=rc["team_id"], season=rc["season"]))
    return redirect(url_for("admin_rosters"))


@app.route("/admin/rosters/coaches/<int:coach_id>/set_head", methods=["POST"])
def admin_roster_coaches_set_head(coach_id):
    conn = get_db()
    rc = conn.execute("SELECT * FROM roster_coaches WHERE id=?", (coach_id,)).fetchone()
    if rc:
        # Exactly one Head Coach per team/season, for coach-record tracking
        # down the road — clear any other head-coach flag in this same
        # team/season before setting this one.
        conn.execute(
            "UPDATE roster_coaches SET is_head_coach=0 WHERE team_id=? AND season=?",
            (rc["team_id"], rc["season"]),
        )
        conn.execute("UPDATE roster_coaches SET is_head_coach=1 WHERE id=?", (coach_id,))
        conn.commit()
    conn.close()
    if rc:
        return redirect(url_for("admin_roster_coaches", team=rc["team_id"], season=rc["season"]))
    return redirect(url_for("admin_rosters"))


@app.route("/admin/rosters/export")
def admin_rosters_export():
    conn = get_db()
    school = get_school(conn)
    team_id = request.args.get("team", type=int)
    season = request.args.get("season")
    team = conn.execute("SELECT * FROM teams WHERE id=? AND school_id=?", (team_id, school["id"])).fetchone()
    if not team:
        conn.close()
        abort(404)
    roster = conn.execute(
        """SELECT roster_entries.*, athletes.first_name, athletes.last_name, athletes.hometown, athletes.high_school
           FROM roster_entries JOIN athletes ON roster_entries.athlete_id = athletes.id
           WHERE roster_entries.team_id=? AND roster_entries.season=?
           ORDER BY roster_entries.sort_order, roster_entries.jersey_number""",
        (team_id, season),
    ).fetchall()
    conn.close()

    # The reverse of Paste Import — same column order, so a roster exported
    # here pastes back in cleanly on the other end (or into another team's
    # import, or a spreadsheet).
    lines = [",".join(IMPORT_COLUMNS)]
    for p in roster:
        row = [
            p["first_name"], p["last_name"], p["jersey_number"] or "", p["position"] or "",
            p["class_year"] or "", height_display(p["height_ft"], p["height_in"]) or "",
            p["weight"] or "", p["hometown"] or "", p["high_school"] or "",
        ]
        # Minimal CSV-safe quoting — wrap any field containing a comma or quote.
        escaped = []
        for cell in row:
            cell = str(cell)
            if "," in cell or '"' in cell:
                cell = '"' + cell.replace('"', '""') + '"'
            escaped.append(cell)
        lines.append(",".join(escaped))
    csv_text = "\r\n".join(lines) + "\r\n"

    filename = f"{team['slug']}-roster-{season}.csv"
    return Response(
        csv_text, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Bulk Edit — every roster entry's editable fields as plain text boxes in one
# big form, saved together in one commit. Distinct from Paste Import (which
# adds *new* players): this is for touching several *existing* entries at
# once — a jersey renumbering, a bunch of hometowns typed in, etc. — without
# opening each player's Edit screen one at a time.
# ---------------------------------------------------------------------------

@app.route("/admin/rosters/bulk_edit")
def admin_rosters_bulk_edit():
    conn = get_db()
    school = get_school(conn)
    team_id = request.args.get("team", type=int)
    season = request.args.get("season")
    team = conn.execute("SELECT * FROM teams WHERE id=? AND school_id=?", (team_id, school["id"])).fetchone()
    if not team or not season:
        conn.close()
        abort(404)
    roster = conn.execute(
        """SELECT roster_entries.*, athletes.first_name, athletes.last_name
           FROM roster_entries JOIN athletes ON roster_entries.athlete_id = athletes.id
           WHERE roster_entries.team_id=? AND roster_entries.season=?
           ORDER BY roster_entries.sort_order, roster_entries.jersey_number""",
        (team_id, season),
    ).fetchall()
    settings = _get_or_create_roster_settings(conn, team_id, season)
    conn.close()
    return render_template(
        "admin_roster_bulk_edit.html", school=school, team=team, season=season, roster=roster,
        user=CURRENT_USER, settings=settings,
    )


@app.route("/admin/rosters/bulk_edit/commit", methods=["POST"])
def admin_rosters_bulk_edit_commit():
    f = request.form
    conn = get_db()
    team_id = f.get("team_id", type=int)
    season = f.get("season")
    count = int(f.get("row_count", 0))
    for i in range(count):
        prefix = f"row_{i}_"
        entry_id = f.get(prefix + "entry_id", type=int)
        if not entry_id:
            continue
        conn.execute(
            """UPDATE roster_entries SET jersey_number=?, position=?, class_year=?, height_ft=?, height_in=?,
                                          weight=?, bats_throws=?, custom_field_1=?, custom_field_2=?,
                                          custom_field_3=?, hidden=? WHERE id=?""",
            (
                f.get(prefix + "jersey_number") or None, f.get(prefix + "position") or None,
                f.get(prefix + "class_year") or None, f.get(prefix + "height_ft", type=int),
                f.get(prefix + "height_in", type=int), f.get(prefix + "weight") or None,
                f.get(prefix + "bats_throws") or None, f.get(prefix + "custom_field_1") or None,
                f.get(prefix + "custom_field_2") or None, f.get(prefix + "custom_field_3") or None,
                1 if f.get(prefix + "hidden") == "on" else 0, entry_id,
            ),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_rosters", team=team_id, season=season))


# ---------------------------------------------------------------------------
# Season Rollover — the real version of what was, until now, just "edit the
# Season field by hand on every returning player." Three steps: pick the new
# season, mark each current player Returning or Not Returning, then create
# the new season's roster from whoever's Returning — landing right back on
# that new roster, where Add Player / Paste Import handle the incoming
# class. "Not Returning" doesn't write any status anywhere — per direct
# feedback, a player is created once and stays in the system forever; simply
# not carrying their old entry into the new season's roster_entries *is* the
# graduated/transferred signal, on this one team only, with no separate
# status field to keep in sync (and nothing here ever touches any other
# team's roster_entries for that same person).
# ---------------------------------------------------------------------------

@app.route("/admin/rosters/rollover")
def admin_rosters_rollover():
    conn = get_db()
    school = get_school(conn)
    team_id = request.args.get("team", type=int)
    team = conn.execute("SELECT * FROM teams WHERE id=? AND school_id=?", (team_id, school["id"])).fetchone()
    if not team:
        conn.close()
        abort(404)
    seasons = [
        r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM roster_entries WHERE team_id=? ORDER BY season DESC", (team_id,)
        ).fetchall()
    ]
    conn.close()
    return render_template(
        "admin_roster_rollover.html", school=school, team=team, seasons=seasons, user=CURRENT_USER,
    )


@app.route("/admin/rosters/rollover/preview", methods=["POST"])
def admin_rosters_rollover_preview():
    conn = get_db()
    school = get_school(conn)
    f = request.form
    team_id = f.get("team_id", type=int)
    from_season = f.get("from_season")
    new_season = (f.get("new_season") or "").strip()
    team = conn.execute("SELECT * FROM teams WHERE id=? AND school_id=?", (team_id, school["id"])).fetchone()
    if not team or not new_season:
        conn.close()
        abort(404)
    roster = conn.execute(
        """SELECT roster_entries.*, athletes.first_name, athletes.last_name
           FROM roster_entries JOIN athletes ON roster_entries.athlete_id = athletes.id
           WHERE roster_entries.team_id=? AND roster_entries.season=?
           ORDER BY roster_entries.sort_order, roster_entries.jersey_number""",
        (team_id, from_season),
    ).fetchall()
    conn.close()
    return render_template(
        "admin_roster_rollover_review.html", school=school, team=team, from_season=from_season,
        new_season=new_season, roster=roster, user=CURRENT_USER,
    )


@app.route("/admin/rosters/rollover/commit", methods=["POST"])
def admin_rosters_rollover_commit():
    conn = get_db()
    f = request.form
    team_id = f.get("team_id", type=int)
    new_season = f.get("new_season")
    count = int(f.get("row_count", 0))

    max_sort = -1
    carried = 0
    for i in range(count):
        prefix = f"row_{i}_"
        entry_id = f.get(prefix + "entry_id", type=int)
        athlete_id = f.get(prefix + "athlete_id", type=int)
        returning = f.get(prefix + "returning", "yes") == "yes"
        old_entry = conn.execute("SELECT * FROM roster_entries WHERE id=?", (entry_id,)).fetchone()
        if not old_entry:
            continue

        if not returning:
            # Not returning to THIS team. Nothing to write anywhere — the old
            # entry stays exactly as it is (a true historical record of that
            # season), and simply not creating a new-season row for them here
            # is the entire signal. They may well still be active on a
            # second team; nothing here touches any other roster_entries row.
            continue

        max_sort += 1
        conn.execute(
            """INSERT INTO roster_entries
                 (athlete_id, team_id, season, jersey_number, position, class_year, height_ft, height_in, weight,
                  bats_throws, bio_text, custom_field_1, custom_field_2, custom_field_3, hidden, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (athlete_id, team_id, new_season, old_entry["jersey_number"], old_entry["position"],
             old_entry["class_year"], old_entry["height_ft"], old_entry["height_in"], old_entry["weight"],
             old_entry["bats_throws"], old_entry["bio_text"], old_entry["custom_field_1"],
             old_entry["custom_field_2"], old_entry["custom_field_3"], max_sort),
        )
        carried += 1

    # Rolling over is the clearest possible admin signal that the new season
    # is now the one that matters — auto-promote it to Current for this
    # team's roster (still changeable by hand afterward via Mark as Current).
    conn.execute("UPDATE teams SET current_roster_season=? WHERE id=?", (new_season, team_id))

    conn.commit()
    conn.close()
    return redirect(url_for("admin_rosters", team=team_id, season=new_season, rolled_over=carried))


def _roster_form_context(conn, school, team_id, roster_entry=None, athlete=None):
    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    # Every athlete this school has on file, school-wide (not just this team) —
    # this is what makes the "Existing Athlete" picker work for multi-sport
    # athletes and returning players. current_teams is a display-only hint
    # (which team/season(s) they're already attached to) so the admin can spot
    # a likely duplicate without this being a hard block on legitimate
    # multi-team assignments.
    athletes = conn.execute(
        """SELECT athletes.*,
                  (SELECT GROUP_CONCAT(teams.name || ' ' || roster_entries.season, ', ')
                     FROM roster_entries JOIN teams ON roster_entries.team_id = teams.id
                    WHERE roster_entries.athlete_id = athletes.id) as current_teams
           FROM athletes WHERE athletes.school_id=? ORDER BY athletes.last_name, athletes.first_name""",
        (school["id"],),
    ).fetchall()
    return {
        "school": school, "teams": teams, "athletes": athletes, "team_id": team_id,
        "roster_entry": roster_entry, "athlete": athlete, "user": CURRENT_USER,
        "class_years": CLASS_YEARS, "social_platforms": SOCIAL_PLATFORMS,
    }


@app.route("/admin/rosters/new")
def admin_rosters_new():
    conn = get_db()
    school = get_school(conn)
    team_id = request.args.get("team", type=int)
    ctx = _roster_form_context(conn, school, team_id)
    # Reached from a player's own profile via "+ Add to a Roster" — preselect
    # Existing Athlete mode with that specific person already chosen, and
    # send them back to their profile (not the team roster list) on save.
    ctx["preselect_athlete_id"] = request.args.get("athlete_id", type=int)
    ctx["next_url"] = request.args.get("next")
    # Team and Season are locked to whichever roster this was launched from —
    # confirmed on-screen, not re-pickable, so a player can never accidentally
    # land on the wrong sport's roster. Defaults to that team's current
    # season, same resolution the roster hub/detail pages use.
    team = next((t for t in ctx["teams"] if t["id"] == team_id), None)
    ctx["default_season"] = _resolve_current_season(conn, team_id, "roster_entries", team["current_roster_season"]) if team else None
    conn.close()
    return render_template("admin_roster_form.html", **ctx)


def _parse_athlete_form(f):
    return {
        "first_name": (f.get("first_name") or "").strip(),
        "last_name": (f.get("last_name") or "").strip(),
        "hometown": f.get("hometown") or None,
        "high_school": f.get("high_school") or None,
        "previous_school": f.get("previous_school") or None,
        "headshot_url": f.get("headshot_url") or None,
        "bio_text": f.get("bio_text") or None,
        "nil_shop_url": f.get("nil_shop_url") or None,
        "academic_major": f.get("academic_major") or None,
        "nickname": f.get("nickname") or None,
        "pronunciation": f.get("pronunciation") or None,
        "birthdate": f.get("birthdate") or None,
    }


_ATHLETE_FIELDS = (
    "school_id, first_name, last_name, hometown, high_school, previous_school, headshot_url, bio_text, "
    "nil_shop_url, academic_major, nickname, pronunciation, birthdate"
)
_ATHLETE_PARAMS = (
    ":school_id, :first_name, :last_name, :hometown, :high_school, :previous_school, :headshot_url, :bio_text, "
    ":nil_shop_url, :academic_major, :nickname, :pronunciation, :birthdate"
)


def _parse_roster_entry_form(f):
    return {
        "team_id": f.get("team_id", type=int),
        "season": f.get("season") or "2026",
        "jersey_number": f.get("jersey_number") or None,
        "position": f.get("position") or None,
        "class_year": f.get("class_year") or None,
        "height_ft": f.get("height_ft", type=int),
        "height_in": f.get("height_in", type=int),
        "weight": f.get("weight") or None,
        "bats_throws": f.get("bats_throws") or None,
        # Deliberately a different form field name than the athlete-level
        # Master Bio textarea (both live in the same <form> on the roster
        # form/player-profile-linked edit) — same name would collide and
        # Werkzeug's MultiDict.get() would silently only ever save the first
        # one's value.
        "bio_text": f.get("roster_bio_text") or None,
        "custom_field_1": f.get("custom_field_1") or None,
        "custom_field_2": f.get("custom_field_2") or None,
        "custom_field_3": f.get("custom_field_3") or None,
        # Hides this player from this team's public roster without removing
        # the assignment — replaces the old Active/Graduated/Departed status
        # dropdown per direct feedback (see the Rollover comment above).
        "hidden": 1 if f.get("hidden") == "on" else 0,
    }


_ROSTER_ENTRY_FIELDS = (
    "athlete_id, team_id, season, jersey_number, position, class_year, height_ft, height_in, weight, bats_throws, "
    "bio_text, custom_field_1, custom_field_2, custom_field_3, hidden, sort_order"
)
_ROSTER_ENTRY_PARAMS = (
    ":athlete_id, :team_id, :season, :jersey_number, :position, :class_year, :height_ft, :height_in, :weight, "
    ":bats_throws, :bio_text, :custom_field_1, :custom_field_2, :custom_field_3, :hidden, :sort_order"
)


@app.route("/admin/rosters/new", methods=["POST"])
def admin_rosters_create():
    conn = get_db()
    school = get_school(conn)
    f = request.form
    mode = f.get("mode", "new")
    if mode == "existing":
        athlete_id = f.get("athlete_id", type=int)
    else:
        data = _parse_athlete_form(f)
        data["school_id"] = school["id"]
        cur = conn.execute(f"INSERT INTO athletes ({_ATHLETE_FIELDS}) VALUES ({_ATHLETE_PARAMS})", data)
        athlete_id = cur.lastrowid
        # New Athlete now shows the full player-profile field set in one go
        # (bio, miscellaneous, social links) instead of sending the admin to
        # a separate profile page afterward — so social links, if any were
        # filled in, get created right here alongside the athlete record.
        platforms = f.getlist("social_platform")
        handles = f.getlist("social_handle")
        for i, handle in enumerate(handles):
            handle = (handle or "").strip()
            if not handle:
                continue
            platform = platforms[i] if i < len(platforms) else "other"
            conn.execute(
                "INSERT INTO athlete_social_links (athlete_id, platform, handle_or_url, sort_order) VALUES (?, ?, ?, ?)",
                (athlete_id, platform, handle, i),
            )

    entry = _parse_roster_entry_form(f)
    entry["athlete_id"] = athlete_id
    max_sort = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) as m FROM roster_entries WHERE team_id=?", (entry["team_id"],)
    ).fetchone()["m"]
    entry["sort_order"] = max_sort + 1
    conn.execute(f"INSERT INTO roster_entries ({_ROSTER_ENTRY_FIELDS}) VALUES ({_ROSTER_ENTRY_PARAMS})", entry)
    conn.commit()
    team_id, season = entry["team_id"], entry["season"]
    conn.close()
    # "next" lets this be reused from the Players profile page's "Add to
    # another roster" flow, which wants to land back on that player's
    # profile instead of the team roster list.
    next_url = f.get("next")
    if next_url:
        return redirect(next_url)
    return redirect(url_for("admin_rosters", team=team_id, season=season))


@app.route("/admin/rosters/<int:entry_id>/edit")
def admin_rosters_edit(entry_id):
    conn = get_db()
    school = get_school(conn)
    roster_entry = conn.execute("SELECT * FROM roster_entries WHERE id=?", (entry_id,)).fetchone()
    if not roster_entry:
        conn.close()
        abort(404)
    athlete = conn.execute("SELECT * FROM athletes WHERE id=?", (roster_entry["athlete_id"],)).fetchone()
    ctx = _roster_form_context(conn, school, roster_entry["team_id"], roster_entry=roster_entry, athlete=athlete)
    conn.close()
    return render_template("admin_roster_form.html", **ctx)


@app.route("/admin/rosters/<int:entry_id>/edit", methods=["POST"])
def admin_rosters_update(entry_id):
    conn = get_db()
    roster_entry = conn.execute("SELECT * FROM roster_entries WHERE id=?", (entry_id,)).fetchone()
    if not roster_entry:
        conn.close()
        abort(404)
    f = request.form

    # By edit time the athlete's identity is already fixed (no New/Existing
    # toggle here) — this always updates both the linked athletes row's bio
    # fields and the roster_entries row's assignment fields together.
    athlete_data = _parse_athlete_form(f)
    athlete_set_clause = ", ".join(f"{k}=:{k}" for k in athlete_data)
    athlete_data["id"] = roster_entry["athlete_id"]
    conn.execute(f"UPDATE athletes SET {athlete_set_clause} WHERE id=:id", athlete_data)

    # Team and Season are locked on this screen — shown read-only just to
    # confirm which roster is being edited, not a way to move a player
    # between teams/seasons (that's what Season Rollover and the Existing
    # Athlete picker are for). Ignore whatever the form posted for them and
    # keep the entry's real values, even though the fields render read-only
    # client-side too — belt and suspenders.
    entry = _parse_roster_entry_form(f)
    entry["team_id"] = roster_entry["team_id"]
    entry["season"] = roster_entry["season"]
    entry_set_clause = ", ".join(f"{k}=:{k}" for k in entry)
    entry["id"] = entry_id
    conn.execute(f"UPDATE roster_entries SET {entry_set_clause} WHERE id=:id", entry)
    conn.commit()
    team_id, season = entry["team_id"], entry["season"]
    conn.close()
    next_url = f.get("next")
    if next_url:
        return redirect(next_url)
    return redirect(url_for("admin_rosters", team=team_id, season=season))


@app.route("/admin/rosters/<int:entry_id>/remove", methods=["POST"])
def admin_rosters_remove(entry_id):
    conn = get_db()
    roster_entry = conn.execute("SELECT * FROM roster_entries WHERE id=?", (entry_id,)).fetchone()
    team_id = roster_entry["team_id"] if roster_entry else None
    season = roster_entry["season"] if roster_entry else None
    # Only the team/season assignment is removed — the athletes person record
    # is left intact (they may still be attached to other teams/seasons, and
    # even if not, deleting the actual person record isn't this action's job).
    conn.execute("DELETE FROM roster_entries WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    next_url = request.form.get("next")
    if next_url:
        return redirect(next_url)
    return redirect(url_for("admin_rosters", team=team_id, season=season))


# ---------------------------------------------------------------------------
# Players — the school-wide athlete directory. roster_entries rows are just
# team/season assignments; athletes is the actual person record, and this is
# its own home independent of any one team's roster, matching the mental
# model of "a bunch of player files, assigned to rosters" rather than the
# player only existing inside whichever roster created them.
# ---------------------------------------------------------------------------

@app.route("/admin/players")
def admin_players():
    conn = get_db()
    school = get_school(conn)
    players = conn.execute(
        """SELECT athletes.*,
                  (SELECT GROUP_CONCAT(teams.name || ' ' || roster_entries.season, ', ')
                     FROM roster_entries JOIN teams ON roster_entries.team_id = teams.id
                    WHERE roster_entries.athlete_id = athletes.id
                    ORDER BY roster_entries.season DESC) as current_teams
           FROM athletes WHERE athletes.school_id=? ORDER BY athletes.last_name, athletes.first_name""",
        (school["id"],),
    ).fetchall()
    conn.close()
    return render_template("admin_players.html", school=school, players=players, user=CURRENT_USER)


@app.route("/admin/players/<int:athlete_id>")
def admin_player_profile(athlete_id):
    conn = get_db()
    school = get_school(conn)
    athlete = conn.execute("SELECT * FROM athletes WHERE id=? AND school_id=?", (athlete_id, school["id"])).fetchone()
    if not athlete:
        conn.close()
        abort(404)
    entries = conn.execute(
        """SELECT roster_entries.*, teams.name as team_name
           FROM roster_entries JOIN teams ON roster_entries.team_id = teams.id
           WHERE roster_entries.athlete_id=? ORDER BY roster_entries.season DESC, teams.sort_order""",
        (athlete_id,),
    ).fetchall()
    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    social_links = conn.execute(
        "SELECT * FROM athlete_social_links WHERE athlete_id=? ORDER BY sort_order", (athlete_id,)
    ).fetchall()
    conn.close()
    return render_template(
        "admin_player_profile.html", school=school, athlete=athlete, entries=entries, teams=teams,
        class_years=CLASS_YEARS, social_links=social_links,
        social_platforms=SOCIAL_PLATFORMS, user=CURRENT_USER,
    )


@app.route("/admin/players/<int:athlete_id>/edit", methods=["POST"])
def admin_player_update(athlete_id):
    conn = get_db()
    school = get_school(conn)
    athlete = conn.execute("SELECT * FROM athletes WHERE id=? AND school_id=?", (athlete_id, school["id"])).fetchone()
    if not athlete:
        conn.close()
        abort(404)
    data = _parse_athlete_form(request.form)
    set_clause = ", ".join(f"{k}=:{k}" for k in data)
    data["id"] = athlete_id
    conn.execute(f"UPDATE athletes SET {set_clause} WHERE id=:id", data)
    conn.commit()
    conn.close()
    return redirect(url_for("admin_player_profile", athlete_id=athlete_id))


@app.route("/admin/players/<int:athlete_id>/delete", methods=["POST"])
def admin_player_delete(athlete_id):
    conn = get_db()
    school = get_school(conn)
    athlete = conn.execute("SELECT * FROM athletes WHERE id=? AND school_id=?", (athlete_id, school["id"])).fetchone()
    if not athlete:
        conn.close()
        abort(404)
    # ON DELETE CASCADE on roster_entries.athlete_id means this also removes
    # every team/season assignment this person has, everywhere — the profile
    # page's confirm dialog says so explicitly before this route is ever hit.
    conn.execute("DELETE FROM athletes WHERE id=?", (athlete_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_players"))


@app.route("/admin/players/<int:athlete_id>/social/add", methods=["POST"])
def admin_player_social_add(athlete_id):
    conn = get_db()
    school = get_school(conn)
    athlete = conn.execute("SELECT * FROM athletes WHERE id=? AND school_id=?", (athlete_id, school["id"])).fetchone()
    if not athlete:
        conn.close()
        abort(404)
    handle = (request.form.get("handle_or_url") or "").strip()
    platform = request.form.get("platform") or "other"
    if handle:
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) as m FROM athlete_social_links WHERE athlete_id=?", (athlete_id,)
        ).fetchone()["m"]
        conn.execute(
            "INSERT INTO athlete_social_links (athlete_id, platform, handle_or_url, sort_order) VALUES (?, ?, ?, ?)",
            (athlete_id, platform, handle, max_sort + 1),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("admin_player_profile", athlete_id=athlete_id))


@app.route("/admin/players/<int:athlete_id>/social/<int:link_id>/delete", methods=["POST"])
def admin_player_social_delete(athlete_id, link_id):
    conn = get_db()
    conn.execute("DELETE FROM athlete_social_links WHERE id=? AND athlete_id=?", (link_id, athlete_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_player_profile", athlete_id=athlete_id))


# ---------------------------------------------------------------------------
# Staff Directory — the school-wide staff_members table finally gets its own
# admin screens, same "shared directory, per-context assignment" shape as
# Players/athletes: this is where a person's bio, categories, and social
# links live; attaching them to a specific team/season's roster still
# happens from that roster's Coaches & Staff tab, not from here.
# ---------------------------------------------------------------------------

@app.route("/admin/staff")
def admin_staff():
    conn = get_db()
    school = get_school(conn)
    _ensure_staff_categories(conn, school["id"])
    # Active is the default view — a departed staff member comes off this
    # list without being deleted (there's no delete at all any more, see
    # the Danger-Zone-turned-Status section on the profile page); ?status=
    # switches to see them again, e.g. to bring someone back or just fix a
    # typo in their bio after they've left.
    status_filter = request.args.get("status", "active")
    where = "staff_members.school_id=?"
    if status_filter == "active":
        where += " AND staff_members.active=1"
    elif status_filter == "inactive":
        where += " AND staff_members.active=0"
    staff = conn.execute(
        f"""SELECT staff_members.*,
                  (SELECT GROUP_CONCAT(COALESCE(teams.name, staff_categories.name), ', ')
                     FROM staff_category_assignments
                     JOIN staff_categories ON staff_category_assignments.category_id = staff_categories.id
                     LEFT JOIN teams ON staff_categories.team_id = teams.id
                    WHERE staff_category_assignments.staff_id = staff_members.id) as category_names,
                  (SELECT GROUP_CONCAT(teams.name || ' ' || roster_coaches.season, ', ')
                     FROM roster_coaches JOIN teams ON roster_coaches.team_id = teams.id
                    WHERE roster_coaches.staff_id = staff_members.id
                    ORDER BY roster_coaches.season DESC) as current_teams
           FROM staff_members WHERE {where}
           ORDER BY staff_members.last_name, staff_members.first_name""",
        (school["id"],),
    ).fetchall()
    conn.close()
    return render_template(
        "admin_staff.html", school=school, staff=staff, status_filter=status_filter, user=CURRENT_USER,
    )


@app.route("/admin/staff/new")
def admin_staff_new():
    conn = get_db()
    school = get_school(conn)
    _ensure_staff_categories(conn, school["id"])
    categories = _list_staff_categories(conn, school["id"])
    conn.close()
    return render_template(
        "admin_staff_form.html", school=school, staff=None, categories=categories,
        selected_category_ids=set(), custom_fields=[], social_links=[],
        social_platforms=SOCIAL_PLATFORMS, user=CURRENT_USER,
    )


@app.route("/admin/staff/new", methods=["POST"])
def admin_staff_create():
    conn = get_db()
    school = get_school(conn)
    data = _parse_staff_form(request.form)
    if not data["first_name"] or not data["last_name"]:
        conn.close()
        return redirect(url_for("admin_staff_new"))
    data["school_id"] = school["id"]
    cur = conn.execute(
        """INSERT INTO staff_members (school_id, first_name, last_name, title, email, phone, photo_url,
                                        bio_text, hidden_from_directory)
           VALUES (:school_id, :first_name, :last_name, :title, :email, :phone, :photo_url,
                   :bio_text, :hidden_from_directory)""",
        data,
    )
    staff_id = cur.lastrowid
    category_ids = [int(v) for v in request.form.getlist("category_ids")]
    _save_staff_category_assignments(conn, staff_id, category_ids)
    _save_staff_custom_fields(conn, staff_id, _parse_staff_custom_fields_form(request.form))
    _save_staff_social_links(conn, staff_id, _parse_staff_social_form(request.form))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_profile", staff_id=staff_id))


@app.route("/admin/staff/<int:staff_id>")
def admin_staff_profile(staff_id):
    conn = get_db()
    school = get_school(conn)
    staff = conn.execute(
        "SELECT * FROM staff_members WHERE id=? AND school_id=?", (staff_id, school["id"])
    ).fetchone()
    if not staff:
        conn.close()
        abort(404)
    _ensure_staff_categories(conn, school["id"])
    categories = _list_staff_categories(conn, school["id"])
    selected_category_ids = {
        r["category_id"] for r in conn.execute(
            "SELECT category_id FROM staff_category_assignments WHERE staff_id=?", (staff_id,)
        ).fetchall()
    }
    custom_fields = conn.execute(
        "SELECT * FROM staff_custom_fields WHERE staff_id=? ORDER BY sort_order", (staff_id,)
    ).fetchall()
    social_links = conn.execute(
        "SELECT * FROM staff_social_links WHERE staff_id=? ORDER BY sort_order", (staff_id,)
    ).fetchall()
    assignments = conn.execute(
        """SELECT roster_coaches.*, teams.name as team_name FROM roster_coaches
           JOIN teams ON roster_coaches.team_id = teams.id
           WHERE roster_coaches.staff_id=? ORDER BY roster_coaches.season DESC, teams.sort_order""",
        (staff_id,),
    ).fetchall()
    # Always computed, on every staff profile — not just current Head
    # Coaches. Manual entries (previous-school seasons, or hand-correcting a
    # season here) are how anyone's record gets built up over time, so the
    # "+ Add a Season Record" form needs to be reachable even for someone
    # who's never been flagged Head Coach at this school at all yet.
    career_record = _compute_coach_career_record(conn, staff_id)
    conn.close()
    return render_template(
        "admin_staff_profile.html", school=school, staff=staff, categories=categories,
        selected_category_ids=selected_category_ids, custom_fields=custom_fields, social_links=social_links,
        social_platforms=SOCIAL_PLATFORMS, assignments=assignments, career_record=career_record,
        user=CURRENT_USER,
    )


@app.route("/admin/staff/<int:staff_id>/edit", methods=["POST"])
def admin_staff_update(staff_id):
    conn = get_db()
    school = get_school(conn)
    staff = conn.execute(
        "SELECT * FROM staff_members WHERE id=? AND school_id=?", (staff_id, school["id"])
    ).fetchone()
    if not staff:
        conn.close()
        abort(404)
    data = _parse_staff_form(request.form)
    set_clause = ", ".join(f"{k}=:{k}" for k in data)
    data["id"] = staff_id
    conn.execute(f"UPDATE staff_members SET {set_clause} WHERE id=:id", data)
    category_ids = [int(v) for v in request.form.getlist("category_ids")]
    _save_staff_category_assignments(conn, staff_id, category_ids)
    _save_staff_custom_fields(conn, staff_id, _parse_staff_custom_fields_form(request.form))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_profile", staff_id=staff_id))


@app.route("/admin/staff/<int:staff_id>/toggle_active", methods=["POST"])
def admin_staff_toggle_active(staff_id):
    # Deliberately no delete route for staff at all any more — direct
    # feedback was that removing a staff member permanently shouldn't be
    # something a user can do regardless of access level. Deactivating
    # (and reactivating) is the only way off/back onto this list and the
    # public directory; every roster attachment, category placement, social
    # link, custom field, and manual career record stays intact either way.
    conn = get_db()
    school = get_school(conn)
    staff = conn.execute(
        "SELECT * FROM staff_members WHERE id=? AND school_id=?", (staff_id, school["id"])
    ).fetchone()
    if staff:
        conn.execute(
            "UPDATE staff_members SET active=? WHERE id=?", (0 if staff["active"] else 1, staff_id)
        )
        conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_profile", staff_id=staff_id))


@app.route("/admin/staff/<int:staff_id>/records/add", methods=["POST"])
def admin_staff_manual_record_add(staff_id):
    conn = get_db()
    school = get_school(conn)
    staff = conn.execute(
        "SELECT * FROM staff_members WHERE id=? AND school_id=?", (staff_id, school["id"])
    ).fetchone()
    if not staff:
        conn.close()
        abort(404)
    season = (request.form.get("season") or "").strip()
    team_name = (request.form.get("team_name") or "").strip()
    if season and team_name:
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) as m FROM staff_manual_records WHERE staff_id=?", (staff_id,)
        ).fetchone()["m"]
        conn.execute(
            """INSERT INTO staff_manual_records (staff_id, season, team_name, wins, losses, ties, note, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                staff_id, season, team_name,
                request.form.get("wins", type=int) or 0, request.form.get("losses", type=int) or 0,
                request.form.get("ties", type=int) or 0, request.form.get("note") or None, max_sort + 1,
            ),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_profile", staff_id=staff_id))


@app.route("/admin/staff/<int:staff_id>/records/<int:record_id>/delete", methods=["POST"])
def admin_staff_manual_record_delete(staff_id, record_id):
    conn = get_db()
    conn.execute("DELETE FROM staff_manual_records WHERE id=? AND staff_id=?", (record_id, staff_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_profile", staff_id=staff_id))


@app.route("/admin/staff/<int:staff_id>/social/add", methods=["POST"])
def admin_staff_social_add(staff_id):
    conn = get_db()
    school = get_school(conn)
    staff = conn.execute(
        "SELECT * FROM staff_members WHERE id=? AND school_id=?", (staff_id, school["id"])
    ).fetchone()
    if not staff:
        conn.close()
        abort(404)
    handle = (request.form.get("handle_or_url") or "").strip()
    platform = request.form.get("platform") or "other"
    if handle:
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) as m FROM staff_social_links WHERE staff_id=?", (staff_id,)
        ).fetchone()["m"]
        conn.execute(
            "INSERT INTO staff_social_links (staff_id, platform, handle_or_url, sort_order) VALUES (?, ?, ?, ?)",
            (staff_id, platform, handle, max_sort + 1),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_profile", staff_id=staff_id))


@app.route("/admin/staff/<int:staff_id>/social/<int:link_id>/delete", methods=["POST"])
def admin_staff_social_delete(staff_id, link_id):
    conn = get_db()
    conn.execute("DELETE FROM staff_social_links WHERE id=? AND staff_id=?", (link_id, staff_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_profile", staff_id=staff_id))


# --- Staff Categories (the /admin/staff/categories management screen) -----

@app.route("/admin/staff/categories")
def admin_staff_categories():
    conn = get_db()
    school = get_school(conn)
    _ensure_staff_categories(conn, school["id"])
    categories = _list_staff_categories(conn, school["id"])
    conn.close()
    return render_template("admin_staff_categories.html", school=school, categories=categories, user=CURRENT_USER)


@app.route("/admin/staff/categories/add", methods=["POST"])
def admin_staff_categories_add():
    conn = get_db()
    school = get_school(conn)
    name = (request.form.get("name") or "").strip()
    if name:
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) as m FROM staff_categories WHERE school_id=?", (school["id"],)
        ).fetchone()["m"]
        conn.execute(
            "INSERT INTO staff_categories (school_id, name, team_id, sort_order) VALUES (?, ?, NULL, ?)",
            (school["id"], name, max_sort + 1),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_categories"))


@app.route("/admin/staff/categories/<int:category_id>/rename", methods=["POST"])
def admin_staff_categories_rename(category_id):
    conn = get_db()
    cat = conn.execute("SELECT * FROM staff_categories WHERE id=?", (category_id,)).fetchone()
    name = (request.form.get("name") or "").strip()
    # Sport categories are never renamed from here — their name always comes
    # from the live teams.name join, so a rename here would just be
    # silently overwritten the next time this list renders anyway.
    if cat and not cat["team_id"] and name:
        conn.execute("UPDATE staff_categories SET name=? WHERE id=?", (name, category_id))
        conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_categories"))


@app.route("/admin/staff/categories/<int:category_id>/delete", methods=["POST"])
def admin_staff_categories_delete(category_id):
    conn = get_db()
    cat = conn.execute("SELECT * FROM staff_categories WHERE id=?", (category_id,)).fetchone()
    if cat and not cat["team_id"]:
        # ON DELETE CASCADE on staff_category_assignments — a staff member
        # simply loses this one placement, their own record is untouched.
        conn.execute("DELETE FROM staff_categories WHERE id=?", (category_id,))
        conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_categories"))


@app.route("/admin/staff/categories/reorder", methods=["POST"])
def admin_staff_categories_reorder():
    conn = get_db()
    school = get_school(conn)
    try:
        order = json.loads(request.form.get("order_json") or "[]")
    except ValueError:
        order = []
    for i, category_id in enumerate(order):
        conn.execute(
            "UPDATE staff_categories SET sort_order=? WHERE id=? AND school_id=?",
            (i, category_id, school["id"]),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_categories"))


@app.route("/admin/staff/categories/<int:category_id>/order")
def admin_staff_categories_order(category_id):
    conn = get_db()
    school = get_school(conn)
    cat = conn.execute(
        "SELECT staff_categories.*, teams.name as team_name FROM staff_categories LEFT JOIN teams ON staff_categories.team_id = teams.id WHERE staff_categories.id=? AND staff_categories.school_id=?",
        (category_id, school["id"]),
    ).fetchone()
    if not cat:
        conn.close()
        abort(404)
    members = conn.execute(
        f"""SELECT staff_category_assignments.id as assignment_id, staff_members.id as staff_id,
                  {STAFF_NAME_SQL} as name, staff_members.title
           FROM staff_category_assignments JOIN staff_members ON staff_category_assignments.staff_id = staff_members.id
           WHERE staff_category_assignments.category_id=?
           ORDER BY staff_category_assignments.sort_order""",
        (category_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "admin_staff_category_order.html", school=school, category=dict(cat),
        display_name=cat["team_name"] or cat["name"], members=members, user=CURRENT_USER,
    )


@app.route("/admin/staff/categories/<int:category_id>/order/save", methods=["POST"])
def admin_staff_categories_order_save(category_id):
    conn = get_db()
    try:
        order = json.loads(request.form.get("order_json") or "[]")
    except ValueError:
        order = []
    for i, staff_id in enumerate(order):
        conn.execute(
            "UPDATE staff_category_assignments SET sort_order=? WHERE category_id=? AND staff_id=?",
            (i, category_id, staff_id),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_staff_categories_order", category_id=category_id))


@app.route("/admin/staff/settings", methods=["GET", "POST"])
def admin_staff_settings():
    conn = get_db()
    school = get_school(conn)
    if request.method == "POST":
        fields_config = []
        keys = request.form.getlist("field_key")
        for key in keys:
            fields_config.append({
                "key": key,
                "enabled": request.form.get(f"enabled_{key}") == "on",
                "label": request.form.get(f"label_{key}") or None,
            })
        # Custom field labels are freeform text, not a fixed key list — same
        # blank-row-dropped repeater convention as Related Links/Custom
        # Fields elsewhere, matched against staff_custom_fields.label at
        # render time (see _staff_directory_custom_field_labels).
        custom_field_labels = [
            label.strip() for label in request.form.getlist("directory_custom_field_label") if label.strip()
        ]
        directory_template = request.form.get("directory_template") or "cards"
        if directory_template not in ("cards", "table"):
            directory_template = "cards"
        conn.execute(
            """INSERT INTO staff_directory_settings
                   (school_id, intro_text, fields_config, custom_field_labels, directory_template)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(school_id) DO UPDATE SET intro_text=excluded.intro_text,
                   fields_config=excluded.fields_config, custom_field_labels=excluded.custom_field_labels,
                   directory_template=excluded.directory_template""",
            (
                school["id"], request.form.get("intro_text") or None, json.dumps(fields_config),
                json.dumps(custom_field_labels), directory_template,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_staff_settings", saved=1))
    settings = _get_or_create_staff_directory_settings(conn, school["id"])
    conn.close()
    return render_template(
        "admin_staff_settings.html", school=school, settings=settings,
        fields_config=_staff_fields_config(settings),
        custom_field_labels=_staff_directory_custom_field_labels(settings), user=CURRENT_USER,
    )


# ---------------------------------------------------------------------------
# Roster paste-import — paste a whole roster in one go instead of adding
# players one at a time. Two steps: parse-and-preview (so a bad paste can be
# fixed before anything's written), then commit. Every pasted name is
# checked against this school's *entire* athlete database, not just this
# team, since the same person could already be on file for a different
# sport — each match is flagged for the admin to confirm as a returning
# player (merge) or a different person who happens to share a name (new).
# ---------------------------------------------------------------------------

def _find_athlete_matches(conn, school_id, first_name, last_name):
    return conn.execute(
        """SELECT athletes.*,
                  (SELECT GROUP_CONCAT(teams.name || ' ' || roster_entries.season, ', ')
                     FROM roster_entries JOIN teams ON roster_entries.team_id = teams.id
                    WHERE roster_entries.athlete_id = athletes.id) as current_teams
           FROM athletes
           WHERE athletes.school_id=? AND lower(athletes.first_name)=lower(?) AND lower(athletes.last_name)=lower(?)""",
        (school_id, first_name, last_name),
    ).fetchall()


# Documented paste order — first line can optionally be a header row using
# these same names, which gets auto-detected and skipped.
IMPORT_COLUMNS = ["first_name", "last_name", "jersey_number", "position", "class_year", "height", "weight", "hometown", "high_school"]


def _parse_roster_paste(raw_text):
    rows = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Tab-separated if a paste from a spreadsheet carried real tabs;
        # otherwise fall back to comma-separated.
        cells = line.split("\t") if "\t" in line else line.split(",")
        cells = [c.strip() for c in cells]
        row = {}
        for i, col in enumerate(IMPORT_COLUMNS):
            row[col] = cells[i] if i < len(cells) else ""
        rows.append(row)
    # Drop a leading header row if it's just column names typed back at us.
    if rows and rows[0].get("first_name", "").lower() in ("first name", "first", "first_name"):
        rows = rows[1:]
    return rows


@app.route("/admin/rosters/import")
def admin_rosters_import():
    conn = get_db()
    school = get_school(conn)
    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    team_id = request.args.get("team", type=int) or (teams[0]["id"] if teams else None)
    conn.close()
    return render_template(
        "admin_roster_import.html", school=school, teams=teams, team_id=team_id, user=CURRENT_USER,
        import_columns=IMPORT_COLUMNS,
    )


@app.route("/admin/rosters/import/preview", methods=["POST"])
def admin_rosters_import_preview():
    conn = get_db()
    school = get_school(conn)
    f = request.form
    team_id = f.get("team_id", type=int)
    season = f.get("season") or "2026"
    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    current_team = next((t for t in teams if t["id"] == team_id), None)

    parsed = _parse_roster_paste(f.get("paste_text", ""))
    preview_rows = []
    for row in parsed:
        matches = []
        if row["first_name"] and row["last_name"]:
            matches = _find_athlete_matches(conn, school["id"], row["first_name"], row["last_name"])
        preview_rows.append({
            "row": row,
            "matches": [dict(m) for m in matches],
            # Default guess only — the admin confirms or flips this per row
            # before anything is committed. Exactly one existing name-match
            # defaults to "merge" (most likely a returning/multi-sport
            # player); zero or multiple matches default to "new" since an
            # ambiguous match shouldn't be silently auto-merged.
            "default_mode": "merge" if len(matches) == 1 else "new",
        })
    conn.close()

    if not parsed:
        return render_template(
            "admin_roster_import.html", school=school, teams=teams, team_id=team_id, user=CURRENT_USER,
            import_columns=IMPORT_COLUMNS, empty_paste=True,
        )

    return render_template(
        "admin_roster_import_preview.html", school=school, teams=teams, current_team=current_team,
        team_id=team_id, season=season, preview_rows=preview_rows, user=CURRENT_USER,
    )


@app.route("/admin/rosters/import/commit", methods=["POST"])
def admin_rosters_import_commit():
    conn = get_db()
    school = get_school(conn)
    f = request.form
    team_id = f.get("team_id", type=int)
    season = f.get("season") or "2026"
    count = int(f.get("row_count", 0))

    max_sort = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) as m FROM roster_entries WHERE team_id=?", (team_id,)
    ).fetchone()["m"]

    added = 0
    for i in range(count):
        prefix = f"row_{i}_"
        first_name = (f.get(prefix + "first_name") or "").strip()
        last_name = (f.get(prefix + "last_name") or "").strip()
        if not first_name or not last_name:
            continue  # a row the admin blanked out on the preview screen — skip it
        mode = f.get(prefix + "mode", "new")

        if mode == "merge":
            athlete_id = f.get(prefix + "athlete_id", type=int)
        else:
            data = {
                "school_id": school["id"], "first_name": first_name, "last_name": last_name,
                "hometown": f.get(prefix + "hometown") or None,
                "high_school": f.get(prefix + "high_school") or None,
                "previous_school": None, "headshot_url": None, "bio_text": None,
                "nil_shop_url": None, "academic_major": None, "nickname": None, "pronunciation": None, "birthdate": None,
            }
            cur = conn.execute(f"INSERT INTO athletes ({_ATHLETE_FIELDS}) VALUES ({_ATHLETE_PARAMS})", data)
            athlete_id = cur.lastrowid

        # Guard against double-importing the same person into the same
        # team+season twice (e.g. re-running an import over the same paste).
        already = conn.execute(
            "SELECT id FROM roster_entries WHERE athlete_id=? AND team_id=? AND season=?",
            (athlete_id, team_id, season),
        ).fetchone()
        if already:
            continue

        max_sort += 1
        height_ft, height_in = _parse_height_text(f.get(prefix + "height"))
        entry = {
            "athlete_id": athlete_id, "team_id": team_id, "season": season,
            "jersey_number": f.get(prefix + "jersey_number") or None,
            "position": f.get(prefix + "position") or None,
            "class_year": f.get(prefix + "class_year") or None,
            "height_ft": height_ft,
            "height_in": height_in,
            "weight": f.get(prefix + "weight") or None,
            "bats_throws": None,
            "bio_text": None,
            "custom_field_1": None, "custom_field_2": None, "custom_field_3": None,
            "hidden": 0,
            "sort_order": max_sort,
        }
        conn.execute(f"INSERT INTO roster_entries ({_ROSTER_ENTRY_FIELDS}) VALUES ({_ROSTER_ENTRY_PARAMS})", entry)
        added += 1

    conn.commit()
    conn.close()
    return redirect(url_for("admin_rosters", team=team_id, season=season, imported=added))


@app.route("/admin/schedule")
def admin_schedule():
    conn = get_db()
    school = get_school(conn)
    teams = conn.execute(
        "SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)
    ).fetchall()
    view = request.args.get("view")
    team_id = request.args.get("team", type=int)
    season_param = request.args.get("season")

    # Browse by Sport, step 1 — pick which team's history to look at.
    if view == "by_sport" and not team_id:
        conn.close()
        return render_template(
            "admin_schedule_hub.html", school=school, teams=teams, mode="by_sport_pick_team", user=CURRENT_USER,
        )

    # Browse by Sport, step 2 — every season that team has a schedule for.
    if view == "by_sport" and team_id:
        picked_team = next((t for t in teams if t["id"] == team_id), None)
        if not picked_team:
            conn.close()
            abort(404)
        seasons_for_team = [
            r["season"] for r in conn.execute(
                "SELECT DISTINCT season FROM games WHERE team_id=? ORDER BY season DESC", (team_id,)
            ).fetchall()
        ]
        conn.close()
        return render_template(
            "admin_schedule_hub.html", school=school, teams=teams, mode="by_sport_pick_season",
            picked_team=picked_team, seasons_for_team=seasons_for_team,
            current_schedule_season=picked_team["current_schedule_season"], user=CURRENT_USER,
        )

    # Browse by Season, step 1 — every season any team has a schedule for.
    if view == "by_season" and not season_param:
        all_seasons = [
            r["season"] for r in conn.execute(
                """SELECT DISTINCT games.season FROM games
                   JOIN teams ON games.team_id = teams.id
                   WHERE teams.school_id=? ORDER BY games.season DESC""",
                (school["id"],),
            ).fetchall()
        ]
        conn.close()
        return render_template(
            "admin_schedule_hub.html", school=school, teams=teams, mode="by_season_pick_season",
            all_seasons=all_seasons, user=CURRENT_USER,
        )

    # Browse by Season, step 2 — every team that has a schedule for that season.
    if view == "by_season" and season_param:
        teams_for_season = conn.execute(
            """SELECT DISTINCT teams.* FROM teams JOIN games ON games.team_id = teams.id
               WHERE teams.school_id=? AND games.season=? ORDER BY teams.sort_order""",
            (school["id"], season_param),
        ).fetchall()
        conn.close()
        return render_template(
            "admin_schedule_hub.html", school=school, teams=teams, mode="by_season_pick_team",
            picked_season=season_param, teams_for_season=teams_for_season, user=CURRENT_USER,
        )

    # A specific team's schedule — the detail/edit view. Reached only by an
    # explicit ?team=, never as the bare-URL default any more — see Rosters
    # for why (same "too easy to edit the wrong sport" concern applies here).
    if team_id:
        current_team = next((t for t in teams if t["id"] == team_id), None)
        if not current_team:
            conn.close()
            abort(404)
        seasons = [
            r["season"] for r in conn.execute(
                "SELECT DISTINCT season FROM games WHERE team_id=? ORDER BY season DESC", (current_team["id"],)
            ).fetchall()
        ]
        resolved_current = _resolve_current_season(
            conn, current_team["id"], "games", current_team["current_schedule_season"]
        )
        current_season = season_param or resolved_current
        games = []
        if current_season:
            games = conn.execute(
                """SELECT games.*, opponents.name as opponent_name, opponents.logo_url as opponent_logo,
                          opp2.name as opponent_two_name, event_groups.name as event_group_name
                   FROM games
                   JOIN opponents ON games.opponent_id = opponents.id
                   LEFT JOIN opponents opp2 ON games.opponent_two_id = opp2.id
                   LEFT JOIN event_groups ON games.event_group_id = event_groups.id
                   WHERE games.team_id=? AND games.season=? ORDER BY games.date""",
                (current_team["id"], current_season),
            ).fetchall()
        current_enabled_views = json.loads(current_team["enabled_schedule_views"])
        sched_settings = _get_or_create_schedule_settings(conn, current_team["id"], current_season) if current_season else None
        stat_categories = _team_stat_categories(current_team)
        games_with_stats = set()
        if stat_categories and games:
            games_with_stats = set(_load_game_stats(conn, [g["id"] for g in games]).keys())
        games_pending_review = {g["id"] for g in games if g["pending_feed_xml"]}
        conn.close()
        return render_template(
            "admin_schedule.html", school=school, teams=teams, current_team=current_team, games=games,
            seasons=seasons, current_season=current_season, user=CURRENT_USER,
            schedule_views=SCHEDULE_VIEWS, current_enabled_views=current_enabled_views,
            is_current_season=(bool(current_season) and current_season == resolved_current),
            settings=sched_settings, stat_categories=stat_categories, games_with_stats=games_with_stats,
            games_pending_review=games_pending_review,
        )

    # Bare /admin/schedule — the new default landing: one card per team
    # showing whichever season is Current for its schedule.
    cards = []
    for t in teams:
        cs = _resolve_current_season(conn, t["id"], "games", t["current_schedule_season"])
        count = conn.execute(
            "SELECT COUNT(*) c FROM games WHERE team_id=? AND season=?", (t["id"], cs)
        ).fetchone()["c"] if cs else 0
        cards.append({"team": t, "season": cs, "count": count})
    conn.close()
    return render_template(
        "admin_schedule_hub.html", school=school, teams=teams, mode="current", cards=cards, user=CURRENT_USER,
    )


@app.route("/admin/schedule/current", methods=["POST"])
def admin_schedule_mark_current():
    f = request.form
    team_id = f.get("team_id", type=int)
    season = f.get("season")
    conn = get_db()
    conn.execute("UPDATE teams SET current_schedule_season=? WHERE id=?", (season, team_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_schedule", team=team_id, season=season))


@app.route("/admin/schedule/settings", methods=["POST"])
def admin_schedule_settings():
    f = request.form
    team_id = f.get("team_id", type=int)
    season = f.get("season", "")

    default_view = f.get("default_schedule_view")
    enabled_views = f.getlist("enabled_schedule_views")
    if default_view and default_view not in enabled_views:
        # The admin UI is built to prevent this (the current default can't be
        # unchecked), but enforce it server-side too rather than trust the client.
        enabled_views.append(default_view)

    conn = get_db()
    conn.execute(
        """UPDATE teams SET default_schedule_view=?, enabled_schedule_views=?,
                             show_record_bar=?, results_format=?, live_scoring_mode=? WHERE id=?""",
        (
            default_view,
            json.dumps(enabled_views),
            1 if f.get("show_record_bar") == "on" else 0,
            f.get("results_format"),
            # The Live Scoring select is only rendered when this team's
            # sport has stat categories defined — absent otherwise, which
            # would read as None here; "off" is the correct value either way.
            f.get("live_scoring_mode") or "off",
            team_id,
        ),
    )
    if season:
        _get_or_create_schedule_settings(conn, team_id, season)
        conn.execute(
            """UPDATE schedule_settings SET hidden=?, publish_at=?, publish_makes_current=?, publish_applied=0
               WHERE team_id=? AND season=?""",
            (
                1 if f.get("hidden") == "on" else 0, f.get("publish_at") or None,
                1 if f.get("publish_makes_current") == "on" else 0,
                team_id, season,
            ),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_schedule", team=team_id, season=season))


@app.route("/admin/schedule/settings/regenerate-token", methods=["POST"])
def admin_regenerate_stats_feed_token():
    """Rotates a team's stats-feed webhook secret (see schema.sql's own
    comment on teams.stats_feed_token) — every existing webhook URL a
    provider has on file stops working the moment this runs, so this is a
    deliberate, separate action rather than something that happens as a
    side effect of any other save."""
    team_id = request.form.get("team_id", type=int)
    season = request.form.get("season", "")
    conn = get_db()
    conn.execute(
        "UPDATE teams SET stats_feed_token=? WHERE id=?", (secrets.token_urlsafe(16), team_id)
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_schedule", team=team_id, season=season))


# ---------------------------------------------------------------------------
# Bulk Edit for Schedule — same shape as the roster one: every game's most
# commonly-touched fields as text boxes in one form, saved together. Doesn't
# cover every field the full Edit Game form has (opponent, ticket/stream
# links, etc.) — deliberately scoped to what's fastest to want to bulk-fix:
# date/time, location, and the result.
# ---------------------------------------------------------------------------

@app.route("/admin/schedule/bulk_edit")
def admin_schedule_bulk_edit():
    conn = get_db()
    school = get_school(conn)
    team_id = request.args.get("team", type=int)
    season = request.args.get("season")
    team = conn.execute("SELECT * FROM teams WHERE id=? AND school_id=?", (team_id, school["id"])).fetchone()
    if not team or not season:
        conn.close()
        abort(404)
    games = conn.execute(
        """SELECT games.*, opponents.name as opponent_name FROM games
           JOIN opponents ON games.opponent_id = opponents.id
           WHERE games.team_id=? AND games.season=? ORDER BY games.date""",
        (team_id, season),
    ).fetchall()
    conn.close()
    return render_template(
        "admin_schedule_bulk_edit.html", school=school, team=team, season=season, games=games, user=CURRENT_USER,
    )


@app.route("/admin/schedule/bulk_edit/commit", methods=["POST"])
def admin_schedule_bulk_edit_commit():
    f = request.form
    conn = get_db()
    team_id = f.get("team_id", type=int)
    season = f.get("season")
    count = int(f.get("row_count", 0))
    for i in range(count):
        prefix = f"row_{i}_"
        game_id = f.get(prefix + "game_id", type=int)
        if not game_id:
            continue
        conn.execute(
            """UPDATE games SET date=?, time=?, home_away=?, city=?, state=?, facility=?,
                                 score_for=?, score_against=? WHERE id=?""",
            (
                f.get(prefix + "date") or None, f.get(prefix + "time") or None,
                f.get(prefix + "home_away") or "home", f.get(prefix + "city") or None,
                f.get(prefix + "state") or None, f.get(prefix + "facility") or None,
                f.get(prefix + "score_for") or None, f.get(prefix + "score_against") or None,
                game_id,
            ),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_schedule", team=team_id, season=season))


def _game_form_context(conn, school, team_id, game=None):
    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    opponents = conn.execute("SELECT * FROM opponents WHERE school_id=? ORDER BY name", (school["id"],)).fetchall()
    galleries = conn.execute("SELECT * FROM galleries WHERE school_id=? ORDER BY title", (school["id"],)).fetchall()
    event_group_rows = conn.execute(
        "SELECT * FROM event_groups WHERE school_id=? ORDER BY start_date DESC, id DESC", (school["id"],)
    ).fetchall()

    # JSON payloads for the form's vanilla-JS auto-load/show-hide behavior —
    # no framework, consistent with the rest of this prototype.
    opponents_json = json.dumps([dict(o) for o in opponents])
    teams_json = json.dumps({str(t["id"]): {"results_format": t["results_format"]} for t in teams})
    event_groups_json = json.dumps(
        {str(t["id"]): [dict(g) for g in event_group_rows if g["team_id"] == t["id"]] for t in teams}
    )

    # Live/box-score stats — only surfaced in the Results section at all
    # when this team's sport has categories defined (see TEAM_STAT_CATEGORIES).
    current_team = next((t for t in teams if t["id"] == (game["team_id"] if game else team_id)), None)
    stat_categories = _team_stat_categories(current_team)
    game_stats = None
    webhook_url = None
    if game and stat_categories:
        gstats = _load_game_stats(conn, [game["id"]]).get(game["id"], {})
        game_stats = [
            {
                "label": cat["label"], "key": cat["key"],
                "for": gstats.get("for", {}).get(cat["key"]),
                "against": gstats.get("against", {}).get(cat["key"]),
            }
            for cat in stat_categories
        ]
        if current_team and current_team["live_scoring_mode"] != "off":
            token = _get_or_create_stats_feed_token(conn, current_team)
            webhook_url = url_for("stats_feed_webhook", token=token, game_id=game["id"], _external=True)

    # Story System — "Add Recap"/"Edit Recap" on a finished game (data-model.md
    # §7's comment: a game's recap link is derived by looking up whether any
    # articles row has linked_game_id pointing at it, not a column on games).
    recap_article = None
    if game:
        recap_article = conn.execute(
            "SELECT * FROM articles WHERE linked_game_id=? ORDER BY id DESC LIMIT 1", (game["id"],)
        ).fetchone()

    return {
        "school": school, "teams": teams, "opponents": opponents, "galleries": galleries,
        "team_id": team_id, "game": game, "user": CURRENT_USER,
        "game_types": GAME_TYPES, "result_types": RESULT_TYPES, "common_timezones": COMMON_TIMEZONES,
        "opponents_json": opponents_json, "teams_json": teams_json, "event_groups_json": event_groups_json,
        "current_team": current_team, "stat_categories": stat_categories, "game_stats": game_stats,
        "webhook_url": webhook_url, "recap_article": recap_article,
    }


@app.route("/admin/schedule/new")
def admin_schedule_new():
    conn = get_db()
    school = get_school(conn)
    team_id = request.args.get("team", type=int)
    ctx = _game_form_context(conn, school, team_id)
    conn.close()
    return render_template("admin_game_form.html", **ctx)


def _parse_game_form(f):
    status = f.get("status", "upcoming")
    is_neutral_host = 1 if f.get("is_neutral_host") == "on" else 0
    opponent_two_id = f.get("opponent_two_id", type=int) if is_neutral_host else None
    result_type = f.get("result_type", "standard")
    finish_place = f.get("finish_place", type=int) if result_type == "standard" else None
    total_teams = f.get("total_teams", type=int) if result_type == "standard" else None
    score_for = f.get("score_for") or None
    score_against = f.get("score_against") or None
    if result_type != "standard":
        # No-team-score / no-result rows never carry a score, regardless of
        # what's still sitting in the (hidden) score inputs client-side.
        score_for = None
        score_against = None
    time_type = f.get("time_type") or "specific"
    time_value = f.get("time") or None
    if time_type != "specific":
        # All Day / TBA never carry a clock time, regardless of what's still
        # sitting in the (disabled) time input client-side.
        time_value = None
    return {
        "team_id": f.get("team_id", type=int),
        "season": f.get("season"),
        "date": f.get("date"),
        "time": time_value,
        "time_type": time_type,
        "time_zone": f.get("time_zone") or "America/Chicago",
        "opponent_id": f.get("opponent_id", type=int),
        "opponent_two_id": opponent_two_id,
        "opponent_display_name": f.get("opponent_display_name") or None,
        "opponent_display_website_url": f.get("opponent_display_website_url") or None,
        "is_neutral_host": is_neutral_host,
        "game_type": f.get("game_type"),
        "result_type": result_type,
        "finish_place": finish_place,
        "total_teams": total_teams,
        "home_away": f.get("home_away"),
        "city": f.get("city") or None,
        "state": f.get("state") or None,
        "facility": f.get("facility") or None,
        "event_group_id": f.get("event_group_id", type=int) or None,
        "status": status,
        "score_for": score_for,
        "score_against": score_against,
        "score_finalized_by": ("manual_entry" if status == "final" else None),
        "box_score_url": f.get("box_score_url") or None,
        "linked_gallery_id": f.get("linked_gallery_id", type=int) or None,
        "tickets_url": f.get("tickets_url") or None,
        "livestream_url": f.get("livestream_url") or None,
    }


_GAME_FIELDS = (
    "team_id, season, date, time, time_type, time_zone, opponent_id, opponent_two_id, opponent_display_name, "
    "opponent_display_website_url, is_neutral_host, game_type, result_type, finish_place, total_teams, "
    "home_away, city, state, facility, event_group_id, status, score_for, score_against, score_finalized_by, "
    "box_score_url, linked_gallery_id, tickets_url, livestream_url"
)
_GAME_PARAMS = (
    ":team_id, :season, :date, :time, :time_type, :time_zone, :opponent_id, :opponent_two_id, :opponent_display_name, "
    ":opponent_display_website_url, :is_neutral_host, :game_type, :result_type, :finish_place, :total_teams, "
    ":home_away, :city, :state, :facility, :event_group_id, :status, :score_for, :score_against, "
    ":score_finalized_by, :box_score_url, :linked_gallery_id, :tickets_url, :livestream_url"
)


@app.route("/admin/schedule/new", methods=["POST"])
def admin_schedule_create():
    conn = get_db()
    data = _parse_game_form(request.form)
    conn.execute(f"INSERT INTO games ({_GAME_FIELDS}) VALUES ({_GAME_PARAMS})", data)
    conn.commit()
    team_id, season = data["team_id"], data["season"]
    conn.close()
    return redirect(url_for("admin_schedule", team=team_id, season=season))


@app.route("/admin/schedule/<int:game_id>/edit")
def admin_schedule_edit(game_id):
    conn = get_db()
    school = get_school(conn)
    game = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if not game:
        conn.close()
        abort(404)
    ctx = _game_form_context(conn, school, game["team_id"], game=game)
    conn.close()
    return render_template("admin_game_form.html", **ctx)


@app.route("/admin/schedule/<int:game_id>/edit", methods=["POST"])
def admin_schedule_update(game_id):
    conn = get_db()
    data = _parse_game_form(request.form)
    set_clause = ", ".join(f"{field.strip()}=:{field.strip()}" for field in _GAME_FIELDS.split(","))
    data["id"] = game_id
    conn.execute(f"UPDATE games SET {set_clause} WHERE id=:id", data)
    conn.commit()
    team_id, season = data["team_id"], data["season"]
    conn.close()
    return redirect(url_for("admin_schedule", team=team_id, season=season))


@app.route("/admin/schedule/<int:game_id>/delete", methods=["POST"])
def admin_schedule_delete(game_id):
    conn = get_db()
    game = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    team_id = game["team_id"] if game else None
    season = game["season"] if game else None
    conn.execute("DELETE FROM games WHERE id=?", (game_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_schedule", team=team_id, season=season))


# ---------------------------------------------------------------------------
# Live/box-score stats-feed import — the real mechanism behind what used to
# be the Results section's disabled "Upload & Fill In Results" placeholder.
# Only reachable for a team whose sport has stat categories defined (see
# TEAM_STAT_CATEGORIES) and whose live_scoring_mode isn't 'off'.
#   manual_confirm: parse -> preview page -> a separate Confirm & Publish
#     post before anything is written (the safer default).
#   auto_publish: parse and save in one step, no preview screen — meant for
#     a school that trusts its real feed enough to skip the human check.
# Both paths funnel through _commit_stats_import() so there's exactly one
# place that ever writes to game_team_stats/games for this.
# ---------------------------------------------------------------------------

def _match_athlete_id(conn, team_id, season, player_name, jersey_number):
    """Best-effort lookup of one of THIS team's own roster_entries for a
    feed-reported "for"-side player, so game_player_stats can link back to
    a real athlete profile (and eventually a headshot/bio) instead of just
    a loose name string. Opponent ("against") players are never looked up
    here — they aren't in our athletes table, full stop.

    Tries jersey number first (unambiguous when it matches), then falls
    back to a case-insensitive "first last" name match against this
    team+season's roster. Returns None rather than guessing when nothing
    matches — a missing athlete_id just means the box score shows the
    feed's name as plain text instead of a linked player."""
    if jersey_number:
        row = conn.execute(
            """SELECT athlete_id FROM roster_entries
               WHERE team_id=? AND season=? AND jersey_number=?""",
            (team_id, season, jersey_number),
        ).fetchone()
        if row:
            return row["athlete_id"]

    name = (player_name or "").strip().lower()
    if not name:
        return None
    row = conn.execute(
        """SELECT roster_entries.athlete_id FROM roster_entries
           JOIN athletes ON athletes.id = roster_entries.athlete_id
           WHERE roster_entries.team_id=? AND roster_entries.season=?
             AND lower(athletes.first_name || ' ' || athletes.last_name) = ?""",
        (team_id, season, name),
    ).fetchone()
    return row["athlete_id"] if row else None


def _apply_feed_score_update(conn, game, parsed):
    """The score/status half of a feed import, split out from the full box-
    score commit below so a live push can apply just this (see the stats-
    feed webhook route) without touching game_team_stats/game_plays/
    game_player_stats at all — this app's whole live-score story is now
    "the schedule page's score updates the moment a live push lands," not
    "re-render a whole box score every poll." Never overwrites a human-
    confirmed score (score_finalized_by='manual_entry') — the feed's own
    reported score always lands in feed_score_for/against, but the
    "official" score_for/against only follows the feed when nothing manual
    has already claimed authority over it."""
    updates = {
        "feed_score_for": parsed["score_for"], "feed_score_against": parsed["score_against"],
        "stats_feed_updated_at": parsed["updated_at"] or datetime.now().isoformat(timespec="minutes"),
    }
    if game["score_finalized_by"] != "manual_entry":
        if parsed["score_for"] is not None:
            updates["score_for"] = parsed["score_for"]
        if parsed["score_against"] is not None:
            updates["score_against"] = parsed["score_against"]
        updates["score_finalized_by"] = "live_feed_auto"
    if parsed["status"]:
        updates["status"] = parsed["status"]

    set_clause = ", ".join(f"{k}=:{k}" for k in updates)
    updates["id"] = game["id"]
    conn.execute(f"UPDATE games SET {set_clause} WHERE id=:id", updates)
    conn.commit()


def _commit_stats_import(conn, game, parsed):
    """Writes one parsed stats-feed result to game_team_stats, game_plays,
    game_player_stats, and games. Never overwrites a human-confirmed score
    (score_finalized_by='manual_entry') — the feed's own reported score
    always lands in feed_score_for/against, but the "official"
    score_for/against only follows the feed when nothing manual has
    already claimed authority over it (same for/against, feed-vs-manual
    distinction score_finalized_by was always meant to carry).

    Plays and player stats are both a full replace-on-import for this game
    (delete then re-insert) rather than an upsert like game_team_stats —
    a live feed's play list only ever grows/corrects in place, and refetching
    the whole list each poll is simpler and safer than trying to diff it."""
    for side in ("for", "against"):
        for i, (key, value) in enumerate(parsed[side].items()):
            conn.execute(
                """INSERT INTO game_team_stats (game_id, side, stat_key, stat_value, sort_order)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(game_id, side, stat_key) DO UPDATE SET stat_value=excluded.stat_value""",
                (game["id"], side, key, value, i),
            )

    conn.execute("DELETE FROM game_plays WHERE game_id=?", (game["id"],))
    for play in parsed["plays"]:
        conn.execute(
            """INSERT INTO game_plays
               (game_id, sequence, period, clock, team_side, description,
                score_for, score_against, is_scoring_play, yard_line, down_distance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (game["id"], play["sequence"], play["period"], play["clock"], play["side"],
             play["description"], play["score_for"], play["score_against"],
             1 if play["is_scoring_play"] else 0, play.get("yard_line"), play.get("down_distance")),
        )

    conn.execute("DELETE FROM game_player_stats WHERE game_id=?", (game["id"],))
    for ps in parsed["player_stats"]:
        athlete_id = None
        if ps["side"] == "for":
            athlete_id = _match_athlete_id(
                conn, game["team_id"], game["season"], ps["player_name"], ps["jersey_number"]
            )
        for i, (key, value) in enumerate(ps["stats"].items()):
            conn.execute(
                """INSERT INTO game_player_stats
                   (game_id, side, player_name, jersey_number, athlete_id, category,
                    stat_key, stat_value, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (game["id"], ps["side"], ps["player_name"], ps["jersey_number"], athlete_id,
                 ps["category"], key, value, i),
            )

    _apply_feed_score_update(conn, game, parsed)
    # This IS "the box score" landing, whatever triggered it (a manually
    # pasted/uploaded feed, or an admin confirming a webhook-received final
    # push) — so any pending review copy staged by the webhook is now
    # stale/superseded and gets cleared, whether or not this particular
    # commit was the one that came from it.
    conn.execute(
        "UPDATE games SET pending_feed_xml=NULL, pending_feed_received_at=NULL WHERE id=?",
        (game["id"],),
    )
    conn.commit()


def _get_or_create_stats_feed_token(conn, team):
    """One secret token per team, lazily generated on first use rather than
    at team-creation time, so existing/seeded teams (which predate this
    feature) get one automatically the first time anyone asks for their
    webhook URL instead of needing a migration/backfill step. Not real
    auth — just enough of an unguessable-URL convention that the webhook
    isn't wide open (see schema.sql's own comment on this column for the
    full reasoning)."""
    if team["stats_feed_token"]:
        return team["stats_feed_token"]
    token = secrets.token_urlsafe(16)
    conn.execute("UPDATE teams SET stats_feed_token=? WHERE id=?", (token, team["id"]))
    conn.commit()
    return token


def _stats_import_context(conn, game_id):
    """Shared lookup + gate for all three stats-import routes below: 404s
    when the game doesn't exist, its sport has no stat categories on file,
    or the team's Live Scoring is switched Off — so a URL typed by hand
    can't reach a feature that shouldn't be visible/usable for that team."""
    game = conn.execute(
        """SELECT games.*, opponents.name as opponent_name FROM games
           JOIN opponents ON games.opponent_id = opponents.id WHERE games.id=?""",
        (game_id,),
    ).fetchone()
    if not game:
        return None, None, None
    team = conn.execute("SELECT * FROM teams WHERE id=?", (game["team_id"],)).fetchone()
    categories = _team_stat_categories(team)
    if team and team["live_scoring_mode"] == "off":
        categories = None
    return game, team, categories


@app.route("/admin/schedule/<int:game_id>/stats/import")
def admin_stats_import(game_id):
    conn = get_db()
    school = get_school(conn)
    game, team, categories = _stats_import_context(conn, game_id)
    if not game or not categories:
        conn.close()
        abort(404)
    token = _get_or_create_stats_feed_token(conn, team)
    webhook_url = url_for("stats_feed_webhook", token=token, game_id=game_id, _external=True)
    conn.close()
    return render_template(
        "admin_stats_import.html", school=school, game=game, team=team, user=CURRENT_USER,
        error=None, feed_text=game["pending_feed_xml"] or "", webhook_url=webhook_url,
        pending_since=game["pending_feed_received_at"],
    )


@app.route("/admin/schedule/<int:game_id>/stats/import", methods=["POST"])
def admin_stats_import_parse(game_id):
    conn = get_db()
    school = get_school(conn)
    game, team, categories = _stats_import_context(conn, game_id)
    if not game or not categories:
        conn.close()
        abort(404)

    uploaded = request.files.get("feed_file")
    xml_text = (
        uploaded.read().decode("utf-8", errors="replace") if uploaded and uploaded.filename
        else request.form.get("feed_text", "")
    )

    try:
        parsed = _parse_stats_feed_xml(
            xml_text, home_away=game["home_away"],
            opponent_name_hint=game["opponent_display_name"] or game["opponent_name"],
        )
    except StatsFeedError as e:
        token = _get_or_create_stats_feed_token(conn, team)
        webhook_url = url_for("stats_feed_webhook", token=token, game_id=game_id, _external=True)
        conn.close()
        return render_template(
            "admin_stats_import.html", school=school, game=game, team=team, user=CURRENT_USER,
            error=str(e), feed_text=xml_text, webhook_url=webhook_url,
            pending_since=game["pending_feed_received_at"],
        )

    if team["live_scoring_mode"] == "auto_publish":
        _commit_stats_import(conn, game, parsed)
        conn.close()
        return redirect(url_for("admin_schedule_edit", game_id=game_id, imported="1"))

    # manual_confirm (or any other/unset value — default to the safer,
    # human-in-the-loop path): show a preview, write nothing yet.
    preview_rows = [
        {
            "label": cat["label"],
            "for": parsed["for"].get(cat["key"]),
            "against": parsed["against"].get(cat["key"]),
        }
        for cat in categories
    ]
    # Same bucketing/filtering the public schedule/game-card box score uses
    # (see _enrich_game/_load_scoring_plays) — a Scoring Summary of just the
    # plays that changed the score, oldest first, not the full play-by-play
    # log (this app doesn't have a page for that — a real feed can easily
    # parse into 200+ plays, almost none of which ever publish anywhere),
    # and player lines grouped into this sport's stat categories.
    scoring_preview = [p for p in parsed["plays"] if p["is_scoring_play"]]
    player_categories = _player_stat_categories(team)
    player_tables_preview = _build_player_stat_tables(parsed["player_stats"], player_categories)
    conn.close()
    return render_template(
        "admin_stats_import_preview.html", school=school, game=game, team=team, user=CURRENT_USER,
        parsed=parsed, preview_rows=preview_rows, feed_text=xml_text,
        scoring_preview=scoring_preview, total_plays_parsed=len(parsed["plays"]),
        player_tables_preview=player_tables_preview,
    )


@app.route("/admin/schedule/<int:game_id>/stats/import/commit", methods=["POST"])
def admin_stats_import_commit(game_id):
    conn = get_db()
    game, team, categories = _stats_import_context(conn, game_id)
    if not game or not categories:
        conn.close()
        abort(404)
    try:
        parsed = _parse_stats_feed_xml(
            request.form.get("feed_text", ""), home_away=game["home_away"],
            opponent_name_hint=game["opponent_display_name"] or game["opponent_name"],
        )
    except StatsFeedError:
        # Only reachable if the pasted text was edited between preview and
        # confirm — re-parse rather than trust a stashed value blindly.
        conn.close()
        return redirect(url_for("admin_stats_import", game_id=game_id))
    _commit_stats_import(conn, game, parsed)
    conn.close()
    return redirect(url_for("admin_schedule_edit", game_id=game_id, imported="1"))


# ---------------------------------------------------------------------------
# Stats-feed webhook — what "receiving the live XML from stat computers"
# actually means: a URL a school gives to their stat-computer software (or
# a provider like PrestoSports/StatBroadcast) once per team, that pushes
# straight to it during and after a game with no admin sitting at the paste
# form. This replaced this app's earlier assumption that a human always
# initiates an import — see the admin Import Stats page, now framed as a
# manual fallback/testing tool that shares this same parsing/commit code,
# not a second implementation of it.
# ---------------------------------------------------------------------------

@app.route("/api/stats-feed/<token>/<int:game_id>", methods=["POST"])
def stats_feed_webhook(token, game_id):
    """A live push updates just the score/status, immediately, regardless
    of this team's Live Scoring setting — that's the product decision this
    round: fans see the score update in real time no matter what, and the
    full box score is the thing that setting still gates. A final push
    always updates the score too, and then either commits the full box
    score right away (auto_publish) or stashes the XML on the game for an
    admin to review (manual_confirm) — see games.pending_feed_xml and
    _commit_stats_import's own clearing of it.

    The token is this team's stats_feed_token (see _get_or_create_stats_
    feed_token) — not real auth, just an unguessable-URL convention (this
    whole prototype has no real auth, see README). A wrong/missing token,
    a game that doesn't belong to that team, or a team with stats off all
    404 identically, so a probing request can't tell which reason applied."""
    conn = get_db()
    game, team, categories = _stats_import_context(conn, game_id)
    if not game or not categories or not team["stats_feed_token"] or team["stats_feed_token"] != token:
        conn.close()
        abort(404)

    xml_text = request.get_data(as_text=True)
    try:
        parsed = _parse_stats_feed_xml(
            xml_text, home_away=game["home_away"],
            opponent_name_hint=game["opponent_display_name"] or game["opponent_name"],
        )
    except StatsFeedError as e:
        conn.close()
        return jsonify({"ok": False, "error": str(e)}), 400

    if parsed["status"] == "final" and team["live_scoring_mode"] == "auto_publish":
        _commit_stats_import(conn, game, parsed)
        applied = "final_box_score"
    else:
        _apply_feed_score_update(conn, game, parsed)
        if parsed["status"] == "final":
            conn.execute(
                "UPDATE games SET pending_feed_xml=?, pending_feed_received_at=? WHERE id=?",
                (xml_text, datetime.now().isoformat(timespec="minutes"), game["id"]),
            )
            conn.commit()
            applied = "final_pending_review"
        else:
            applied = "live_score"

    conn.close()
    return jsonify({"ok": True, "status": parsed["status"], "applied": applied})


@app.route("/admin/schedule/events")
def admin_event_groups():
    conn = get_db()
    school = get_school(conn)
    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    team_id = request.args.get("team", type=int) or (teams[0]["id"] if teams else None)
    current_team = next((t for t in teams if t["id"] == team_id), teams[0] if teams else None)
    groups = []
    if current_team:
        groups = conn.execute(
            """SELECT event_groups.*,
                      (SELECT COUNT(*) FROM games WHERE games.event_group_id = event_groups.id) as game_count
               FROM event_groups WHERE team_id=? ORDER BY start_date DESC, id DESC""",
            (current_team["id"],),
        ).fetchall()
    conn.close()
    return render_template(
        "admin_event_groups.html", school=school, teams=teams, current_team=current_team,
        groups=groups, user=CURRENT_USER,
    )


@app.route("/admin/schedule/events/new", methods=["POST"])
def admin_event_groups_create():
    conn = get_db()
    school = get_school(conn)
    f = request.form
    team_id = f.get("team_id", type=int)
    name = f.get("name", "").strip()
    if name:
        conn.execute(
            """INSERT INTO event_groups (school_id, team_id, season, name, start_date, end_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (school["id"], team_id, f.get("season") or "2026", name,
             f.get("start_date") or None, f.get("end_date") or None),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("admin_event_groups", team=team_id))


@app.route("/admin/opponents")
def admin_opponents():
    conn = get_db()
    school = get_school(conn)
    opponents = conn.execute(
        "SELECT * FROM opponents WHERE school_id=? ORDER BY name", (school["id"],)
    ).fetchall()
    conn.close()
    return render_template("admin_opponents.html", school=school, opponents=opponents, user=CURRENT_USER)


@app.route("/admin/opponents/new", methods=["POST"])
def admin_opponents_create():
    conn = get_db()
    school = get_school(conn)
    name = request.form.get("name", "").strip()
    if name:
        conn.execute(
            "INSERT INTO opponents (school_id, name, logo_url, website_url, city, state) VALUES (?, ?, ?, ?, ?, ?)",
            (
                school["id"], name, request.form.get("logo_url") or None,
                request.form.get("website_url") or None,
                request.form.get("city") or None, request.form.get("state") or None,
            ),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("admin_opponents"))


@app.route("/admin/pages")
def admin_pages():
    conn = get_db()
    school = get_school(conn)
    pages = conn.execute(
        "SELECT * FROM pages WHERE school_id=? ORDER BY id", (school["id"],)
    ).fetchall()
    conn.close()
    return render_template("admin_pages.html", school=school, pages=pages, user=CURRENT_USER)


# ---------------------------------------------------------------------------
# The Story System (data-model.md §7, core-systems.md §1) — recaps and
# general stories. `articles` and its draft/pending_review/published status
# column already existed (seeded with one recap article), but had no admin
# screen and no public archive built against it until now.
# ---------------------------------------------------------------------------

@app.route("/admin/news")
def admin_news():
    conn = get_db()
    school = get_school(conn)
    status_filter = request.args.get("status", "")
    category_filter = request.args.get("category", "")
    team_filter = request.args.get("team", type=int)
    season_filter = request.args.get("season", "")
    search_query = request.args.get("q", "").strip()

    query = """SELECT articles.*, teams.name as team_name FROM articles
               LEFT JOIN teams ON articles.team_id = teams.id
               WHERE articles.school_id=?"""
    params = [school["id"]]
    if status_filter:
        query += " AND articles.status=?"
        params.append(status_filter)
    if category_filter:
        query += " AND articles.category=?"
        params.append(category_filter)
    if team_filter:
        query += " AND articles.team_id=?"
        params.append(team_filter)
    if season_filter:
        query += " AND articles.season=?"
        params.append(season_filter)
    if search_query:
        query += " AND (articles.title LIKE ? OR articles.subheadline LIKE ? OR articles.author_name LIKE ?)"
        like = f"%{search_query}%"
        params.extend([like, like, like])
    query += " ORDER BY articles.id DESC"
    articles = conn.execute(query, params).fetchall()

    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    seasons = [
        r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM articles WHERE school_id=? AND season IS NOT NULL ORDER BY season DESC",
            (school["id"],),
        ).fetchall()
    ]
    conn.close()
    return render_template(
        "admin_news.html", school=school, articles=articles, teams=teams, seasons=seasons,
        categories=ARTICLE_CATEGORIES, statuses=ARTICLE_STATUSES,
        status_filter=status_filter, category_filter=category_filter, team_filter=team_filter,
        season_filter=season_filter, search_query=search_query, user=CURRENT_USER,
    )


# --- News rankings (Home page / Sport page rotators) ------------------------
# The News Dashboard's own ranking control, separate from the Article
# Editor's simpler per-story Placement dropdown — direct feedback asked for
# a dedicated screen to drag stories into order, lock important ones in
# place, and bulk-reset everything else back to date order in one click.

def _news_rank_columns(scope):
    if scope == "sport":
        return "sport_rank", "sport_excluded", "sport_locked"
    return "home_rank", "home_excluded", "home_locked"


@app.route("/admin/news/rankings")
def admin_news_rankings():
    conn = get_db()
    school = get_school(conn)
    scope = request.args.get("scope") if request.args.get("scope") in ("home", "sport") else "home"
    team_id = request.args.get("team", type=int)
    rank_col, excluded_col, locked_col = _news_rank_columns(scope)

    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    team = None
    if scope == "sport":
        if not team_id:
            conn.close()
            return render_template(
                "admin_news_rankings.html", school=school, scope=scope, team=None, teams=teams,
                picking_team=True, user=CURRENT_USER,
            )
        team = next((t for t in teams if t["id"] == team_id), None)
        if not team:
            conn.close()
            abort(404)

    base_where = "school_id=? AND status='published' AND hidden=0"
    base_params = [school["id"]]
    if scope == "sport":
        base_where += " AND team_id=?"
        base_params.append(team_id)

    ranked = conn.execute(
        f"""SELECT * FROM articles WHERE {base_where} AND {excluded_col}=0
            ORDER BY ({rank_col} IS NULL), {rank_col} ASC, published_at DESC""",
        base_params,
    ).fetchall()
    excluded = conn.execute(
        f"""SELECT * FROM articles WHERE {base_where} AND {excluded_col}=1
            ORDER BY published_at DESC""",
        base_params,
    ).fetchall()
    conn.close()
    return render_template(
        "admin_news_rankings.html", school=school, scope=scope, team=team, teams=teams,
        picking_team=False, ranked=ranked, excluded=excluded,
        rank_col=rank_col, locked_col=locked_col, user=CURRENT_USER,
    )


@app.route("/admin/news/rankings/save", methods=["POST"])
def admin_news_rankings_save():
    scope = request.form.get("scope") if request.form.get("scope") in ("home", "sport") else "home"
    team_id = request.form.get("team_id", type=int)
    rank_col, excluded_col, locked_col = _news_rank_columns(scope)
    try:
        order = json.loads(request.form.get("order_json") or "[]")
    except ValueError:
        order = []

    conn = get_db()
    # Every row in the drag-list gets an explicit rank matching its final
    # position — including a story that was only sitting there because it
    # sorted by date, not because it was ever manually placed. Visiting this
    # screen and clicking Save is a deliberate act of setting the order,
    # per direct feedback that "the story rank on the News Dashboard can
    # override the ranking in the story editor." "Rank by Release Date"
    # below is the way back to automatic date ordering.
    for i, item in enumerate(order):
        article_id = item.get("id")
        if not article_id:
            continue
        conn.execute(
            f"UPDATE articles SET {rank_col}=?, {locked_col}=? WHERE id=?",
            (i + 1, 1 if item.get("locked") else 0, article_id),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_news_rankings", scope=scope, team=team_id))


@app.route("/admin/news/rankings/rank_by_date", methods=["POST"])
def admin_news_rankings_rank_by_date():
    scope = request.form.get("scope") if request.form.get("scope") in ("home", "sport") else "home"
    team_id = request.form.get("team_id", type=int)
    rank_col, excluded_col, locked_col = _news_rank_columns(scope)

    conn = get_db()
    school = get_school(conn)
    where = f"school_id=? AND status='published' AND hidden=0 AND {excluded_col}=0 AND {locked_col}=0"
    params = [school["id"]]
    if scope == "sport":
        where += " AND team_id=?"
        params.append(team_id)
    conn.execute(f"UPDATE articles SET {rank_col}=NULL WHERE {where}", params)
    conn.commit()
    conn.close()
    return redirect(url_for("admin_news_rankings", scope=scope, team=team_id))


@app.route("/admin/news/rankings/toggle_excluded", methods=["POST"])
def admin_news_rankings_toggle_excluded():
    scope = request.form.get("scope") if request.form.get("scope") in ("home", "sport") else "home"
    team_id = request.form.get("team_id", type=int)
    article_id = request.form.get("article_id", type=int)
    excluded = request.form.get("excluded") == "1"
    rank_col, excluded_col, locked_col = _news_rank_columns(scope)

    conn = get_db()
    # Bringing a story back into rotation drops it in at the bottom (rank
    # cleared, so it sorts by date) rather than guessing where it should be
    # pinned — same reasoning as "Rank by Release Date."
    if excluded:
        conn.execute(f"UPDATE articles SET {excluded_col}=1 WHERE id=?", (article_id,))
    else:
        conn.execute(f"UPDATE articles SET {excluded_col}=0, {rank_col}=NULL, {locked_col}=0 WHERE id=?", (article_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_news_rankings", scope=scope, team=team_id))


def _rank_choice_options(current_rank):
    """The Home/Sport Page Placement dropdown's numbered options — a plain
    1..20 range covers any realistic prototype use, extended if the story
    already has a rank higher than that (an edit from before this range
    existed, or a Dashboard-assigned rank past 20 on a school with a lot of
    ranked stories) so the current value is never silently missing from its
    own dropdown."""
    top = 20
    if current_rank and current_rank > top:
        top = current_rank
    return list(range(1, top + 1))


def _rank_choice_value(rank, excluded):
    """Collapses (rank, excluded) into the single value the Placement
    <select> uses: 'off' (excluded), '' (unranked — sort by release date,
    the default), or the rank number as a string."""
    if excluded:
        return "off"
    if rank is None:
        return ""
    return str(rank)


def _parse_rank_choice(value):
    """Inverse of _rank_choice_value, for form parsing. Returns
    (rank, excluded)."""
    if value == "off":
        return None, True
    if not value:
        return None, False
    try:
        return int(value), False
    except (TypeError, ValueError):
        return None, False


def _strip_html_for_check(html):
    """Plain-text-emptiness check for the rich-text Body editor — a
    contenteditable div with no real content still posts something like
    '<p><br></p>', which .strip() alone wouldn't catch as empty."""
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _article_form_context(conn, school, article=None, error=None, form_data=None):
    teams = conn.execute("SELECT * FROM teams WHERE school_id=? ORDER BY sort_order", (school["id"],)).fetchall()
    galleries = conn.execute("SELECT * FROM galleries WHERE school_id=? ORDER BY title", (school["id"],)).fetchall()

    # Games available to link, grouped by team for the form's vanilla-JS
    # team+season-scoped dropdown (same pattern as event groups on the game
    # form) — only final games, since there's nothing to preview/recap
    # against yet otherwise. Most recent first; each entry carries its own
    # season so the JS can also filter down to just the season selected
    # above it ("once the sport is selected, assign to a game from that
    # sport/season schedule").
    game_rows = conn.execute(
        """SELECT games.id, games.team_id, games.date, games.season, games.home_away,
                  games.opponent_display_name, opponents.name as opponent_name
           FROM games JOIN teams ON games.team_id = teams.id
                      JOIN opponents ON games.opponent_id = opponents.id
           WHERE teams.school_id=? AND games.status='final' ORDER BY games.date DESC""",
        (school["id"],),
    ).fetchall()
    games_by_team = {}
    for g in game_rows:
        label = f"{g['date']} {'at' if g['home_away']=='away' else 'vs.'} {g['opponent_display_name'] or g['opponent_name']}"
        games_by_team.setdefault(str(g["team_id"]), []).append(
            {"id": g["id"], "label": label, "season": g["season"]}
        )
    games_by_team_json = json.dumps(games_by_team)

    # Team -> that team's current schedule season, so the form can
    # auto-fill Season the moment a Team is picked ("default to current
    # based on schedule but allow to change to different seasons") without
    # clobbering a season the admin already typed.
    team_current_season = {
        str(t["id"]): _resolve_current_season(conn, t["id"], "games", t["current_schedule_season"])
        for t in teams
    }

    can_publish = "publish_news" in _current_user_permissions(conn, school["id"])
    current_user_row = _current_user_row(conn, school["id"])
    byline_default_on = (not current_user_row) or bool(current_user_row["show_byline_by_default"])

    article_links = []
    if article:
        article_links = [
            dict(r) for r in conn.execute(
                "SELECT * FROM article_links WHERE article_id=? ORDER BY sort_order", (article["id"],)
            ).fetchall()
        ]

    # Prefill precedence: a validation-failed resubmit's posted values win;
    # otherwise an existing article being edited; otherwise the "Add
    # Recap"/"Add Preview" link's query params (?linked_game_id=&team_id=
    # &season=&category=), which is how a game's Edit screen jumps in here
    # pre-linked. A brand-new story's Release Date & Time defaults to right
    # now, formatted to match a <input type="datetime-local"> value exactly
    # (same "%Y-%m-%dT%H:%M" shape _apply_due_publishes compares against).
    values = {
        "team_id": article["team_id"] if article else request.args.get("team_id", type=int),
        "linked_game_id": article["linked_game_id"] if article else request.args.get("linked_game_id", type=int),
        "linked_gallery_id": article["linked_gallery_id"] if article else None,
        "category": article["category"] if article else (request.args.get("category") or "general_article"),
        "season": article["season"] if article else request.args.get("season"),
        "title": article["title"] if article else "",
        "subheadline": article["subheadline"] if article else "",
        "filename": article["slug"] if article else "",
        "body": article["body"] if article else "",
        "featured_image_url": article["featured_image_url"] if article else "",
        "author_name": (
            article["author_name"] if article
            else (CURRENT_USER["name"] if byline_default_on else "")
        ),
        "published_at": (
            (article["published_at"] if article else None)
            or datetime.now().strftime("%Y-%m-%dT%H:%M")
        ),
        "hidden": bool(article["hidden"]) if article else False,
        "home_rank_choice": (
            _rank_choice_value(article["home_rank"], article["home_excluded"]) if article else ""
        ),
        "sport_rank_choice": (
            _rank_choice_value(article["sport_rank"], article["sport_excluded"]) if article else ""
        ),
        "template_type": article["template_type"] if article else "standard",
    }
    if form_data:
        values.update(form_data)

    linked_game = None
    if values["linked_game_id"]:
        linked_game = conn.execute(
            """SELECT games.*, opponents.name as opponent_name FROM games
               JOIN opponents ON games.opponent_id = opponents.id WHERE games.id=?""",
            (values["linked_game_id"],),
        ).fetchone()
        if linked_game and not values["team_id"]:
            values["team_id"] = linked_game["team_id"]
        if linked_game and not values["season"]:
            values["season"] = linked_game["season"]

    rank_choice_options = _rank_choice_options(
        max(article["home_rank"] or 0, article["sport_rank"] or 0) if article else 0
    )

    return {
        "school": school, "teams": teams, "galleries": galleries, "article": article,
        "values": values, "categories": ARTICLE_CATEGORIES, "template_types": ARTICLE_TEMPLATE_TYPES,
        "can_publish": can_publish, "article_links": article_links, "rank_choice_options": rank_choice_options,
        "games_by_team_json": games_by_team_json, "team_current_season_json": json.dumps(team_current_season),
        "linked_game": linked_game, "error": error, "user": CURRENT_USER,
        "now_str": datetime.now().strftime("%Y-%m-%dT%H:%M"),
    }


def _parse_article_form(f):
    home_rank, home_excluded = _parse_rank_choice(f.get("home_rank_choice", ""))
    sport_rank, sport_excluded = _parse_rank_choice(f.get("sport_rank_choice", ""))
    return {
        "team_id": f.get("team_id", type=int) or None,
        "linked_game_id": f.get("linked_game_id", type=int) or None,
        "linked_gallery_id": f.get("linked_gallery_id", type=int) or None,
        "category": f.get("category") or "general_article",
        "season": f.get("season") or None,
        "title": f.get("title", "").strip(),
        "subheadline": f.get("subheadline") or None,
        "filename": f.get("filename", "").strip(),
        "body": f.get("body", "").strip(),
        "featured_image_url": f.get("featured_image_url") or None,
        # No fallback to CURRENT_USER here on purpose — that's the bug this
        # replaced: saving with the Byline field cleared used to silently
        # re-write the current user's name right back in. The default is
        # only ever a starting *prefill* on a brand-new story's form (see
        # byline_default_on in _article_form_context) — once saved, blank
        # means blank, and the public story page's {% if article['author_name'] %}
        # check already handles not printing a "By " line at all.
        "author_name": f.get("author_name").strip() if f.get("author_name") else None,
        "published_at": f.get("published_at") or None,
        "hidden": f.get("hidden") == "on",
        "home_rank": home_rank,
        "home_excluded": home_excluded,
        "home_rank_choice": f.get("home_rank_choice", ""),
        "sport_rank": sport_rank,
        "sport_excluded": sport_excluded,
        "sport_rank_choice": f.get("sport_rank_choice", ""),
        "template_type": f.get("template_type") or "standard",
    }


def _parse_article_links_form(f):
    """Related Links is a repeatable Title+URL list (a dynamic add/remove
    row UI, not a fixed field count) — parallel getlist() arrays, same
    index across both, blank rows (from an add-then-not-filled-in row)
    silently dropped rather than saved as empty links."""
    titles = f.getlist("link_title")
    urls = f.getlist("link_url")
    links = []
    for title, url in zip(titles, urls):
        title, url = title.strip(), url.strip()
        if title and url:
            links.append((title, url))
    return links


def _save_article_links(conn, article_id, links):
    """Delete-and-reinsert on every save — same convention as
    _commit_stats_import's plays/player-stats replace, and just as
    reasonable here: there's no per-link identity worth preserving across
    edits, only the current list."""
    conn.execute("DELETE FROM article_links WHERE article_id=?", (article_id,))
    conn.executemany(
        "INSERT INTO article_links (article_id, title, url, sort_order) VALUES (?, ?, ?, ?)",
        [(article_id, title, url, i) for i, (title, url) in enumerate(links)],
    )


def _resolve_article_publish_fields(published_at, hidden, current_publish_applied=1):
    """The Release Date & Time + Hidden gate (schema.sql's articles.hidden/
    published_at/publish_applied comment has the full reasoning): a
    published_at in the future always forces hidden=1 regardless of the
    checkbox — that's the actual embargo — with publish_applied=0 so
    _apply_due_publishes() knows to flip it live later. A published_at
    that's blank or already past just uses the checkbox as a plain manual
    hold, with nothing left for the scheduler to do."""
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")
    is_future = bool(published_at) and published_at > now_str
    if is_future:
        return published_at, 1, 0
    return published_at, (1 if hidden else 0), 1


def _resolve_article_status(action, can_publish, current_status="draft"):
    """One shared status-transition rule for both create and edit, since
    the same actions (and the same publish_news gate) apply either way.
    'save' is the only action that leaves status untouched — every other
    action is an explicit transition. An unpermitted 'publish' degrades to
    'pending_review' rather than silently doing nothing, so the click
    still does *something* useful."""
    if action == "save_draft":
        return "draft"
    if action == "submit_review":
        return "pending_review"
    if action == "publish":
        return "published" if can_publish else "pending_review"
    if action == "unpublish":
        return "draft"
    return current_status  # action == "save" (or anything unrecognized)


@app.route("/admin/news/new")
def admin_news_new():
    conn = get_db()
    school = get_school(conn)
    ctx = _article_form_context(conn, school)
    conn.close()
    return render_template("admin_news_form.html", **ctx)


@app.route("/admin/news/new", methods=["POST"])
def admin_news_create():
    conn = get_db()
    school = get_school(conn)
    data = _parse_article_form(request.form)
    links = _parse_article_links_form(request.form)
    if not data["title"] or not _strip_html_for_check(data["body"]):
        ctx = _article_form_context(conn, school, form_data=data, error="Title and story body are both required.")
        conn.close()
        return render_template("admin_news_form.html", **ctx)

    can_publish = "publish_news" in _current_user_permissions(conn, school["id"])
    status = _resolve_article_status(request.form.get("action", "save_draft"), can_publish)
    published_at, hidden, publish_applied = _resolve_article_publish_fields(data["published_at"], data["hidden"])
    slug = _unique_article_slug(conn, school["id"], data["filename"] or data["title"])
    params = {
        **data, "school_id": school["id"], "slug": slug, "status": status,
        "published_at": published_at, "hidden": hidden, "publish_applied": publish_applied,
    }
    cur = conn.execute(
        """INSERT INTO articles (school_id, team_id, linked_game_id, linked_gallery_id, category, season,
                                  title, subheadline, slug, body, featured_image_url, author_name, status,
                                  published_at, hidden, publish_applied, home_rank, home_excluded,
                                  sport_rank, sport_excluded, template_type)
           VALUES (:school_id, :team_id, :linked_game_id, :linked_gallery_id, :category, :season,
                   :title, :subheadline, :slug, :body, :featured_image_url, :author_name, :status,
                   :published_at, :hidden, :publish_applied, :home_rank, :home_excluded,
                   :sport_rank, :sport_excluded, :template_type)""",
        params,
    )
    article_id = cur.lastrowid
    _save_article_links(conn, article_id, links)
    conn.commit()
    conn.close()
    return redirect(url_for("admin_news_edit", article_id=article_id, saved=1))


@app.route("/admin/news/<int:article_id>/edit")
def admin_news_edit(article_id):
    conn = get_db()
    school = get_school(conn)
    article = conn.execute(
        "SELECT * FROM articles WHERE id=? AND school_id=?", (article_id, school["id"])
    ).fetchone()
    if not article:
        conn.close()
        abort(404)
    ctx = _article_form_context(conn, school, article=article)
    conn.close()
    return render_template("admin_news_form.html", **ctx)


@app.route("/admin/news/<int:article_id>/edit", methods=["POST"])
def admin_news_update(article_id):
    conn = get_db()
    school = get_school(conn)
    article = conn.execute(
        "SELECT * FROM articles WHERE id=? AND school_id=?", (article_id, school["id"])
    ).fetchone()
    if not article:
        conn.close()
        abort(404)
    data = _parse_article_form(request.form)
    links = _parse_article_links_form(request.form)
    if not data["title"] or not _strip_html_for_check(data["body"]):
        ctx = _article_form_context(
            conn, school, article=article, form_data=data, error="Title and story body are both required."
        )
        conn.close()
        return render_template("admin_news_form.html", **ctx)

    can_publish = "publish_news" in _current_user_permissions(conn, school["id"])
    status = _resolve_article_status(request.form.get("action", "save"), can_publish, article["status"])
    published_at, hidden, publish_applied = _resolve_article_publish_fields(data["published_at"], data["hidden"])
    # Only re-slug if the Filename field actually changed enough to change
    # the slug — an already-published article's URL shouldn't shift under a
    # reader's feet over a trivial edit (title punctuation, say) that
    # slugifies the same way as before.
    wanted_slug = _slugify(data["filename"] or data["title"])
    slug = (
        article["slug"] if wanted_slug == article["slug"]
        else _unique_article_slug(conn, school["id"], data["filename"] or data["title"], exclude_id=article_id)
    )
    data.update({
        "id": article_id, "slug": slug, "status": status,
        "published_at": published_at, "hidden": hidden, "publish_applied": publish_applied,
    })
    conn.execute(
        """UPDATE articles SET team_id=:team_id, linked_game_id=:linked_game_id,
               linked_gallery_id=:linked_gallery_id, category=:category, season=:season,
               title=:title, subheadline=:subheadline, slug=:slug, body=:body,
               featured_image_url=:featured_image_url, author_name=:author_name, status=:status,
               published_at=:published_at, hidden=:hidden, publish_applied=:publish_applied,
               home_rank=:home_rank, home_excluded=:home_excluded,
               sport_rank=:sport_rank, sport_excluded=:sport_excluded, template_type=:template_type
           WHERE id=:id""",
        data,
    )
    _save_article_links(conn, article_id, links)
    conn.commit()
    conn.close()
    return redirect(url_for("admin_news_edit", article_id=article_id, saved=1))


@app.route("/admin/news/<int:article_id>/delete", methods=["POST"])
def admin_news_delete(article_id):
    conn = get_db()
    conn.execute("DELETE FROM articles WHERE id=?", (article_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_news"))


# ---------------------------------------------------------------------
# Public site — proves the "one CMS, unique public site" mechanism for
# real: pages are rows, blocks are rows, this just renders them.
# ---------------------------------------------------------------------

def build_calendar_months(games):
    """Groups games by month and builds a Sun-start week grid for each month
    that has at least one game — powers the calendar_view schedule template."""
    by_month = {}
    for g in games:
        d = datetime.strptime(g["date"], "%Y-%m-%d")
        key = (d.year, d.month)
        by_month.setdefault(key, {}).setdefault(d.day, []).append(g)

    months = []
    c = cal.Calendar(firstweekday=6)  # Sunday-start, matches US sports-calendar convention
    for (year, month) in sorted(by_month.keys()):
        weeks = []
        for week in c.monthdayscalendar(year, month):
            week_cells = []
            for day in week:
                week_cells.append({"day": day, "games": by_month[(year, month)].get(day, [])} if day else None)
            weeks.append(week_cells)
        months.append({
            "label": datetime(year, month, 1).strftime("%B %Y"),
            "weeks": weeks,
        })
    return months


def _load_game_stats(conn, game_ids):
    """One query for every game_team_stats row across a whole list of games,
    keyed game_id -> side -> {stat_key: stat_value} — called once per
    schedule-block render rather than once per game, same batching reason
    as everywhere else in this app that loops over a query result."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" for _ in game_ids)
    rows = conn.execute(
        f"SELECT * FROM game_team_stats WHERE game_id IN ({placeholders})", game_ids
    ).fetchall()
    by_game = {}
    for r in rows:
        by_game.setdefault(r["game_id"], {}).setdefault(r["side"], {})[r["stat_key"]] = r["stat_value"]
    return by_game


def _load_game_player_stats(conn, game_ids):
    """Same batching idea as _load_game_stats, for game_player_stats — keyed
    game_id -> list of {"side", "category", "player_name", "jersey_number",
    "stats"} entries (the flat one-row-per-stat-key rows collapsed per
    player), ready to hand to _build_player_stat_tables."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" for _ in game_ids)
    rows = conn.execute(
        f"""SELECT * FROM game_player_stats WHERE game_id IN ({placeholders})
            ORDER BY game_id, side, category, sort_order""",
        game_ids,
    ).fetchall()
    by_game = {}
    by_player = {}
    for r in rows:
        pkey = (r["game_id"], r["side"], r["category"], r["player_name"])
        entry = by_player.get(pkey)
        if entry is None:
            entry = {
                "side": r["side"], "category": r["category"], "player_name": r["player_name"],
                "jersey_number": r["jersey_number"], "stats": {},
            }
            by_player[pkey] = entry
            by_game.setdefault(r["game_id"], []).append(entry)
        entry["stats"][r["stat_key"]] = r["stat_value"]
    return by_game


def _load_recaps(conn, game_ids):
    """Keyed game_id -> its published recap article {id, title, slug}, if
    one exists. This is the mechanism data-model.md §7 calls for instead of
    a recap_url column on games: 'a game's recap link is derived by looking
    up whether any articles row has linked_game_id pointing at it' — one
    less field to ever drift out of sync. A draft/pending_review recap
    intentionally doesn't show here; only a published one is a real public
    link. If a game somehow has more than one linked article (nothing stops
    it), the most recently published one wins."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" for _ in game_ids)
    rows = conn.execute(
        f"""SELECT id, title, slug, linked_game_id FROM articles
            WHERE linked_game_id IN ({placeholders}) AND status='published' AND hidden=0
            ORDER BY published_at ASC""",
        game_ids,
    ).fetchall()
    by_game = {}
    for r in rows:
        by_game[r["linked_game_id"]] = {"id": r["id"], "title": r["title"], "slug": r["slug"]}
    return by_game


def _load_game_story_article(conn, game_id, category):
    """The most recently published article of `category` ('game_preview' or
    'game_recap') linked to this one game, or None. Deliberately
    category-specific and scoped to a single game — unlike _load_recaps
    (which just grabs whichever published article linked to a game most
    recently, for the schedule's quick game-card modal), Game Central needs
    a preview and a recap kept distinct: a team that posts a preview before
    kickoff and a recap afterward should see both in their own right place
    on the page, not have the preview silently disappear the moment a recap
    exists."""
    return conn.execute(
        """SELECT id, title, subheadline, slug, featured_image_url, body, author_name, published_at
           FROM articles
           WHERE linked_game_id=? AND category=? AND status='published' AND hidden=0
           ORDER BY published_at DESC LIMIT 1""",
        (game_id, category),
    ).fetchone()


def _load_last_time_out(conn, school, team_id, current_game_id, current_date):
    """Game Central's "Last Time Out" box: the most recently completed game
    for this team strictly before the current one (by date, then id as a
    same-day tiebreak), or None if this is the team's first game on file.
    Enriched the same way the main game is (_enrich_game) so the template
    can reuse display_name/time_info, plus its recap article if published."""
    row = conn.execute(
        """SELECT games.*, opponents.name as opponent_name, opponents.logo_url as opponent_logo,
                  opponents.website_url as opponent_website
           FROM games JOIN opponents ON games.opponent_id = opponents.id
           WHERE games.team_id=? AND games.status='final' AND games.id != ?
             AND (games.date < ? OR (games.date = ? AND games.id < ?))
           ORDER BY games.date DESC, games.id DESC LIMIT 1""",
        (team_id, current_game_id, current_date, current_date, current_game_id),
    ).fetchone()
    if not row:
        return None
    g = _enrich_game(row, school)
    g["recap"] = _load_game_story_article(conn, g["id"], "game_recap")
    return g


def _load_up_next(conn, school, team_id, current_game_id, current_date):
    """Game Central's "Up Next" box: the nearest not-yet-played game for
    this team strictly after the current one, or None if nothing further
    is on the schedule yet."""
    row = conn.execute(
        """SELECT games.*, opponents.name as opponent_name, opponents.logo_url as opponent_logo,
                  opponents.website_url as opponent_website
           FROM games JOIN opponents ON games.opponent_id = opponents.id
           WHERE games.team_id=? AND games.status IN ('upcoming','live') AND games.id != ?
             AND (games.date > ? OR (games.date = ? AND games.id > ?))
           ORDER BY games.date ASC, games.id ASC LIMIT 1""",
        (team_id, current_game_id, current_date, current_date, current_game_id),
    ).fetchone()
    if not row:
        return None
    return _enrich_game(row, school)


def _load_recent_team_stories(conn, team_id, limit, exclude_ids):
    """Game Central's "Recent Stories" grid: the same published/hidden/
    sport_excluded-scoped query the news_feed page block uses (see
    render_block's 'news_feed' branch), reused here scoped to this game's
    team, minus whichever articles are already shown as the page's own
    story teaser (its preview/recap) so nothing appears twice on the page."""
    exclude_ids = [i for i in (exclude_ids or []) if i]
    rows = conn.execute(
        """SELECT * FROM articles WHERE team_id=? AND status='published' AND hidden=0
           AND sport_excluded=0
           ORDER BY (sport_rank IS NULL), sport_rank ASC, published_at DESC LIMIT ?""",
        (team_id, limit + len(exclude_ids)),
    ).fetchall()
    out = [r for r in rows if r["id"] not in exclude_ids]
    return out[:limit]


def _load_scoring_plays(conn, game_ids):
    """Same batching idea again, for game_plays — keyed game_id -> its
    scoring plays only (is_scoring_play=1), oldest first, for the box
    score's Scoring Summary. Not the full play-by-play log (this app
    doesn't have a page for that anymore now that the fan-facing Live Game
    Center is out of scope — see README); just the handful of plays that
    actually changed the score, the same "Scoring Summary" section a real
    printed box score has."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" for _ in game_ids)
    rows = conn.execute(
        f"""SELECT * FROM game_plays WHERE game_id IN ({placeholders}) AND is_scoring_play=1
            ORDER BY game_id, sequence""",
        game_ids,
    ).fetchall()
    by_game = {}
    for r in rows:
        by_game.setdefault(r["game_id"], []).append(dict(r))
    return by_game


def _enrich_game(row, school, stats_by_game=None, categories=None,
                  player_categories=None, player_stats_by_game=None, scoring_by_game=None,
                  recap_by_game=None):
    """Turns a raw games+joins row into a dict with the resolved display
    name/website (opponent record, unless overridden for this one game —
    see data-model.md §5f), the three time-zone display strings, and — when
    this team's sport has stat categories defined and this game has stats
    on file — the full box score for the game detail card: a team_stats
    comparison list, player_stats tables (grouped by category, same shape
    the stats-import preview uses), and a scoring_plays list for a compact
    Scoring Summary. All three are independently None/empty when a game
    simply doesn't have that piece on file yet (e.g. a live push has only
    ever updated the score, never committed a box score — see
    _apply_feed_score_update) rather than showing an empty table. `recap`
    is independent of all of that (a recap is a Story System article, not
    part of the box score) — see _load_recaps."""
    g = dict(row)
    g["display_name"] = g.get("opponent_display_name") or g.get("opponent_name")
    g["display_website"] = g.get("opponent_display_website_url") or g.get("opponent_website")
    g["time_info"] = game_time_info(g, school)
    g["team_stats"] = None
    if categories and stats_by_game and g["id"] in stats_by_game:
        gstats = stats_by_game[g["id"]]
        rows_out = []
        for cat in categories:
            fv = gstats.get("for", {}).get(cat["key"])
            av = gstats.get("against", {}).get(cat["key"])
            if fv is not None or av is not None:
                rows_out.append({"label": cat["label"], "for": fv, "against": av})
        if rows_out:
            g["team_stats"] = rows_out
    g["player_stats"] = None
    if player_categories and player_stats_by_game and g["id"] in player_stats_by_game:
        tables = _build_player_stat_tables(player_stats_by_game[g["id"]], player_categories)
        if tables["for"] or tables["against"]:
            g["player_stats"] = tables
    g["scoring_plays"] = (scoring_by_game or {}).get(g["id"]) or None
    g["recap"] = (recap_by_game or {}).get(g["id"]) or None
    return g


def group_by_month(games):
    """Flat chronological list -> [{label, games}, ...] grouped by calendar
    month, in order — what Compact List and Season Story's regular-season
    sections need (Calendar already has its own day-grid version of this
    in build_calendar_months)."""
    months = []
    by_key = {}
    for g in games:
        d = datetime.strptime(g["date"], "%Y-%m-%d")
        key = (d.year, d.month)
        if key not in by_key:
            section = {"label": d.strftime("%B %Y"), "games": []}
            by_key[key] = section
            months.append(section)
        by_key[key]["games"].append(g)
    return months


def build_season_story(games):
    """The Season Story view's shape (data-model.md §12): a single featured
    Next Game, a short Upcoming strip after it, the rest of the regular
    season by month, and Postseason pulled into its own section regardless
    of which month it falls in. Status-driven (upcoming/live vs. final),
    not date-math against "now" — consistent with how the rest of this app
    treats a game's status as the source of truth."""
    regular = [g for g in games if g["game_type"] != "postseason"]
    postseason = [g for g in games if g["game_type"] == "postseason"]
    future = [g for g in games if g["status"] in ("upcoming", "live")]

    next_game = future[0] if future else None
    upcoming = [g for g in future[1:5]]  # the next few, after the featured one
    regular_minus_next = [g for g in regular if not next_game or g["id"] != next_game["id"]]

    return {
        "next_game": next_game,
        "upcoming": upcoming,
        "months": group_by_month(regular_minus_next),
        "postseason": postseason,
    }


def _resolve_current_season(conn, team_id, table, explicit_current):
    """Resolves "the" current season for a team's roster (table='roster_entries')
    or schedule (table='games'). An explicitly-set teams.current_roster_season /
    current_schedule_season always wins; otherwise falls back to the most
    recent season on file, so this is safe to call for a team that never had
    Current set by hand — nothing in the admin or public site requires it."""
    if explicit_current:
        return explicit_current
    row = conn.execute(
        f"SELECT season FROM {table} WHERE team_id=? ORDER BY season DESC LIMIT 1", (team_id,)
    ).fetchone()
    return row["season"] if row else None


def _group_by_event(games):
    """Groups games sharing an event_group_id under one section — the
    public display mechanism from data-model.md §5g. Scoring is completely
    untouched by this; it's purely how the list is presented."""
    sections = []
    seen = {}
    for g in games:
        gid = g.get("event_group_id")
        if gid and gid in seen:
            seen[gid]["games"].append(g)
        elif gid:
            section = {"is_group": True, "name": g.get("event_group_name"), "games": [g]}
            seen[gid] = section
            sections.append(section)
        else:
            sections.append({"is_group": False, "games": [g]})
    return sections


def render_block(conn, school, block):
    content = json.loads(block["content"])
    bt = block["block_type"]

    if bt == "hero":
        return {"type": "hero", "content": content}

    if bt == "roster_grid":
        team_id = content.get("team_id")
        team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        # Roster entries are per-season (data-model.md's athletes/roster_entries
        # split), so this can't just pull every entry ever made for the team —
        # that would mix current players in with every past roster. There's no
        # public season switcher yet (that's part of the deferred Roster page
        # redesign), so this links to whichever season is marked Current for
        # this team (falling back to the most recent season on file if none
        # has been explicitly set) — the same resolution the admin hub uses.
        current_season = _resolve_current_season(
            conn, team_id, "roster_entries", team["current_roster_season"] if team else None
        )
        settings = _get_or_create_roster_settings(conn, team_id, current_season) if current_season else None
        # A season can be Current and still not public yet — Hidden/scheduled
        # publish (Roster Settings) is a separate, deliberate gate on top of
        # Current, for a future roster that's been built out but shouldn't go
        # live until game day (or a scheduled publish time — see
        # _apply_due_publishes, which flips this off automatically once due).
        if settings and settings.get("hidden"):
            return {
                "type": "roster_grid", "team": team, "roster": [], "season": current_season,
                "roster_template": team["roster_template"] if team else "card",
                "social_by_athlete": {}, "settings": settings, "not_yet_published": True,
                "roster_title": _roster_title(school, team, current_season, settings.get("custom_title")) if team else None,
                "coaches": [],
            }
        order_by = "athletes.last_name, athletes.first_name" if settings and settings.get("sort_mode") == "alpha" \
            else "roster_entries.sort_order"
        roster = conn.execute(
            f"""SELECT roster_entries.*, athletes.first_name, athletes.last_name, athletes.hometown,
                      athletes.high_school, athletes.previous_school,
                      athletes.headshot_url, athletes.nickname, athletes.nil_shop_url
               FROM roster_entries JOIN athletes ON roster_entries.athlete_id = athletes.id
               WHERE roster_entries.team_id=? AND roster_entries.season=? AND roster_entries.hidden=0
               ORDER BY {order_by}""",
            (team_id, current_season),
        ).fetchall() if current_season else []
        # Season Rollover is what's supposed to keep a departed player's
        # roster_entries row from ever existing in a new season in the first
        # place — but that only works if the admin always uses it. Filtering
        # on hidden here too is the defensive backstop for a stale row left
        # over from hand-editing a season field (still possible — see
        # admin_roster_form.html); hidden is per-team, so it can never affect
        # any other sport that same person is on.
        social_by_athlete = {}
        if roster:
            links = conn.execute(
                "SELECT * FROM athlete_social_links WHERE athlete_id IN ({}) ORDER BY sort_order".format(
                    ",".join("?" * len(roster))
                ),
                [r["athlete_id"] for r in roster],
            ).fetchall()
            for link in links:
                social_by_athlete.setdefault(link["athlete_id"], []).append(dict(link))
        coaches = []
        if settings and settings.get("show_coaches"):
            coaches = [
                dict(c) for c in conn.execute(
                    """SELECT roster_coaches.*, staff_members.first_name, staff_members.last_name,
                          (staff_members.first_name || ' ' || staff_members.last_name) as name,
                          staff_members.title, staff_members.photo_url
                       FROM roster_coaches JOIN staff_members ON roster_coaches.staff_id = staff_members.id
                       WHERE roster_coaches.team_id=? AND roster_coaches.season=? AND roster_coaches.hidden=0
                       ORDER BY roster_coaches.is_head_coach DESC, roster_coaches.sort_order""",
                    (team_id, current_season),
                ).fetchall()
            ]
        return {
            "type": "roster_grid", "team": team, "roster": roster, "season": current_season,
            "roster_template": team["roster_template"] if team else "card",
            "social_by_athlete": social_by_athlete, "settings": settings,
            "fields_config": _roster_fields_config(settings) if settings else [],
            "roster_field_labels": ROSTER_FIELD_LABELS,
            "roster_title": _roster_title(school, team, current_season, settings.get("custom_title")) if settings and team else None,
            "coaches": coaches,
        }

    if bt == "schedule":
        team_id = content.get("team_id")
        limit = content.get("limit", 5)
        full_page = content.get("full_page", False)
        show_record_bar = False
        results_format = "score"
        record_bar = None

        join_sql = (
            """SELECT games.*, opponents.name as opponent_name, opponents.logo_url as opponent_logo,
                      opponents.website_url as opponent_website,
                      opp2.name as opponent_two_name, opp2.logo_url as opponent_two_logo,
                      teams.name as team_name, event_groups.name as event_group_name
               FROM games
               JOIN opponents ON games.opponent_id = opponents.id
               JOIN teams ON games.team_id = teams.id
               LEFT JOIN opponents opp2 ON games.opponent_two_id = opp2.id
               LEFT JOIN event_groups ON games.event_group_id = event_groups.id"""
        )

        if not team_id:
            # School-wide preview widget (the home page's "This Week" strip) —
            # always a short, simple list. Not a candidate for the view
            # switcher below, which is scoped to one team's full schedule.
            rows = conn.execute(
                join_sql + " WHERE teams.school_id=? ORDER BY games.date DESC LIMIT ?",
                (school["id"], limit),
            ).fetchall()
            recap_by_game = _load_recaps(conn, [r["id"] for r in rows])
            enriched = [_enrich_game(r, school, recap_by_game=recap_by_game) for r in rows]
            return {
                "type": "schedule", "full_page": False, "games": enriched,
                "sections": _group_by_event(enriched), "games_json": json.dumps(enriched),
            }

        team_row = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        default_view = team_row["default_schedule_view"] if team_row else "classic"
        enabled_views = json.loads(team_row["enabled_schedule_views"]) if team_row else [default_view]
        show_record_bar = bool(team_row["show_record_bar"]) if team_row else False
        results_format = team_row["results_format"] if team_row else "score"
        stat_categories = _team_stat_categories(team_row)

        if full_page:
            # The dedicated Schedule page (data-model.md §12): every enabled
            # view needs the *whole* season available to switch into, not
            # just the last N games — same reason Calendar always did. That's
            # the whole CURRENT season, though, not literally every game this
            # team has ever played — this used to have no season filter at
            # all, which was only harmless because no team had more than one
            # season of games on file yet. Links to whichever season is
            # marked Current (falling back to most recent), same as Rosters.
            current_schedule_season = _resolve_current_season(
                conn, team_id, "games", team_row["current_schedule_season"] if team_row else None
            )
            sched_settings = (
                _get_or_create_schedule_settings(conn, team_id, current_schedule_season)
                if current_schedule_season else None
            )
            if sched_settings and sched_settings.get("hidden"):
                # Same Hidden/scheduled-publish gate as Rosters — a future
                # schedule can exist and be edited without being public yet.
                rows = []
            else:
                rows = conn.execute(
                    join_sql + " WHERE games.team_id=? AND games.season=? ORDER BY games.date",
                    (team_id, current_schedule_season),
                ).fetchall() if current_schedule_season else []
        else:
            # A compact "Upcoming Games" preview — e.g. the team hub page's
            # teaser strip, which links out to the full Schedule page rather
            # than trying to be it. Always a simple Classic-style list
            # regardless of the team's chosen views, since it's just a teaser.
            rows = conn.execute(
                join_sql + " WHERE games.team_id=? AND games.status IN ('upcoming','live') "
                "ORDER BY games.date ASC LIMIT ?", (team_id, limit)
            ).fetchall()

        player_categories = _player_stat_categories(team_row)
        game_ids = [r["id"] for r in rows]
        stats_by_game = _load_game_stats(conn, game_ids) if stat_categories else {}
        player_stats_by_game = _load_game_player_stats(conn, game_ids) if player_categories else {}
        scoring_by_game = _load_scoring_plays(conn, game_ids) if stat_categories else {}
        recap_by_game = _load_recaps(conn, game_ids)
        enriched = [
            _enrich_game(r, school, stats_by_game, stat_categories,
                         player_categories, player_stats_by_game, scoring_by_game, recap_by_game)
            for r in rows
        ]
        record_bar = compute_record_bar(conn, team_id) if show_record_bar else None
        result = {
            "type": "schedule", "full_page": full_page, "games": enriched,
            "sections": _group_by_event(enriched), "team_name": team_row["name"] if team_row else None,
            "results_format": results_format, "show_record_bar": show_record_bar, "record_bar": record_bar,
            "default_view": default_view, "enabled_views": enabled_views,
            "link_href": content.get("link_href"), "link_label": content.get("link_label", "View Full Schedule"),
            # Every game's full detail, JSON-embedded once per block — click-to-open
            # detail card on every view (data-model.md-adjacent: not spec'd yet,
            # requested directly by the client) reads from this rather than a
            # per-game page/route, since none exists.
            "games_json": json.dumps(enriched),
        }
        if full_page:
            if "calendar" in enabled_views:
                result["months"] = build_calendar_months(enriched)
            if "compact_list" in enabled_views:
                result["by_month"] = group_by_month(enriched)
            if "season_story" in enabled_views:
                result["season_story"] = build_season_story(enriched)
        return result

    if bt == "news_feed":
        # Default order is by release date, newest first; home_rank/sport_rank
        # (set from either the Article Editor's Placement dropdown or the
        # News Dashboard's Manage Rankings drag-list — whichever was saved
        # last) pin a story to a specific slot ahead of the date-ordered
        # rest. home_excluded/sport_excluded is the real "keep off this page"
        # switch — direct feedback changed *_rank from "blank = not shown"
        # to "blank = sort by date" here, so exclusion needed its own flag.
        team_id = content.get("team_id")
        limit = content.get("limit", 3)
        if team_id:
            rows = conn.execute(
                """SELECT * FROM articles WHERE team_id=? AND status='published' AND hidden=0
                   AND sport_excluded=0
                   ORDER BY (sport_rank IS NULL), sport_rank ASC, published_at DESC LIMIT ?""",
                (team_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM articles WHERE school_id=? AND status='published' AND hidden=0
                   AND home_excluded=0
                   ORDER BY (home_rank IS NULL), home_rank ASC, published_at DESC LIMIT ?""",
                (school["id"], limit),
            ).fetchall()
        return {"type": "news_feed", "articles": rows, "archive_href": f"/site/{school['slug']}/news"}

    if bt == "sponsor_strip":
        rows = conn.execute(
            "SELECT * FROM sponsors WHERE school_id=? ORDER BY sort_order", (school["id"],)
        ).fetchall()
        return {"type": "sponsor_strip", "sponsors": rows}

    return {"type": bt, "content": content}


def render_page_by_slug(school_slug, page_slug):
    conn = get_db()
    school = conn.execute("SELECT * FROM schools WHERE slug=?", (school_slug,)).fetchone()
    if not school:
        conn.close()
        abort(404)
    page = conn.execute(
        "SELECT * FROM pages WHERE school_id=? AND slug=? AND status='published'",
        (school["id"], page_slug),
    ).fetchone()
    if not page:
        conn.close()
        abort(404)
    block_rows = conn.execute(
        "SELECT * FROM page_blocks WHERE page_id=? ORDER BY position", (page["id"],)
    ).fetchall()
    blocks = [render_block(conn, school, b) for b in block_rows]
    conn.close()
    return render_template("site_page.html", school=school, page=page, blocks=blocks)


# News Archive — data-model.md §7: "a reserved view every school gets
# automatically, not something built in the Page Builder." Registered
# ahead of the catch-all /site/<school>/<path:page_slug> route below, and
# Werkzeug's routing already prefers a rule's static path segments over a
# <path:...> wildcard regardless of registration order — verified directly
# (a real page slug literally named "news" would be unreachable otherwise,
# which is an acceptable trade this prototype makes deliberately: a
# system-generated archive wins the collision over a hand-built Page).
@app.route("/site/<school_slug>/staff")
def site_staff_directory(school_slug):
    conn = get_db()
    school = conn.execute("SELECT * FROM schools WHERE slug=?", (school_slug,)).fetchone()
    if not school:
        conn.close()
        abort(404)
    _ensure_staff_categories(conn, school["id"])
    settings = _get_or_create_staff_directory_settings(conn, school["id"])
    fields_config = _staff_fields_config(settings)
    directory_custom_field_labels = _staff_directory_custom_field_labels(settings)
    directory_template = settings.get("directory_template") or "cards"
    categories = _list_staff_categories(conn, school["id"])

    # One section per category that actually has a visible member — an
    # empty category (a brand-new custom one, or a sport with no staff
    # assigned yet) just doesn't render a heading for nothing, same "don't
    # show a filter/section that returns nothing" spirit as elsewhere.
    # Inactive staff are excluded here the same as hidden_from_directory —
    # a deactivated person comes off the public page too, not just the
    # admin list.
    sections = []
    for cat in categories:
        members = conn.execute(
            """SELECT staff_members.* FROM staff_category_assignments
               JOIN staff_members ON staff_category_assignments.staff_id = staff_members.id
               WHERE staff_category_assignments.category_id=? AND staff_members.hidden_from_directory=0
                     AND staff_members.active=1
               ORDER BY staff_category_assignments.sort_order""",
            (cat["id"],),
        ).fetchall()
        if members:
            sections.append({"name": cat["display_name"], "members": members})

    all_staff_ids = [m["id"] for s in sections for m in s["members"]]
    social_by_staff = {}
    custom_fields_by_staff = {}
    if all_staff_ids:
        placeholders = ",".join("?" * len(all_staff_ids))
        links = conn.execute(
            f"SELECT * FROM staff_social_links WHERE staff_id IN ({placeholders}) ORDER BY sort_order",
            all_staff_ids,
        ).fetchall()
        for link in links:
            social_by_staff.setdefault(link["staff_id"], []).append(dict(link))
        if directory_custom_field_labels:
            # Matched by label text, case/whitespace-insensitive, against
            # whatever a school configured on the Directory Settings page —
            # custom fields have no fixed schema to key a toggle off of, so
            # this is the only way to say "show Alma Mater everywhere it's
            # filled in."
            wanted = {label.strip().lower() for label in directory_custom_field_labels}
            fields = conn.execute(
                f"SELECT * FROM staff_custom_fields WHERE staff_id IN ({placeholders}) ORDER BY sort_order",
                all_staff_ids,
            ).fetchall()
            for cf in fields:
                if cf["label"].strip().lower() in wanted:
                    custom_fields_by_staff.setdefault(cf["staff_id"], []).append(dict(cf))
    conn.close()
    return render_template(
        "site_staff_directory.html", school=school, sections=sections, social_by_staff=social_by_staff,
        custom_fields_by_staff=custom_fields_by_staff, custom_field_labels=directory_custom_field_labels,
        intro_text=settings.get("intro_text"), fields_config=fields_config, directory_template=directory_template,
    )


@app.route("/site/<school_slug>/news")
def site_news_archive(school_slug):
    conn = get_db()
    school = conn.execute("SELECT * FROM schools WHERE slug=?", (school_slug,)).fetchone()
    if not school:
        conn.close()
        abort(404)
    category_filter = request.args.get("category", "")
    team_filter = request.args.get("team", type=int)
    season_filter = request.args.get("season", "")

    query = """SELECT articles.*, teams.name as team_name FROM articles
               LEFT JOIN teams ON articles.team_id = teams.id
               WHERE articles.school_id=? AND articles.status='published' AND articles.hidden=0"""
    params = [school["id"]]
    if category_filter:
        query += " AND articles.category=?"
        params.append(category_filter)
    if team_filter:
        query += " AND articles.team_id=?"
        params.append(team_filter)
    if season_filter:
        query += " AND articles.season=?"
        params.append(season_filter)
    query += " ORDER BY articles.published_at DESC"
    articles = conn.execute(query, params).fetchall()

    # Filter dropdown options only ever list teams/seasons that actually
    # have a published story on file — same "don't offer a filter that
    # returns nothing" spirit as Schedule's season dropdown.
    teams = conn.execute(
        """SELECT DISTINCT teams.* FROM teams JOIN articles ON articles.team_id = teams.id
           WHERE teams.school_id=? AND articles.status='published' AND articles.hidden=0
           ORDER BY teams.sort_order""",
        (school["id"],),
    ).fetchall()
    seasons = [
        r["season"] for r in conn.execute(
            """SELECT DISTINCT season FROM articles WHERE school_id=? AND status='published' AND hidden=0
               AND season IS NOT NULL ORDER BY season DESC""",
            (school["id"],),
        ).fetchall()
    ]
    conn.close()
    return render_template(
        "site_news_archive.html", school=school, articles=articles, teams=teams, seasons=seasons,
        categories=ARTICLE_CATEGORIES, category_filter=category_filter, team_filter=team_filter,
        season_filter=season_filter,
    )


@app.route("/site/<school_slug>/games/<int:game_id>")
def site_game_central(school_slug, game_id):
    """Game Central — a permanent, linkable page for one game, covering its
    whole lifecycle: matchup/location/coverage info before kickoff, the box
    score and scoring summary once stats land, and whichever of a linked
    preview/recap article exists. Every game gets this page automatically —
    there's no separate "create a Game Central page" step, since it's built
    entirely from data the game/box-score/story systems already produce.

    Deliberately static on load, no auto-refresh polling: a full real-time,
    fan-facing in-game experience (drive chart, play-by-play ticker, 20s
    polling) was built in an earlier round and explicitly removed in favor
    of partnering with a real stats provider for that piece (see README's
    "The pivot" section) — this page shows the same score/status data that
    pivot already keeps current via the stats webhook, it just doesn't poll
    for it client-side. Bringing back real-time behavior here would quietly
    re-reverse that decision, so it's intentionally out of scope.

    Registered ahead of the generic /site/<school>/<page_slug> catch-all the
    same way /news/<slug> already is — Werkzeug prefers a rule's static path
    segments ("games") over the wildcard regardless of registration order."""
    conn = get_db()
    school = conn.execute("SELECT * FROM schools WHERE slug=?", (school_slug,)).fetchone()
    if not school:
        conn.close()
        abort(404)

    join_sql = (
        """SELECT games.*, opponents.name as opponent_name, opponents.logo_url as opponent_logo,
                  opponents.website_url as opponent_website,
                  opp2.name as opponent_two_name, opp2.logo_url as opponent_two_logo,
                  teams.name as team_name, event_groups.name as event_group_name
           FROM games
           JOIN opponents ON games.opponent_id = opponents.id
           JOIN teams ON games.team_id = teams.id
           LEFT JOIN opponents opp2 ON games.opponent_two_id = opp2.id
           LEFT JOIN event_groups ON games.event_group_id = event_groups.id"""
    )
    row = conn.execute(
        join_sql + " WHERE games.id=? AND teams.school_id=?", (game_id, school["id"])
    ).fetchone()
    if not row:
        conn.close()
        abort(404)

    team_row = conn.execute("SELECT * FROM teams WHERE id=?", (row["team_id"],)).fetchone()

    # Same "future schedule not public yet" gate the full schedule list
    # already honors (see schedule_settings.hidden) — an individual game's
    # permalink shouldn't be able to reveal a season before an admin means
    # to, just because someone guesses/shares the URL early.
    sched_settings = _get_or_create_schedule_settings(conn, row["team_id"], row["season"])
    if sched_settings and sched_settings.get("hidden"):
        conn.close()
        abort(404)

    stat_categories = _team_stat_categories(team_row)
    player_categories = _player_stat_categories(team_row)
    stats_by_game = _load_game_stats(conn, [game_id]) if stat_categories else {}
    player_stats_by_game = _load_game_player_stats(conn, [game_id]) if player_categories else {}
    scoring_by_game = _load_scoring_plays(conn, [game_id]) if stat_categories else {}

    game = _enrich_game(
        row, school, stats_by_game, stat_categories, player_categories,
        player_stats_by_game, scoring_by_game,
    )

    preview_article = _load_game_story_article(conn, game_id, "game_preview")
    recap_article = _load_game_story_article(conn, game_id, "game_recap")

    gallery_items = []
    if game["linked_gallery_id"]:
        gallery_items = conn.execute(
            "SELECT * FROM media_assets WHERE gallery_id=?", (game["linked_gallery_id"],)
        ).fetchall()

    has_box_score = bool(game["team_stats"] or game["player_stats"] or game["scoring_plays"])

    # Which side of the matchup header reads left vs. right — always
    # away-team-first (the "Team A at Team B" convention), regardless of
    # which one happens to be this school's own team, so the header reads
    # naturally for a home game and an away game alike. This school has no
    # logo asset of its own in this prototype (schools has no logo_url
    # column, only per-opponent logo_url does), so "our" side always
    # renders with the same initial-badge mark the site header already
    # uses, never a photo.
    # Season Snapshot — this team's live-computed record, the same
    # Overall/Conference/Home/Away/Neutral/Streak numbers the schedule
    # page's Record Bar already shows (compute_record_bar), reused as-is.
    # No "National Rank" row: nothing in this app tracks a poll/ranking for
    # a team, so — same "absent, not a disabled stub" rule as the Leaders
    # box — that row is left out entirely rather than shown empty or faked.
    season_snapshot = compute_record_bar(conn, row["team_id"])

    # "Our" side shows a real record (Season Snapshot's Overall); the
    # opponent's side never does — opponents are just names/logos we play
    # against here, not tracked teams with a full season of their own, so
    # there's no real record to show without fabricating one.
    us = {
        "name": f"{school['name']} {school['mascot']}", "logo": None, "is_us": True,
        "record": season_snapshot["overall"],
    }
    opponent = {"name": game["display_name"], "logo": game["opponent_logo"], "is_us": False, "record": None}
    if game["is_neutral_host"]:
        left_side = {"name": game["display_name"], "logo": game["opponent_logo"], "is_us": False, "record": None}
        right_side = {"name": game["opponent_two_name"], "logo": game["opponent_two_logo"], "is_us": False, "record": None}
    elif game["home_away"] == "home":
        left_side, right_side = opponent, us
    else:  # 'away', or a single-opponent 'neutral' game — team listed first either way
        left_side, right_side = us, opponent

    # Game Leaders — the single top performer per stat category per side,
    # for this one game (see _compute_game_leaders). Only meaningful once
    # there's a real box score on file, and only for a sport with player
    # stat categories defined at all (football today) — same "absent, not
    # a disabled stub" rule every other sport-gated feature in this app
    # already follows, so a sport without categories yet just never shows
    # this box rather than showing an empty/broken one.
    leaders = (
        _compute_game_leaders(player_stats_by_game.get(game_id, []), player_categories)
        if player_categories else None
    )

    # Last Time Out / Up Next — the games immediately before and after this
    # one on this team's schedule, independent of whether the current game
    # itself is upcoming, live, or final (a completed game still has an
    # "Up Next" for whatever's scheduled after it).
    last_time_out = _load_last_time_out(conn, school, row["team_id"], game_id, row["date"])
    up_next = _load_up_next(conn, school, row["team_id"], game_id, row["date"])

    # Recent Stories — this team's latest published articles, minus the
    # preview/recap already shown as the page's own story teaser above, so
    # nothing is duplicated on the page.
    recent_stories = _load_recent_team_stories(
        conn, row["team_id"], 3,
        [preview_article["id"] if preview_article else None,
         recap_article["id"] if recap_article else None],
    )

    conn.close()
    return render_template(
        "site_game_central.html", school=school, game=game, team=team_row,
        left_side=left_side, right_side=right_side,
        preview_article=preview_article, recap_article=recap_article,
        gallery_items=gallery_items, has_box_score=has_box_score, leaders=leaders,
        season_snapshot=season_snapshot, last_time_out=last_time_out, up_next=up_next,
        recent_stories=recent_stories,
    )


@app.route("/site/<school_slug>/news/<slug>")
def site_news_article(school_slug, slug):
    conn = get_db()
    school = conn.execute("SELECT * FROM schools WHERE slug=?", (school_slug,)).fetchone()
    if not school:
        conn.close()
        abort(404)
    article = conn.execute(
        """SELECT articles.*, teams.name as team_name FROM articles
           LEFT JOIN teams ON articles.team_id = teams.id
           WHERE articles.school_id=? AND articles.slug=? AND articles.status='published' AND articles.hidden=0""",
        (school["id"], slug),
    ).fetchone()
    if not article:
        conn.close()
        abort(404)
    linked_game = None
    if article["linked_game_id"]:
        linked_game = conn.execute(
            """SELECT games.*, opponents.name as opponent_name FROM games
               JOIN opponents ON games.opponent_id = opponents.id WHERE games.id=?""",
            (article["linked_game_id"],),
        ).fetchone()
    gallery_items = []
    if article["linked_gallery_id"]:
        gallery_items = conn.execute(
            "SELECT * FROM media_assets WHERE gallery_id=?", (article["linked_gallery_id"],)
        ).fetchall()
    links = conn.execute(
        "SELECT * FROM article_links WHERE article_id=? ORDER BY sort_order", (article["id"],)
    ).fetchall()
    # A simple "More Stories" rail — most-recent-other-published, no
    # category/team affinity scoring; good enough for this prototype's
    # single-school scale.
    related = conn.execute(
        """SELECT * FROM articles WHERE school_id=? AND status='published' AND hidden=0 AND id!=?
           ORDER BY published_at DESC LIMIT 3""",
        (school["id"], article["id"]),
    ).fetchall()
    conn.close()
    return render_template(
        "site_news_article.html", school=school, article=article, linked_game=linked_game,
        gallery_items=gallery_items, links=links, related=related,
    )


@app.route("/site/<school_slug>")
def site_home(school_slug):
    return render_page_by_slug(school_slug, "")


@app.route("/site/<school_slug>/<path:page_slug>")
def site_page(school_slug, page_slug):
    return render_page_by_slug(school_slug, page_slug)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
