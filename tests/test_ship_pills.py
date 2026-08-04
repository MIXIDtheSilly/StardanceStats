from __future__ import annotations

import pytest
from selectolax.parser import HTMLParser

from src.parsers.project import _ship_pills

# payout_blessing reuses the latest-ship-status class/modifiers, so it must
# not be mistaken for the status itself; no fixture has one, hence snippets.
BLESSED_PILL = (
    '<span class="project-show__latest-ship-status project-show__latest-ship-status--approved"'
    ' title="Blessed (+20% bonus)">✨ Blessed</span>'
)
CURSED_PILL = (
    '<span class="project-show__latest-ship-status project-show__latest-ship-status--returned"'
    ' title="Cursed (-50% reduction)">\U0001f480 Cursed</span>'
)
PENDING_PILL = (
    '<span class="project-show__latest-ship-status'
    ' project-show__latest-ship-status--pending">Pending review</span>'
)
RETURNED_PILL = (
    '<span class="project-show__latest-ship-status'
    ' project-show__latest-ship-status--returned">Changes requested</span>'
)


def card(*pills: str):
    html = (
        '<article class="feed-post-card project-show__latest-ship">'
        '<div class="feed-post-card__reposted-by project-show__latest-ship-label">'
        "<span>Ship #3</span>" + "".join(pills) + "</div></article>"
    )
    return HTMLParser(html).css_first("article")


@pytest.mark.parametrize(
    "pills,expected",
    [
        ((), ("approved", None)),
        ((PENDING_PILL,), ("pending", None)),
        ((RETURNED_PILL,), ("returned", None)),
        # The regression: a blessing must not be mistaken for the status.
        ((BLESSED_PILL,), ("approved", "blessed")),
        ((CURSED_PILL,), ("approved", "cursed")),
        # Both pills together, in the order the template emits them.
        ((PENDING_PILL, BLESSED_PILL), ("pending", "blessed")),
        ((RETURNED_PILL, CURSED_PILL), ("returned", "cursed")),
    ],
)
def test_pills_are_disambiguated(pills, expected):
    assert _ship_pills(card(*pills)) == expected


def test_a_cursed_ship_is_not_read_as_returned():
    """The cursed pill carries the --returned modifier but is not a rejection."""
    status, blessing = _ship_pills(card(CURSED_PILL))
    assert status == "approved"
    assert blessing == "cursed"


def test_a_blessed_ship_is_still_approved():
    status, _ = _ship_pills(card(BLESSED_PILL))
    assert status == "approved"


def test_blessing_survives_without_the_emoji():
    """Match on wording, not decoration. The emoji is not load-bearing."""
    plain = (
        '<span class="project-show__latest-ship-status'
        ' project-show__latest-ship-status--approved" title="Blessed (+20% bonus)">Blessed</span>'
    )
    assert _ship_pills(card(plain)) == ("approved", "blessed")


def test_blessing_survives_a_title_only_pill():
    no_text = (
        '<span class="project-show__latest-ship-status'
        ' project-show__latest-ship-status--approved" title="Cursed (-50% reduction)"></span>'
    )
    assert _ship_pills(card(no_text)) == ("approved", "cursed")
