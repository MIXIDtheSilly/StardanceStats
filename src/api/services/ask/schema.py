from __future__ import annotations

# Refused whatever the allowlist below says, and whatever anyone adds to it later.
FORBIDDEN: frozenset[str] = frozenset(
    {"ask_log", "crawl_log", "crawl_frontier", "crawl_state"}
)

COLLECTIONS: frozenset[str] = frozenset(
    {
        "users",
        "projects",
        "devlogs",
        "comments",
        "ships",
        "shop_items",
        "missions",
        "global_snapshots",
        "user_snapshots",
        "project_snapshots",
        "devlog_snapshots",
    }
) - FORBIDDEN

# A $match on one of these is read as a date even when written as a string.
DATE_FIELDS: frozenset[str] = frozenset(
    {
        "comments_crawled_at",
        "created_at_estimate",
        "enabled_until",
        "first_comment_at",
        "first_seen",
        "joined_at",
        "last_changed",
        "last_comment_at",
        "last_crawled",
        "posted_at",
        "project_ids_seen_at",
        "shipped_at",
        "snapshot_at",
        "super_star_at",
        "ts",
    }
)

SCHEMA = """\
Database: stardance_stats, the one Stardance Stats keeps. Stardance is a Hack
Club programme where teenagers build projects, post devlogs about them, ship
them, and earn stardust to spend in a shop.

This is not Stardance's own database. A crawler reads Stardance's public pages
and writes what it sees here, so every figure is the public view as of the last
time that page was read: rejected ships, deleted devlogs and unverified
profiles never appear, and a row is only as current as its last_crawled. Counts
are of what we hold, not of what the platform has.

users (~50k docs, _id = int user id)
  username (str), username_lower (str, lowercased handle used for joins)
  slack_id, bio, avatar_url, banner_url, hidden (bool)
  joined_at, first_seen, last_changed, last_crawled (dates)
  project_ids (list of int)
  stats.followers, stats.following, stats.devlogs, stats.projects, stats.ships,
    stats.votes, stats.streak, stats.achievements_earned, stats.achievements_total (int)
  totals.ship_stardust, totals.voted_stardust, totals.mission_stardust (int)
  totals.hours, totals.shipped_hours, totals.paid_hours, totals.unpaid_hours,
    totals.ratable_unpaid_hours, totals.mission_pending_hours (float)
  totals.likes_received, totals.comments_received, totals.reposts_received,
    totals.views_received (int)
  totals.comments_sent, totals.comments_to_others, totals.comment_threads,
    totals.projects_commented, totals.comment_chars (int)
  totals.avg_comment_length, totals.comments_sent_per_received (float)
  totals.best_multiplier, totals.avg_multiplier, totals.stardust_per_paid_hour,
    totals.estimated_total_stardust, totals.estimated_pending_stardust
    (float, often null: unrated work has no multiplier yet)
  coverage.complete (bool, false when we have not seen all of their projects)

projects (~41k docs, _id = int project id)
  title, description, repo_url, demo_url, banner_url (str)
  owner_id (int), owner_username (str), members (list of str), member_ids (list of int)
  is_hardware, is_super_star (bool), super_star_at (date), super_star_note (str)
  created_at_estimate, first_seen, last_changed, last_crawled (dates)
  mission.slug, mission.name (str), mission.shipped (bool); mission is null when
    the project belongs to no mission
  stats.devlogs, stats.ships, stats.likes, stats.comments, stats.views,
    stats.reposts, stats.followers (int)
  stats.total_hours, stats.summed_hours, stats.shipped_hours, stats.paid_hours,
    stats.unpaid_hours, stats.ratable_unpaid_hours, stats.mission_pending_hours,
    stats.fixed_payout_hours, stats.flat_rate_hours (float)
  stats.stardust_total, stats.voted_stardust, stats.mission_stardust,
    stats.mission_pending_stardust (int)
  stats.latest_multiplier, stats.avg_multiplier, stats.stardust_per_paid_hour,
    stats.estimated_total_stardust (float, often null)

devlogs (~36k docs, _id = int devlog id)
  project_id (int), user_id (int), username (str), username_lower (str)
  posted_at (date), duration_seconds (int, time logged on this devlog)
  body (str, the whole post), body_preview (str, older rows only)
  likes, comments, views, reposts (int)
  media (list of {kind: "image"|"video", url}), last_crawled (date)
  A full-text index covers body and body_preview, so $match with $text works.

comments (~7k docs, _id = int comment id)
  devlog_id, project_id, user_id (int), username, username_lower (str)
  body (str), body_length (int), posted_at (date), position (int)
  is_self (bool, the comment is by the devlog's own author), mentions (list of str)

ships (~4.6k docs, _id = int post id)
  project_id, user_id (int), username, username_lower (str)
  ship_number (int, 1 is the first ship of that project), shipped_at (date)
  body (str), status (str), hours_at_ship (float), devlogs_at_ship (int)
  mission (str), mission_slug (str), payout_path (str)
  multiplier, payout, payout_hours, mission_stardust (float, null until rated)

shop_items (~110 docs, _id = int item id)
  name, description, url, image_url (str)
  categories (list of str), regions (list of str), regions_available (int)
  prices.US / .EU / .UK / .IN / .CA / .AU / .XX (int stardust, region priced)
  full_prices.<region> (int, the price before a sale)
  price_min, price_max, price_spread (int), on_sale (bool), sale_percentage
  hours_low, hours_high (int, the hours the shop claims an item is worth)
  purchases (int), out_of_stock, is_new, mission_locked, achievement_locked (bool)

missions (9 docs, _id = str slug)
  slug, name, description, difficulty, payout_path (str)
  estimated_minutes (int), fixed_stardust (int), stardust_per_hour (float or null)
  available, rated, is_hardware (bool), criteria (list of str)
  prizes (list of {title, blurb}), gallery (list of {project_id, approved})

global_snapshots (~260 docs, one row per platform-wide crawl)
  ts (date), scope ("platform"), users, users_known, projects, projects_known,
  devlogs, ships, comments, likes, views, reposts, followers (int),
  hours, shipped_hours, paid_hours (float), stardust_paid (int)

user_snapshots (~134k docs, time series of one user)
  ts (date), uid (int, the user _id), then the same counters as users.stats and
  users.totals but unprefixed: followers, following, devlogs, projects, ships,
  hours, shipped_hours, paid_hours, ship_stardust, likes_received,
  comments_received, views_received, streak, votes

project_snapshots (~600k docs, time series of one project)
  ts (date), pid (int, the project _id), devlogs, ships, stardust_total (int),
  total_hours (float), synthetic (bool, a carried-forward heartbeat row)

devlog_snapshots (~212k docs, time series of one devlog)
  ts (date), did (int, the devlog _id), likes, comments, views, reposts,
  duration_seconds (int)

Joins and gotchas
  - users._id == devlogs.user_id == comments.user_id == ships.user_id == projects.owner_id
  - projects._id == devlogs.project_id == comments.project_id == ships.project_id
  - Match a handle case-insensitively on username_lower, never on username.
  - A missing number is null, not 0. Add a null guard when you rank on one, for
    example {"stats.likes": {"$ne": null}}.
  - Money is "stardust". Time is hours except on devlogs, which store seconds.
  - Only shipped work gets paid, so paid_hours <= shipped_hours <= hours.
  - Snapshot collections are for change over time. For "who has the most X"
    read users or projects, which already hold the latest figure.
  - last_crawled is when we last read that page, not when anything happened on
    it. first_seen is when the crawler first met the row, not when the thing was
    made; joined_at, posted_at and shipped_at are the platform's own dates.
  - A user whose coverage.complete is false has projects we have not read yet,
    so their totals are a floor rather than the whole story.
"""
