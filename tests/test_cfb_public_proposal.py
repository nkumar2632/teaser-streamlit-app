from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.market_data import LocalHistory
from teaser_app.public_cfb import build_cfb_public_board, prepare_cfb_reference
from teaser_app.providers.espn import parse_scoreboard
from teaser_app.url_ingest import MarketPreview, identify_provider, save_confirmed_snapshot


NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
CAPTURED = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
KICKOFF = "2026-09-26T16:00:00+00:00"


def public_snapshot(history: LocalHistory) -> dict:
    games = (
        ("Alpha State", "Beta", "-2.5", "45.5"),
        ("Gamma", "Delta", "-1.5", "46.5"),
        ("Epsilon", "Zeta", "+7.5", "48.5"),
        ("Eta", "Theta", "+8.5", "50.5"),
    )
    events = []
    for index, (away, home, spread, total) in enumerate(games, 1):
        events.append({"source_event_id": f"espn-{index}", "away_team": away,
                       "home_team": home, "away_school": away, "home_school": home,
                       "kickoff": KICKOFF, "spread_home": spread,
                       "spread_away": str(-float(spread)), "total": total,
                       "event_state": "pre", "sportsbook": "DraftKings"})
    return history.save_confirmed_reference({
        "schema_version": 2, "kind": "market_snapshot", "league": "CFB",
        "season": 2026, "week": 4, "captured_at": CAPTURED.isoformat(),
        "source": "reference_url", "source_type": "url", "source_provider": "ESPN",
        "source_url": "https://www.espn.com/college-football/scoreboard?dates=20260926&groups=80",
        "market_role": "REFERENCE", "sportsbook": "DraftKings", "events": events,
    })


def test_espn_review_confirmation_feeds_cfb_paper_without_manual_sides(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "espn_cfb_scoreboard.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["events"][0]["competitions"][0]["competitors"][0]["team"]["location"] = "Tennessee"
    payload["events"][0]["competitions"][0]["competitors"][1]["team"]["location"] = "Texas"
    payload["events"][0]["status"] = {"type": {"state": "pre"}}
    rows, warnings = parse_scoreboard(json.dumps(payload).encode(), "CFB")
    preview = MarketPreview(
        identify_provider("https://www.espn.com/college-football/scoreboard?dates=20260926&groups=80"),
        CAPTURED.isoformat(), tuple(rows), tuple(warnings))
    edited = [dict(row) for row in preview.rows]
    edited[0].update(spread_home="+4.5", spread_away="-4.5")
    snapshot = save_confirmed_snapshot(preview, edited, history)
    assert snapshot["events"][0]["away_school"] == "Texas"
    assert snapshot["events"][0]["event_state"] == "pre"
    view, prepared, _ = build_cfb_public_board(history, adapter, snapshot, now=NOW)
    assert [side["team"] for side in prepared.slate["rows"]] == ["Texas", "Tennessee"]
    assert view.strategy.status == "PAPER"
    assert history.live_placements() == []


def test_public_cfb_snapshot_builds_paper_card_without_manual_sides(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    snapshot = public_snapshot(history)
    prepared = prepare_cfb_reference(snapshot, adapter, now=NOW)
    assert len(prepared.slate["rows"]) == 8
    assert prepared.slate["source"] == "reference_source"
    assert all(row["kickoff"].endswith("+00:00") for row in prepared.slate["rows"])
    for first, second in zip(prepared.slate["rows"][::2], prepared.slate["rows"][1::2]):
        assert first["team"] == first["away_team"]
        assert second["team"] == second["home_team"]
        assert float(first["spread"]) == -float(second["spread"])
    view, prepared, saved = build_cfb_public_board(history, adapter, snapshot, now=NOW)
    assert view.strategy.status == "PAPER" and view.run_id == saved["run_id"]
    assert view.snapshot_id == snapshot["snapshot_id"]
    assert view.qualifying_legs and view.tickets
    assert all(ticket["ev_per_unit"] == "UNAVAILABLE" for ticket in view.tickets)
    assert all(leg["track"] == "PAPER" for leg in view.legs)
    assert saved["actual_placements"] == saved["actual_units_staked"] == 0
    assert saved["board_kind"] == "cfb_paper_board"
    assert saved["lines_source"] == "ESPN/DraftKings"
    assert saved["menu_book"] == "bluecoins.ag"


def test_only_missing_menu_price_can_complete_priced_outputs(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    snapshot = public_snapshot(history)
    unpriced, _, _ = build_cfb_public_board(history, adapter, snapshot, now=NOW)
    priced, prepared, saved = build_cfb_public_board(
        history, adapter, snapshot, now=NOW,
        prices={"2": "+170", "3": ""})
    assert prepared.slate["prices"] == {"2": "+170", "3": ""}
    assert all(ticket["ev_per_unit"] != "UNAVAILABLE" for ticket in priced.tickets if ticket["n_legs"] == 2)
    assert all(ticket["ev_per_unit"] == "UNAVAILABLE" for ticket in priced.tickets if ticket["n_legs"] == 3)
    assert [(leg["leg_id"], leg["p_est"]) for leg in unpriced.legs] == [
        (leg["leg_id"], leg["p_est"]) for leg in priced.legs]
    assert saved["slate"]["prices"] == {"2": "+170", "3": ""}
    assert history.live_placements() == []


def test_source_ids_do_not_change_model_identity_or_order(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    first = public_snapshot(history)
    changed = deepcopy(first)
    changed["snapshot_id"] = "mkt_different"
    for index, event in enumerate(changed["events"], 1):
        event["source_event_id"] = f"another-provider-id-{index}"
    a = adapter.grade_cfb_paper(prepare_cfb_reference(first, adapter, now=NOW).slate)
    b = adapter.grade_cfb_paper(prepare_cfb_reference(changed, adapter, now=NOW).slate)
    assert [(leg["game_id"], leg["leg_id"], leg["p_est"]) for leg in a.legs] == [
        (leg["game_id"], leg["leg_id"], leg["p_est"]) for leg in b.legs]
    assert [ticket["ticket_key"] for ticket in a.tickets] == [
        ticket["ticket_key"] for ticket in b.tickets]


def test_invalid_duplicate_conflicting_and_started_games_are_excluded_per_game(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    snapshot = public_snapshot(history)
    changed = deepcopy(snapshot)
    changed["events"].append(deepcopy(changed["events"][0]))
    changed["events"][-1]["source_event_id"] = "duplicate"
    changed["events"][1]["spread_home"] = "-1.25"
    changed["events"][2]["event_state"] = "in"
    changed["events"][3]["kickoff"] = "2026-09-24T16:00:00+00:00"
    prepared = prepare_cfb_reference(changed, adapter, now=NOW)
    assert len(prepared.included) == 1
    assert {game["reason"] for game in prepared.excluded} >= {"DUPLICATE", "INVALID_VALUE", "STARTED"}
    assert len(adapter.grade_cfb_paper(prepared.slate).legs) == 2
    conflicting = deepcopy(snapshot)
    conflicting["events"].append(deepcopy(conflicting["events"][0]))
    conflicting["events"][-1]["spread_home"] = "-3.5"
    conflicting["events"][-1]["spread_away"] = "+3.5"
    assert any(game["reason"] == "CONFLICT" for game in
               prepare_cfb_reference(conflicting, adapter, now=NOW).excluded)


def test_cfb_limits_are_not_nfl_limits_and_manual_fallback_survives(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    snapshot = public_snapshot(history)
    changed = deepcopy(snapshot)
    changed["events"][0].update(spread_home="-35.5", spread_away="+35.5", total="81")
    prepared = prepare_cfb_reference(changed, adapter, now=NOW)
    assert len(prepared.included) == 4
    assert any(row["spread"] == "-35.5" and row["total"] == "81"
               for row in prepared.slate["rows"])
    from teaser_app.cfb_page import initial_cfb_slate
    manual = initial_cfb_slate()
    assert manual["source"] == "manual_sportsbook" and manual["rows"] == []


def test_cfb_public_streamlit_rerun_keeps_saved_board_and_no_placement(tmp_path, monkeypatch):
    import teaser_app.cfb_page as page
    import teaser_app.market_compare_page as compare_page
    import teaser_app.results_page as results_page
    import teaser_app.url_import as url_import

    history = LocalHistory(tmp_path)
    public_snapshot(history)
    for module in (page, compare_page, results_page, url_import):
        monkeypatch.setattr(module, "LocalHistory", lambda: history)
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    next(item for item in app.radio if item.label == "League and model track").set_value("CFB · PAPER").run()
    assert all(item.label != "Add CFB side" for item in app.button)
    assert all(item.label != "Build PAPER card from entered slate" for item in app.button)
    next(item for item in app.button if item.label == "Use this slate for proposal").click().run()
    next(item for item in app.text_input if item.label.startswith("Missing 2-team")).set_value("+300").run()
    next(item for item in app.text_input if item.label.startswith("Missing 3-team")).set_value("+600").run()
    next(item for item in app.button if item.label == "Build PAPER card from public slate").click().run()
    assert not app.exception
    runs = history.runs(status="PAPER")
    assert len(runs) == 1
    run_id = runs[0]["run_id"]
    assert any("PAPER — NOT REAL MONEY" in item.value for item in app.warning)
    assert app.session_state["cfb_view"].selected_tickets
    assert any("Lines: ESPN/DraftKings" in item.value and
               "Teaser pricing: bluecoins.ag" in item.value for item in app.caption)
    assert all("PLACE" not in item.label.upper() for item in app.button)
    app.run()
    assert not app.exception
    assert len(history.runs(status="PAPER")) == 1
    assert history.runs(status="PAPER")[0]["run_id"] == run_id
    next(item for item in app.radio if item.label == "CFB market source").set_value("Manual entry").run()
    assert any(item.label == "Build PAPER card from entered slate" for item in app.button)
    next(item for item in app.radio if item.label == "CFB market source").set_value("Public lines").run()
    assert any(item.label == "Build PAPER card from public slate" for item in app.button)
    assert len(history.runs(status="PAPER")) == 1
