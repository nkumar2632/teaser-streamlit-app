"""Operator defaults: the screenshot league follows the mode, the sportsbook and teaser menu
defaults are inherited and labelled, week inference has an explicit fallback, and a newly
confirmed CFB screenshot becomes the selected CFB (PAPER) source."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.market_data import LocalHistory
from teaser_app.nfl_market import prepare_nfl_snapshot
from teaser_app.screenshot_ingest import (DEFAULT_TEASER_MENU, OCRLine, extract_screenshots,
                                          normalize_screenshot_review, validate_uploads)

APP = Path(__file__).resolve().parents[1] / "app.py"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
KICKOFF = (NOW + timedelta(days=2)).isoformat()
ROWS = [("Wisconsin", "Penn St", "+10", "-10", "44"), ("Illinois", "Ohio St", "+26", "-26", "54"),
        ("Delaware", "Virginia", "+20.5", "-20.5", "52.5")]


def _patch(monkeypatch, history):
    import teaser_app.cfb_page as cfb_page
    import teaser_app.market_compare_page as compare_page
    import teaser_app.market_data as market_data
    import teaser_app.nfl_market_page as nfl_page
    import teaser_app.results_page as results_page
    import teaser_app.screenshot_import as screenshot_import
    import teaser_app.url_import as url_import
    for module in (cfb_page, compare_page, market_data, nfl_page, results_page, screenshot_import, url_import):
        monkeypatch.setattr(module, "LocalHistory", lambda: history)


def _cfb_preview(book="bluecoins.ag", captured=NOW - timedelta(hours=1)):
    buffer = BytesIO()
    Image.new("RGB", (210, 90), "white").save(buffer, format="PNG")

    class Extractor:
        name = "fixture"

        def extract(self, image):
            return [OCRLine(f"{team} {KICKOFF} {spread} -110 {side} {total} -110", .96, image.sha256)
                    for away, home, away_spread, home_spread, total in ROWS
                    for team, spread, side in ((away, away_spread, "O"), (home, home_spread, "U"))]

    files = validate_uploads([("cfb.png", "image/png", buffer.getvalue())], now=NOW)
    return extract_screenshots(files, "CFB", book, captured.isoformat(), Extractor())


def _espn(history, captured=NOW - timedelta(minutes=30), week=4):
    events = [{"source_event_id": f"espn-{index}", "away_team": away, "home_team": home, "away_school": away,
               "home_school": home, "kickoff": KICKOFF, "spread_away": sa, "spread_home": sh, "total": total,
               "event_state": "pre", "sportsbook": "DraftKings"}
              for index, (away, home, sa, sh, total) in enumerate(ROWS, 1)]
    return history.save_confirmed_reference({
        "schema_version": 2, "kind": "market_snapshot", "league": "CFB", "season": 2026, "week": week,
        "captured_at": captured.isoformat(), "source": "reference_url", "source_type": "url",
        "source_provider": "ESPN", "source_url": "https://www.espn.com/college-football/scoreboard?groups=80",
        "market_role": "REFERENCE", "sportsbook": "DraftKings", "events": events})


def _cfb_app():
    app = AppTest.from_file(APP, default_timeout=30).run()
    next(item for item in app.radio if item.label == "League and model track").set_value("CFB · PAPER").run()
    return app


def test_screenshot_league_follows_the_mode_with_the_override_under_advanced(tmp_path, monkeypatch):
    _patch(monkeypatch, LocalHistory(tmp_path))
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert next(item for item in app.selectbox if item.label == "Screenshot league override").value == "NFL"
    assert next(item for item in app.text_input if item.label == "Sportsbook override").value == "bluecoins.ag"
    next(item for item in app.radio if item.label == "League and model track").set_value("CFB · PAPER").run()
    assert next(item for item in app.selectbox if item.label == "Screenshot league override").value == "CFB"
    assert any("League: CFB · PAPER · follows the mode above · Sportsbook: bluecoins.ag" in item.value
               for item in app.caption)
    assert all(item.label != "Screenshot league" for item in app.selectbox)  # no routine league input


def test_confirmed_cfb_screenshot_replaces_a_selected_espn_slate_and_builds_paper_in_one_action(
        tmp_path, monkeypatch):
    history = LocalHistory(tmp_path)
    espn = _espn(history)  # newer than the screenshot capture, and explicitly chosen first
    _patch(monkeypatch, history)
    app = _cfb_app()
    next(item for item in app.selectbox if item.label == "Saved CFB snapshot").set_value(espn["snapshot_id"]).run()
    assert any("REFERENCE — ESPN" in item.value for item in app.markdown)
    app.session_state["screenshot_preview"] = _cfb_preview()
    app.run()
    stale = "2026-09-01T09:00:00-04:00"
    next(item for item in app.text_input if item.label.startswith("Lines captured at")).set_value(stale).run()
    next(item for item in app.button if item.label == "Confirm EXECUTION Snapshot").click().run()
    assert not app.exception and not app.error
    # The next import starts from a fresh capture time, never the one just confirmed.
    assert next(item for item in app.text_input if item.label.startswith("Lines captured at")).value != stale
    screenshot = history.market_history(league="CFB", role="EXECUTION")[-1]
    assert next(item for item in app.selectbox if item.label == "Saved CFB snapshot").value == screenshot["snapshot_id"]
    assert any("SPORTSBOOK SNAPSHOT — Bluecoins.ag" in item.value for item in app.markdown)
    assert any("CFB week inferred from a saved ESPN slate with the same dates: 2026 Week 4" in item.value
               for item in app.caption)
    assert all(item.label != "Use this slate for proposal" for item in app.button)
    next(item for item in app.button if item.label == "Build CFB PAPER card from this snapshot").click().run()
    assert not app.exception
    runs = history.runs(status="PAPER")
    assert len(runs) == 1 and runs[0]["source_snapshot_id"] == screenshot["snapshot_id"]
    assert runs[0]["strategy"]["status"] == "PAPER" and runs[0]["lines_source"] == "bluecoins.ag screenshot"
    assert all("PLACE" not in item.label.upper() for item in app.button)
    assert history.live_placements() == []


def test_cfb_menu_book_is_inherited_from_the_screenshot_and_week_fallback_is_explicit(tmp_path, monkeypatch):
    history = LocalHistory(tmp_path)
    preview = _cfb_preview(book="Heritage")
    history.save_confirmed_execution(normalize_screenshot_review(
        preview, [dict(row) for row in preview.candidates], dict(DEFAULT_TEASER_MENU)))
    _patch(monkeypatch, history)
    app = _cfb_app()
    assert any("SPORTSBOOK SNAPSHOT — Heritage" in item.value for item in app.markdown)
    assert next(item for item in app.text_input if item.label == "Teaser menu sportsbook setting").value == "Heritage"
    assert any("Teaser pricing: Heritage · PAPER" in item.value for item in app.caption)
    assert any("Could not infer the CFB season and week" in item.value for item in app.warning)
    assert any(item.label == "CFB week for public slate" for item in app.number_input)
    assert any("standard default menu, not an observed or verified quote" in item.value for item in app.caption)


def test_nfl_saved_path_prefills_default_menu_without_counting_it_as_observed(tmp_path, monkeypatch):
    history = LocalHistory(tmp_path)
    kickoffs = [NOW + timedelta(days=2), NOW + timedelta(days=9)]  # two different NFL weeks
    snapshot = history.save_confirmed_execution({
        "schema_version": 3, "kind": "market_snapshot", "league": "NFL", "season": None, "week": None,
        "captured_at": (NOW - timedelta(minutes=5)).isoformat(), "source": "user_screenshot",
        "source_type": "screenshot", "source_provider": "apple_vision_local", "market_role": "EXECUTION",
        "sportsbook": "bluecoins.ag", "teaser_prices": {"6_point": {"2_team": None, "3_team": None}},
        "events": [{"source_event_id": f"row-{index}", "away_team": away, "home_team": home,
                    "kickoff": kickoff.isoformat(), "spread_away": "+2.5", "spread_home": "-2.5", "total": "44.5",
                    "event_state": "pre", "sportsbook": "bluecoins.ag", "market_role": "EXECUTION",
                    "source_type": "screenshot"}
                   for index, (away, home, kickoff) in enumerate(
                       (("Jacksonville Jaguars", "Denver Broncos", kickoffs[0]),
                        ("Atlanta Falcons", "Carolina Panthers", kickoffs[1])), 1)]})
    _patch(monkeypatch, history)
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert any("Could not infer one NFL regular-season week" in item.value for item in app.warning)
    assert [item.value for item in app.text_input if item.label.startswith("Missing")] == ["-110", "+170"]
    assert any("standard default · not an observed or verified quote" in item.value for item in app.caption)
    assert next(item for item in app.text_input if item.label == "Teaser-menu sportsbook").value == "bluecoins.ag"
    prepared = prepare_nfl_snapshot(snapshot, TeaserModelAdapter(), now=datetime.now(timezone.utc),
                                    season=2026, week=4, prices=dict(DEFAULT_TEASER_MENU))
    assert prepared.context["menu_observed_at"] == {"2": None, "3": None}  # the gate still needs verification
