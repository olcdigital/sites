-- Phase 1 schema — matches data-model.md, scoped to the core CMS.
-- Live-stats fields on games are included as nullable so Phase 3 doesn't
-- require a breaking migration later; nothing writes to them yet.

PRAGMA foreign_keys = ON;

CREATE TABLE schools (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  slug TEXT UNIQUE NOT NULL,
  domain TEXT,
  mascot TEXT,
  conference TEXT,
  city TEXT,
  state TEXT,
  default_timezone TEXT DEFAULT 'America/Chicago',  -- IANA tz id; Site Settings would edit this
  primary_color TEXT DEFAULT '#0f172a',
  secondary_color TEXT DEFAULT '#2563eb',
  logo_url TEXT,
  status TEXT DEFAULT 'active',            -- trial | active | suspended
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE teams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  gender TEXT NOT NULL,                    -- men | women | coed
  sport_season TEXT NOT NULL,              -- fall | winter | spring
  roster_template TEXT DEFAULT 'card',            -- card | photo | table — which of the roster
                                                   -- display varieties this team's public roster uses
  -- School-site-centric schedule views (data-model.md §12) — renamed from
  -- schedule_template; six views built around the client's own team (at/vs.
  -- opponent) rather than a neutral, both-sides-equal conference scoreboard.
  default_schedule_view TEXT DEFAULT 'classic',   -- classic | matchup_cards | compact_list | timeline | calendar | season_story
  enabled_schedule_views TEXT DEFAULT '["classic"]',  -- JSON array — subset of the above the public switcher offers; always includes default_schedule_view
  -- No per-view logo toggle: Compact List is always text-only (a plain
  -- density-first list is the point of it) and every other view always
  -- shows the opponent logo (falling back to initials, never optional).
  show_record_bar INTEGER DEFAULT 1,       -- public Record Bar on/off (off for golf/XC-style sports)
  results_format TEXT DEFAULT 'score',     -- score | place_finish
  -- A canonical sport key, independent of the school's own display `name`
  -- (a school could rename "Football" to "Falcons Football" without ever
  -- breaking a lookup keyed off this). Only sports with an entry in
  -- app.py's TEAM_STAT_CATEGORIES get the live-stats feature surfaced at
  -- all — everyone else just doesn't see the Results section's stats UI.
  sport TEXT,
  -- Now wired (was a dead placeholder through Phase 1's earlier rounds):
  -- 'off' hides the stats-feed webhook/import UI entirely and the webhook
  -- endpoint rejects pushes for this team. This only ever gates the FINAL
  -- box score commit (game_team_stats/game_plays/game_player_stats) — a
  -- live push's score/status update always applies immediately regardless
  -- of this setting (see _apply_feed_score_update), since that's just a
  -- number and low-risk either way. 'manual_confirm': a live push still
  -- updates the score right away, but a final push's full box score is
  -- stashed on the game (games.pending_feed_xml) for an admin to review
  -- and Confirm & Publish, same as reviewing a manually-pasted feed always
  -- has. 'auto_publish': a final push's box score commits immediately too,
  -- no review step — for a school whose feed source is trusted enough to
  -- skip the human-in-the-loop check.
  live_scoring_mode TEXT DEFAULT 'off',    -- auto_publish | manual_confirm | off
  -- Secret path segment in this team's stats-feed webhook URL
  -- (/api/stats-feed/<token>/<game_id>) — one token per team, shared across
  -- every one of that team's games, since a school's stat-computer setup is
  -- normally configured once per program/season rather than per game. Not
  -- real auth (this whole prototype has none — see README's "No auth"), just
  -- enough of an unguessable-URL convention that the webhook isn't wide
  -- open to anyone who finds it. Generated lazily on first use
  -- (_get_or_create_stats_feed_token) so existing/seeded teams don't need a
  -- migration step to get one; NULL until then.
  stats_feed_token TEXT,
  active INTEGER DEFAULT 1,
  sort_order INTEGER DEFAULT 0,
  -- Which season is "the" current one for this team's roster/schedule —
  -- what both the admin hub cards and the public site link to automatically.
  -- NULL means "not explicitly set yet"; every read falls back to the most
  -- recent season on file for that team (same as before this existed), so
  -- this is additive, not a required migration for existing data.
  current_roster_season TEXT,
  current_schedule_season TEXT,
  UNIQUE(school_id, slug)
);

-- The school-wide staff/coach directory (Staff Directory, this round, plus
-- the pre-existing Coaches-on-a-roster attachment via roster_coaches below).
-- first_name/last_name replaces the original single `name` column, to match
-- the Staff Bio form and Players' athletes table. `team_id` and
-- `is_head_coach` here predate roster_coaches's own per-team-season
-- is_head_coach flag and are no longer the source of truth for either
-- concept (a person's real team/season attachments — and who's Head Coach
-- of which — live on roster_coaches now, since one person can be attached
-- to more than one roster); both columns are kept only so the inline
-- quick-add on the Coaches & Staff tab has somewhere to stash its one-time
-- guess at a new hire's primary team, not read anywhere as authoritative.
CREATE TABLE staff_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  team_id INTEGER REFERENCES teams(id),    -- vestigial, see comment above — not authoritative
  first_name TEXT NOT NULL,
  last_name TEXT NOT NULL,
  title TEXT,
  is_head_coach INTEGER DEFAULT 0,         -- vestigial, see comment above — roster_coaches.is_head_coach
                                            -- (scoped per team+season) is what actually gets used
  photo_url TEXT,
  bio_text TEXT,
  email TEXT,
  phone TEXT,
  hidden_from_directory INTEGER DEFAULT 0, -- e.g. a GA or student assistant: still attachable to a
                                            -- roster (they coach a team), just left off the public
                                            -- Staff Directory page and its categories
  active INTEGER DEFAULT 1,                -- deactivating (not deleting — hard delete isn't offered
                                            -- anywhere in the UI) is how a departed staff member comes
                                            -- off /admin/staff's default list and the public directory
                                            -- without losing their record; re-activating brings them
                                            -- right back, roster history and all
  sort_order INTEGER DEFAULT 0
);

-- A staff category is purely a Staff Directory grouping/display concept —
-- entirely separate from a roster_coaches attachment ("the category they're
-- assigned is only for the staff directory"). Every team gets one
-- auto-managed category (team_id set, kept in sync with `teams` — see
-- _ensure_sport_staff_categories), always named after that team and never
-- renamable/deletable from here; anything else (Administration, Athletic
-- Trainers, Athletic Communications, seeded as starting points, or any
-- further custom one) has team_id NULL and is freely added/renamed/removed.
CREATE TABLE staff_categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  name TEXT NOT NULL,
  team_id INTEGER REFERENCES teams(id),
  sort_order INTEGER DEFAULT 0,
  UNIQUE(school_id, team_id)
);

-- Many-to-many: a staff member can sit in more than one category
-- ("sometimes multiple"). sort_order is that person's rank *within this one
-- category* — the "place to rank the order staff displays" — independent of
-- their position in any other category they're also in.
CREATE TABLE staff_category_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_id INTEGER NOT NULL REFERENCES staff_members(id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES staff_categories(id) ON DELETE CASCADE,
  sort_order INTEGER DEFAULT 0,
  UNIQUE(staff_id, category_id)
);

-- Same shape as athlete_social_links, one table reused for the same idea on
-- a different person type rather than adding staff-specific platform logic.
CREATE TABLE staff_social_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_id INTEGER NOT NULL REFERENCES staff_members(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  handle_or_url TEXT NOT NULL,
  sort_order INTEGER DEFAULT 0
);

-- "Ability to add custom field" on the Staff Bio — an open-ended
-- label+value repeater (delete-and-reinsert on save, same convention as
-- article_links) rather than a handful of fixed custom_field_1/2/3 slots
-- like roster_entries has, since staff aren't scoped to one team/season the
-- way a roster spot is.
CREATE TABLE staff_custom_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_id INTEGER NOT NULL REFERENCES staff_members(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  value TEXT,
  sort_order INTEGER DEFAULT 0
);

-- One row per school (school_id doubles as the primary key, same singleton
-- shape convention as anywhere else this app has exactly one settings row
-- per tenant). intro_text is free text shown at the top of the public Staff
-- Directory page; fields_config is the same {key, enabled, label} JSON shape
-- as roster_settings.fields_config, governing which of Name/Title/Email/
-- Phone/Social show on each public directory card and under what header.
CREATE TABLE staff_directory_settings (
  school_id INTEGER PRIMARY KEY REFERENCES schools(id),
  intro_text TEXT,
  fields_config TEXT,
  custom_field_labels TEXT,                -- JSON array of label strings (e.g. ["Alma Mater","Office
                                            -- Location"]) — custom fields are freeform per staff member,
                                            -- so "which ones show on the directory" is matched by label
                                            -- text at render time rather than a fixed per-school schema
  directory_template TEXT DEFAULT 'cards'  -- cards | table — cards is the photo-forward layout this
                                            -- shipped with; table is the plain text-only alternative
);

-- A manually-entered season/team record, added on top of whatever
-- _compute_coach_career_record() derives from this school's own
-- roster_coaches + games — for a season coached before joining this school
-- (team_name is then free text, not one of this school's teams), or to
-- hand-correct a season here where the Head Coach flag doesn't cleanly
-- cover who actually coached which games (a mid-season change, for
-- example). Always additive to the computed total, never a replacement for
-- it — the computed portion still reflects only this school's own games.
CREATE TABLE staff_manual_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_id INTEGER NOT NULL REFERENCES staff_members(id) ON DELETE CASCADE,
  season TEXT NOT NULL,
  team_name TEXT NOT NULL,
  wins INTEGER DEFAULT 0,
  losses INTEGER DEFAULT 0,
  ties INTEGER DEFAULT 0,
  note TEXT,
  sort_order INTEGER DEFAULT 0
);

CREATE TABLE roles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  name TEXT NOT NULL,                      -- "Admin" | "Manager" | "Staffer" ship as defaults
  rank INTEGER DEFAULT 0,
  is_default INTEGER DEFAULT 0,
  UNIQUE(school_id, name)
);

CREATE TABLE permissions (
  key TEXT PRIMARY KEY,
  label TEXT NOT NULL
);

CREATE TABLE role_permissions (
  role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  permission_key TEXT NOT NULL REFERENCES permissions(key),
  PRIMARY KEY (role_id, permission_key)
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  role_id INTEGER NOT NULL REFERENCES roles(id),
  status TEXT DEFAULT 'active',            -- invited | active | disabled
  staff_member_id INTEGER REFERENCES staff_members(id),
  show_byline_by_default INTEGER DEFAULT 1, -- whether a NEW story this user starts pre-fills the
                                             -- Byline field with their name. Only affects the initial
                                             -- prefill — the field is always a free-text override, and
                                             -- clearing it on save now always saves blank (see app.py's
                                             -- _parse_article_form) rather than silently re-defaulting.
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(school_id, email)
);

CREATE TABLE user_team_scopes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  scope_type TEXT DEFAULT 'specific_teams', -- specific_teams | all_teams
  team_id INTEGER REFERENCES teams(id)
);

CREATE TABLE opponents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  name TEXT NOT NULL,
  logo_url TEXT,
  website_url TEXT,
  city TEXT,
  state TEXT
  -- No global_school_id / master-schools linkage in this build yet — that's
  -- data-model.md §5a/§5b, a separate (larger) piece not built out here.
  -- This table plays the same role global_schools would eventually feed:
  -- the record a game's opponent picker loads name/website/location from.
);

CREATE TABLE event_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  team_id INTEGER NOT NULL REFERENCES teams(id),
  season TEXT NOT NULL,
  name TEXT NOT NULL,              -- "Empire Conference Tournament", "Ridgeview Invitational"
  start_date TEXT,
  end_date TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE galleries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  team_id INTEGER REFERENCES teams(id),
  title TEXT NOT NULL
);

CREATE TABLE media_assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gallery_id INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  type TEXT NOT NULL,                      -- image | video_embed
  url TEXT NOT NULL,
  caption TEXT
);

CREATE TABLE games (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  season TEXT NOT NULL,
  date TEXT NOT NULL,
  time TEXT,
  -- specific = `time` above is a real clock time; all_day / tba both mean
  -- "no clock time" but for different reasons, and `time` is cleared
  -- whenever either is chosen. Every game has to land on one of the three
  -- at save time — the admin form doesn't allow a silent fourth "just
  -- blank" state (see admin_game_form.html's time-type toggle).
  time_type TEXT DEFAULT 'specific',       -- specific | all_day | tba
  time_zone TEXT DEFAULT 'America/Chicago', -- IANA tz id the `time` above is local to; defaults from schools.default_timezone, always editable
  opponent_id INTEGER NOT NULL REFERENCES opponents(id),   -- "Team A" when is_neutral_host
  opponent_two_id INTEGER REFERENCES opponents(id),        -- "Team B", only set when is_neutral_host
  opponent_display_name TEXT,              -- overrides displayed opponent name for THIS game only; null = use opponents.name
  opponent_display_website_url TEXT,       -- same, for the clickable website link
  is_neutral_host INTEGER DEFAULT 0,       -- true = two outside teams, client isn't a participant
  game_type TEXT NOT NULL DEFAULT 'non_conference', -- scrimmage | exhibition | non_conference | conference | postseason
  result_type TEXT DEFAULT 'standard',     -- standard | no_team_score | no_result
  finish_place INTEGER,                    -- used when the team's results_format = place_finish
  total_teams INTEGER,
  home_away TEXT NOT NULL,                 -- home | away | neutral
  city TEXT,
  state TEXT,                              -- state, or a country name for a game outside the US
  facility TEXT,                           -- specific venue: stadium/field/court/arena/course/pool
  event_group_id INTEGER REFERENCES event_groups(id),
  status TEXT DEFAULT 'upcoming',          -- upcoming | live | final | postponed | cancelled
  -- Now wired: a stats-feed import writes the feed's own reported score
  -- here, always. score_for/score_against below are the "official"
  -- displayed score — copied from the feed automatically UNLESS a human
  -- has already saved a manual score (score_finalized_by='manual_entry'),
  -- in which case a later feed import updates feed_score_for/against but
  -- never clobbers the human-entered official score.
  feed_score_for INTEGER,
  feed_score_against INTEGER,
  score_for INTEGER,
  score_against INTEGER,
  score_finalized_by TEXT,                 -- live_feed_auto | manual_override | manual_entry
  stats_feed_updated_at TEXT,               -- when the last stats-feed import landed, stored/shown exactly as
                                             -- the feed reported it (PrestoSports sends a bare date like
                                             -- "10/20/2025", not a full ISO datetime); null = never
  -- A final push received over the webhook while this team's Live Scoring
  -- is 'manual_confirm' lands here instead of committing straight to
  -- game_team_stats/game_plays/game_player_stats — the score itself still
  -- updates immediately (see _apply_feed_score_update), but the full box
  -- score waits for an admin to review and Confirm & Publish, same review
  -- step a manually-pasted feed always got. The Import Stats page reads
  -- this on load and pre-fills its textarea from it when present, so
  -- reviewing a webhook-received feed and reviewing a pasted one are the
  -- same screen. Cleared (both columns back to null) once confirmed.
  pending_feed_xml TEXT,
  pending_feed_received_at TEXT,
  box_score_url TEXT,
  linked_gallery_id INTEGER REFERENCES galleries(id),
  tickets_url TEXT,
  livestream_url TEXT
);

-- Team-level box score stats for one game, one row per (game, side, stat).
-- 'side' is 'for'/'against', the same client-centric convention as
-- games.score_for/score_against (this is a client site, not a neutral
-- scoreboard) — so "for" is always the client school's own team's stat,
-- regardless of which opponent it was against. Deliberately EAV-shaped
-- (a stat_key/stat_value pair, not one column per stat) rather than a wide
-- fixed-column table, so a new sport's whole different set of categories
-- (football's passing/rushing/etc. vs. a future sport's own categories)
-- never needs a schema change — only a new entry in app.py's
-- TEAM_STAT_CATEGORIES registry, which supplies the header label and
-- display order for whatever keys a given sport's feed happens to send.
-- stat_value is stored as the feed's own display-ready text (e.g. "6-14"
-- for third-down conversions, "31:12" for time of possession) rather than
-- a number Claude has to reformat — the feed is assumed to already format
-- it the way it should read on the page.
CREATE TABLE game_team_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  side TEXT NOT NULL,                      -- for | against
  stat_key TEXT NOT NULL,
  stat_value TEXT,
  sort_order INTEGER DEFAULT 0,
  UNIQUE(game_id, side, stat_key)
);

-- Play-by-play — the fan-facing "watching along" feed (StatMonitr-style),
-- one row per logged play, ordered by `sequence` (the feed's own numbering,
-- not an assumption about timestamps — a feed can safely re-send the same
-- game and this upserts by (game_id, sequence) rather than duplicating).
-- `period`/`clock` are stored as feed-provided display text (e.g. "Q1",
-- "12:45"), same "store it exactly as sent" rule as game_team_stats — this
-- prototype doesn't try to normalize every sport's own idea of a period.
-- `team_side` is nullable because not every play belongs to one side (a
-- timeout, an official review) — for | against | null.
CREATE TABLE game_plays (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL,
  period TEXT,
  clock TEXT,
  team_side TEXT,                          -- for | against | null
  description TEXT NOT NULL,
  score_for INTEGER,                       -- running score snapshot at this play, if the feed sent one
  score_against INTEGER,
  is_scoring_play INTEGER DEFAULT 0,
  -- Field position for the Live Game Center's drive chart (see
  -- _load_game_center_data/_current_drive in app.py), both optional/
  -- nullable — a feed that doesn't report field position still imports
  -- fine, the drive chart just doesn't render for that game.
  -- yard_line is always in the "for" team's own frame regardless of who
  -- has the ball (0 = for's own goal line, 100 = opponent's goal line),
  -- same for/against-relative convention as everything else in this
  -- schema — this is what lets the ball marker plot on a single 0-100
  -- track without caring which side is driving which direction.
  yard_line INTEGER,
  -- Feed-provided, stored/shown exactly as sent (e.g. "2nd & 7") — same
  -- "display exactly as the feed formats it" convention as game_team_stats'
  -- third_down/time_of_possession values, not decomposed into down+distance
  -- columns since there's nothing this app needs to compute from the parts.
  down_distance TEXT,
  UNIQUE(game_id, sequence)
);

-- Defined here, ahead of game_player_stats below, rather than down by its
-- own athlete_social_links/roster_entries block — game_player_stats.
-- athlete_id references athletes(id), and a REFERENCES target has to
-- already exist by the time a stricter database parses the table that
-- points at it (SQLite is lenient about forward references within one
-- executescript() batch; Postgres, which this same schema.sql also feeds
-- for a real hosted deploy, is not).
CREATE TABLE athletes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  first_name TEXT NOT NULL,
  last_name TEXT NOT NULL,
  hometown TEXT,
  high_school TEXT,
  previous_school TEXT,
  headshot_url TEXT,
  bio_text TEXT,                           -- "Master Bio" — general facts, shared everywhere this
                                            -- person appears (see roster_entries.bio_text for the
                                            -- separate per-sport/season version)
  -- No status field here on purpose (direct feedback): once a player is
  -- created they stay in the system forever, full stop. Whether they're
  -- "active" is entirely a function of which roster(s) they're currently
  -- assigned to (roster_entries) — there's nothing else to track at the
  -- person level, so nothing to make required or keep in sync.
  nil_shop_url TEXT,                       -- link to this player's own NIL/personal shop, if any
  academic_major TEXT,
  nickname TEXT,
  pronunciation TEXT,                      -- name pronunciation guide, e.g. "cass-TEE-oh"
  birthdate TEXT                           -- free-text date; no calendar-math depends on this in Phase 1
);

-- Individual player box score lines — deliberately EAV-shaped like
-- game_team_stats (category + stat_key + stat_value), so a new sport's
-- entirely different stat groups (football's passing/rushing/receiving
-- vs. a future sport's own categories) never needs a schema change, only
-- a new entry in app.py's PLAYER_STAT_CATEGORIES registry. player_name/
-- jersey_number are stored directly from the feed — they're what's always
-- available, whether or not the player can be matched to a roster row —
-- and athlete_id is filled in only when a same-team (side='for') player
-- could be matched to this school's own athletes by name/jersey; an
-- opponent's players (side='against') are never in our athletes table at
-- all, so athlete_id stays null for them by design, not as a fallback.
CREATE TABLE game_player_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  side TEXT NOT NULL,                      -- for | against
  player_name TEXT NOT NULL,
  jersey_number TEXT,
  athlete_id INTEGER REFERENCES athletes(id),
  category TEXT NOT NULL,                  -- e.g. passing | rushing | receiving
  stat_key TEXT NOT NULL,
  stat_value TEXT,
  sort_order INTEGER DEFAULT 0,
  UNIQUE(game_id, side, player_name, category, stat_key)
);

CREATE TABLE athlete_social_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  athlete_id INTEGER NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,                  -- x | instagram | tiktok | facebook | youtube | threads | snapchat | other
  handle_or_url TEXT NOT NULL,
  sort_order INTEGER DEFAULT 0
);

CREATE TABLE roster_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  athlete_id INTEGER NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  season TEXT NOT NULL,
  jersey_number TEXT,
  position TEXT,
  class_year TEXT,                         -- Fr | So | Jr | Sr | Grad
  height_ft INTEGER,
  height_in INTEGER,
  weight TEXT,
  bats_throws TEXT,                        -- sport-specific (e.g. baseball/softball); left blank when N/A
  bio_text TEXT,                           -- this sport/season's bio — separate from athletes.bio_text
                                            -- (the shared "master" bio), so a multi-sport athlete can have
                                            -- a different write-up per sport instead of one bio for everything
  custom_field_1 TEXT,                     -- free-text, per-school use (labels set in roster_settings)
  custom_field_2 TEXT,
  custom_field_3 TEXT,
  hidden INTEGER DEFAULT 0,                -- hides this one player from THIS team's public roster
                                            -- without removing the assignment (injured, suspended,
                                            -- redshirt, etc.) — deliberately NOT a status enum. Per
                                            -- direct feedback: a player graduating/transferring is
                                            -- represented purely by not being carried into the next
                                            -- season's roster_entries at Rollover, nothing else needed.
  sort_order INTEGER DEFAULT 0
);

-- Per-team-per-season roster display settings — kept off `teams` because
-- most of these (title, team photo, hidden/publish) are genuinely
-- season-specific, not team-wide defaults.
CREATE TABLE roster_settings (
  team_id INTEGER NOT NULL REFERENCES teams(id),
  season TEXT NOT NULL,
  custom_title TEXT,                       -- overrides the computed default, e.g. "2025-26 Northfield
                                            -- Men's Basketball Roster" ({season} {school} {team} Roster)
  team_photo_url TEXT,
  show_coaches INTEGER DEFAULT 0,          -- Yes/No: display the Coaching Staff section below the roster
  sort_mode TEXT DEFAULT 'jersey',         -- jersey (jersey #, then last name — the default) | alpha
  fields_config TEXT,                      -- JSON list of {"key","enabled","label"} in display order —
                                            -- which roster columns show (Jersey #/Name included, not
                                            -- pinned), in what order (drag-and-drop on its own page,
                                            -- see /admin/rosters/fields), and each field's header text
                                            -- (label: null means "use the app's default for this key")
  hidden INTEGER DEFAULT 0,                -- hide this season's roster from the public site entirely —
                                            -- a future roster can exist and be edited without going live
  publish_at TEXT,                         -- optional scheduled go-live datetime; once it passes, hidden
                                            -- is lazily flipped back to 0 on the next relevant request
                                            -- (no real background scheduler in this prototype)
  publish_makes_current INTEGER DEFAULT 0, -- if set, going live also promotes this season to Current
  publish_applied INTEGER DEFAULT 0,       -- internal: has the scheduled publish already fired once
  PRIMARY KEY (team_id, season)
);

-- Mirrors roster_settings' hidden/publish mechanics for schedules.
CREATE TABLE schedule_settings (
  team_id INTEGER NOT NULL REFERENCES teams(id),
  season TEXT NOT NULL,
  hidden INTEGER DEFAULT 0,
  publish_at TEXT,
  publish_makes_current INTEGER DEFAULT 0,
  publish_applied INTEGER DEFAULT 0,
  PRIMARY KEY (team_id, season)
);

-- Coaches attached to one team's specific-season roster — mirrors the
-- players model (a shared staff_members directory, with per-roster
-- assignments) rather than a fixed one-coach-per-team relationship, so a
-- coaching change between seasons doesn't rewrite history.
CREATE TABLE roster_coaches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  season TEXT NOT NULL,
  staff_id INTEGER NOT NULL REFERENCES staff_members(id) ON DELETE CASCADE,
  is_head_coach INTEGER DEFAULT 0,         -- exactly one expected per team/season, for coach-record
                                            -- tracking later — not DB-enforced in this prototype
  hidden INTEGER DEFAULT 0,                -- e.g. let go mid-season — stays on record, hidden publicly
  sort_order INTEGER DEFAULT 0
);

CREATE TABLE sponsors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  name TEXT NOT NULL,
  logo_url TEXT,
  link_url TEXT,
  tier TEXT,
  sort_order INTEGER DEFAULT 0
);

CREATE TABLE articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  team_id INTEGER REFERENCES teams(id),
  linked_game_id INTEGER REFERENCES games(id),
  linked_gallery_id INTEGER REFERENCES galleries(id),
  category TEXT DEFAULT 'general_article', -- game_preview | game_recap | general_article | feature_story
                                            -- ("Classification" on the Article Editor)
  season TEXT,
  title TEXT NOT NULL,
  subheadline TEXT,                        -- optional secondary title, shown under the headline
  slug TEXT NOT NULL,                      -- "Filename" on the Article Editor — auto-suggested from the
                                            -- title (or an explicit Filename override), editable; this is
                                            -- what appears after /news/ once live
  body TEXT NOT NULL,                      -- rich-text HTML from the Article Editor's WYSIWYG toolbar
                                            -- (contenteditable + document.execCommand — no external editor
                                            -- library, consistent with this prototype's vanilla-JS approach)
  featured_image_url TEXT,                 -- a URL for now; "loaded from the system" (a real upload/picker
                                            -- against the deferred Media & File Library, data-model.md §13)
                                            -- is future work, same as every other image field in this app
  author_name TEXT,                        -- free text, defaults to CURRENT_USER but editable — no real
                                            -- users FK yet, same reasoning as everywhere else auth is stubbed
  status TEXT DEFAULT 'draft',             -- draft | pending_review | published — the editorial/approval
                                            -- state, gated by the publish_news permission
  -- Release scheduling — same hidden + scheduled-timestamp + one-shot-applied
  -- pattern already used by roster_settings/schedule_settings (see
  -- _apply_due_publishes): published_at doubles as the release gate here,
  -- not just an audit timestamp. Set it in the future and the story stays
  -- hidden=1 until that moment, then the before_request hook flips it live
  -- on its own; leave it blank/past and Hidden is a plain manual hold you
  -- clear yourself whenever you're ready. Either way, status still has to
  -- be 'published' first — hidden/published_at only ever govern an
  -- *approved* story's on-site timing, never a draft's.
  published_at TEXT,
  hidden INTEGER DEFAULT 0,
  publish_applied INTEGER DEFAULT 0,
  -- Editorial placement, independent of publish timing. Direct feedback on
  -- the first pass of this ("Story rank... with an option to keep off")
  -- changed the default behavior: a rotator (Home page or a team's Sport
  -- page) now shows every eligible published story ordered by release date
  -- by default, and *_rank only overrides that default to pin a story to a
  -- specific numbered slot — it's no longer "blank = not shown." *_excluded
  -- is the actual "keep off this page" switch, independent of rank.
  -- *_locked protects a pinned story from the News Dashboard's "Rank by
  -- Release Date" bulk action, which otherwise clears every *unlocked*
  -- story's rank back to date-ordering in one click. The News Dashboard's
  -- Manage Rankings screen (drag-and-drop) and the Article Editor's
  -- Home/Sport Page Placement dropdown both just read/write these same four
  -- columns — whichever saved last wins, by design ("the Dashboard can
  -- override the Editor").
  home_rank INTEGER,                       -- explicit position in the Home page's News rotator; NULL = sort
                                            -- by release date instead
  home_excluded INTEGER DEFAULT 0,         -- 1 = never show on the Home page, regardless of rank/date
  home_locked INTEGER DEFAULT 0,           -- 1 = "Rank by Release Date" on the Dashboard leaves this story's
                                            -- home_rank alone
  sport_rank INTEGER,                      -- same as home_rank, for this story's team's Sport page
  sport_excluded INTEGER DEFAULT 0,
  sport_locked INTEGER DEFAULT 0,
  template_type TEXT DEFAULT 'standard',   -- which public template renders this story — only 'standard'
                                            -- actually exists so far; the field is here so more can be
                                            -- added later without a schema change
  UNIQUE(school_id, slug)
);

-- A story's "Related Links" — title + URL pairs an admin adds by hand
-- (pages/documents/galleries elsewhere on the site aren't pickable yet,
-- just pasted as plain URLs — see the Article Editor's footnote there).
CREATE TABLE article_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  sort_order INTEGER DEFAULT 0
);

CREATE TABLE pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id INTEGER NOT NULL REFERENCES schools(id),
  title TEXT NOT NULL,
  slug TEXT NOT NULL,                      -- '' = home page
  status TEXT DEFAULT 'draft',             -- draft | published
  nav_placement TEXT,
  season TEXT,
  seo_title TEXT,
  seo_description TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(school_id, slug)
);

CREATE TABLE page_blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  block_type TEXT NOT NULL,                -- hero | roster_grid | schedule | news_feed | text_image | gallery | sponsor_strip | video_embed | custom_embed
  position INTEGER NOT NULL,
  content TEXT NOT NULL DEFAULT '{}'       -- JSON, stored as text (SQLite has no native JSON type)
);
