"""Synthetic NFL snapshot-to-proposal and app-level execution boundaries."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.live_history import record_reported_placements
from teaser_app.market_data import LocalHistory, _save
from teaser_app.nfl_market import execution_book, execution_status, prepare_nfl_snapshot

CAPTURED = "2026-09-22T12:00:00+00:00"
KICKOFF = "2026-09-27T17:00:00+00:00"
NOW = datetime(2026, 9, 23, 20, tzinfo=timezone.utc)
APP = Path(__file__).resolve().parents[1] / "app.py"


def snapshot(history: LocalHistory, role: str, *, book: str = "bluecoins.ag",
             prices: dict | None = None, captured: str = CAPTURED) -> dict:
    games = (
        ("Jacksonville Jaguars", "Denver Broncos", "+2.5", "-2.5", "44.5"),
        ("Atlanta Falcons", "Carolina Panthers", "+2.5", "-2.5", "43.5"),
        ("Tampa Bay Buccaneers", "Cleveland Browns", "-8.5", "+8.5", "41.5"),
        ("New York Giants", "Los Angeles Rams", "+2.5", "-2.5", "46.5"),
    )
    events = []
    for index, (away, home, away_spread, home_spread, total) in enumerate(games, 1):
        events.append({"source_event_id": f"provider-{index}", "away_team": away,
                       "home_team": home, "kickoff": KICKOFF,
                       "spread_away": away_spread, "spread_home": home_spread,
                       "total": total, "event_state": "pre", "sportsbook": book,
                       "market_role": role, "source_type": "url" if role == "REFERENCE" else "screenshot"})
    payload = {"schema_version": 2 if role == "REFERENCE" else 3,
               "kind": "market_snapshot", "league": "NFL", "season": 2026, "week": 3,
               "captured_at": captured, "source": "reference_url" if role == "REFERENCE" else "user_screenshot",
               "source_type": "url" if role == "REFERENCE" else "screenshot",
               "source_provider": "ESPN" if role == "REFERENCE" else "Apple Vision",
               "source_url": "https://www.espn.com/nfl/scoreboard?dates=20260927" if role == "REFERENCE" else None,
               "market_role": role, "sportsbook": book, "events": events}
    if role == "EXECUTION":
        payload["teaser_prices"] = {"6_point": {"2_team": (prices or {}).get("2"),
                                                   "3_team": (prices or {}).get("3")}}
        return history.save_confirmed_execution(payload)
    return history.save_confirmed_reference(payload)


def test_reference_and_execution_build_without_manual_sides_with_equal_model_fields(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    reference = snapshot(history, "REFERENCE", book="DraftKings")
    execution = snapshot(history, "EXECUTION", prices={"2": "+170", "3": "+170"})
    ref = prepare_nfl_snapshot(reference, adapter, now=NOW,
                               prices={"2": "+170", "3": "+170"}, menu_book="bluecoins.ag")
    exe = prepare_nfl_snapshot(execution, adapter, now=NOW)
    assert len(ref.slate["rows"]) == len(exe.slate["rows"]) == 8
    assert all(row["team"] in adapter.teams for row in ref.slate["rows"])
    assert ref.slate["price_sportsbook"] == "bluecoins.ag"
    assert ref.slate["sportsbook"] == "DraftKings"
    assert exe.slate["sportsbook"] == exe.slate["price_sportsbook"] == "bluecoins.ag"
    assert adapter._snapshots(ref.slate)[1].sportsbook == "bluecoins.ag"
    a, b = adapter.grade(ref.slate), adapter.grade(exe.slate)
    assert [(leg["game_id"], leg["leg_id"], leg["p_est"]) for leg in a.qualifying_legs] == [
        (leg["game_id"], leg["leg_id"], leg["p_est"]) for leg in b.qualifying_legs]
    assert [(ticket["ticket_key"], ticket["p_ticket"], ticket["ev_per_unit"], ticket["selected"])
            for ticket in a.tickets] == [
        (ticket["ticket_key"], ticket["p_ticket"], ticket["ev_per_unit"], ticket["selected"])
        for ticket in b.tickets]
    assert a.selected_tickets and b.selected_tickets
    assert history.live_placements() == []
    assert ref.context["role"] == "REFERENCE" and not ref.context["confirmed_execution"]
    assert exe.context["role"] == "EXECUTION" and exe.context["confirmed_execution"]


def test_missing_teaser_prices_keep_grading_and_only_missing_size_can_be_added(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    source = snapshot(history, "EXECUTION", prices={"2": "+170"})
    unpriced = prepare_nfl_snapshot(source, adapter, now=NOW)
    base = adapter.grade(unpriced.slate)
    assert base.qualifying_legs and base.tickets
    assert all(t["ev_per_unit"] == "UNAVAILABLE" for t in base.tickets if t["n_legs"] == 3)
    updated = prepare_nfl_snapshot(source, adapter, now=NOW,
                                   prices={"2": "", "3": "+170"})
    priced = adapter.grade(updated.slate)
    assert updated.slate["prices"] == {"2": "+170", "3": "+170"}
    assert all(t["ev_per_unit"] != "UNAVAILABLE" for t in priced.tickets if t["n_legs"] == 3)
    assert [(leg["leg_id"], leg["p_est"]) for leg in base.qualifying_legs] == [
        (leg["leg_id"], leg["p_est"]) for leg in priced.qualifying_legs]
    assert updated.context["menu_observed_at"]["3"] is None


def test_aliases_and_provider_identifiers_do_not_change_model_game_identity(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    first = snapshot(history, "EXECUTION", prices={"2": "+170"})
    changed = deepcopy(first)
    changed["snapshot_id"] = "mkt_other"
    changed["events"][3]["home_team"] = "LAR"
    changed["events"][3]["away_team"] = "NYG"
    changed["events"][3]["source_event_id"] = "screenshot-other"
    changed["events"][0]["away_team"] = "JAC"
    changed["events"][0]["source_event_id"] = "espn-other"
    a = adapter.grade(prepare_nfl_snapshot(first, adapter, now=NOW).slate)
    b = adapter.grade(prepare_nfl_snapshot(changed, adapter, now=NOW).slate)
    assert [leg["game_id"] for leg in a.qualifying_legs] == [
        leg["game_id"] for leg in b.qualifying_legs]
    assert any("_NYG_LA" in leg["game_id"] for leg in b.qualifying_legs)
    assert [ticket["ticket_key"] for ticket in a.tickets] == [
        ticket["ticket_key"] for ticket in b.tickets]
    assert adapter.canonical_team("WSH") == "WAS"
    washington = deepcopy(first)
    washington["events"][0]["away_team"] = "Washington Commanders"
    washington_alias = deepcopy(washington)
    washington_alias["events"][0]["away_team"] = "WSH"
    full = adapter.grade(prepare_nfl_snapshot(washington, adapter, now=NOW).slate)
    alias = adapter.grade(prepare_nfl_snapshot(washington_alias, adapter, now=NOW).slate)
    assert [leg["game_id"] for leg in full.qualifying_legs] == [
        leg["game_id"] for leg in alias.qualifying_legs]


def test_duplicate_invalid_started_and_conflicting_games_are_isolated(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    source = snapshot(history, "REFERENCE")
    changed = deepcopy(source)
    changed["events"].append(deepcopy(changed["events"][0]))
    changed["events"][-1]["source_event_id"] = "duplicate"
    changed["events"][1]["total"] = "100"
    changed["events"][2]["kickoff"] = "2026-09-22T11:00:00+00:00"
    prepared = prepare_nfl_snapshot(changed, adapter, now=NOW)
    assert len(prepared.included) == 2
    assert {item["reason"] for item in prepared.excluded} >= {"DUPLICATE", "INVALID_VALUE", "STARTED"}
    assert len(adapter.grade(prepared.slate).research) == 4
    conflicting = deepcopy(source)
    conflicting["events"].append(deepcopy(conflicting["events"][0]))
    conflicting["events"][-1]["spread_home"] = "-3.5"
    conflicting["events"][-1]["spread_away"] = "+3.5"
    assert any(item["reason"] == "CONFLICT" for item in
               prepare_nfl_snapshot(conflicting, adapter, now=NOW).excluded)
    naive = deepcopy(source)
    naive["events"][0]["kickoff"] = "2026-09-27T13:00:00"
    assert any(item["reason"] == "INVALID_VALUE" for item in
               prepare_nfl_snapshot(naive, adapter, now=NOW).excluded)


def test_nfl_input_order_invalid_extra_and_missing_prices_leave_leg_model_fields_unchanged(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    source = snapshot(history, "REFERENCE")
    priced = adapter.grade(prepare_nfl_snapshot(source, adapter, now=NOW,
                            prices={"2": "+170", "3": "-110"}).slate)
    reordered = deepcopy(source)
    reordered["events"].reverse()
    reordered["source_provider"] = "Another reference provider"
    reordered["events"].append({"away_team": "Arizona Cardinals", "home_team": "Seattle Seahawks",
                                 "kickoff": KICKOFF, "spread_home": "-3.5", "total": "not-a-total"})
    prepared = prepare_nfl_snapshot(reordered, adapter, now=NOW)
    assert len(prepared.included) == len(source["events"])
    assert len(prepared.excluded) == 1 and prepared.excluded[0]["reason"] == "INVALID_VALUE"
    unpriced = adapter.grade(prepared.slate)
    priced_legs = [(leg["game_id"], leg["leg_id"], leg["rank"], leg["p_est"])
                   for leg in priced.qualifying_legs]
    assert priced_legs == [(leg["game_id"], leg["leg_id"], leg["rank"], leg["p_est"])
                           for leg in unpriced.qualifying_legs]
    assert sorted((ticket["ticket_key"], ticket["p_ticket"]) for ticket in priced.tickets) == sorted(
        (ticket["ticket_key"], ticket["p_ticket"]) for ticket in unpriced.tickets)
    assert any(ticket["ev_per_unit"] == "UNAVAILABLE" for ticket in unpriced.tickets)


def test_legacy_screenshot_book_is_read_without_rewriting_record(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    source = snapshot(history, "EXECUTION", prices={"2": "+170"})
    legacy = deepcopy(source)
    legacy["sportsbook"] = "USER_SPORTSBOOK_SCREENSHOT"
    legacy["source_reference"] = "sportsbook: bluecoins.ag"
    legacy["schema_version"] = 2
    legacy["source_type"] = "screenshot_transcription"
    legacy.pop("snapshot_id")
    saved = _save(history.root / "normalized", "mkt", legacy)
    path = history.root / "normalized" / f"{saved['snapshot_id']}.json"
    original_bytes = path.read_bytes()
    loaded = history.get_snapshot(saved["snapshot_id"])
    prepared = prepare_nfl_snapshot(loaded, adapter, now=NOW)
    assert execution_book(loaded) == "bluecoins.ag"
    assert prepared.slate["sportsbook"] == "bluecoins.ag"
    assert not prepared.context["confirmed_execution"]
    assert path.read_bytes() == original_bytes
    loaded.pop("source_reference")
    assert execution_book(loaded) == "UNKNOWN"


def test_execution_gate_requires_confirmed_book_match_fresh_recheck_and_explicit_menu_verification(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    first = snapshot(history, "EXECUTION", prices={"2": "+170", "3": "+170"})
    original = prepare_nfl_snapshot(first, adapter, now=NOW)
    view = adapter.grade(original.slate)
    graded_at = datetime.fromisoformat(view.graded_at)
    capture_time = graded_at + timedelta(seconds=1)
    rechecked_at = graded_at + timedelta(seconds=5)
    review_time = graded_at + timedelta(seconds=10)
    recheck_record = snapshot(history, "EXECUTION", prices={"3": "+170"},
                              captured=capture_time.isoformat())
    recheck = prepare_nfl_snapshot(recheck_record, adapter, now=review_time,
                                   prices={"2": "+170", "3": ""})
    verdict = SimpleNamespace(verdict="VALIDATED", original_card_id=view.card_id,
                              rechecked_at=rechecked_at.isoformat())
    allowed, reason = execution_status(original.context, recheck.context, view, verdict,
                                       recheck.slate, now=review_time)
    assert not allowed and "2-team" in reason
    verification = {"snapshot_id": recheck_record["snapshot_id"], "book": "bluecoins.ag",
                    "prices": {"2": "+170", "3": "+170"},
                    "observed_at": (graded_at + timedelta(seconds=2)).isoformat()}
    verified = prepare_nfl_snapshot(recheck_record, adapter, now=review_time,
                                    prices={"2": "+170", "3": ""},
                                    menu_verification=verification)
    assert verified.slate["price_captured_at"] == verification["observed_at"]
    assert adapter._snapshots(verified.slate)[1].label == "OPERATOR_RECONFIRMED"
    assert adapter._snapshots(verified.slate)[1].snapshot_id != adapter._snapshots(recheck.slate)[1].snapshot_id
    assert execution_status(original.context, verified.context, view, verdict,
                            verified.slate, now=review_time)[0]
    mismatch = prepare_nfl_snapshot(recheck_record, adapter, now=review_time,
                                    prices={"2": "+170", "3": ""}, menu_book="other book")
    assert not execution_status(original.context, mismatch.context, view, verdict,
                                mismatch.slate, now=review_time)[0]
    assert not execution_status(original.context, original.context, view, verdict,
                                original.slate, now=review_time)[0]
    assert not execution_status(None, verified.context, view, verdict,
                                verified.slate, now=review_time)[0]
    premature = deepcopy(recheck.context)
    premature["captured_at"] = view.graded_at
    assert "postdate" in execution_status(original.context, premature, view, verdict,
                                          recheck.slate, now=review_time)[1]
    old_board_new_menu = deepcopy(verified.context)
    old_board_new_menu["menu_observed_at"] = {
        "2": (graded_at + timedelta(hours=1)).isoformat(),
        "3": (graded_at + timedelta(hours=1)).isoformat(),
    }
    late_check = SimpleNamespace(verdict="VALIDATED", original_card_id=view.card_id,
                                 rechecked_at=(graded_at + timedelta(hours=1, seconds=5)).isoformat())
    allowed, reason = execution_status(original.context, old_board_new_menu, view, late_check,
                                       verified.slate, now=graded_at + timedelta(hours=1, seconds=10))
    assert not allowed and "market" in reason.lower()
    assert history.live_placements() == []


def test_execution_status_fails_closed_across_invalid_state_combinations(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    source = snapshot(history, "EXECUTION", prices={"2": "+170", "3": "+170"})
    original = prepare_nfl_snapshot(source, adapter, now=NOW)
    view = adapter.grade(original.slate)
    graded = datetime.fromisoformat(view.graded_at)
    capture = graded + timedelta(seconds=1)
    current = snapshot(history, "EXECUTION", prices={"2": "+170", "3": "+170"},
                       captured=capture.isoformat())
    verification = {"snapshot_id": current["snapshot_id"], "book": "bluecoins.ag",
                    "prices": {"2": "+170", "3": "+170"},
                    "observed_at": (graded + timedelta(seconds=2)).isoformat()}
    prepared = prepare_nfl_snapshot(current, adapter, now=graded + timedelta(seconds=10),
                                    menu_verification=verification)
    verdict = SimpleNamespace(verdict="VALIDATED", original_card_id=view.card_id,
                              rechecked_at=(graded + timedelta(seconds=5)).isoformat())
    assert execution_status(original.context, prepared.context, view, verdict, prepared.slate,
                            now=graded + timedelta(seconds=10))[0]

    reference = deepcopy(original.context)
    reference.update(role="REFERENCE", confirmed_execution=False)
    unknown = deepcopy(original.context)
    unknown["line_book"] = "UNKNOWN"
    mixed = deepcopy(original.context)
    mixed["menu_book"] = "other book"
    same = deepcopy(prepared.context)
    same["snapshot_id"] = original.context["snapshot_id"]
    older = deepcopy(prepared.context)
    older["captured_at"] = (datetime.fromisoformat(original.context["captured_at"])
                             - timedelta(seconds=1)).isoformat()
    wrong_recheck_book = deepcopy(prepared.context)
    wrong_recheck_book.update(line_book="other book", menu_book="other book")
    stale_menu = deepcopy(prepared.context)
    stale_menu["menu_observed_at"] = {"2": (graded - timedelta(hours=1)).isoformat(),
                                       "3": (graded - timedelta(hours=1)).isoformat()}
    cases = [
        (None, prepared.context, verdict, prepared.slate, graded + timedelta(seconds=10)),
        (reference, prepared.context, verdict, prepared.slate, graded + timedelta(seconds=10)),
        (unknown, prepared.context, verdict, prepared.slate, graded + timedelta(seconds=10)),
        (mixed, prepared.context, verdict, prepared.slate, graded + timedelta(seconds=10)),
        (original.context, same, verdict, prepared.slate, graded + timedelta(seconds=10)),
        (original.context, older, verdict, prepared.slate, graded + timedelta(seconds=10)),
        (original.context, wrong_recheck_book, verdict, prepared.slate, graded + timedelta(seconds=10)),
        (original.context, stale_menu, verdict, prepared.slate, graded + timedelta(seconds=10)),
        (original.context, prepared.context, None, prepared.slate, graded + timedelta(seconds=10)),
        (original.context, prepared.context, verdict, prepared.slate, graded + timedelta(minutes=31)),
    ]
    assert all(not execution_status(card, recheck, view, check, slate, now=instant)[0]
               for card, recheck, check, slate, instant in cases)

    after_kickoff = deepcopy(prepared.context)
    after_kickoff["captured_at"] = "2026-09-27T16:50:00+00:00"
    after_kickoff["menu_observed_at"] = {"2": "2026-09-27T16:55:00+00:00",
                                          "3": "2026-09-27T16:55:00+00:00"}
    late_verdict = SimpleNamespace(verdict="VALIDATED", original_card_id=view.card_id,
                                   rechecked_at="2026-09-27T16:55:00+00:00")
    allowed, reason = execution_status(original.context, after_kickoff, view, late_verdict,
                                       prepared.slate, now=datetime(2026, 9, 27, 17, 1,
                                                                    tzinfo=timezone.utc))
    assert not allowed and "started" in reason.lower()


def test_explicit_menu_verification_is_append_only_and_rejects_bad_american_odds(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    source = snapshot(history, "EXECUTION", prices={"2": "+170", "3": "+170"})
    path = history.root / "normalized" / f"{source['snapshot_id']}.json"
    original_bytes = path.read_bytes()
    assert not (history.root / "menu_verifications").exists()
    for bad in ("0", "50", "abc", "1.0", "-99"):
        with pytest.raises(ValueError, match="American odds"):
            history.save_menu_verification(snapshot_id=source["snapshot_id"],
                                           sportsbook="bluecoins.ag",
                                           prices={"2": bad, "3": "+170"},
                                           observed_at="2026-09-23T19:58:00+00:00")
    assert not (history.root / "menu_verifications").exists()
    saved = history.save_menu_verification(snapshot_id=source["snapshot_id"],
                                           sportsbook="bluecoins.ag",
                                           prices={"2": "+170", "3": "+170"},
                                           observed_at="2026-09-23T19:58:00+00:00")
    assert saved["source_snapshot_id"] == source["snapshot_id"]
    assert saved["sportsbook"] == "bluecoins.ag"
    assert saved["prices"] == {"2": "+170", "3": "+170"}
    assert saved["verification_id"].startswith("mnu_")
    assert path.read_bytes() == original_bytes
    original = prepare_nfl_snapshot(source, adapter, now=NOW)
    verified = prepare_nfl_snapshot(source, adapter, now=NOW,
                                    menu_verification={"snapshot_id": source["snapshot_id"],
                                                       "book": saved["sportsbook"],
                                                       "prices": saved["prices"],
                                                       "observed_at": saved["observed_at"],
                                                       "verification_id": saved["verification_id"]})
    assert verified.slate["price_reconfirmation_id"] == saved["verification_id"]
    assert verified.context["menu_observed_at"] == {"2": saved["observed_at"],
                                                    "3": saved["observed_at"]}
    assert adapter._snapshots(verified.slate)[1].snapshot_id != adapter._snapshots(original.slate)[1].snapshot_id


def test_operator_reports_use_frozen_cumulative_weekly_exposure_cap():
    adapter = TeaserModelAdapter()
    existing = [{"season": 2026, "week": 3, "stake_units": 1,
                 "leg_ids": ["2026_03_JAX_DEN:JAX", "2026_03_ATL_CAR:ATL"]}]
    proposed = [{"season": 2026, "week": 3, "stake_units": 1,
                 "leg_ids": ["2026_03_JAX_DEN:JAX", "2026_03_TB_CLE:TB"]}]
    adapter.validate_live_exposure(existing, proposed, season=2026, week=3)
    adapter.validate_live_exposure([], proposed + proposed, season=2026, week=3)
    with pytest.raises(ValueError, match="exposure cap"):
        adapter.validate_live_exposure(existing, proposed + proposed, season=2026, week=3)
    adapter.validate_live_exposure(existing, proposed + proposed, season=2026, week=4)
    adapter.validate_live_exposure(existing, proposed + proposed, season=2027, week=3)
    other_identity = [{"season": 2026, "week": 3, "stake_units": 1,
                       "leg_ids": ["2026_03_OTHER_MATCH:JAX"]}]
    adapter.validate_live_exposure(existing, other_identity + other_identity,
                                   season=2026, week=3)


def test_weekly_exposure_survives_restart_settlement_and_duplicate_reports(tmp_path):
    history = LocalHistory(tmp_path)
    first = _save(history.root / "placements", "plc", {
        "kind": "operator_reported_placement", "card_id": "card_one",
        "ticket_key": "ticket_one", "season": 2026, "week": 3,
        "stake_units": 1, "leg_ids": ["2026_03_JAX_DEN:JAX"],
        "placed_at": "2026-09-27T12:00:00+00:00"})
    assert len(LocalHistory(tmp_path).live_placements()) == 1
    history.save_live_settlement({"kind": "operator_confirmed_settlement",
                                  "placement_id": first["placement_id"],
                                  "settled_at": "2026-09-28T12:00:00+00:00",
                                  "book_settlement": "WIN", "profit_loss_units": "0.5"})
    _save(history.root / "placements", "plc", {
        "kind": "operator_reported_placement", "card_id": "card_two",
        "ticket_key": "ticket_two", "season": 2026, "week": 3,
        "stake_units": 1, "leg_ids": ["2026_03_JAX_DEN:JAX"],
        "placed_at": "2026-09-27T12:05:00+00:00"})
    restarted = LocalHistory(tmp_path)
    prior = restarted.live_placements()
    assert len(prior) == 2
    candidate = [{"season": 2026, "week": 3, "stake_units": 1,
                  "leg_ids": ["2026_03_JAX_DEN:JAX"]}]
    with pytest.raises(ValueError, match="exposure cap"):
        TeaserModelAdapter().validate_live_exposure(prior, candidate, season=2026, week=3)
    assert len(LocalHistory(tmp_path).live_placements()) == 2


@pytest.mark.parametrize("settlement", ["WIN", "LOSS", "PUSH", "VOID"])
def test_settlement_status_never_restores_consumed_weekly_exposure(tmp_path, settlement):
    history = LocalHistory(tmp_path)
    saved = []
    for index in (1, 2):
        saved.append(_save(history.root / "placements", "plc", {
            "kind": "operator_reported_placement", "card_id": f"card_{index}",
            "ticket_key": f"ticket_{index}", "season": 2026, "week": 3,
            "stake_units": 1, "leg_ids": ["2026_03_JAX_DEN:JAX"],
            "placed_at": f"2026-09-27T12:0{index}:00+00:00"}))
    history.save_live_settlement({"kind": "operator_confirmed_settlement",
                                  "placement_id": saved[0]["placement_id"],
                                  "settled_at": "2026-09-28T12:00:00+00:00",
                                  "book_settlement": settlement,
                                  "profit_loss_units": "0"})
    prior = LocalHistory(tmp_path).live_placements()
    with pytest.raises(ValueError, match="exposure cap"):
        TeaserModelAdapter().validate_live_exposure(
            prior, [{"season": 2026, "week": 3, "stake_units": 1,
                     "leg_ids": ["2026_03_JAX_DEN:JAX"]}], season=2026, week=3)


def test_cfb_paper_run_never_consumes_nfl_exposure(tmp_path):
    history = LocalHistory(tmp_path)
    history.save_run({"kind": "model_run", "strategy": {
        "league": "CFB", "bet_type": "TEASER", "model_version": "teaser_v1.0", "status": "PAPER"},
        "season": 2026, "week": 3, "captured_at": "2026-09-27T10:00:00+00:00",
        "hypothetical_exposure": {"2026_03_JAX_DEN:JAX": 2}})
    assert history.live_placements() == []
    TeaserModelAdapter().validate_live_exposure(
        history.live_placements(), [{"season": 2026, "week": 3, "stake_units": 1,
                                     "leg_ids": ["2026_03_JAX_DEN:JAX"]}],
        season=2026, week=3)


def test_manual_or_reference_handler_calls_cannot_create_placements(tmp_path):
    history, adapter = LocalHistory(tmp_path), TeaserModelAdapter()
    slate, view = adapter.week2_example()
    recheck = SimpleNamespace(verdict="VALIDATED", original_card_id=view.card_id,
                              rechecked_at=slate["captured_at"],
                              new_market_snapshot_id="new", new_price_snapshot_id="price")
    with pytest.raises(ValueError, match="confirmed execution"):
        record_reported_placements(history, view, slate, slate, recheck,
                                   [view.selected_ticket_keys[0]], slate["captured_at"], adapter)
    assert history.live_placements() == [] and history.runs() == []


def test_streamlit_saved_nfl_reference_and_execution_builds_require_no_manual_sides(tmp_path, monkeypatch):
    import teaser_app.nfl_market_page as page

    history = LocalHistory(tmp_path)
    reference = snapshot(history, "REFERENCE", book="DraftKings")
    execution = snapshot(history, "EXECUTION", prices={"2": "+170", "3": "+170"})
    monkeypatch.setattr(page, "LocalHistory", lambda: history)
    app = AppTest.from_file(APP, default_timeout=30).run()
    next(item for item in app.radio if item.label == "NFL input path").set_value("Saved market snapshot").run()
    chooser = next(item for item in app.selectbox if item.label == "Saved NFL market snapshot")
    chooser.set_value(reference["snapshot_id"]).run()
    next(item for item in app.button if item.label == "Use this slate for proposal").click().run()
    assert not any(item.label == "Add side" for item in app.button)
    next(item for item in app.button if item.label == "Build proposal from saved NFL slate").click().run()
    assert not app.exception
    first_card_id = app.session_state["card"].card_id
    app.run()
    assert app.session_state["card"].card_id == first_card_id
    next(item for item in app.button if item.label == "Build proposal from saved NFL slate").click().run()
    assert app.session_state["card"].card_id == first_card_id
    assert any("REFERENCE SCREENING — NOT PLACEABLE" in item.value for item in app.warning)
    assert all(item.label != "Record operator-reported PLACED status" for item in app.button)
    assert history.live_placements() == []
    chooser = next(item for item in app.selectbox if item.label == "Saved NFL market snapshot")
    chooser.set_value(execution["snapshot_id"]).run()
    next(item for item in app.button if item.label == "Use this slate for proposal").click().run()
    next(item for item in app.button if item.label == "Build proposal from saved NFL slate").click().run()
    assert not app.exception
    assert app.session_state["nfl_card_context"]["confirmed_execution"]
    assert any("SHADOW — NOT PLACED" in item.value for item in app.markdown)
    assert history.live_placements() == []


def test_streamlit_execution_placement_form_waits_for_new_confirmed_recheck(tmp_path, monkeypatch):
    import teaser_app.nfl_market_page as page
    import teaser_app.market_data as market_data

    history = LocalHistory(tmp_path)
    first = snapshot(history, "EXECUTION", prices={"2": "+170", "3": "+170"})
    monkeypatch.setattr(page, "LocalHistory", lambda: history)
    monkeypatch.setattr(market_data, "LocalHistory", lambda: history)
    app = AppTest.from_file(APP, default_timeout=30).run()
    next(item for item in app.radio if item.label == "NFL input path").set_value("Saved market snapshot").run()
    chooser = next(item for item in app.selectbox if item.label == "Saved NFL market snapshot")
    chooser.set_value(first["snapshot_id"]).run()
    next(item for item in app.button if item.label == "Use this slate for proposal").click().run()
    next(item for item in app.button if item.label == "Build proposal from saved NFL slate").click().run()
    assert not any(item.label == "Record operator-reported PLACED status" for item in app.button)
    second = snapshot(history, "EXECUTION", prices={"2": "+170", "3": "+170"},
                      captured=datetime.now(timezone.utc).isoformat())
    app.run()
    chooser = next(item for item in app.selectbox if item.label == "Saved NFL market snapshot")
    chooser.set_value(second["snapshot_id"]).run()
    next(item for item in app.button if item.label == "Use this EXECUTION slate for recheck").click().run()
    recheck = next(item for item in app.button if item.label == "Recheck selected card against current input")
    assert not recheck.disabled
    recheck.click().run()
    assert not app.exception
    assert app.session_state["recheck"].verdict == "VALIDATED"
    assert any(item.label == "Record operator-reported PLACED status" for item in app.button)
    assert history.live_placements() == []
    mismatched = deepcopy(app.session_state["slate"])
    mismatched["price_sportsbook"] = "another book"
    with pytest.raises(ValueError, match="EXECUTION"):
        record_reported_placements(history, app.session_state["card"],
                                   app.session_state["card_slate"], mismatched,
                                   app.session_state["recheck"],
                                   [app.session_state["card"].selected_ticket_keys[0]],
                                   datetime.now(timezone.utc).isoformat(),
                                   app.session_state["adapter"])
    tickets = next(item for item in app.multiselect if item.label == "Tickets already placed")
    tickets.set_value([tickets.options[0]]).run()
    next(item for item in app.checkbox if item.label.startswith("I confirm these tickets")).set_value(True).run()
    next(item for item in app.button if item.label == "Record operator-reported PLACED status").click().run()
    assert not app.exception
    placements = history.live_placements()
    assert len(placements) == 1, [item.value for item in app.error]
    assert placements[0]["confirmed_execution_snapshot_id"] == first["snapshot_id"]
    assert placements[0]["recheck_confirmed_execution_snapshot_id"] == second["snapshot_id"]
