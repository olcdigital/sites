"""Seeds Northfield College sample data, including the Game Type/Neutral
Host, Time Zone, Record Bar, Alternative Results Format, Location/Facility
split + opponent display overrides, and Event Group features from
data-model.md §5c-5g."""

import json
from datetime import datetime, timedelta
from db import get_db, init_db, using_postgres
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "dev.db")


def run():
    # Local SQLite dev only: start from a clean file every time this is
    # run, same as always. Against Postgres (DATABASE_URL/POSTGRES_URL
    # set) there's no local file to reset — init_db() below just creates
    # the schema fresh in whatever database that URL points at, meant to
    # be run once against a brand-new, empty database.
    if not using_postgres() and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    conn = get_db()
    cur = conn.cursor()

    # --- School -----------------------------------------------------
    cur.execute(
        """INSERT INTO schools (name, slug, mascot, conference, city, state, default_timezone,
                                 primary_color, secondary_color, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        ("Northfield College", "northfield-college", "Falcons", "Empire Athletic Conference",
         "Northfield", "IL", "America/Chicago", "#14532d", "#eab308"),
    )
    school_id = cur.lastrowid

    # --- Permissions catalog (platform-wide, not school-scoped) ------
    permissions = [
        ("manage_pages", "Create/edit pages in the Page Builder"),
        ("manage_rosters", "Edit rosters, run season rollover"),
        ("manage_schedule", "Edit schedule/results"),
        ("manage_staff", "Edit coach/staff bios"),
        ("manage_news", "Create/edit articles"),
        ("publish_news", "Publish directly, bypassing Pending Review"),
        ("manage_media", "Manage galleries/videos"),
        ("manage_sponsors", "Manage sponsor listings"),
        ("manage_settings", "Edit branding/site settings"),
        ("manage_users", "Invite/edit staff accounts and role assignments"),
        ("confirm_live_scores", "Review/confirm auto-finalized live scores"),
    ]
    cur.executemany("INSERT OR IGNORE INTO permissions (key, label) VALUES (?, ?)", permissions)

    # --- Roles + role_permissions (default templates) ----------------
    def make_role(name, rank, perms):
        cur.execute(
            "INSERT INTO roles (school_id, name, rank, is_default) VALUES (?, ?, ?, 1)",
            (school_id, name, rank),
        )
        role_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO role_permissions (role_id, permission_key) VALUES (?, ?)",
            [(role_id, p) for p in perms],
        )
        return role_id

    admin_role_id = make_role("Admin", 0, [p[0] for p in permissions])
    manager_role_id = make_role(
        "Manager", 1, ["manage_rosters", "manage_schedule", "manage_news", "confirm_live_scores"]
    )
    staffer_role_id = make_role("Staffer", 2, ["manage_news"])

    # --- Teams --------------------------------------------------------
    def make_team(name, slug, gender, roster_template="card",
                  default_schedule_view="classic", enabled_schedule_views=None,
                  show_record_bar=1, results_format="score", sport=None, live_scoring_mode="off"):
        cur.execute(
            """INSERT INTO teams (school_id, name, slug, gender, sport_season, roster_template,
                                   default_schedule_view, enabled_schedule_views,
                                   show_record_bar, results_format, sport, live_scoring_mode, sort_order)
               VALUES (?, ?, ?, ?, 'fall', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (school_id, name, slug, gender, roster_template, default_schedule_view,
             json.dumps(enabled_schedule_views or [default_schedule_view]),
             show_record_bar, results_format, sport, live_scoring_mode, 0),
        )
        return cur.lastrowid

    # Deliberately diverse across all 4 teams, to exercise the full view
    # system (data-model.md §12) end to end: every view built at least once,
    # a team with all six enabled (the full switcher), and a team with only
    # one (switcher hidden entirely). No per-view logo option any more —
    # Compact List is always text-only, every other view always shows the
    # opponent logo.
    mens_soccer_id = make_team(
        "Men's Soccer", "mens-soccer", "men", sport="soccer",
        default_schedule_view="matchup_cards",
        enabled_schedule_views=["classic", "matchup_cards", "compact_list", "timeline", "calendar", "season_story"],
    )
    womens_soccer_id = make_team(
        "Women's Soccer", "womens-soccer", "women", sport="soccer",
        roster_template="photo",
        default_schedule_view="classic",
        enabled_schedule_views=["classic", "matchup_cards", "timeline"],
    )
    volleyball_id = make_team(
        "Volleyball", "volleyball", "women", sport="volleyball",
        roster_template="table",
        default_schedule_view="season_story",
        enabled_schedule_views=["season_story", "calendar", "matchup_cards"],
    )
    # Cross Country is the place-finish demo: scored by team finish, not a
    # score, so the Record Bar (W-L-shaped) is turned off for it. Compact
    # List only — the dense, text-only case §12 was built for.
    xc_id = make_team(
        "Cross Country", "cross-country", "coed", sport="cross_country",
        roster_template="table",
        default_schedule_view="compact_list",
        enabled_schedule_views=["compact_list"],
        results_format="place_finish", show_record_bar=0,
    )
    # Football — the live/box-score team stats demo (app.py's
    # TEAM_STAT_CATEGORIES). Live Scoring set to manual_confirm: a stats
    # feed always previews before it's saved, the safer default for a
    # school just turning this on. Table roster template, since a football
    # roster is exactly the dense-list case that display was built for.
    football_id = make_team(
        "Football", "football", "men", sport="football", live_scoring_mode="manual_confirm",
        roster_template="table",
        default_schedule_view="classic",
        enabled_schedule_views=["classic", "matchup_cards"],
    )

    # --- Staff ------------------------------------------------------------
    # staff_members is now a school-wide first_name/last_name directory row,
    # same "shared directory, per-context assignment" shape as
    # athletes/roster_entries. team_id/is_head_coach here are vestigial —
    # kept only so this quick-add helper still has somewhere to put them —
    # roster_coaches.team_id/is_head_coach (inserted below, per team/season)
    # is what's actually authoritative everywhere else in the app.
    def make_staff(first_name, last_name, title, team_id, is_head_coach=0, email=None, phone=None,
                   bio_text=None, hidden_from_directory=0):
        cur.execute(
            """INSERT INTO staff_members (school_id, team_id, first_name, last_name, title, is_head_coach,
                                            email, phone, bio_text, hidden_from_directory)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (school_id, team_id, first_name, last_name, title, is_head_coach, email, phone, bio_text,
             hidden_from_directory),
        )
        return cur.lastrowid

    laura_staff_id = make_staff(
        "Laura", "Tran", "Athletic Director", None, email="ltran@northfieldcollege.edu", phone="555-0101",
        bio_text="Laura has led Northfield Athletics since 2019, overseeing all 12 varsity programs.",
    )
    marco_staff_id = make_staff(
        "Marco", "Diaz", "Head Coach", mens_soccer_id, is_head_coach=1,
        email="mdiaz@northfieldcollege.edu", phone="555-0110",
        bio_text="Marco is in his 6th season leading the Men's Soccer program.",
    )
    jamie_staff_id = make_staff(
        "Jamie", "Ellis", "Assistant Coach", mens_soccer_id, email="jellis@northfieldcollege.edu",
    )
    rachel_staff_id = make_staff(
        "Rachel", "Kim", "Head Coach", volleyball_id, is_head_coach=1,
        email="rkim@northfieldcollege.edu", phone="555-0115",
        bio_text="Rachel took over the Volleyball program in 2023 after four years as an assistant.",
    )
    dwight_staff_id = make_staff(
        "Dwight", "Coleman", "Head Coach", football_id, is_head_coach=1,
        email="dcoleman@northfieldcollege.edu", phone="555-0120",
    )
    # A GA-type example: attachable to a roster (works with Athletic
    # Training), but deliberately left off the public Staff Directory.
    priya_staff_id = make_staff(
        "Priya", "Nair", "Graduate Assistant Athletic Trainer", None, hidden_from_directory=1,
        email="pnair@northfieldcollege.edu",
    )

    # --- Staff Categories, Directory Settings, Social Links, Custom Fields --
    # _ensure_sport_staff_categories() in app.py lazily creates/keeps-in-sync
    # a category row per team the first time anything touches /admin/staff,
    # so it's safe to seed those directly here too rather than depend on a
    # request happening first. Note that only the *category row* is
    # auto-managed — actually placing a staff member into it (sport or
    # custom) is always the same manual staff_category_assignments row,
    # since a coach's roster attachment and their Staff Directory category
    # are deliberately decoupled (a school may not want every assistant
    # coach listed under their sport on the public directory).
    cur.executemany(
        "INSERT OR IGNORE INTO staff_categories (school_id, name, team_id, sort_order) VALUES (?, ?, ?, ?)",
        [
            (school_id, "Men's Soccer", mens_soccer_id, 3),
            (school_id, "Volleyball", volleyball_id, 4),
            (school_id, "Football", football_id, 5),
        ],
    )
    mens_soccer_cat_id = cur.execute(
        "SELECT id FROM staff_categories WHERE school_id=? AND team_id=?", (school_id, mens_soccer_id)
    ).fetchone()["id"]
    volleyball_cat_id = cur.execute(
        "SELECT id FROM staff_categories WHERE school_id=? AND team_id=?", (school_id, volleyball_id)
    ).fetchone()["id"]

    admin_cat_id = cur.execute(
        "SELECT id FROM staff_categories WHERE school_id=? AND name='Administration'", (school_id,)
    ).fetchone()
    trainers_cat_id = cur.execute(
        "SELECT id FROM staff_categories WHERE school_id=? AND name='Athletic Trainers'", (school_id,)
    ).fetchone()
    comms_cat_id = cur.execute(
        "SELECT id FROM staff_categories WHERE school_id=? AND name='Athletic Communications'", (school_id,)
    ).fetchone()
    if not (admin_cat_id and trainers_cat_id and comms_cat_id):
        cur.executemany(
            "INSERT INTO staff_categories (school_id, name, team_id, sort_order) VALUES (?, ?, NULL, ?)",
            [
                (school_id, "Administration", 0),
                (school_id, "Athletic Trainers", 1),
                (school_id, "Athletic Communications", 2),
            ],
        )
        admin_cat_id = cur.execute(
            "SELECT id FROM staff_categories WHERE school_id=? AND name='Administration'", (school_id,)
        ).fetchone()
        trainers_cat_id = cur.execute(
            "SELECT id FROM staff_categories WHERE school_id=? AND name='Athletic Trainers'", (school_id,)
        ).fetchone()
        comms_cat_id = cur.execute(
            "SELECT id FROM staff_categories WHERE school_id=? AND name='Athletic Communications'", (school_id,)
        ).fetchone()
    admin_cat_id, trainers_cat_id, comms_cat_id = admin_cat_id["id"], trainers_cat_id["id"], comms_cat_id["id"]

    # Laura sits in Administration. Priya (hidden from the directory
    # entirely, per hidden_from_directory above) still gets a category
    # placement, proving that flag — not category membership — is what
    # actually hides her. Marco is seeded into two categories at once, the
    # "sometimes multiple" case from the spec: his sport category (Men's
    # Soccer) plus Administration, since he also chairs the coaches'
    # advisory council. Rachel is just her sport category, the common case.
    cur.executemany(
        "INSERT INTO staff_category_assignments (staff_id, category_id, sort_order) VALUES (?, ?, ?)",
        [
            (laura_staff_id, admin_cat_id, 0),
            (marco_staff_id, mens_soccer_cat_id, 0),
            (marco_staff_id, admin_cat_id, 1),
            (rachel_staff_id, volleyball_cat_id, 0),
            (priya_staff_id, trainers_cat_id, 0),
        ],
    )

    cur.executemany(
        "INSERT INTO staff_social_links (staff_id, platform, handle_or_url, sort_order) VALUES (?, ?, ?, ?)",
        [
            (marco_staff_id, "twitter", "@CoachDiazNFC", 0),
            (rachel_staff_id, "instagram", "@coachkim_vb", 0),
        ],
    )

    cur.executemany(
        "INSERT INTO staff_custom_fields (staff_id, label, value, sort_order) VALUES (?, ?, ?, ?)",
        [
            (laura_staff_id, "Alma Mater", "Northfield College '02", 0),
            (marco_staff_id, "Alma Mater", "Eastmoor University '14", 0),
            (marco_staff_id, "Office Location", "Falcon Fieldhouse, Room 210", 1),
        ],
    )

    # A previous-school stint, hand-entered — proves the manual half of
    # Career Record for someone whose computed half (this school's own
    # roster_coaches + games) wouldn't otherwise show it at all.
    cur.execute(
        """INSERT INTO staff_manual_records (staff_id, season, team_name, wins, losses, ties, note, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (dwight_staff_id, "2022", "Cedar Valley", 7, 3, 0, "Interim Head Coach, prior to joining Northfield", 0),
    )

    cur.execute(
        """INSERT INTO staff_directory_settings
               (school_id, intro_text, fields_config, custom_field_labels, directory_template)
           VALUES (?, ?, ?, ?, 'cards')
           ON CONFLICT(school_id) DO NOTHING""",
        (
            school_id,
            "Meet the coaches, administrators, and support staff behind Northfield Falcons Athletics.",
            json.dumps([
                {"key": "name", "enabled": True, "label": None},
                {"key": "title", "enabled": True, "label": None},
                {"key": "email", "enabled": True, "label": None},
                {"key": "phone", "enabled": False, "label": None},
                {"key": "social", "enabled": True, "label": None},
            ]),
            json.dumps(["Alma Mater", "Office Location"]),
        ),
    )


    # --- Users (logins) + team scopes -----------------------------------
    def make_user(name, email, role_id, staff_member_id=None, status="active"):
        cur.execute(
            """INSERT INTO users (school_id, name, email, role_id, staff_member_id, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (school_id, name, email, role_id, staff_member_id, status),
        )
        return cur.lastrowid

    sarah_id = make_user("Sarah Jennings", "sarah@northfieldcollege.edu", admin_role_id, laura_staff_id)
    marco_user_id = make_user("Marco Diaz", "mdiaz@northfieldcollege.edu", manager_role_id, marco_staff_id)
    rachel_user_id = make_user("Rachel Kim", "rkim@northfieldcollege.edu", manager_role_id, rachel_staff_id)
    theo_user_id = make_user("Theo Park", "tpark@northfieldcollege.edu", staffer_role_id, status="invited")

    cur.execute(
        "INSERT INTO user_team_scopes (user_id, scope_type, team_id) VALUES (?, 'specific_teams', ?)",
        (marco_user_id, mens_soccer_id),
    )
    cur.execute(
        "INSERT INTO user_team_scopes (user_id, scope_type, team_id) VALUES (?, 'specific_teams', ?)",
        (rachel_user_id, volleyball_id),
    )
    cur.execute(
        "INSERT INTO user_team_scopes (user_id, scope_type, team_id) VALUES (?, 'all_teams', NULL)",
        (theo_user_id,),
    )

    # --- Opponents --------------------------------------------------------
    # Cedar Valley deliberately has no website_url on file, to prove the
    # "renders as plain text, not a dead link" fallback on the public site.
    def make_opponent(name, city=None, state=None, website_url=None, logo_url=None):
        cur.execute(
            """INSERT INTO opponents (school_id, name, logo_url, website_url, city, state)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (school_id, name, logo_url, website_url, city, state),
        )
        return cur.lastrowid

    ridgeview_id = make_opponent(
        "Ridgeview College", city="Ridgeview", state="CA", website_url="https://ridgeviewathletics.com"
    )
    eastmoor_id = make_opponent(
        "Eastmoor University", city="Eastmoor", state="IL", website_url="https://eastmooru.edu/athletics"
    )
    cedar_valley_id = make_opponent("Cedar Valley", city="Cedar Valley", state="CO")
    alumni_id = make_opponent("Northfield Alumni")  # exhibition-only opponent, no location/website needed

    # --- Gallery for the Cedar Valley recap --------------------------------
    cur.execute(
        "INSERT INTO galleries (school_id, team_id, title) VALUES (?, ?, 'Season Opener')",
        (school_id, mens_soccer_id),
    )
    opener_gallery_id = cur.lastrowid
    cur.executemany(
        "INSERT INTO media_assets (gallery_id, type, url, caption) VALUES (?, 'image', ?, ?)",
        [
            (opener_gallery_id, "/static/placeholder.svg", "Jordan Castillo opens the scoring"),
            (opener_gallery_id, "/static/placeholder.svg", "Falcons celebrate at Northfield Field"),
        ],
    )

    # --- Event Groups -------------------------------------------------------
    def make_event_group(team_id, name, start_date, end_date):
        cur.execute(
            """INSERT INTO event_groups (school_id, team_id, season, name, start_date, end_date)
               VALUES (?, ?, '2026', ?, ?, ?)""",
            (school_id, team_id, name, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")),
        )
        return cur.lastrowid

    # --- Games ----------------------------------------------------------
    # Anchored to the real current date (not a fixed date) so "today" in
    # the seed data always lines up with the dashboard's real Today's
    # Games widget, however far in the future this gets reseeded.
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def make_game(team_id, opponent_id, date, home_away, city, state, status, feed_for=None, feed_against=None,
                  linked_gallery_id=None, time=None, time_type="specific", tickets_url=None, livestream_url=None,
                  facility=None, game_type="non_conference", time_zone=None, is_neutral_host=0,
                  opponent_two_id=None, opponent_display_name=None, opponent_display_website_url=None,
                  result_type="standard", finish_place=None, total_teams=None, event_group_id=None,
                  box_score_url=None):
        score_for, score_against = feed_for, feed_against
        finalized_by = "manual_entry" if status == "final" else None
        if result_type != "standard":
            # no_team_score / no_result rows never carry a score.
            score_for = score_against = feed_for = feed_against = None
        if time_type != "specific":
            time = None
        params = {
            "team_id": team_id, "date": date.strftime("%Y-%m-%d"), "time": time, "time_type": time_type,
            "time_zone": time_zone or "America/Chicago", "opponent_id": opponent_id,
            "opponent_two_id": opponent_two_id, "opponent_display_name": opponent_display_name,
            "opponent_display_website_url": opponent_display_website_url, "is_neutral_host": is_neutral_host,
            "game_type": game_type, "result_type": result_type, "finish_place": finish_place,
            "total_teams": total_teams, "home_away": home_away, "city": city, "state": state,
            "facility": facility, "event_group_id": event_group_id, "status": status,
            "feed_score_for": feed_for, "feed_score_against": feed_against,
            "score_for": score_for, "score_against": score_against, "score_finalized_by": finalized_by,
            "linked_gallery_id": linked_gallery_id, "tickets_url": tickets_url, "livestream_url": livestream_url,
            "box_score_url": box_score_url,
        }
        cur.execute(
            """INSERT INTO games (team_id, season, date, time, time_type, time_zone, opponent_id, opponent_two_id,
                                   opponent_display_name, opponent_display_website_url, is_neutral_host,
                                   game_type, result_type, finish_place, total_teams, home_away, city, state,
                                   facility, event_group_id, status, feed_score_for, feed_score_against,
                                   score_for, score_against, score_finalized_by, linked_gallery_id,
                                   tickets_url, livestream_url, box_score_url)
               VALUES (:team_id, '2026', :date, :time, :time_type, :time_zone, :opponent_id, :opponent_two_id,
                       :opponent_display_name, :opponent_display_website_url, :is_neutral_host,
                       :game_type, :result_type, :finish_place, :total_teams, :home_away, :city, :state,
                       :facility, :event_group_id, :status, :feed_score_for, :feed_score_against,
                       :score_for, :score_against, :score_finalized_by, :linked_gallery_id,
                       :tickets_url, :livestream_url, :box_score_url)""",
            params,
        )
        return cur.lastrowid

    # --- Men's Soccer ---------------------------------------------------
    cedar_valley_game_id = make_game(
        mens_soccer_id, cedar_valley_id, today - timedelta(days=5), "home", "Northfield", "IL",
        "final", feed_for=3, feed_against=1, linked_gallery_id=opener_gallery_id, time="14:00",
        facility="Northfield Field", game_type="conference",
        box_score_url="https://stats.northfieldfalcons.com/boxscore/mens-soccer/2026-08-15",
    )
    make_game(mens_soccer_id, ridgeview_id, today + timedelta(days=2), "home", "Northfield", "IL",
              "upcoming", time="13:00", tickets_url="https://northfieldfalcons.com/tickets",
              facility="Northfield Field", game_type="non_conference")
    make_game(mens_soccer_id, eastmoor_id, today + timedelta(days=6), "away", "Eastmoor", "IL",
              "upcoming", time="16:00", livestream_url="https://youtube.com/live/example",
              facility="Eastmoor Stadium", game_type="conference", time_zone="America/Chicago")
    make_game(mens_soccer_id, ridgeview_id, today + timedelta(days=13), "away", "Ridgeview", "CA",
              "upcoming", time="19:00", facility="Ridgeview Soccer Complex", game_type="non_conference",
              time_zone="America/Los_Angeles")
    make_game(mens_soccer_id, cedar_valley_id, today + timedelta(days=20), "home", "Northfield", "IL",
              "upcoming", time="13:00", facility="Northfield Field", game_type="conference")
    make_game(mens_soccer_id, eastmoor_id, today - timedelta(days=12), "home", "Northfield", "IL",
              "final", feed_for=2, feed_against=2, time="15:00", facility="Northfield Field",
              game_type="conference")
    # Proves the record exclusion: a lopsided exhibition result that must
    # NOT move the Record Bar's W-L, even though it's a real final score.
    # Also the All Day demo — a homecoming/alumni event, not a single
    # kickoff time.
    make_game(mens_soccer_id, alumni_id, today - timedelta(days=20), "home", "Northfield", "IL",
              "final", feed_for=6, feed_against=0, time_type="all_day", facility="Northfield Field",
              game_type="exhibition")
    # Today's Games dashboard demo (score-based quick entry) — lands on
    # whatever "today" is when this gets seeded, since `today` above is
    # anchored to the real current date.
    make_game(mens_soccer_id, eastmoor_id, today, "home", "Northfield", "IL",
              "upcoming", time="18:00", facility="Northfield Field", game_type="conference",
              tickets_url="https://northfieldfalcons.com/tickets")

    # --- Women's Soccer ---------------------------------------------------
    make_game(womens_soccer_id, eastmoor_id, today - timedelta(days=7), "away", "Eastmoor", "IL",
              "final", feed_for=4, feed_against=1, time="14:00", facility="Eastmoor Stadium",
              game_type="conference")
    make_game(womens_soccer_id, cedar_valley_id, today - timedelta(days=14), "home", "Northfield", "IL",
              "final", feed_for=1, feed_against=1, time="13:00", facility="Northfield Field",
              game_type="conference")
    make_game(womens_soccer_id, ridgeview_id, today + timedelta(days=3), "home", "Northfield", "IL",
              "upcoming", time="15:00", tickets_url="https://northfieldfalcons.com/tickets",
              facility="Northfield Field", game_type="non_conference")
    # Opponent display-name override demo: Ridgeview is loaded for its logo
    # and website, but this game displays as an invitational, not "Ridgeview".
    make_game(womens_soccer_id, ridgeview_id, today + timedelta(days=10), "home", "Northfield", "IL",
              "upcoming", time="14:00", livestream_url="https://youtube.com/live/example",
              facility="Northfield Field", game_type="non_conference",
              opponent_display_name="Fall Kickoff Classic")
    make_game(womens_soccer_id, cedar_valley_id, today + timedelta(days=17), "away", "Cedar Valley", "CO",
              "upcoming", time="16:00", facility="Cedar Valley Athletic Complex",
              game_type="conference", time_zone="America/Denver")

    # --- Volleyball ---------------------------------------------------------
    make_game(volleyball_id, ridgeview_id, today - timedelta(days=9), "home", "Northfield", "IL",
              "final", feed_for=3, feed_against=2, time="19:00", facility="Northfield Gym",
              game_type="conference",
              box_score_url="https://stats.northfieldfalcons.com/boxscore/volleyball/2026-08-11")
    make_game(volleyball_id, cedar_valley_id, today - timedelta(days=2), "away", "Cedar Valley", "CO",
              "final", feed_for=1, feed_against=3, time="18:30", facility="Cedar Valley Fieldhouse",
              game_type="non_conference", time_zone="America/Denver")
    make_game(volleyball_id, eastmoor_id, today + timedelta(days=4), "home", "Northfield", "IL",
              "upcoming", time="19:00", tickets_url="https://northfieldfalcons.com/tickets",
              facility="Northfield Gym", game_type="conference")
    make_game(volleyball_id, ridgeview_id, today + timedelta(days=11), "away", "Ridgeview", "CA",
              "upcoming", time="18:00", facility="Ridgeview Fieldhouse", game_type="conference",
              time_zone="America/Los_Angeles")
    make_game(volleyball_id, cedar_valley_id, today + timedelta(days=18), "home", "Northfield", "IL",
              "upcoming", time="19:00", facility="Northfield Gym", game_type="non_conference")
    # Neutral Host demo: Ridgeview vs. Eastmoor, hosted at Northfield Gym.
    # Northfield's own Volleyball team isn't a participant — filed under
    # Volleyball purely for admin/organizational purposes.
    make_game(
        volleyball_id, ridgeview_id, today - timedelta(days=1), "neutral", "Northfield", "IL",
        "final", feed_for=2, feed_against=1, time="12:00", facility="Northfield Gym",
        game_type="non_conference", is_neutral_host=1, opponent_two_id=eastmoor_id,
    )
    # Event Group demo: two Volleyball postseason games grouped under one
    # tournament header — each fully independent (own opponent, own result,
    # own game_type), grouping is display-only.
    conf_tourney_id = make_event_group(
        volleyball_id, "Empire Conference Tournament", today + timedelta(days=25), today + timedelta(days=26)
    )
    make_game(volleyball_id, ridgeview_id, today + timedelta(days=25), "neutral", "Eastmoor", "IL",
              "upcoming", time="17:00", facility="Eastmoor Fieldhouse", game_type="postseason",
              event_group_id=conf_tourney_id)
    make_game(volleyball_id, eastmoor_id, today + timedelta(days=26), "neutral", "Eastmoor", "IL",
              "upcoming", time="19:00", facility="Eastmoor Fieldhouse", game_type="postseason",
              event_group_id=conf_tourney_id)

    # --- Cross Country — the place-finish demo ------------------------------
    # Each meet's opponent is set to the host school (so its logo/site still
    # anchor the listing), but opponent_display_name relabels it with the
    # meet's real name — this is the actual fix for "many-team meets don't
    # have one real opponent" from data-model.md §5f.
    # Cross country meet start times are routinely weather/heat-dependent
    # and announced day-of — every meet here is the TBA demo.
    make_game(xc_id, ridgeview_id, today - timedelta(days=10), "away", "Ridgeview", "CA",
              "final", facility="Ridgeview Cross Country Course", game_type="non_conference",
              time_zone="America/Los_Angeles", opponent_display_name="Ridgeview Invitational",
              time_type="tba", finish_place=3, total_teams=12)
    make_game(xc_id, eastmoor_id, today - timedelta(days=3), "away", "Eastmoor", "IL",
              "final", facility="Eastmoor Park", game_type="non_conference",
              opponent_display_name="Eastmoor Fall Classic", time_type="tba", finish_place=1, total_teams=8)
    # Individuals compete, no team score is kept for this one.
    make_game(xc_id, cedar_valley_id, today - timedelta(days=17), "away", "Cedar Valley", "CO",
              "final", facility="Cedar Valley Trail Park", game_type="non_conference",
              time_zone="America/Denver", opponent_display_name="Cedar Valley Twilight Meet",
              time_type="tba", result_type="no_team_score")
    # Happened, but the school isn't publishing a result for it.
    make_game(xc_id, ridgeview_id, today - timedelta(days=24), "home", "Northfield", "IL",
              "final", facility="Northfield Park", game_type="non_conference",
              opponent_display_name="Northfield Twilight Opener", time_type="tba", result_type="no_result")
    # Upcoming — no result yet either way.
    make_game(xc_id, eastmoor_id, today + timedelta(days=21), "away", "Eastmoor", "IL",
              "upcoming", facility="Eastmoor Park", game_type="postseason", time_type="tba",
              opponent_display_name="Empire Conference Championships", total_teams=10)
    # Today's Games dashboard demo (place-finish quick entry) — same "today"
    # anchor as the Men's Soccer game above.
    make_game(xc_id, cedar_valley_id, today, "away", "Cedar Valley", "CO",
              "upcoming", facility="Cedar Valley Trail Park", game_type="non_conference",
              time_zone="America/Denver", opponent_display_name="Cedar Valley Twilight Meet II",
              time_type="tba", total_teams=10)

    # --- Football — the live/box-score stats demo ---------------------------
    # A completed game with a full imported box score (game_team_stats),
    # a game marked Live mid-game with a partial box score (proving the
    # "phased into live" path — same import mechanism, just run again as
    # the game progresses), and a couple of ordinary upcoming games so the
    # schedule doesn't look like it exists only to demo one feature.
    def make_team_stat(game_id, side, key, value, sort_order):
        cur.execute(
            "INSERT INTO game_team_stats (game_id, side, stat_key, stat_value, sort_order) VALUES (?, ?, ?, ?, ?)",
            (game_id, side, key, value, sort_order),
        )

    def make_play(game_id, sequence, period, clock, side, description,
                   score_for=None, score_against=None, is_scoring=False,
                   yard_line=None, down_distance=None):
        cur.execute(
            """INSERT INTO game_plays
               (game_id, sequence, period, clock, team_side, description, score_for, score_against,
                is_scoring_play, yard_line, down_distance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (game_id, sequence, period, clock, side, description, score_for, score_against,
             1 if is_scoring else 0, yard_line, down_distance),
        )

    def make_player_stat(game_id, side, player_name, jersey_number, athlete_id, category, key, value, sort_order):
        cur.execute(
            """INSERT INTO game_player_stats
               (game_id, side, player_name, jersey_number, athlete_id, category, stat_key, stat_value, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (game_id, side, player_name, jersey_number, athlete_id, category, key, value, sort_order),
        )

    fb_final_id = make_game(
        football_id, ridgeview_id, today - timedelta(days=6), "home", "Northfield", "IL",
        "final", feed_for=27, feed_against=24, time="19:00", facility="Northfield Stadium",
        game_type="conference",
    )
    # Backfilled to look exactly like a stats-feed import landed here (see
    # _commit_stats_import in app.py) — score_finalized_by='live_feed_auto',
    # not 'manual_entry', and stats_feed_updated_at stamped, since that's
    # what actually happened for this demo game.
    cur.execute(
        "UPDATE games SET score_finalized_by='live_feed_auto', stats_feed_updated_at=? WHERE id=?",
        ((today - timedelta(days=6)).strftime("%Y-%m-%dT22:47"), fb_final_id),
    )
    fb_stat_rows = [
        ("total_yards", "412", "356"), ("passing_yards", "248", "201"), ("rushing_yards", "164", "155"),
        ("first_downs", "22", "18"), ("third_down", "6-14", "4-13"), ("turnovers", "1", "2"),
        ("penalties", "7-65", "5-40"), ("time_of_possession", "31:12", "28:48"),
    ]
    for i, (key, for_val, against_val) in enumerate(fb_stat_rows):
        make_team_stat(fb_final_id, "for", key, for_val, i)
        make_team_stat(fb_final_id, "against", key, against_val, i)

    # In progress right now — partial stats on file, LIVE badge on the
    # public site, exactly what a mid-game re-import would look like.
    fb_live_id = make_game(
        football_id, eastmoor_id, today, "away", "Eastmoor", "IL",
        "live", feed_for=14, feed_against=10, time="18:00", facility="Eastmoor Stadium",
        game_type="conference", livestream_url="https://youtube.com/live/example",
    )
    cur.execute(
        "UPDATE games SET score_finalized_by='live_feed_auto', stats_feed_updated_at=? WHERE id=?",
        (today.strftime("%Y-%m-%dT19:32"), fb_live_id),
    )
    for i, (key, for_val, against_val) in enumerate([
        ("total_yards", "198", "165"), ("passing_yards", "110", "90"), ("rushing_yards", "88", "75"),
        ("first_downs", "11", "9"),
    ]):
        make_team_stat(fb_live_id, "for", key, for_val, i)
        make_team_stat(fb_live_id, "against", key, against_val, i)

    make_game(football_id, cedar_valley_id, today + timedelta(days=7), "home", "Northfield", "IL",
              "upcoming", time="13:00", tickets_url="https://northfieldfalcons.com/tickets",
              facility="Northfield Stadium", game_type="non_conference")
    make_game(football_id, ridgeview_id, today + timedelta(days=14), "away", "Ridgeview", "CA",
              "upcoming", time="19:00", facility="Ridgeview Stadium", game_type="non_conference",
              time_zone="America/Los_Angeles")

    # --- Athletes + roster entries (2026) -----------------------
    # No athletes.status column any more — a player exists forever once
    # created; height is feet/inches now (displayed everywhere as FT-IN,
    # e.g. 5-11), not a free-text string.
    def make_athlete_and_entry(first, last, hometown, team_id, jersey, position, class_year, height_ft, height_in,
                                weight, headshot_url=None):
        cur.execute(
            "INSERT INTO athletes (school_id, first_name, last_name, hometown, headshot_url) VALUES (?, ?, ?, ?, ?)",
            (school_id, first, last, hometown, headshot_url),
        )
        athlete_id = cur.lastrowid
        cur.execute(
            """INSERT INTO roster_entries (athlete_id, team_id, season, jersey_number, position, class_year,
                                            height_ft, height_in, weight)
               VALUES (?, ?, '2026', ?, ?, ?, ?, ?, ?)""",
            (athlete_id, team_id, jersey, position, class_year, height_ft, height_in, weight),
        )
        return athlete_id

    make_athlete_and_entry("Jordan", "Castillo", "Denver, CO", mens_soccer_id, "7", "M", "Jr", 5, 11, "165")
    make_athlete_and_entry("Devon", "Walsh", "Austin, TX", mens_soccer_id, "11", "F", "So", 6, 0, "172")
    make_athlete_and_entry("Ravi", "Nair", "Portland, OR", mens_soccer_id, "1", "GK", "Sr", 6, 2, "185")

    # Women's Soccer — the Photo template demo. One has a headshot on file
    # (real <img>), one doesn't (falls back to the initials monogram), on
    # purpose, so both code paths are visibly exercised.
    make_athlete_and_entry("Priya", "Anand", "Seattle, WA", womens_soccer_id, "10", "F", "Jr", 5, 6, "130",
                            headshot_url="https://images.unsplash.com/photo-1594381898411-846e7d193883?w=300&h=300&fit=crop&crop=faces")
    make_athlete_and_entry("Grace", "Kimura", "Sacramento, CA", womens_soccer_id, "4", "D", "So", 5, 8, "140")

    # Volleyball — the Table template demo.
    nia_id = make_athlete_and_entry("Nia", "Robertson", "Atlanta, GA", volleyball_id, "12", "OH", "Sr", 5, 11, "155")
    make_athlete_and_entry("Elle", "Sorensen", "Fargo, ND", volleyball_id, "2", "S", "Fr", 5, 7, "135")

    # Football — the live/box-score stats demo team. A small roster (not
    # every position), since the point of this team is exercising the
    # stats-feed import and box score display, not a full roster build-out.
    banks_id = make_athlete_and_entry("Tyrell", "Banks", "Cleveland, OH", football_id, "12", "QB", "Jr", 6, 2, "205")
    ade_id = make_athlete_and_entry("Marcus", "Ade", "Baton Rouge, LA", football_id, "24", "RB", "So", 5, 10, "195")
    whitfield_id = make_athlete_and_entry("Cole", "Whitfield", "Tampa, FL", football_id, "88", "WR", "Sr", 6, 3, "190")
    make_athlete_and_entry("Isaiah", "Odom", "Detroit, MI", football_id, "54", "LB", "Jr", 6, 1, "225")

    # --- Football play-by-play + player box score (stats-feed demo data) --
    # Seeded here, after the roster exists, so "for"-side plays/stats can
    # link athlete_id the same way a real stats-feed import would (see
    # _match_athlete_id in app.py) — this is what a live feed import would
    # have produced, hand-written instead of actually parsed from XML.
    # fb_final_id/fb_live_id were captured above when those two games were
    # created and are still in scope here.
    #
    # fb_final_id: a full, final 27-24 win, five scoring plays for
    # Northfield and four for Ridgeview, chosen so the running score lands
    # exactly on the game's actual final (27-24). yard_line is always in
    # Northfield's ("for") own frame — 0 is Northfield's goal line, 100 is
    # Ridgeview's — regardless of which side has the ball (see the
    # yard_line comment in schema.sql). The last drive (sequences 10-14)
    # is deliberately several plays long, ending on the game-winning field
    # goal with 1:12 left. Only the is_scoring_play=1 rows actually surface
    # anywhere in the UI now (the public game-card modal's Scoring Summary);
    # the rest of this drive is stored but unused, same as a real feed's
    # full play-by-play would be.
    for play in [
        (1, "Q1", "12:45", "for", "Banks pass complete to Whitfield for 12 yards.", None, None, False, 37, "2nd & 3"),
        (2, "Q1", "9:15", "for", "Ade 4 yard TD run. PAT good.", 7, 0, True, 100, "1st & Goal"),
        (3, "Q1", "3:40", "against", "Ridgeview TD pass, 18 yards. PAT good.", 7, 7, True, 0, None),
        (4, "Q2", "10:20", "for", "Banks 22 yard TD pass to Whitfield. PAT good.", 14, 7, True, 100, "2nd & Goal"),
        (5, "Q2", "6:05", "against", "Ridgeview 32 yard field goal.", 14, 10, True, 15, None),
        (6, "Q3", "9:30", "for", "35 yard field goal.", 17, 10, True, 85, None),
        (7, "Q3", "4:15", "against", "Ridgeview 6 yard TD run. PAT good.", 17, 17, True, 0, None),
        (8, "Q4", "11:00", "for", "Ade 12 yard TD run. PAT good.", 24, 17, True, 100, "1st & Goal"),
        (9, "Q4", "5:30", "against", "Ridgeview TD pass, 9 yards. PAT good.", 24, 24, True, 0, None),
        (10, "Q4", "4:10", "for", "Banks pass complete to Whitfield for 18 yards.", None, None, False, 42, "1st & 10"),
        (11, "Q4", "3:15", "for", "Ade run for 8 yards.", None, None, False, 50, "2nd & 2"),
        (12, "Q4", "2:40", "for", "Banks pass complete to Whitfield for 15 yards.", None, None, False, 65, "1st & 10"),
        (13, "Q4", "1:50", "for", "Ade run for 7 yards.", None, None, False, 72, "2nd & 3"),
        (14, "Q4", "1:12", "for", "28 yard field goal. Falcons take the lead.", 27, 24, True, 72, "3rd & 3"),
    ]:
        make_play(fb_final_id, *play)

    for i, (key, val) in enumerate([("comp_att", "18-27"), ("yards", "248"), ("td", "2"), ("int", "1")]):
        make_player_stat(fb_final_id, "for", "Tyrell Banks", "12", banks_id, "passing", key, val, i)
    for i, (key, val) in enumerate([("car", "22"), ("yards", "164"), ("td", "2")]):
        make_player_stat(fb_final_id, "for", "Marcus Ade", "24", ade_id, "rushing", key, val, i)
    for i, (key, val) in enumerate([("rec", "7"), ("yards", "112"), ("td", "1")]):
        make_player_stat(fb_final_id, "for", "Cole Whitfield", "88", whitfield_id, "receiving", key, val, i)
    for i, (key, val) in enumerate([("comp_att", "16-29"), ("yards", "201"), ("td", "2"), ("int", "0")]):
        make_player_stat(fb_final_id, "against", "Derek Coyle", "9", None, "passing", key, val, i)
    for i, (key, val) in enumerate([("car", "19"), ("yards", "88"), ("td", "1")]):
        make_player_stat(fb_final_id, "against", "Malik Reyes", "21", None, "rushing", key, val, i)
    for i, (key, val) in enumerate([("rec", "6"), ("yards", "97"), ("td", "1")]):
        make_player_stat(fb_final_id, "against", "Trey Nolan", "80", None, "receiving", key, val, i)

    # fb_live_id: in-progress through the second quarter, ending on the
    # most recent play reported by the feed (sequence 10). This game's
    # score/status come from games.score_for/score_against/status directly
    # (updated live via the stats-feed webhook) — these plays are stored the
    # same way a real live push's plays would be, but nothing renders them
    # for an in-progress game since only a final push's Scoring Summary
    # displays on the public site.
    for play in [
        (1, "Q1", "11:50", "for", "Banks pass complete to Whitfield for 9 yards.", None, None, False, 34, "2nd & 1"),
        (2, "Q1", "7:30", "for", "Ade 6 yard TD run. PAT good.", 7, 0, True, 100, "1st & Goal"),
        (3, "Q1", "2:15", "against", "Eastmoor 29 yard field goal.", 7, 3, True, 12, None),
        (4, "Q2", "9:40", "against", "Eastmoor TD pass, 14 yards. PAT good.", 7, 10, True, 0, None),
        (5, "Q2", "8:40", "for", "Banks pass complete to Ade for 11 yards.", None, None, False, 31, "1st & 10"),
        (6, "Q2", "7:55", "for", "Ade run for 9 yards.", None, None, False, 40, "2nd & 1"),
        (7, "Q2", "7:10", "for", "Ade run for 2 yards. First down.", None, None, False, 42, "1st & 10"),
        (8, "Q2", "6:20", "for", "Banks pass complete to Whitfield for 27 yards.", None, None, False, 69, "1st & 10"),
        (9, "Q2", "5:40", "for", "Ade run for 6 yards.", None, None, False, 75, "2nd & 4"),
        (10, "Q2", "5:05", "for", "Banks 25 yard TD pass to Whitfield. PAT good.", 14, 10, True, 100, "2nd & Goal"),
    ]:
        make_play(fb_live_id, *play)

    for i, (key, val) in enumerate([("comp_att", "10-15"), ("yards", "110"), ("td", "1"), ("int", "0")]):
        make_player_stat(fb_live_id, "for", "Tyrell Banks", "12", banks_id, "passing", key, val, i)
    for i, (key, val) in enumerate([("car", "14"), ("yards", "88"), ("td", "1")]):
        make_player_stat(fb_live_id, "for", "Marcus Ade", "24", ade_id, "rushing", key, val, i)
    for i, (key, val) in enumerate([("rec", "4"), ("yards", "58"), ("td", "1")]):
        make_player_stat(fb_live_id, "for", "Cole Whitfield", "88", whitfield_id, "receiving", key, val, i)
    for i, (key, val) in enumerate([("comp_att", "9-13"), ("yards", "97"), ("td", "1"), ("int", "1")]):
        make_player_stat(fb_live_id, "against", "Jordan Vance", "6", None, "passing", key, val, i)
    for i, (key, val) in enumerate([("car", "10"), ("yards", "52"), ("td", "0")]):
        make_player_stat(fb_live_id, "against", "Sam Ridley", "33", None, "rushing", key, val, i)
    for i, (key, val) in enumerate([("rec", "3"), ("yards", "41"), ("td", "1")]):
        make_player_stat(fb_live_id, "against", "Chris Boone", "18", None, "receiving", key, val, i)

    # --- Roster Settings + Coaches (2026) --------------------------------
    # Volleyball demos: Show Coaches Below Roster on, a custom field with a
    # school-chosen label populated, and its head coach tied to this
    # specific roster (not just the team, in case that ever changes).
    cur.execute(
        """INSERT INTO roster_settings (team_id, season, show_coaches, fields_config)
           VALUES (?, '2026', 1, ?)""",
        (volleyball_id, json.dumps([
            {"key": "jersey_number", "enabled": True, "label": None}, {"key": "name", "enabled": True, "label": None},
            {"key": "position", "enabled": True, "label": None}, {"key": "class_year", "enabled": True, "label": None},
            {"key": "height", "enabled": True, "label": None}, {"key": "weight", "enabled": True, "label": None},
            {"key": "hometown", "enabled": True, "label": None}, {"key": "high_school", "enabled": False, "label": None},
            {"key": "previous_school", "enabled": False, "label": None}, {"key": "bats_throws", "enabled": False, "label": None},
            {"key": "custom_field_1", "enabled": True, "label": "Eligibility"},
            {"key": "custom_field_2", "enabled": False, "label": None}, {"key": "custom_field_3", "enabled": False, "label": None},
        ])),
    )
    cur.execute("UPDATE roster_entries SET custom_field_1='Eligible' WHERE athlete_id=?", (nia_id,))
    cur.execute(
        "INSERT INTO roster_coaches (team_id, season, staff_id, is_head_coach, sort_order) VALUES (?, '2026', ?, 1, 0)",
        (volleyball_id, rachel_staff_id),
    )
    cur.execute(
        "INSERT INTO roster_coaches (team_id, season, staff_id, is_head_coach, sort_order) VALUES (?, '2026', ?, 1, 0)",
        (mens_soccer_id, marco_staff_id),
    )
    cur.execute(
        "INSERT INTO roster_coaches (team_id, season, staff_id, is_head_coach, sort_order) VALUES (?, '2026', ?, 1, 0)",
        (football_id, dwight_staff_id),
    )

    # --- Sponsors -----------------------------------------------------------
    cur.executemany(
        "INSERT INTO sponsors (school_id, name, tier, sort_order) VALUES (?, ?, ?, ?)",
        [
            (school_id, "Northfield Ford", "Platinum", 0),
            (school_id, "Valley Sports Med", "Gold", 1),
            (school_id, "Campus Credit Union", "Gold", 2),
        ],
    )

    # --- Article (the recap) -------------------------------------------------
    # Bodies are stored as rich-text HTML now (the Article Editor's WYSIWYG
    # toolbar saves contenteditable markup) rather than plain text split on
    # blank lines, so every seeded body below is hand-wrapped in <p> tags to
    # match what a real save from that editor would produce.
    def _html_body(*paragraphs):
        return "".join(f"<p>{p}</p>" for p in paragraphs)

    recap_body = _html_body(
        "Northfield's Men's Soccer team opened the season in front of a home crowd Saturday, "
        "defeating Cedar Valley 3&ndash;1 at Northfield Field.",
        "Jordan Castillo opened the scoring in the 22nd minute, with Devon Walsh adding two "
        "second-half goals to seal the win. Goalkeeper Ravi Nair made six saves to secure the result.",
        "The Falcons return to action Wednesday at Eastmoor University.",
    )
    cur.execute(
        """INSERT INTO articles (school_id, team_id, linked_game_id, linked_gallery_id, category, season,
                                  title, subheadline, slug, body, author_name, status, published_at,
                                  hidden, publish_applied, home_rank, sport_rank, template_type)
           VALUES (?, ?, ?, ?, 'game_recap', '2026', ?, ?, ?, ?, 'Sarah Jennings', 'published', ?,
                   0, 1, 2, 1, 'standard')""",
        (school_id, mens_soccer_id, cedar_valley_game_id, opener_gallery_id,
         "Soccer Tops Cedar Valley, 3–1", "Falcons open the home slate with a statement win",
         "soccer-tops-cedar-valley", recap_body,
         (today - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")),
    )
    recap_article_id = cur.lastrowid
    cur.executemany(
        "INSERT INTO article_links (article_id, title, url, sort_order) VALUES (?, ?, ?, ?)",
        [
            (recap_article_id, "Full Box Score", f"/site/northfield-college/teams/mens-soccer/schedule", 0),
            (recap_article_id, "Men's Soccer Schedule", "/site/northfield-college/teams/mens-soccer/schedule", 1),
        ],
    )

    # Three more, exercising the rest of the Story System: a school-wide
    # Feature (no team, no linked game — just a story), a team-scoped
    # General Article, and a Pending Review General Article with no publish
    # date yet — the state a Staffer-authored story lands in until someone
    # with publish_news (Admin, or Manager by default) approves it. Together
    # with the recap above, all four Classifications and all three statuses
    # have at least one real row on file.
    feature_body = _html_body(
        "Northfield's strength and conditioning staff spent the offseason rebuilding how every "
        "varsity program trains, moving from sport-specific weight blocks to a shared movement-first "
        "program built around sprint mechanics and change-of-direction work.",
        "&ldquo;We stopped asking &lsquo;what does a soccer player lift&rsquo; and started asking "
        "&lsquo;what makes any Falcon faster,&rsquo;&rdquo; said head strength coach Elena Ruiz. Early "
        "testing numbers across three programs already show measurable gains in 10-yard split times.",
        "The full rollout continues through the fall, with results tracked program-by-program.",
    )
    cur.execute(
        """INSERT INTO articles (school_id, category, season, title, subheadline, slug, body, author_name,
                                  status, published_at, hidden, publish_applied, home_rank, template_type)
           VALUES (?, 'feature_story', '2026', ?, ?, ?, ?, 'Sarah Jennings', 'published', ?, 0, 1, 1, 'standard')""",
        (school_id, "Inside the Offseason: Building a Faster Falcons Roster",
         "How a shared strength program is changing every Falcons roster",
         "inside-the-offseason-faster-falcons", feature_body,
         (today - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")),
    )

    press_release_body = _html_body(
        "Northfield College Athletics announced today a contract extension for Men's Soccer head "
        "coach Marco Bellucci through the 2029 season.",
        "Bellucci, entering his fourth year with the Falcons, has led the program to consecutive "
        "postseason appearances and a 2025 Empire Athletic Conference regular-season title.",
        "&ldquo;Marco has built something real here, on and off the field,&rdquo; said Athletic Director "
        "Sarah Jennings. &ldquo;This extension reflects how much we believe in where this program is headed.&rdquo;",
    )
    cur.execute(
        """INSERT INTO articles (school_id, team_id, category, season, title, subheadline, slug, body,
                                  author_name, status, published_at, hidden, publish_applied, home_rank,
                                  sport_rank, template_type)
           VALUES (?, ?, 'general_article', '2026', ?, ?, ?, ?, 'Athletics Communications', 'published', ?,
                   0, 1, 3, 2, 'standard')""",
        (school_id, mens_soccer_id, "Northfield College Extends Men's Soccer Coach Marco Bellucci Through 2029",
         "Bellucci's extension runs through the 2029 season",
         "bellucci-extension-2029", press_release_body,
         (today - timedelta(days=18)).strftime("%Y-%m-%d %H:%M:%S")),
    )

    homecoming_body = _html_body(
        "Homecoming Weekend is set for October 24-25, headlined by Saturday's football game against "
        "Eastmoor University at 1:00 PM.",
        "Festivities begin Friday evening with the alumni tailgate outside Founders Stadium, followed "
        "by Saturday's parade through campus ahead of kickoff. A full schedule of events will be "
        "shared closer to the date.",
        "This is a draft pending review — nothing here publishes until someone with publish permission signs off.",
    )
    cur.execute(
        """INSERT INTO articles (school_id, category, season, title, subheadline, slug, body, author_name, status)
           VALUES (?, 'general_article', '2026', ?, ?, ?, ?, 'Marcus Webb', 'pending_review')""",
        (school_id, "Homecoming Weekend Set for October 24-25", "Alumni weekend returns to Founders Stadium",
         "homecoming-weekend-2026", homecoming_body),
    )

    # --- Pages + blocks --------------------------------------------------------
    def make_page(title, slug, nav_placement, blocks):
        cur.execute(
            """INSERT INTO pages (school_id, title, slug, status, nav_placement, updated_at)
               VALUES (?, ?, ?, 'published', ?, CURRENT_TIMESTAMP)""",
            (school_id, title, slug, nav_placement),
        )
        page_id = cur.lastrowid
        for i, (block_type, content) in enumerate(blocks):
            cur.execute(
                "INSERT INTO page_blocks (page_id, block_type, position, content) VALUES (?, ?, ?, ?)",
                (page_id, block_type, i, json.dumps(content)),
            )
        return page_id

    make_page(
        "Home", "", "Home",
        [
            ("hero", {
                "eyebrow": "Northfield College Athletics",
                "headline": "Home of the Falcons",
                "subheading": "17 varsity programs. One tradition of excellence.",
                "primary_button": "View Schedule",
                "secondary_button": "Latest News",
                "secondary_button_href": "/site/northfield-college/news",
            }),
            ("news_feed", {"team_id": None, "limit": 3}),
            ("schedule", {"team_id": None, "limit": 3}),
            ("sponsor_strip", {}),
        ],
    )

    # Each team gets a hub page (hero + a short "Upcoming Games" preview that
    # links out, + roster where built) and, separately, its own full Schedule
    # page (data-model.md §12 — "the schedule should be its own page, separate
    # from the roster"). The preview block never sets full_page, so it always
    # stays the same short, simple list regardless of the team's chosen views —
    # a block that fits the hub page, not a second copy of the full switcher.
    team_pages = [
        (mens_soccer_id, "Men's Soccer", "mens-soccer", True),
        (womens_soccer_id, "Women's Soccer", "womens-soccer", True),
        (volleyball_id, "Volleyball", "volleyball", True),
        (xc_id, "Cross Country", "cross-country", True),
        (football_id, "Football", "football", True),
    ]
    for team_id, name, slug, with_roster in team_pages:
        schedule_href = f"/site/northfield-college/teams/{slug}/schedule"
        hub_blocks = [
            ("hero", {
                "eyebrow": name,
                "headline": "2026 Season",
                "primary_button": "View Schedule",
                "primary_button_href": schedule_href,
            }),
        ]
        if with_roster:
            hub_blocks.append(("roster_grid", {"team_id": team_id}))
        # This is what exercises the sport-page News rotator (sport_rank) —
        # only Men's Soccer has any ranked stories seeded above, so every
        # other team's rotator legitimately renders its "No published
        # stories yet" empty state.
        hub_blocks.append(("news_feed", {"team_id": team_id, "limit": 3}))
        hub_blocks.append(
            ("schedule", {"team_id": team_id, "limit": 4, "link_href": schedule_href, "link_label": "View Full Schedule"})
        )
        hub_blocks.append(("sponsor_strip", {}))
        make_page(name, f"teams/{slug}", None, hub_blocks)

        make_page(
            f"{name} Schedule", f"teams/{slug}/schedule", None,
            [("schedule", {"team_id": team_id, "full_page": True}), ("sponsor_strip", {})],
        )

    conn.commit()
    conn.close()
    print("Seeded Northfield College — school_id:", school_id)


if __name__ == "__main__":
    run()
