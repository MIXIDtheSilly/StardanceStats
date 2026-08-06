from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.parsers import ParseError, parse_user_page

FIXTURES = Path(__file__).parent / "fixtures"
USER = FIXTURES / "user_32.html"
PROJECTS_TAB = FIXTURES / "user_the_craw_projects.html"
FEED_TAB = FIXTURES / "user_the_craw.html"
PLACEHOLDER = FIXTURES / "user_unverified_placeholder.html"


@pytest.fixture(scope="module")
def user():
    return parse_user_page(USER.read_text(encoding="utf-8"), 32).data["user"]


@pytest.fixture(scope="module")
def parsed_user():
    return parse_user_page(USER.read_text(encoding="utf-8"), 32)


def test_identity(user):
    assert user["_id"] == 32
    assert user["username"] == "Fantamomo"
    assert user["hidden"] is False
    assert user["joined_at"] == datetime(2026, 5, 31)
    assert "developer who loves to code" in user["bio"]


def test_avatar_and_slack_id(user):
    assert user["avatar_url"] == "https://cachet.dunkirk.sh/users/U0905G0BRU5/r"
    assert user["slack_id"] == "U0905G0BRU5"
    assert user["avatar_kind"] == "slack"


def test_stats_are_read_by_label(user):
    assert user["devlogs_count"] == 10
    assert user["projects_count"] == 8
    assert user["ships_count"] == 0
    assert user["votes_count"] == 0


def test_follow_counts_are_not_swapped(user):
    """Both pills share one class, so wording is what separates them."""
    assert user["followers"] == 5
    assert user["following"] == 0


def test_streak_is_read_from_the_badge():
    user = parse_user_page(PROJECTS_TAB.read_text(encoding="utf-8"), 11155).data["user"]
    assert user["streak"] == 61


def test_no_badge_means_a_broken_streak_not_a_broken_parser():
    """The badge renders only while a streak is alive. Zero, not missing."""
    html = '<html><body><p class="profile__handle">@x</p></body></html>'
    parsed = parse_user_page(html, 1)

    assert parsed.data["user"]["streak"] == 0
    assert "streak" not in parsed.missing


def test_a_badge_we_cannot_read_is_a_failure():
    html = (
        '<html><body><p class="profile__handle">@x</p>'
        '<div class="profile__stats"></div>'
        '<span class="streak-badge" aria-label="on fire lately"></span></body></html>'
    )
    parsed = parse_user_page(html, 1)

    assert parsed.data["user"]["streak"] is None
    assert "streak" in parsed.missing


def test_achievements(user):
    assert user["achievements_earned"] == 0
    assert user["achievements_total"] == 11


def test_nothing_went_unparsed(parsed_user):
    assert parsed_user.missing == set()
    assert parsed_user.warnings == []


def test_projects_tab_yields_every_project_id():
    """The list is the only way to reach projects a profile does not link to."""
    user = parse_user_page(PROJECTS_TAB.read_text(encoding="utf-8"), 11155).data["user"]

    assert user["project_ids"] == [8100, 18181, 19167, 19371, 41154]
    assert len(user["project_ids"]) == user["projects_count"] == 5


def test_unverified_placeholder_is_read_as_a_hidden_profile():
    """Banned and unverified users get a placeholder page that still names them."""
    user = parse_user_page(PLACEHOLDER.read_text(encoding="utf-8"), 4242).data["user"]

    assert user["username"] == "quiet_comet"
    assert user["hidden"] is True
    assert user["slack_id"] == "U0912ABCDEF"
    # None, not [], so ingest leaves a previously known list alone.
    assert user["project_ids"] is None


def test_a_page_title_is_never_mistaken_for_a_handle():
    """Such pages share one title, so all would claim the same bogus username."""
    html = (
        '<html><head><meta property="og:title" content="Verifying… - Stardance">'
        '</head><body></body></html>'
    )
    with pytest.raises(ParseError):
        parse_user_page(html, 4242)


def test_a_real_profile_og_title_still_yields_the_handle():
    """The fallback that matters: a profile page whose header markup changed."""
    html = (
        '<html><head><meta property="og:title" content="@The_Craw | Stardance">'
        '</head><body></body></html>'
    )
    assert parse_user_page(html, 11155).data["user"]["username"] == "The_Craw"


def test_projects_tab_still_carries_the_profile_header():
    """It replaces /users/:id in the crawl, so it must parse identically."""
    user = parse_user_page(PROJECTS_TAB.read_text(encoding="utf-8"), 11155).data["user"]

    assert user["username"] == "The_Craw"
    assert user["followers"] is not None
    assert user["devlogs_count"] and user["ships_count"]
    assert user["joined_at"] and user["bio"] and user["slack_id"]


def test_other_tabs_report_unknown_rather_than_empty():
    """An empty list on the feed tab would read as 'owns no projects'."""
    user = parse_user_page(FEED_TAB.read_text(encoding="utf-8"), 11155).data["user"]

    assert user["project_ids"] is None
    assert user["projects_count"] == 5


def test_page_without_a_handle_raises():
    with pytest.raises(ParseError):
        parse_user_page("<html><body><h1>500</h1></body></html>", 1)


def test_handle_falls_back_to_og_title():
    html = '<html><head><meta property="og:title" content="@Someone | Stardance"></head><body></body></html>'
    parsed = parse_user_page(html, 7)
    assert parsed.data["user"]["username"] == "Someone"
    assert parsed.data["user"]["hidden"] is True


def test_ordinal_dates_parse():
    for day, expected in (("1st", 1), ("2nd", 2), ("3rd", 3), ("11th", 11)):
        html = (
            '<html><body><p class="profile__handle">@x</p>'
            f'<p class="profile__joined">Joined June {day}, 2026</p></body></html>'
        )
        assert parse_user_page(html, 1).data["user"]["joined_at"].day == expected


