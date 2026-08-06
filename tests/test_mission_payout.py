from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.ingest.mission import assign_payout_paths, mission_pending
from src.ingest.project import build_stats, estimate_unpaid

UTC = timezone.utc
SHIPPED = datetime(2026, 7, 1, tzinfo=UTC)

MISSIONS = {
    "slack-bot": {"payout_path": "static_prize", "fixed_stardust": 30},
    "hackpad": {"payout_path": "flat_rate", "stardust_per_hour": 5.0},
    "frictionless": {"payout_path": "voting"},
}


def ship(n: int, *, mission=None, hours=10.0, payout=None) -> dict:
    return {
        "_id": n,
        "ship_number": n,
        "shipped_at": SHIPPED + timedelta(days=n),
        "hours_at_ship": hours,
        "payout": payout,
        "mission_slug": mission,
    }


def test_an_ordinary_ship_is_on_the_voting_path():
    ships = [ship(1)]
    assign_payout_paths(ships, MISSIONS)
    assert ships[0]["payout_path"] == "voting"


def test_a_rated_missions_ship_is_still_on_the_voting_path():
    ships = [ship(1, mission="frictionless")]
    assign_payout_paths(ships, MISSIONS)
    assert ships[0]["payout_path"] == "voting"


def test_a_fixed_payout_missions_first_ship_leaves_the_rating_pool():
    ships = [ship(1, mission="slack-bot")]
    assign_payout_paths(ships, MISSIONS)
    assert ships[0]["payout_path"] == "static_prize"


def test_only_the_first_ship_to_a_fixed_mission_takes_the_prize():
    """Upstream rates every reship once the builder has claimed the prize."""
    ships = [ship(1, mission="slack-bot"), ship(2, mission="slack-bot")]
    assign_payout_paths(ships, MISSIONS)
    assert [s["payout_path"] for s in ships] == ["static_prize", "voting"]


def test_ship_order_decides_which_one_claims_it():
    ships = [ship(2, mission="slack-bot"), ship(1, mission="slack-bot")]
    assign_payout_paths(ships, MISSIONS)
    assert {s["_id"]: s["payout_path"] for s in ships} == {1: "static_prize", 2: "voting"}


def test_an_observed_payout_beats_the_inference():
    """Only voting ever writes a payout, so one settles the question."""
    ships = [ship(1, mission="slack-bot", payout=400)]
    assign_payout_paths(ships, MISSIONS)
    assert ships[0]["payout_path"] == "voting"


def test_an_uncrawled_mission_falls_back_to_voting():
    ships = [ship(1, mission="brand-new")]
    assign_payout_paths(ships, MISSIONS)
    assert ships[0]["payout_path"] == "voting"


def test_a_flat_rate_missions_ships_stay_flat_rate():
    ships = [ship(1, mission="hackpad"), ship(2, mission="hackpad")]
    assign_payout_paths(ships, MISSIONS)
    assert [s["payout_path"] for s in ships] == ["flat_rate", "flat_rate"]


def test_pending_values_a_fixed_ship_at_the_prize_not_the_rate():
    ships = [ship(1, mission="slack-bot", hours=12.0)]
    assign_payout_paths(ships, MISSIONS)
    pending = mission_pending(ships, MISSIONS)

    assert pending["fixed_payout_hours"] == 12.0
    assert pending["flat_rate_hours"] == 0.0
    assert pending["stardust"] == 30


def test_pending_values_flat_rate_hours_at_the_stated_rate():
    ships = [ship(1, mission="hackpad", hours=8.0)]
    assign_payout_paths(ships, MISSIONS)
    pending = mission_pending(ships, MISSIONS)

    assert pending["flat_rate_hours"] == 8.0
    assert pending["stardust"] == 40           # 8 h at 5/h


def test_a_paid_ship_contributes_nothing_pending():
    ships = [ship(1, mission="hackpad", hours=8.0, payout=99)]
    assign_payout_paths(ships, MISSIONS)
    assert mission_pending(ships, MISSIONS)["stardust"] == 0


def test_the_estimate_is_unchanged_when_no_mission_is_involved():
    out = estimate_unpaid(1000, logged_hours=100.0, paid_hours=50.0)
    assert out["unpaid_hours"] == 50.0
    assert out["ratable_unpaid_hours"] == 50.0
    assert out["estimated_pending_stardust"] == 1000
    assert out["estimated_total_stardust"] == 2000


def test_mission_hours_are_priced_at_mission_terms_not_the_voting_rate():
    out = estimate_unpaid(
        1000, logged_hours=100.0, paid_hours=50.0,
        mission_hours=20.0, mission_stardust=30,
    )
    assert out["unpaid_hours"] == 50.0
    assert out["ratable_unpaid_hours"] == 30.0
    assert out["mission_pending_hours"] == 20.0
    assert out["mission_pending_stardust"] == 30
    # 30 ratable hours at 20/h, plus the mission's own 30.
    assert out["estimated_pending_stardust"] == 630
    assert out["estimated_total_stardust"] == 1630


def test_a_mission_prize_is_estimated_even_with_no_voting_history():
    out = estimate_unpaid(0, logged_hours=12.0, paid_hours=None,
                          mission_hours=12.0, mission_stardust=30)
    assert out["estimated_pending_stardust"] == 30
    assert out["estimated_total_stardust"] == 30


def test_with_no_history_and_no_mission_there_is_nothing_to_estimate():
    out = estimate_unpaid(0, logged_hours=12.0, paid_hours=None)
    assert out["estimated_pending_stardust"] is None
    assert out["estimated_total_stardust"] is None


def test_mission_hours_never_push_ratable_hours_negative():
    out = estimate_unpaid(100, logged_hours=10.0, paid_hours=8.0, mission_hours=99.0)
    assert out["ratable_unpaid_hours"] == 0.0


PROJECT = {"_id": 1, "total_hours": 40.0, "devlogs_count": 4}
DEVLOGS = [{"duration_seconds": 36000}, {"duration_seconds": 36000}]


def test_stats_price_a_fixed_mission_ship_at_its_prize():
    ships = [
        ship(1, mission="slack-bot", hours=6.0),
        ship(2, hours=4.0, payout=200),
    ]
    stats = build_stats(PROJECT, DEVLOGS, ships, MISSIONS)

    assert stats["paid_hours"] == 4.0
    assert stats["unpaid_hours"] == 16.0               # 20 logged - 4 paid
    assert stats["fixed_payout_hours"] == 6.0
    assert stats["ratable_unpaid_hours"] == 10.0
    # 10 h at 50/h from the paid ship, plus the mission's flat 30.
    assert stats["estimated_pending_stardust"] == 530


def test_without_the_mission_the_same_ships_are_overvalued():
    """The bug this replaces: hours that can only ever pay 30 priced at 50/h."""
    ships = [
        ship(1, mission="slack-bot", hours=6.0),
        ship(2, hours=4.0, payout=200),
    ]
    stats = build_stats(PROJECT, DEVLOGS, ships, {})

    assert stats["fixed_payout_hours"] == 0.0
    assert stats["estimated_pending_stardust"] == 800   # 16 h at 50/h


@pytest.mark.parametrize("path", ["static_prize", "flat_rate", "voting"])
def test_every_ship_is_stamped_with_a_path(path):
    slug = {"static_prize": "slack-bot", "flat_rate": "hackpad", "voting": None}[path]
    ships = [ship(1, mission=slug)]
    build_stats(PROJECT, DEVLOGS, ships, MISSIONS)
    assert ships[0]["payout_path"] == path
