"""A ship pays out only after its review window closes. Until then the page
renders no payout or multiplier row, so the parser must treat that as data,
not a broken selector, or every in-review ship trips the anomaly guard."""

from __future__ import annotations

from selectolax.parser import HTMLParser

from src.parsers.common import ParseResult
from src.parsers.project import _parse_ship_card

DEVLOGS_ROW = (
    '<li class="profile-project-card__meta-item"><span>13 devlogs</span></li>'
)
HOURS_ROW = (
    '<li class="profile-project-card__meta-item profile-project-card__meta-item--time">'
    "<span>32h</span></li>"
)
MULTIPLIER_ROW = (
    '<li class="profile-project-card__meta-item profile-project-card__meta-item--multiplier"'
    ' title="Quality multiplier"><span>19.65x multiplier</span></li>'
)
PAYOUT_ROW = (
    '<li class="profile-project-card__meta-item profile-project-card__meta-item--payout"'
    ' title="Stardust payout"><span>633 Stardust</span></li>'
)


def card(*rows: str):
    html = (
        '<article class="feed-post-card project-show__latest-ship"'
        ' data-feed-engagement-post-id-value="20937">'
        '<div class="feed-post-card__reposted-by project-show__latest-ship-label">'
        "<span>Ship #2</span></div>"
        '<ul class="project-show__latest-ship-stats">' + "".join(rows) + "</ul>"
        "</article>"
    )
    return HTMLParser(html).css_first("article")


def parse(*rows: str):
    result = ParseResult()
    ship = _parse_ship_card(card(*rows), 8100, result)
    return ship, result


def test_a_paid_ship_reports_every_field_found():
    ship, result = parse(DEVLOGS_ROW, HOURS_ROW, MULTIPLIER_ROW, PAYOUT_ROW)

    assert ship["payout"] == 633
    assert ship["multiplier"] == 19.65
    assert result.missing == set()


def test_an_unpaid_ship_is_absent_data_not_a_parse_failure():
    ship, result = parse(DEVLOGS_ROW, HOURS_ROW)

    assert ship["payout"] is None
    assert ship["multiplier"] is None
    assert ship["hours_at_ship"] == 32.0
    assert result.missing == set(), "an unpaid ship must not trip the anomaly guard"


def test_a_payout_row_that_will_not_parse_is_still_a_failure():
    """The distinction only works if a present-but-unreadable row still counts."""
    broken = (
        '<li class="profile-project-card__meta-item profile-project-card__meta-item--payout"'
        ' title="Stardust payout"><span>a lot of Stardust</span></li>'
    )
    _, result = parse(DEVLOGS_ROW, HOURS_ROW, broken)

    assert "ship.payout" in result.missing


def test_hours_and_devlogs_are_always_expected():
    """Both render unconditionally, so their absence is a real break."""
    _, result = parse(MULTIPLIER_ROW, PAYOUT_ROW)

    assert "ship.hours_at_ship" in result.missing
    assert "ship.devlogs_at_ship" in result.missing
