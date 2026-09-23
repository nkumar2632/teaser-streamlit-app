from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.inputs import decode_slate, encode_slate
from teaser_app.market_data import LocalHistory, normalize_market
from teaser_app.paper_history import paper_performance, run_record
from teaser_app.strategy import CFB_TEASER, NFL_TEASER, Strategy

from teaser_model_v1.engine.pricing import profit_from_american_odds
from teaser_model_v1.engine.tickets import select_live_tickets
from teaser_model_v1.live.paper import (
    PregameLineInput, build_cfb_paper_legs, build_cfb_paper_tickets, primary_paper_legs,
)


def cfb_slate() -> dict:
    rows = []
    for away, home, team, spread, total in (
        ("Alpha State", "Beta", "Alpha State", "+2.5", "45.5"),
        ("Gamma", "Delta", "Gamma", "+1.5", "46.5"),
        ("Epsilon", "Zeta", "Epsilon", "-7.5", "48.5"),
        ("Eta", "Theta", "Eta", "-8.5", "50.5"),
        ("Iota", "Kappa", "Iota", "+4.5", "47.5"),
        ("Lambda", "Mu", "Lambda", "+2.5", "55.5"),
    ):
        rows.append({"away_team": away, "home_team": home, "team": team,
                     "spread": spread, "total": total, "kickoff": "2026-09-26T12:00:00-04:00"})
    return {"schema_version": 2, "league": "CFB", "source": "manual_sportsbook",
            "line_label": "true_timestamped_pregame", "season": 2026, "week": 4,
            "sportsbook": "Example Book", "captured_at": "2026-09-25T12:00:00-04:00",
            "prices": {"2": "+170", "3": "+400"}, "rows": rows}


def test_cfb_full_paper_output_matches_pinned_source_and_never_places():
    adapter = TeaserModelAdapter()
    slate = cfb_slate()
    view = adapter.grade_cfb_paper(slate)
    inputs = [PregameLineInput(
        game_id=next(leg["game_id"] for leg in view.legs if leg["team"] == row["team"]),
        team=row["team"], opponent=row["home_team"], spread=row["spread"],
        total=row["total"], kickoff=row["kickoff"],
        source="manual_sportsbook:Example Book", line_label=slate["line_label"],
    ) for row in slate["rows"]]
    source_legs = build_cfb_paper_legs(inputs)
    primary = primary_paper_legs(source_legs)
    _, tickets = build_cfb_paper_tickets(primary, {2: profit_from_american_odds(170),
                                                    3: profit_from_american_odds(400)})
    selected = select_live_tickets(tickets, eligibility=lambda t: t.is_positive_ev)
    assert view.strategy == CFB_TEASER
    assert [(r["leg_id"], r["p_est"], r["teased_spread"]) for r in view.legs] == [
        (leg.leg_id, repr(leg.p_est), str(leg.teased_spread)) for leg in source_legs]
    assert [r["leg_id"] for r in view.qualifying_legs] == [leg.leg_id for leg in primary]
    assert len(view.tickets) == len(tickets) == 10
    assert [(t["p_ticket"], t["break_even"], t["ev_per_unit"]) for t in view.tickets] == [
        (repr(t.p_ticket), repr(t.break_even), repr(t.ev)) for t in tickets]
    assert {t["ticket_key"] for t in view.selected_tickets} == {
        "|".join(sorted(t.leg_ids)) for t in selected.selected}
    assert dict(view.exposure) == selected.exposure
    assert next(row for row in view.legs if row["team"] == "Iota")["exclusion_reason"].startswith("secondary geometry")
    assert next(row for row in view.legs if row["team"] == "Lambda")["exclusion_reason"] == "total above CFB guardrail"
    assert all(row["track"] == "PAPER" for row in view.legs)
    with pytest.raises(ValueError, match="NFL live"):
        adapter.grade(slate)


def test_status_is_strategy_property_not_league_global():
    assert NFL_TEASER.status == "LIVE"
    assert CFB_TEASER.status == "PAPER"
    assert Strategy("NFL", "ATS", "future_model", "CHALLENGER").status == "CHALLENGER"
    assert Strategy("NFL", "TEASER", "future_model", "PAPER").status == "PAPER"


def test_append_only_market_and_paper_performance_are_isolated(tmp_path):
    adapter = TeaserModelAdapter()
    slate = cfb_slate()
    history = LocalHistory(tmp_path)
    first = history.ingest_snapshot(slate)
    assert set(first["events"][0]) >= {"snapshot_id", "event_id", "league", "home_team",
                                        "away_team", "kickoff", "captured_at", "source", "sportsbook",
                                        "spread_home", "spread_home_price", "spread_away",
                                        "spread_away_price", "moneyline_home", "moneyline_away",
                                        "total", "over_price", "under_price",
                                        "teaser_2team_6pt_price", "teaser_3team_6pt_price"}
    assert history.ingest_snapshot(slate)["snapshot_id"] == first["snapshot_id"]
    later = deepcopy(slate)
    later["captured_at"] = "2026-09-25T13:00:00-04:00"
    later["rows"][0]["spread"] = "+1.5"
    second = history.ingest_snapshot(later)
    assert second["snapshot_id"] != first["snapshot_id"]
    assert history.get_snapshot(first["snapshot_id"]) == first
    assert len(history.market_history(league="CFB")) == 2
    assert history.latest_market("CFB") == second
    paper = adapter.grade_cfb_paper(slate)
    run = history.save_run(run_record(paper, slate, first["snapshot_id"]))
    assert run["actual_placements"] == run["actual_units_staked"] == 0
    assert history.get_run(run["run_id"]) == run
    assert history.runs(league="CFB", bet_type="TEASER", model_version="teaser_v1.0", status="PAPER") == [run]
    games = {leg["game_id"] for leg in run["legs"]}
    scores = {game_id: {"home": 42, "away": 7} for game_id in games}
    first_results = history.save_paper_results(run["run_id"], scores, recorded_at="2026-09-27T12:00:00-04:00")
    corrected = deepcopy(scores)
    corrected[next(iter(games))] = {"home": 7, "away": 42}
    later_results = history.save_paper_results(run["run_id"], corrected, recorded_at="2026-09-27T13:00:00-04:00")
    assert first_results["result_id"] != later_results["result_id"]
    assert history.latest_paper_results(run["run_id"]) == later_results
    assert first_results["scores"] == scores
    graded = adapter.grade_paper_results(run, scores)
    assert all(t["result"] in {"WIN", "LOSS"} for t in graded["tickets"])
    performance = paper_performance(history, adapter)
    assert performance["settled"] == len(paper.selected_tickets)
    assert performance["actual_placements"] == performance["actual_units_staked"] == 0
    live_run = history.save_run({**run_record(paper, slate, first["snapshot_id"]),
                                 "strategy": NFL_TEASER.to_dict()})
    with pytest.raises(ValueError, match="paper-only"):
        history.save_paper_results(live_run["run_id"], scores,
                                   recorded_at="2026-09-27T12:00:00-04:00")


def test_cfb_rejects_postkickoff_or_off_grid_current_line():
    adapter = TeaserModelAdapter()
    slate = cfb_slate()
    slate["captured_at"] = slate["rows"][0]["kickoff"]
    with pytest.raises(ValueError, match="precede kickoff"):
        adapter.grade_cfb_paper(slate)
    slate = cfb_slate()
    slate["rows"][0]["spread"] = "+2.25"
    with pytest.raises(ValueError, match="half-point-grid"):
        adapter.grade_cfb_paper(slate)


def test_v1_nfl_slate_and_original_cfb_artifact_still_read():
    adapter = TeaserModelAdapter()
    old_slate, old_card = adapter.week2_example()
    loaded = decode_slate(encode_slate(old_slate))
    assert loaded == old_slate
    normalized = normalize_market(loaded)
    assert normalized["source"] == "unspecified"
    assert all(event["source"] == "unspecified" for event in normalized["events"])
    assert adapter.grade(loaded).selected_ticket_keys == old_card.selected_ticket_keys
    assert decode_slate(encode_slate(cfb_slate()))["league"] == "CFB"
    path = Path(__file__).resolve().parents[1] / ".model_reference" / "data" / "live" / "cards" / "cfb_paper_board_2026-09-19.json"
    import json
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["kind"] == "cfb_paper_board"
    assert artifact["actual_units_staked"] == 0


def test_cfb_streamlit_smoke_shows_track_and_full_entry(tmp_path, monkeypatch):
    import teaser_app.cfb_page as cfb_page
    monkeypatch.setattr(cfb_page, "LocalHistory", lambda: LocalHistory(tmp_path))
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    next(radio for radio in app.radio if radio.label == "League and model track").set_value("CFB · PAPER").run()
    next(radio for radio in app.radio if radio.label == "CFB market source").set_value("Manual entry").run()
    assert not app.exception
    assert any("PAPER" in item.value and "teaser_v1.0" in item.value for item in app.caption)
    assert any(button.label == "Build PAPER card from entered slate" for button in app.button)
    assert any(button.label == "Read original 2026-09-19 paper board" for button in app.button)
    app.session_state["cfb_slate"] = cfb_slate()
    app.run()
    next(button for button in app.button if button.label == "Build PAPER card from entered slate").click().run()
    assert not app.exception
    assert any("PAPER — NOT REAL MONEY" in warning.value for warning in app.warning)
    assert any("Hypothetical paper card" == heading.value for heading in app.subheader)
    assert any("PRIMARY / PAPER legs" in heading.value for heading in app.subheader)
    assert any("P_ticket" in item.value and "EV" in item.value for item in app.markdown)
    assert any("Actual placements: 0" in caption.value for caption in app.caption)
