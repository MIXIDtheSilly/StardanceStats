from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.parsers import ParseError, parse_project_page
from src.parsers.common import (
    id_from_path,
    parse_duration_seconds,
    strip_handle,
    to_float,
    to_int,
)

FIXTURE = Path(__file__).parent / "fixtures" / "project_8100.html"


@pytest.fixture(scope="module")
def parsed():
    return parse_project_page(FIXTURE.read_text(encoding="utf-8"), 8100)


def test_header_fields(parsed):
    p = parsed.data["project"]
    assert p["_id"] == 8100
    assert p["title"] == "Crawssembly"
    assert p["owner_username"] == "The_Craw"
    assert p["members"] == ["The_Craw"]
    assert p["repo_url"] == "https://github.com/Jonah-Crawford/Crawssembly"
    assert p["demo_url"] == "https://crawssembly.ultimatecraw.xyz/"
    assert p["owner_avatar_url"].startswith("https://cachet.dunkirk.sh/users/")
    assert p["is_hardware"] is False
    assert "Programming languages are a lie" in p["description"]


def test_header_stats_are_read_by_label_not_position(parsed):
    p = parsed.data["project"]
    assert p["devlogs_count"] == 107
    assert p["total_hours"] == 256
    assert p["followers"] == 72


def test_nothing_went_unparsed(parsed):
    assert parsed.missing == set()
    assert parsed.warnings == []


def test_devlog_count_matches_header(parsed):
    assert len(parsed.data["devlogs"]) == 107


def test_devlog_fields(parsed):
    newest = max(parsed.data["devlogs"], key=lambda d: d["posted_at"])
    assert newest["_id"] == 33892
    assert newest["post_id"] == 40209
    assert newest["project_id"] == 8100
    assert newest["username"] == "The_Craw"
    assert newest["posted_at"] == datetime(2026, 8, 3, 16, 48, 57, tzinfo=timezone.utc)
    assert newest["duration_seconds"] == 5289  # "1h 28m 9s logged"
    assert newest["likes"] == 1
    assert newest["comments"] == 0
    assert newest["reposts"] == 0
    assert newest["views"] == 8
    assert "publishing packages" in newest["body"]


def test_every_devlog_has_identity_and_engagement(parsed):
    for d in parsed.data["devlogs"]:
        assert isinstance(d["_id"], int) and d["_id"] > 0
        assert d["posted_at"] is not None
        assert d["likes"] is not None
        assert d["comments"] is not None
        assert d["views"] is not None


def test_devlog_ids_are_unique(parsed):
    ids = [d["_id"] for d in parsed.data["devlogs"]]
    assert len(ids) == len(set(ids))


def test_the_whole_body_is_kept_not_a_preview(parsed):
    """Upstream clamps long posts with CSS, so the card carries all of the text."""
    longest = max(parsed.data["devlogs"], key=lambda d: len(d["body"] or ""))
    assert len(longest["body"]) == 746
    assert longest["body"].startswith("ATTENTION! The moment has arrived")
    assert longest["body"].endswith("support open-source learning!")


def test_paragraphs_survive_as_breaks(parsed):
    longest = max(parsed.data["devlogs"], key=lambda d: len(d["body"] or ""))
    assert "Raffle!\n\nGet the chance" in longest["body"]


def test_words_either_side_of_inline_markup_keep_their_space(parsed):
    """The old reading welded these together: "cal add -65 r01" came back glued on."""
    bodies = [d["body"] for d in parsed.data["devlogs"] if d["body"]]
    assert any("Rather than checking every char" in body for body in bodies)
    assert not any("Monday.Today" in body for body in bodies)


def test_a_body_longer_than_the_ceiling_is_cut_and_reported():
    html = """
    <html><body>
      <h1 class="project-show__title">T</h1>
      <article class="feed-post-card"
               data-feed-engagement-post-type-value="Post::Devlog"
               data-feed-engagement-post-id-value="7"
               data-card-link-url-value="/projects/1/devlogs/9">
        <div class="feed-post-card__body">%s</div>
      </article>
    </body></html>
    """ % ("x" * 25_000)
    parsed = parse_project_page(html, 1)
    assert len(parsed.data["devlogs"][0]["body"]) == 20_000
    assert any("cut to 20000" in w for w in parsed.warnings)


def test_devlog_media_is_read_off_the_card(parsed):
    newest = max(parsed.data["devlogs"], key=lambda d: d["posted_at"])
    assert newest["media"] == [
        {
            "kind": "image",
            "url": (
                "https://stardance.hackclub.com/rails/active_storage/representations/"
                "proxy/eyJfcmFpbHMiOnsiZGF0YSI6MjQ0MTkzLCJwdXIiOiJibG9iX2lkIn19--"
                "3f820f8e5357a8fadbb18519363d949c79b1fb60/eyJfcmFpbHMiOnsiZGF0YSI6"
                "eyJmb3JtYXQiOiJ3ZWJwIiwicmVzaXplX3RvX2xpbWl0IjpbMTYwMCw5MDBdLCJzYXZl"
                "ciI6eyJzdHJpcCI6dHJ1ZSwicXVhbGl0eSI6NzV9fSwicHVyIjoidmFyaWF0aW9uIn19"
                "--5394ecd620f1b8ee9be71be3f37cd22b8a88953c/Screenshot%202026-08-03%20174846.png"
            ),
        }
    ]


def test_a_carousel_keeps_its_slides_in_order(parsed):
    by_id = {d["_id"]: d for d in parsed.data["devlogs"]}
    urls = [m["url"] for m in by_id[5389]["media"]]
    assert len(urls) == 4
    assert [url.rsplit("/", 1)[-1] for url in urls] == [
        "Screenshot%202026-06-10%20124827.png",
        "Screenshot%202026-06-10%20124818.png",
        "Screenshot%202026-06-10%20124810.png",
        "Screenshot%202026-06-10%20124800.png",
    ]


def test_every_media_item_is_a_kind_we_can_render(parsed):
    kinds = {m["kind"] for d in parsed.data["devlogs"] for m in d["media"]}
    assert kinds == {"image"}
    assert sum(len(d["media"]) for d in parsed.data["devlogs"]) == 115


def test_a_devlog_without_attachments_gets_an_empty_list():
    card = """
    <html><body>
      <h1 class="project-show__title">T</h1>
      <article class="feed-post-card"
               data-feed-engagement-post-type-value="Post::Devlog"
               data-feed-engagement-post-id-value="7"
               data-card-link-url-value="/projects/1/devlogs/9">
        <div class="feed-post-card__body">no pictures here</div>
      </article>
    </body></html>
    """
    devlog = parse_project_page(card, 1).data["devlogs"][0]
    assert devlog["media"] == []


def test_a_video_attachment_is_read_with_its_poster():
    card = """
    <html><body>
      <h1 class="project-show__title">T</h1>
      <article class="feed-post-card"
               data-feed-engagement-post-type-value="Post::Devlog"
               data-feed-engagement-post-id-value="7"
               data-card-link-url-value="/projects/1/devlogs/9">
        <div class="feed-post-card__media">
          <figure class="feed-post-card__media-slide">
            <video poster="/still.jpg"><source src="/clip.mp4" type="video/mp4"></video>
          </figure>
        </div>
      </article>
    </body></html>
    """
    devlog = parse_project_page(card, 1).data["devlogs"][0]
    assert devlog["media"] == [{"kind": "video", "url": "/clip.mp4", "poster": "/still.jpg"}]


def test_an_unreadable_slide_is_reported_rather_than_dropped_quietly():
    card = """
    <html><body>
      <h1 class="project-show__title">T</h1>
      <article class="feed-post-card"
               data-feed-engagement-post-type-value="Post::Devlog"
               data-feed-engagement-post-id-value="7"
               data-card-link-url-value="/projects/1/devlogs/9">
        <div class="feed-post-card__media">
          <figure class="feed-post-card__media-slide"><picture></picture></figure>
        </div>
      </article>
    </body></html>
    """
    parsed = parse_project_page(card, 1)
    assert parsed.data["devlogs"][0]["media"] == []
    assert any("media slide" in w for w in parsed.warnings)


def test_summed_hours_track_the_header(parsed):
    summed = sum(d["duration_seconds"] for d in parsed.data["devlogs"]) / 3600
    assert summed == pytest.approx(256, abs=1.0)


def test_ships(parsed):
    ships = {s["ship_number"]: s for s in parsed.data["ships"]}
    assert set(ships) == {1, 2}

    s2 = ships[2]
    assert s2["_id"] == 20937
    assert s2["devlogs_at_ship"] == 13
    assert s2["hours_at_ship"] == 32.0
    assert s2["multiplier"] == 19.65
    assert s2["payout"] == 633
    assert s2["status"] == "approved"
    assert s2["payout_blessing"] is None  # neutral: no blessing pill rendered
    assert s2["shipped_at"] == datetime(2026, 7, 2, 17, 49, 35, tzinfo=timezone.utc)

    s1 = ships[1]
    assert s1["multiplier"] == 19.78
    assert s1["payout"] == 2409
    assert s1["hours_at_ship"] == 133.0
    assert s1["devlogs_at_ship"] == 46


def test_ship_paragraphs_are_broken_apart_not_welded(parsed):
    """simple_format renders each paragraph as its own <p>, which text_of would run together."""
    body = {s["ship_number"]: s["body"] for s in parsed.data["ships"]}[2]
    assert "ship!\n" in body
    assert "ship!Programming" not in body


def test_ship_paragraphs_do_not_leave_a_blank_line(parsed):
    """Upstream zeroes the paragraph margin, so the lines sit tight rather than spaced."""
    for ship in parsed.data["ships"]:
        assert "\n\n" not in (ship["body"] or "")


def test_fire_event_is_not_mistaken_for_a_devlog(parsed):
    """The page also carries a Post::FireEvent card; it must be ignored."""
    assert len(parsed.data["devlogs"]) + len(parsed.data["ships"]) == 109


def test_unrecognisable_page_raises():
    with pytest.raises(ParseError):
        parse_project_page("<html><body><h1>500</h1></body></html>", 1)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1h 28m 9s logged", 5289),
        ("45m logged", 2700),
        ("2h logged", 7200),
        ("1d 13h 15m", 86400 + 13 * 3600 + 15 * 60),
        ("0s", 0),
        ("nothing here", None),
        (None, None),
    ],
)
def test_parse_duration_seconds(text, expected):
    assert parse_duration_seconds(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("1,234 followers", 1234), ("633 Stardust", 633), ("", None), (None, None)],
)
def test_to_int(text, expected):
    assert to_int(text) == expected


def test_to_float():
    assert to_float("19.65x multiplier") == 19.65
    assert to_float("32h") == 32.0
    assert to_float(None) is None


def test_strip_handle():
    assert strip_handle("@The_Craw") == "The_Craw"
    assert strip_handle(None) is None


def test_id_from_path():
    assert id_from_path("/projects/8100/devlogs/33892", segment="devlogs") == 33892
    assert id_from_path("/projects/8100", segment="devlogs") is None
