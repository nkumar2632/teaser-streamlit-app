from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.integrity import REFERENCE, PIN, verify_model

from teaser_model_v1.live.card import grade_week
from teaser_model_v1.live.market import read_market_csv
from teaser_model_v1.live.pricing import read_price_csv
from teaser_model_v1.live.research import teased_board_rows


def original_week2():
    folder = REFERENCE / "data" / "live" / "input"
    market = read_market_csv(folder / "nfl_2026_week_02_market_screenshot.csv")
    prices = read_price_csv(folder / "nfl_2026_week_02_prices_screenshot.csv", season=2026, week=2)
    return market, prices, grade_week(market, prices, graded_at=market.captured_at)


def test_week2_verified_identities_exact_values_and_source_contract():
    verify_model()
    assert PIN == "94e412ed5bedc03a60b0b86f1146ff80c9f9e195"
    market, prices, source = original_week2()
    adapter = TeaserModelAdapter()
    slate, view = adapter.week2_example()
    assert view.historical
    assert [leg["team"] for leg in view.qualifying_legs] == ["TB", "ATL", "CIN", "BAL"]
    assert [leg["p_est"] for leg in view.qualifying_legs] == [
        "0.7550713499837194", "0.7471593818727904",
        "0.7398722507822", "0.738139885177199",
    ]
    assert [tuple(t["teams"]) for t in view.selected_tickets] == [
        ("TB", "ATL", "CIN"), ("TB", "ATL", "BAL"), ("CIN", "BAL"),
    ]
    assert {leg["team"]: view.exposure[leg["leg_id"]] for leg in view.qualifying_legs} == {
        "TB": 2, "ATL": 2, "CIN": 2, "BAL": 2,
    }
    assert view.card_id == source.card_id
    assert view.market_snapshot_id == market.snapshot_id
    assert view.price_snapshot_id == prices.snapshot_id
    assert [dict(leg) for leg in view.qualifying_legs] == [leg.to_dict() for leg in source.qualifying_legs]
    assert [dict(leg) for leg in view.top_legs] == [leg.to_dict() for leg in source.top_legs]
    for mapped, ticket in zip(view.tickets, source.tickets, strict=True):
        assert {key: mapped[key] for key in ticket.to_dict()} == ticket.to_dict()
        assert mapped["stake_units"] == (1 if ticket.selected else 0)
    assert view.selected_ticket_keys == source.selected_ticket_keys
    assert dict(view.exposure) == source.exposure
    research = teased_board_rows(market)
    assert [(r["team"], r["geometry_class"], r["track"], r["p_est"])
            for r in view.research] == [(r.team, r.geometry_class, r.track, r.p_est) for r in research]
    manual_view = adapter.grade(slate)
    assert [dict(leg)["p_est"] for leg in manual_view.qualifying_legs] == [
        dict(leg)["p_est"] for leg in view.qualifying_legs]
    assert manual_view.selected_ticket_keys == view.selected_ticket_keys
    assert dict(manual_view.exposure) == dict(view.exposure)


def test_missing_prices_and_negative_ev_are_source_results():
    adapter = TeaserModelAdapter()
    slate, _ = adapter.week2_example()
    slate = deepcopy(slate)
    slate["prices"] = {"2": "", "3": ""}
    no_price = adapter.grade(slate)
    assert no_price.tickets
    assert not no_price.selected_tickets
    assert all(t["ev_per_unit"] == "UNAVAILABLE" for t in no_price.tickets)
    slate["prices"] = {"2": "-10000", "3": "-10000"}
    negative = adapter.grade(slate)
    assert negative.tickets
    assert not negative.selected_tickets
    assert all(t["status"] == "NEGATIVE_EV" for t in negative.tickets)
    with pytest.raises(ValueError, match="no selected tickets"):
        adapter.recheck(negative.card_id, slate)


def test_recheck_delegates_to_source_and_keeps_original():
    adapter = TeaserModelAdapter()
    slate, _ = adapter.week2_example()
    live = adapter.grade(slate)
    changed = deepcopy(slate)
    changed["captured_at"] = "2026-09-19T15:50:00-04:00"
    with pytest.raises(ValueError, match="capture time later"):
        adapter.recheck(live.card_id, slate)
    changed["rows"] = [dict(row) for row in changed["rows"]]
    tb = next(row for row in changed["rows"] if row["team"] == "TB")
    cle = next(row for row in changed["rows"] if row["team"] == "CLE")
    tb["spread"] = "-8.0"
    cle["spread"] = "+8.0"
    result = adapter.recheck(live.card_id, changed)
    assert "DISCARD" in result.verdict
    assert result.rebuilt_card is not None
    assert [leg["team"] for leg in live.qualifying_legs] == ["TB", "ATL", "CIN", "BAL"]
    assert "TB" not in [leg["team"] for leg in result.rebuilt_card.qualifying_legs]


@pytest.mark.parametrize("field,value", [("spread", "2.25"), ("total", "nan"), ("spread", "1e4")])
def test_invalid_line_is_rejected(field, value):
    adapter = TeaserModelAdapter()
    slate, _ = adapter.week2_example()
    slate["rows"][0][field] = value
    with pytest.raises(ValueError):
        adapter.grade(slate)
