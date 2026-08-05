"""Super Star is `projects.marked_fire_at` upstream, surfaced two ways: a
header badge and a Post::FireEvent card in the timeline. The badge is
presence-only, so a renamed class would silently read as "not a Super Star"
rather than as a break. Reading both sources keeps that honest."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.parsers import parse_project_page

FIXTURE = Path(__file__).parent / "fixtures" / "project_8100.html"
UTC = timezone.utc

BADGE = '<span class="project-show__badge project-show__badge--fire">⭐ Super Star Project</span>'
EVENT_CARD = (
    '<article class="feed-post-card feed-post-card--fire"'
    ' data-feed-engagement-post-type-value="Post::FireEvent"'
    ' data-feed-engagement-post-id-value="9900">'
    '<div class="feed-post-card__super-star">'
    '<span class="feed-post-card__super-star-title">Super Star</span></div>'
    '<a class="feed-post-card__author" href="/@cskartikey">@cskartikey</a>'
    '<time class="feed-post-card__time" datetime="2026-06-16T12:22:56Z">June 16</time>'
    '<div class="feed-post-card__super-star-body">Great work</div>'
    "</article>"
)


def page(*parts: str) -> str:
    return (
        '<html><body><div class="project-show__panel project-show__panel--read">'
        '<h1 class="project-show__title">T</h1>' + "".join(parts) + "</div></body></html>"
    )


@pytest.fixture(scope="module")
def real():
    return parse_project_page(FIXTURE.read_text(encoding="utf-8"), 8100).data["project"]


def test_the_reference_project_is_a_super_star(real):
    assert real["is_super_star"] is True


def test_the_event_card_supplies_the_award_details(real):
    """The badge says only that it happened; the card says when and by whom."""
    assert real["super_star_at"] == datetime(2026, 6, 16, 12, 22, 56, tzinfo=UTC)
    assert real["super_star_by"] == "cskartikey"
    assert "bonus prize" in real["super_star_note"]


def test_an_ordinary_project_is_not_one():
    parsed = parse_project_page(page(), 1)
    project = parsed.data["project"]

    assert project["is_super_star"] is False
    assert project["super_star_at"] is None
    assert parsed.warnings == []


def test_the_badge_alone_is_enough():
    """Older markings scroll off a long timeline; the badge still stands."""
    project = parse_project_page(page(BADGE), 1).data["project"]

    assert project["is_super_star"] is True
    assert project["super_star_at"] is None


def test_the_event_card_alone_is_enough_but_says_so():
    """Either source going quiet is survivable, and worth a warning."""
    parsed = parse_project_page(page(EVENT_CARD), 1)

    assert parsed.data["project"]["is_super_star"] is True
    assert parsed.data["project"]["super_star_by"] == "cskartikey"
    assert any("badge" in w for w in parsed.warnings)


def test_the_badge_is_matched_on_wording_too():
    """So a dropped --fire modifier does not silently unmark every project."""
    restyled = '<span class="project-show__badge">⭐ Super Star Project</span>'
    assert parse_project_page(page(restyled), 1).data["project"]["is_super_star"] is True


def test_a_super_star_card_is_not_counted_as_a_devlog():
    parsed = parse_project_page(page(BADGE, EVENT_CARD), 1)

    assert parsed.data["devlogs"] == []
    assert parsed.data["ships"] == []
    assert parsed.warnings == []


def test_the_latest_marking_wins():
    later = EVENT_CARD.replace("2026-06-16T12:22:56Z", "2026-07-20T09:00:00Z").replace(
        "cskartikey", "someone_else"
    )
    project = parse_project_page(page(BADGE, EVENT_CARD, later), 1).data["project"]

    assert project["super_star_at"] == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    assert project["super_star_by"] == "someone_else"
