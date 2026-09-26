"""NFL LIVE operator workflow: one-click proposal, active baseline, recheck, restart, guards."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import teaser_app.adapter as adapter_module
import teaser_app.nfl_workflow as workflow
from teaser_app.adapter import TeaserModelAdapter
from teaser_app.market_data import LocalHistory
from teaser_app.nfl_market import execution_status, prepare_nfl_snapshot
from teaser_app.nfl_workflow import (DEFAULT_TEASER_MENU, activate_proposal, active_proposal,
                                     build_active_proposal, infer_nfl_week, recheck_active_proposal,
                                     recheck_candidate, restore_active_proposal)

APP = Path(__file__).resolve().parents[1] / "app.py"
GAMES = (("Jacksonville Jaguars", "Denver Broncos", "+2.5", "-2.5", "44.5"),
         ("Atlanta Falcons", "Carolina Panthers", "+2.5", "-2.5", "43.5"),
         ("Tampa Bay Buccaneers", "Cleveland Browns", "-8.5", "+8.5", "41.5"),
         ("New York Giants", "Los Angeles Rams", "+2.5", "-2.5", "46.5"))


@pytest.fixture
def clock(monkeypatch):
    """Whole-second times relative to the real clock the gate reads, strictly ordered:
    baseline capture < grading < newer capture < recheck < now < kickoff."""
    anchor = datetime.now(timezone.utc).replace(microsecond=0)
    times = {"anchor": anchor, "baseline": anchor - timedelta(minutes=20),
             "graded": anchor - timedelta(minutes=15), "newer": anchor - timedelta(minutes=10),
             "rechecked": anchor - timedelta(minutes=5), "kickoff": anchor + timedelta(days=3)}
    real_grade, real_recheck = adapter_module.grade_week, adapter_module.recheck_card
    monkeypatch.setattr(adapter_module, "grade_week", lambda market, prices, **kw:
                        real_grade(market, prices, **{"graded_at": times["graded"], **kw}))
    monkeypatch.setattr(adapter_module, "recheck_card", lambda card, market, prices, **kw:
                        real_recheck(card, market, prices, **{"rechecked_at": times["rechecked"], **kw}))
    # Week inference is tested on fixed dates below; here it must not depend on the calendar.
    import teaser_app.nfl_market_page as page
    monkeypatch.setattr(workflow, "infer_nfl_week", lambda snapshot: (2026, 4))
    monkeypatch.setattr(page, "infer_nfl_week", lambda snapshot: (2026, 4))
    return times


def execution(history, captured: datetime, kickoff: datetime, *, book="bluecoins.ag", league="NFL",
              prices=(None, None)) -> dict:
    events = [{"source_event_id": f"row-{index}", "away_team": away, "home_team": home,
               "kickoff": kickoff.isoformat(), "spread_away": sa, "spread_home": sh, "total": total,
               "event_state": "pre", "sportsbook": book, "market_role": "EXECUTION", "source_type": "screenshot"}
              for index, (away, home, sa, sh, total) in enumerate(GAMES, 1)]
    return history.save_confirmed_execution({
        "schema_version": 3, "kind": "market_snapshot", "league": league, "season": None, "week": None,
        "captured_at": captured.isoformat(), "source": "user_screenshot", "source_type": "screenshot",
        "source_provider": "apple_vision_local", "market_role": "EXECUTION", "sportsbook": book,
        "teaser_prices": {"6_point": {"2_team": prices[0], "3_team": prices[1]}}, "events": events})


def gate(view, active, recheck, prepared, now):
    return execution_status(workflow.active_context(active), prepared.context, view, recheck,
                            prepared.slate, now=now)


def test_confirmed_screenshot_builds_the_active_proposal_with_default_menu_not_verified(tmp_path, clock):
    history, adapter = LocalHistory(tmp_path), TeaserModelAdapter()
    snapshot = execution(history, clock["baseline"], clock["kickoff"])
    view, prepared, record = build_active_proposal(history, adapter, snapshot, now=clock["anchor"])
    assert active_proposal(history)["proposal_id"] == record["proposal_id"]
    assert (record["season"], record["week"]) == (2026, 4)
    assert record["graded_at"] == clock["graded"].isoformat() == view.graded_at
    assert prepared.slate["prices"] == DEFAULT_TEASER_MENU and prepared.slate["price_sportsbook"] == "bluecoins.ag"
    # Defaults price the proposal but are never an observed quote.
    assert prepared.context["menu_observed_at"] == {"2": None, "3": None}
    assert view.selected_tickets and history.live_placements() == []


def test_newer_screenshot_rechecks_and_explicit_verification_opens_the_gate(tmp_path, clock):
    history, adapter = LocalHistory(tmp_path), TeaserModelAdapter()
    view, _, active = build_active_proposal(history, adapter, execution(history, clock["baseline"], clock["kickoff"]),
                                            now=clock["anchor"])
    newer = execution(history, clock["newer"], clock["kickoff"])
    assert recheck_candidate(active, newer)[0]
    recheck, prepared, _ = recheck_active_proposal(history, adapter, view, active, newer, now=clock["anchor"])
    assert recheck.verdict == "VALIDATED"
    executable, reason = gate(view, active, recheck, prepared, datetime.now(timezone.utc))
    # The gate checks each selected ticket size; which size is reported first is not significant.
    assert not executable and reason.endswith("teaser menu needs explicit current verification")
    recheck, prepared, verification = recheck_active_proposal(history, adapter, view, active, newer,
                                                              now=datetime.now(timezone.utc), verify_menu=True)
    assert verification["prices"] == DEFAULT_TEASER_MENU
    assert gate(view, active, recheck, prepared, datetime.now(timezone.utc)) == (
        True, "Confirmed sportsbook EXECUTION slate and fresh validated recheck")
    assert history.live_placements() == []  # a report is still a separate explicit operator action


def test_active_proposal_survives_restart_with_original_grading_time(tmp_path, clock):
    history = LocalHistory(tmp_path)
    view, _, active = build_active_proposal(history, TeaserModelAdapter(),
                                            execution(history, clock["baseline"], clock["kickoff"]), now=clock["anchor"])
    clock["graded"] = clock["anchor"]  # a naive rebuild after restart would be graded now
    restarted = TeaserModelAdapter()
    restored, record = restore_active_proposal(history, restarted)
    assert (restored.card_id, restored.graded_at) == (view.card_id, active["graded_at"])
    recheck, _, _ = recheck_active_proposal(history, restarted, restored, record,
                                            execution(history, clock["newer"], clock["kickoff"]), now=clock["anchor"])
    assert recheck.verdict == "VALIDATED" and recheck.original_card_id == view.card_id


def test_older_same_or_other_book_snapshots_cannot_recheck(tmp_path, clock):
    history, adapter = LocalHistory(tmp_path), TeaserModelAdapter()
    baseline = execution(history, clock["baseline"], clock["kickoff"])
    view, _, active = build_active_proposal(history, adapter, baseline, now=clock["anchor"])
    older = execution(history, clock["baseline"] - timedelta(minutes=5), clock["kickoff"])
    before_grading = execution(history, clock["graded"] - timedelta(seconds=30), clock["kickoff"])
    other_book = execution(history, clock["newer"], clock["kickoff"], book="other.book")
    reference = history.save_confirmed_reference({
        "schema_version": 2, "kind": "market_snapshot", "league": "NFL", "season": 2026, "week": 4,
        "captured_at": clock["newer"].isoformat(), "source": "reference_url", "source_type": "url",
        "source_provider": "ESPN", "source_url": "https://www.espn.com/nfl/scoreboard", "market_role": "REFERENCE",
        "sportsbook": "DraftKings", "events": [{**event, "market_role": "REFERENCE", "source_type": "url"}
                                               for event in baseline["events"]]})
    expected = {baseline["snapshot_id"]: "baseline screenshot", older["snapshot_id"]: "not newer",
                before_grading["snapshot_id"]: "before the proposal was graded",
                other_book["snapshot_id"]: "sportsbook differs", reference["snapshot_id"]: "confirmed NFL sportsbook"}
    for snapshot in (baseline, older, before_grading, other_book, reference):
        eligible, reason = recheck_candidate(active, snapshot)
        assert not eligible and expected[snapshot["snapshot_id"]] in reason
        with pytest.raises(ValueError):
            recheck_active_proposal(history, adapter, view, active, snapshot, now=clock["anchor"])


def test_stale_menu_verification_keeps_the_gate_closed(tmp_path, clock):
    history, adapter = LocalHistory(tmp_path), TeaserModelAdapter()
    view, _, active = build_active_proposal(history, adapter, execution(history, clock["baseline"], clock["kickoff"]),
                                            now=clock["anchor"])
    newer = execution(history, clock["newer"], clock["kickoff"])
    stale = history.save_menu_verification(snapshot_id=newer["snapshot_id"], sportsbook="bluecoins.ag",
                                           prices=dict(DEFAULT_TEASER_MENU),
                                           observed_at=(clock["anchor"] - timedelta(minutes=35)).isoformat())
    prepared = prepare_nfl_snapshot(newer, adapter, now=clock["anchor"], season=2026, week=4,
                                    prices=dict(DEFAULT_TEASER_MENU), menu_book="bluecoins.ag",
                                    menu_verification={"snapshot_id": newer["snapshot_id"], "book": "bluecoins.ag",
                                                       "prices": dict(DEFAULT_TEASER_MENU),
                                                       "observed_at": stale["observed_at"],
                                                       "verification_id": stale["verification_id"]})
    recheck = adapter.recheck(view.card_id, prepared.slate)
    assert recheck.verdict == "VALIDATED"
    executable, reason = gate(view, active, recheck, prepared, datetime.now(timezone.utc))
    assert not executable and reason.endswith("teaser menu is not fresh")


def test_active_baseline_is_never_replaced_without_explicit_replace(tmp_path, clock):
    history, adapter = LocalHistory(tmp_path), TeaserModelAdapter()
    view, prepared, first = build_active_proposal(history, adapter, execution(history, clock["baseline"], clock["kickoff"]),
                                                  now=clock["anchor"])
    other = execution(history, clock["baseline"] + timedelta(seconds=1), clock["kickoff"])
    with pytest.raises(ValueError, match="active proposal baseline already exists"):
        build_active_proposal(history, adapter, other, now=clock["anchor"])
    assert active_proposal(history)["proposal_id"] == first["proposal_id"]
    # Rebuilding the same proposal is idempotent; a different one needs replace=True.
    assert activate_proposal(history, view, prepared.slate, prepared.context, now=clock["anchor"])["proposal_id"] == \
        first["proposal_id"]
    clock["graded"] = clock["graded"] + timedelta(seconds=1)
    _, _, second = build_active_proposal(history, adapter, other, now=clock["anchor"], replace=True)
    assert active_proposal(history)["proposal_id"] == second["proposal_id"] != first["proposal_id"]
    assert [entry["action"] for entry in history.activations()] == ["activate", "activate"]


def test_cfb_and_reference_snapshots_can_never_become_a_live_baseline(tmp_path, clock):
    history, adapter = LocalHistory(tmp_path), TeaserModelAdapter()
    cfb = execution(history, clock["baseline"], clock["kickoff"], league="CFB")
    with pytest.raises(ValueError, match="confirmed NFL sportsbook screenshot"):
        build_active_proposal(history, adapter, cfb, now=clock["anchor"])
    reference = history.save_confirmed_reference({
        "schema_version": 2, "kind": "market_snapshot", "league": "NFL", "season": 2026, "week": 4,
        "captured_at": clock["baseline"].isoformat(), "source": "reference_url", "source_type": "url",
        "source_provider": "ESPN", "source_url": "https://www.espn.com/nfl/scoreboard", "market_role": "REFERENCE",
        "sportsbook": "DraftKings", "events": [{"source_event_id": "r", "away_team": "Jacksonville Jaguars",
                                                "home_team": "Denver Broncos", "kickoff": clock["kickoff"].isoformat(),
                                                "spread_away": "+2.5", "spread_home": "-2.5", "total": "44.5",
                                                "event_state": "pre", "sportsbook": "DraftKings"}]})
    with pytest.raises(ValueError, match="confirmed NFL sportsbook screenshot"):
        build_active_proposal(history, adapter, reference, now=clock["anchor"])
    prepared = prepare_nfl_snapshot(reference, adapter, now=clock["anchor"], season=2026, week=4,
                                    prices={"2": "-110", "3": "+170"})
    view = adapter.grade(prepared.slate)
    with pytest.raises(ValueError, match="confirmed sportsbook EXECUTION"):
        activate_proposal(history, view, prepared.slate, prepared.context, now=clock["anchor"])
    assert active_proposal(history) is None


@pytest.mark.parametrize("day, expected", [
    (date(2026, 9, 10), (2026, 1)), (date(2026, 9, 14), (2026, 1)), (date(2026, 9, 15), (2026, 2)),
    (date(2026, 9, 27), (2026, 3)), (date(2027, 1, 10), (2026, 18)), (date(2027, 2, 14), (None, None)),
])
def test_nfl_week_is_inferred_from_kickoff_dates(day, expected):
    kickoff = datetime(day.year, day.month, day.day, 17, tzinfo=timezone.utc).isoformat()
    assert infer_nfl_week({"events": [{"kickoff": kickoff}]}) == expected


def test_games_from_two_weeks_do_not_infer_a_week():
    events = [{"kickoff": "2026-09-27T17:00:00+00:00"}, {"kickoff": "2026-10-04T17:00:00+00:00"}]
    assert infer_nfl_week({"events": events}) == (None, None)


def _patch_history(monkeypatch, history):
    import teaser_app.market_compare_page as compare_page
    import teaser_app.market_data as market_data
    import teaser_app.nfl_market_page as page
    import teaser_app.results_page as results_page
    import teaser_app.screenshot_import as screenshot_import
    import teaser_app.url_import as url_import
    for module in (page, market_data, screenshot_import, compare_page, results_page, url_import):
        monkeypatch.setattr(module, "LocalHistory", lambda: history)


def test_streamlit_restores_active_proposal_and_rechecks_latest_screenshot(tmp_path, monkeypatch, clock):
    history = LocalHistory(tmp_path)
    view, _, active = build_active_proposal(history, TeaserModelAdapter(),
                                            execution(history, clock["baseline"], clock["kickoff"]), now=clock["anchor"])
    newer = execution(history, clock["newer"], clock["kickoff"])
    _patch_history(monkeypatch, history)
    app = AppTest.from_file(APP, default_timeout=30).run()      # a fresh session, as after a restart
    assert not app.exception
    assert app.session_state["card"].card_id == view.card_id
    assert app.session_state["card"].graded_at == active["graded_at"]
    assert any("ACTIVE PROPOSAL / BASELINE" in item.value for item in app.markdown)
    assert any("LATEST EXECUTION SNAPSHOT" in item.value and newer["snapshot_id"] in item.value for item in app.markdown)
    assert all(item.label != "Record operator-reported PLACED status" for item in app.button)
    next(item for item in app.button if item.label == "Recheck active proposal with latest screenshot").click().run()
    assert not app.exception and app.session_state["recheck"].verdict == "VALIDATED"
    assert all(item.label != "Record operator-reported PLACED status" for item in app.button)  # menu not verified
    next(item for item in app.button if item.label == "I verified these teaser prices are unchanged now"
         and item.key == "nfl_workflow_verify_menu").click().run()
    assert not app.exception
    assert any(item.label == "Record operator-reported PLACED status" for item in app.button)
    assert any("Ready for operator placement report ✓" in item.value for item in app.markdown)
    assert history.live_placements() == []
    # A build click on another saved slate cannot silently replace the active baseline.
    chooser = next(item for item in app.selectbox if item.label == "Saved NFL market snapshot")
    chooser.set_value(newer["snapshot_id"]).run()
    next(item for item in app.button if item.label == "Use this slate for proposal").click().run()
    build = next(item for item in app.button if item.label == "Build proposal from saved NFL slate")
    assert build.disabled
    assert active_proposal(history)["proposal_id"] == active["proposal_id"]


def test_saved_snapshot_path_is_the_default_once_snapshots_exist(tmp_path, monkeypatch, clock):
    history = LocalHistory(tmp_path)
    _patch_history(monkeypatch, history)
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert next(item for item in app.radio if item.label == "NFL input path").value == "Manual entry"
    execution(history, clock["baseline"], clock["kickoff"])
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert next(item for item in app.radio if item.label == "NFL input path").value == "Saved market snapshot"
    assert any("NFL week inferred from kickoffs" in item.value for item in app.caption)



def _preview(captured: datetime, kickoff: datetime):
    from io import BytesIO
    from PIL import Image
    from teaser_app.screenshot_ingest import OCRLine, extract_screenshots, validate_uploads

    buffer = BytesIO()
    Image.new("RGB", (220, 90), (int(captured.timestamp()) % 250, 200, 200)).save(buffer, format="PNG")

    class Extractor:
        name = "fixture"

        def extract(self, image):
            lines = []
            for away, home, away_spread, home_spread, total in GAMES:
                lines.append(OCRLine(f"{away} {kickoff.isoformat()} {away_spread} -110 O {total} -110", .97, image.sha256))
                lines.append(OCRLine(f"{home} {kickoff.isoformat()} {home_spread} -110 U {total} -110", .97, image.sha256))
            return lines

    files = validate_uploads([("board.png", "image/png", buffer.getvalue())], now=datetime.now(timezone.utc))
    return extract_screenshots(files, "NFL", "bluecoins.ag", captured.isoformat(), Extractor())


def test_streamlit_one_click_confirm_build_then_confirm_recheck(tmp_path, monkeypatch, clock):
    history = LocalHistory(tmp_path)
    _patch_history(monkeypatch, history)
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert next(item for item in app.text_input if item.label == "Sportsbook").value == "bluecoins.ag"
    app.session_state["screenshot_preview"] = _preview(clock["baseline"], clock["kickoff"])
    app.run()
    assert all(item.label != "Confirm & Recheck Active Proposal" for item in app.button)
    next(item for item in app.button if item.label == "Confirm & Build Proposal").click().run()
    assert not app.exception and not app.error
    active = active_proposal(history)
    assert active is not None and app.session_state["card"].card_id == active["card_id"]
    assert len(history.market_history(league="NFL", role="EXECUTION")) == 1
    assert all(item.label != "Record operator-reported PLACED status" for item in app.button)

    app.session_state["screenshot_preview"] = _preview(clock["newer"], clock["kickoff"])
    app.run()
    assert all(item.label != "Confirm & Build Proposal" for item in app.button)
    next(item for item in app.checkbox if item.key == "screenshot_verify_menu").check().run()
    next(item for item in app.button if item.label == "Confirm & Recheck Active Proposal").click().run()
    assert not app.exception and not app.error
    assert app.session_state["recheck"].verdict == "VALIDATED"
    assert active_proposal(history)["proposal_id"] == active["proposal_id"]   # baseline unchanged
    assert any(item.label == "Record operator-reported PLACED status" for item in app.button)
    assert history.live_placements() == []
