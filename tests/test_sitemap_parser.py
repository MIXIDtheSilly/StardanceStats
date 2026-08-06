from __future__ import annotations

from datetime import datetime, timezone

from src.collector.sitemap import SitemapEntry, parse_sitemap

UTC = timezone.utc

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://stardance.hackclub.com/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://stardance.hackclub.com/leaderboard</loc>
    <changefreq>daily</changefreq>
  </url>
  <url>
    <loc>https://stardance.hackclub.com/projects/8100</loc>
    <lastmod>2026-08-03</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://stardance.hackclub.com/users/11155</loc>
    <lastmod>2026-06-01</lastmod>
    <priority>0.5</priority>
  </url>
  <url>
    <loc>https://stardance.hackclub.com/missions/summer-of-making</loc>
    <lastmod>2026-07-14</lastmod>
  </url>
</urlset>
"""


def test_parses_kinds_ids_and_dates():
    entries = list(parse_sitemap(SITEMAP))
    assert len(entries) == 5

    assert entries[0] == SitemapEntry("other", None, "/", None)
    assert entries[1] == SitemapEntry("other", None, "/leaderboard", None)
    assert entries[2] == SitemapEntry(
        "project", 8100, "/projects/8100", datetime(2026, 8, 3, tzinfo=UTC)
    )
    assert entries[3] == SitemapEntry(
        "user", 11155, "/users/11155", datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert entries[4] == SitemapEntry(
        "mission",
        "summer-of-making",
        "/missions/summer-of-making",
        datetime(2026, 7, 14, tzinfo=UTC),
    )


def test_a_mission_is_keyed_by_slug_not_a_number():
    xml = "<urlset><url><loc>https://x/missions/web-os-2</loc></url></urlset>"
    (entry,) = list(parse_sitemap(xml))
    assert entry.ref_id == "web-os-2"


def test_mission_subpages_are_not_frontier_rows():
    """The guide and gallery hang off the same mission row."""
    xml = "<urlset><url><loc>https://x/missions/web-os-2/guide</loc></url></urlset>"
    (entry,) = list(parse_sitemap(xml))
    assert entry.kind == "other" and entry.ref_id is None


def test_lastmod_is_utc_midnight():
    """Read as naive local time, a bare date shifts every tier decision by the offset."""
    project = list(parse_sitemap(SITEMAP))[2]
    assert project.lastmod.tzinfo is not None
    assert project.lastmod.utcoffset().total_seconds() == 0


def test_ignores_urls_with_nested_paths():
    xml = """<urlset><url><loc>https://x/projects/8100/devlogs/1</loc></url></urlset>"""
    (entry,) = list(parse_sitemap(xml))
    assert entry.kind == "other" and entry.ref_id is None


def test_missing_lastmod_is_none_not_epoch():
    """An epoch would classify every undated page as ancient rather than unknown."""
    xml = """<urlset><url><loc>https://x/projects/1</loc></url></urlset>"""
    (entry,) = list(parse_sitemap(xml))
    assert entry.ref_id == 1 and entry.lastmod is None


def test_malformed_lastmod_does_not_raise():
    xml = """<urlset><url><loc>https://x/users/5</loc><lastmod>soon</lastmod></url></urlset>"""
    (entry,) = list(parse_sitemap(xml))
    assert entry.lastmod is None
