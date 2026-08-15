from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.parsers import ParseError, parse_devlog_page
from src.parsers.devlog import mentions_in

FIXTURE = Path(__file__).parent / "fixtures" / "devlog_28968.html"


@pytest.fixture(scope="module")
def parsed():
    return parse_devlog_page(FIXTURE.read_text(encoding="utf-8"), 28968, 8100)


def test_thread_parses_cleanly(parsed):
    assert parsed.missing == set(), f"selectors went stale: {sorted(parsed.missing)}"
    assert parsed.warnings == []
    assert parsed.data["devlog"] == {
        "_id": 28968,
        "project_id": 8100,
        "comments_count": 5,
        "comments_seen": 5,
    }


def test_comment_fields(parsed):
    first = parsed.data["comments"][0]
    assert first["_id"] == 5110
    assert first["devlog_id"] == 28968
    assert first["project_id"] == 8100
    assert first["username"] == "water"
    assert first["posted_at"] == datetime(2026, 7, 24, 23, 21, 23, tzinfo=timezone.utc)
    assert first["body"] == "this is NOT how to devlog brotato"
    assert first["body_length"] == len(first["body"])
    assert first["position"] == 0


def test_every_comment_is_whole(parsed):
    comments = parsed.data["comments"]
    assert len(comments) == 5
    assert all(c["username"] and c["posted_at"] and c["body"] for c in comments)
    # Position is the reading order, which is what "first reply" is counted on.
    assert [c["position"] for c in comments] == [0, 1, 2, 3, 4]
    assert len({c["_id"] for c in comments}) == 5


def test_author_comes_from_the_href_not_the_link_text(parsed):
    # The badge and the admin bolt render inside the same meta block.
    assert all("@" not in c["username"] for c in parsed.data["comments"])
    assert all(" " not in c["username"] for c in parsed.data["comments"])


def test_a_page_for_another_devlog_is_refused():
    with pytest.raises(ParseError, match="rendered devlog 28968"):
        parse_devlog_page(FIXTURE.read_text(encoding="utf-8"), 999, 8100)


def test_a_page_without_a_thread_is_refused():
    with pytest.raises(ParseError, match="no comments section"):
        parse_devlog_page("<html><body>nope</body></html>", 1, 2)


def test_empty_thread_is_an_answer_not_a_failure():
    html = """
    <section class="devlog-detail__comments">
      <span id="comments_count_post_devlog_7">0</span>
      <p class="devlog-detail__empty">No comments yet. Be the first!</p>
    </section>
    """
    parsed = parse_devlog_page(html, 7, 3)
    assert parsed.data["comments"] == []
    assert parsed.missing == set()
    assert parsed.warnings == []


def test_a_silently_empty_thread_is_a_failure():
    html = '<section class="devlog-detail__comments">' \
           '<span id="comments_count_post_devlog_7">4</span></section>'
    parsed = parse_devlog_page(html, 7, 3)
    assert "comments" in parsed.missing


def test_paragraphs_do_not_run_together():
    html = """
    <section class="devlog-detail__comments">
      <span id="comments_count_post_devlog_7">1</span>
      <div id="comment_11" class="devlog-comment">
        <a class="devlog-comment__author" href="/@zed">@zed</a>
        <time class="devlog-comment__time" datetime="2026-07-01T10:00:00Z"></time>
        <div class="devlog-comment__body"><p>first line</p><p>second line</p></div>
      </div>
    </section>
    """
    parsed = parse_devlog_page(html, 7, 3)
    # Kept as a break rather than a space, so the comment reads as it was written.
    assert parsed.data["comments"][0]["body"] == "first line\n\nsecond line"


def test_words_either_side_of_an_inline_tag_keep_their_space():
    html = """
    <section class="devlog-detail__comments">
      <span id="comments_count_post_devlog_7">1</span>
      <div id="comment_11" class="devlog-comment">
        <a class="devlog-comment__author" href="/@zed">@zed</a>
        <time class="devlog-comment__time" datetime="2026-07-01T10:00:00Z"></time>
        <div class="devlog-comment__body">try the <code>sleep</code> command</div>
      </div>
    </section>
    """
    parsed = parse_devlog_page(html, 7, 3)
    assert parsed.data["comments"][0]["body"] == "try the sleep command"


def test_more_rendered_than_counted_is_reported():
    html = """
    <section class="devlog-detail__comments">
      <span id="comments_count_post_devlog_7">0</span>
      <div id="comment_11" class="devlog-comment">
        <a class="devlog-comment__author" href="/@zed">@zed</a>
        <time class="devlog-comment__time" datetime="2026-07-01T10:00:00Z"></time>
        <div class="devlog-comment__body">hi</div>
      </div>
    </section>
    """
    parsed = parse_devlog_page(html, 7, 3)
    assert any("renders 1" in w for w in parsed.warnings)


def test_an_emote_only_comment_keeps_its_name_and_is_not_a_failure():
    html = """
    <section class="devlog-detail__comments">
      <span id="comments_count_post_devlog_7">1</span>
      <div id="comment_11" class="devlog-comment">
        <a class="devlog-comment__author" href="/@zed">@zed</a>
        <time class="devlog-comment__time" datetime="2026-07-01T10:00:00Z"></time>
        <div class="devlog-comment__body"><p><img src="x.png" alt=":party:"></p></div>
      </div>
    </section>
    """
    parsed = parse_devlog_page(html, 7, 3)
    comment = parsed.data["comments"][0]

    assert parsed.missing == set()
    assert comment["body"] is None
    assert comment["body_length"] == 0
    assert comment["images"] == [":party:"]


def test_a_body_element_going_absent_is_a_failure():
    html = """
    <section class="devlog-detail__comments">
      <span id="comments_count_post_devlog_7">1</span>
      <div id="comment_11" class="devlog-comment">
        <a class="devlog-comment__author" href="/@zed">@zed</a>
        <time class="devlog-comment__time" datetime="2026-07-01T10:00:00Z"></time>
      </div>
    </section>
    """
    assert "comment.body" in parse_devlog_page(html, 7, 3).missing


def test_mentions_follow_upstreams_own_pattern():
    assert mentions_in("hey @Zed and (@ana) nice") == ["ana", "zed"]
    # Mid-word @ is not a mention upstream either, so it is not one here.
    assert mentions_in("mail me at a@b.com") == []
    assert mentions_in("@zed @zed") == ["zed"]
    assert mentions_in(None) == []
