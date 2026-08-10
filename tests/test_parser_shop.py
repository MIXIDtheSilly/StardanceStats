from __future__ import annotations

from pathlib import Path

import pytest

from src.parsers import ParseError, parse_shop_page
from src.parsers.shop import REGION_CODES

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def us():
    return parse_shop_page((FIXTURES / "shop_all_us.html").read_text(encoding="utf-8"), "US")


@pytest.fixture(scope="module")
def india():
    return parse_shop_page((FIXTURES / "shop_all_in.html").read_text(encoding="utf-8"), "IN")


def item(parsed, item_id: int):
    return next(i for i in parsed.data["items"] if i["_id"] == item_id)


def test_catalogue_parses_cleanly(us, india):
    assert us.missing == set(), f"selectors went stale: {sorted(us.missing)}"
    assert us.warnings == []
    assert us.data["region"] == "US"
    assert len(us.data["items"]) == 101
    assert len(india.data["items"]) == 90


def test_item_fields(us):
    pinecil = item(us, 8)
    assert pinecil["name"] == "Pinecil"
    assert pinecil["price"] == 185
    assert pinecil["full_price"] == 185
    assert pinecil["on_sale"] is False
    assert pinecil["url"] == "/shop/items/8"
    assert pinecil["categories"] == ["HQ"]
    assert pinecil["enabled_regions"] == ["US", "EU", "UK", "IN", "CA", "AU", "XX"]
    assert pinecil["purchases"] == 523
    assert (pinecil["hours_low"], pinecil["hours_high"]) == (10, 19)
    assert pinecil["image_url"].startswith("https://")


def test_the_same_item_costs_differently_per_region(us, india):
    assert item(us, 8)["price"] == 185
    assert item(india, 8)["price"] == 379

    shared = {i["_id"] for i in us.data["items"]} & {i["_id"] for i in india.data["items"]}
    by_us = {i["_id"]: i["price"] for i in us.data["items"]}
    by_in = {i["_id"]: i["price"] for i in india.data["items"]}
    differing = [i for i in shared if by_us[i] != by_in[i]]
    assert len(differing) > 20, "regional pricing is the whole reason for this crawl"


def test_a_sale_keeps_both_prices(us):
    on_sale = [i for i in us.data["items"] if i["on_sale"]]
    assert on_sale, "fixture has no sale item to check"

    tote = item(us, 183)
    assert tote["on_sale"] is True
    assert tote["sale_percentage"] == 30
    assert tote["price"] == 116
    assert tote["full_price"] == 165
    assert tote["price"] < tote["full_price"]


def test_every_item_carries_the_two_fields_a_row_needs(us):
    for i in us.data["items"]:
        assert i["_id"] and i["name"], i
        assert i["price"] is not None, i


def test_a_page_priced_for_another_region_is_refused():
    html = (FIXTURES / "shop_all_us.html").read_text(encoding="utf-8")
    with pytest.raises(ParseError, match="priced for US instead"):
        parse_shop_page(html, "IN")


def test_a_page_that_does_not_name_its_region_is_refused():
    with pytest.raises(ParseError, match="does not say which region"):
        parse_shop_page('<div class="shop-category"></div>', "US")


def test_a_closed_shop_is_not_read_as_an_empty_one():
    html = (
        '<div class="shop-category" data-shop-user-region-value="US">'
        "<h2>The shop is not opened yet!</h2></div>"
    )
    with pytest.raises(ParseError, match="did not render"):
        parse_shop_page(html, "US")


def test_an_empty_catalogue_says_so_in_words():
    html = (
        '<div class="shop-category" data-shop-user-region-value="US">'
        '<div class="shop-category__items">'
        '<p class="shop-category__empty">Nothing here yet. Check back soon.</p>'
        "</div></div>"
    )
    parsed = parse_shop_page(html, "US")
    assert parsed.data["items"] == []
    assert parsed.missing == set()


def test_a_silently_empty_catalogue_is_a_failure():
    html = (
        '<div class="shop-category" data-shop-user-region-value="US">'
        '<div class="shop-category__items"></div></div>'
    )
    assert "shop.items" in parse_shop_page(html, "US").missing


def test_stock_and_locks_are_read_off_the_card():
    html = """
    <div class="shop-category" data-shop-user-region-value="US">
      <div class="shop-category__items">
        <div class="shop-item-card shop-item-card--out-of-stock shop-item-card--mission-locked"
             data-shop-id="1" data-price="50" data-regions="US"
             data-achievement-locked="true">
          <h3 class="shop-item-card__title">Gated thing</h3>
          <span class="shop-item-card__stock-badge">Out of stock</span>
        </div>
        <div class="shop-item-card" data-shop-id="2" data-price="10" data-regions="US">
          <h3 class="shop-item-card__title">Nearly gone</h3>
          <span class="shop-item-card__stock-badge">3 left</span>
          <span class="shop-item-card__ribbon shop-item-card__ribbon--new">New</span>
        </div>
      </div>
    </div>
    """
    items = {i["_id"]: i for i in parse_shop_page(html, "US").data["items"]}

    assert items[1]["out_of_stock"] is True
    assert items[1]["remaining_stock"] == 0
    assert items[1]["mission_locked"] is True
    assert items[1]["achievement_locked"] is True

    assert items[2]["remaining_stock"] == 3
    assert items[2]["out_of_stock"] is False
    assert items[2]["is_new"] is True
    # Stock renders only under ten left, so silence is plenty, not unknown.
    assert items[2]["purchases"] is None


def test_an_item_on_a_page_it_disclaims_is_reported():
    html = """
    <div class="shop-category" data-shop-user-region-value="US">
      <div class="shop-category__items">
        <div class="shop-item-card" data-shop-id="1" data-price="50" data-regions="EU,UK">
          <h3 class="shop-item-card__title">Elsewhere</h3>
        </div>
      </div>
    </div>
    """
    parsed = parse_shop_page(html, "US")
    assert any("claims ['EU', 'UK']" in w for w in parsed.warnings)


def test_every_region_the_shop_has_is_one_we_ask_for():
    assert REGION_CODES == ("US", "EU", "UK", "IN", "CA", "AU", "XX")
