"""A confirmed CFB sportsbook screenshot is a PAPER-only source for the CFB card."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.market_data import LocalHistory
from teaser_app.public_cfb import (build_cfb_public_board, cfb_source_kind, infer_cfb_week,
                                   prepare_cfb_reference)
from teaser_app.screenshot_ingest import (OCRLine, extract_screenshots, normalize_screenshot_review,
                                          validate_uploads)

APP = Path(__file__).resolve().parents[1] / "app.py"


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (200, 100), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _screenshot_snapshot(history, league="CFB", now=None, prices=("-110", "+170")):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    kickoff = (now + timedelta(days=2)).isoformat()
    rows = [
        (f"Wisconsin (2-1) {kickoff} +10 -110 O 44 -110", f"#13 Penn St (3-0) {kickoff} -10 -110 U 44 -110"),
        (f"Illinois (2-1) {kickoff} +26 -110 O 54 -110", f"#7 Ohio St (2-1) {kickoff} -26 -110 U 54 -110"),
        (f"Delaware (2-1) {kickoff} +20½ -110 O 52½ -110", f"Virginia (2-1) {kickoff} -20½ -110 U 52½ -110"),
        # Spread unreadable on the board: excluded at confirmation, never on the card.
        (f"Rice (1-2) {kickoff} O 44 -110", f"Fresno St (2-1) {kickoff} U 44 -110"),
    ]
    if league == "NFL":
        rows = [(f"Dallas Cowboys {kickoff} +3 -110", f"New York Giants {kickoff} -3 -110")]

    class Extractor:
        name = "fixture"

        def extract(self, image):
            return [OCRLine(text, .95, image.sha256) for pair in rows for text in pair]

    files = validate_uploads([("board.png", "image/png", _png())], now=now)
    preview = extract_screenshots(files, league, "bluecoins.ag", (now - timedelta(hours=1)).isoformat(),
                                  Extractor())
    payload = normalize_screenshot_review(preview, [dict(row) for row in preview.candidates],
                                          {"2": prices[0], "3": prices[1]})
    return history.save_confirmed_execution(payload), kickoff


def _espn_week(history, kickoff: str, week=4):
    return history.save_confirmed_reference({
        "schema_version": 2, "kind": "market_snapshot", "league": "CFB", "season": 2026, "week": week,
        "captured_at": (datetime.fromisoformat(kickoff) - timedelta(days=1)).isoformat(),
        "source": "reference_url", "source_type": "url", "source_provider": "ESPN",
        "source_url": "https://www.espn.com/college-football/scoreboard?groups=80",
        "market_role": "REFERENCE", "sportsbook": "DraftKings",
        "events": [{"source_event_id": "espn-1", "away_team": "Wisconsin Badgers", "home_team": "Penn State Nittany Lions",
                    "away_school": "Wisconsin", "home_school": "Penn State", "kickoff": kickoff,
                    "spread_home": "-10", "spread_away": "+10", "total": "44", "event_state": "pre",
                    "sportsbook": "DraftKings"}],
    })


def test_confirmed_cfb_screenshot_builds_a_paper_card_with_screenshot_provenance(tmp_path):
    history, adapter = LocalHistory(tmp_path), TeaserModelAdapter()
    snapshot, kickoff = _screenshot_snapshot(history)
    _espn_week(history, kickoff)
    assert cfb_source_kind(snapshot) == "screenshot"
    season, week = infer_cfb_week(history, snapshot)
    assert (season, week) == (2026, 4)
    view, prepared, saved = build_cfb_public_board(history, adapter, snapshot, now=datetime.now(timezone.utc),
                                                   season=season, week=week)
    assert view.strategy.status == "PAPER"
    assert prepared.slate["source"] == "user_screenshot" and prepared.lines_label == "bluecoins.ag screenshot"
    assert prepared.slate["prices"] == {"2": "-110", "3": "+170"}  # the confirmed screenshot menu
    assert prepared.excluded == ()
    assert snapshot["excluded_review_rows"] == [
        {"row": 4, "game": "Rice at Fresno St", "reason": "Row 4 has no spread on either side"}]
    assert {side["team"] for side in prepared.slate["rows"]} >= {"Wisconsin", "Penn St", "Delaware", "Virginia"}
    assert saved["board_kind"] == "cfb_paper_board" and saved["lines_source"] == "bluecoins.ag screenshot"
    assert history.live_placements() == []


def test_week_is_not_guessed_without_a_matching_espn_slate(tmp_path):
    history = LocalHistory(tmp_path)
    snapshot, kickoff = _screenshot_snapshot(history)
    assert infer_cfb_week(history, snapshot) == (None, None)
    _espn_week(history, kickoff, week=4)
    _espn_week(history, kickoff, week=5)  # two different ESPN weeks for the same date: ambiguous
    assert infer_cfb_week(history, snapshot) == (None, None)
    with pytest.raises(ValueError, match="week"):
        prepare_cfb_reference(snapshot, TeaserModelAdapter(), now=datetime.now(timezone.utc), season=2026)


def test_nfl_screenshot_is_never_a_cfb_paper_source(tmp_path):
    history = LocalHistory(tmp_path)
    snapshot, _ = _screenshot_snapshot(history, league="NFL")
    assert cfb_source_kind(snapshot) is None
    with pytest.raises(ValueError, match="CFB market snapshot"):
        prepare_cfb_reference(snapshot, TeaserModelAdapter(), now=datetime.now(timezone.utc),
                              season=2026, week=4)


def test_streamlit_builds_cfb_paper_from_screenshot_slate_without_placement(tmp_path, monkeypatch):
    import teaser_app.cfb_page as page
    import teaser_app.market_compare_page as compare_page
    import teaser_app.results_page as results_page
    import teaser_app.url_import as url_import

    history = LocalHistory(tmp_path)
    screenshot, kickoff = _screenshot_snapshot(history)
    _espn_week(history, kickoff)
    for module in (page, compare_page, results_page, url_import):
        monkeypatch.setattr(module, "LocalHistory", lambda: history)
    app = AppTest.from_file(APP, default_timeout=30).run()
    next(item for item in app.radio if item.label == "League and model track").set_value("CFB · PAPER").run()
    chooser = next(item for item in app.selectbox if item.label == "Saved CFB snapshot")  # Advanced
    assert any("SPORTSBOOK SNAPSHOT — Bluecoins.ag" in option for option in chooser.options)
    assert any("REFERENCE — ESPN" in option for option in chooser.options)
    chooser.set_value(screenshot["snapshot_id"]).run()
    assert any("SPORTSBOOK SNAPSHOT — Bluecoins.ag" in item.value for item in app.markdown)
    next(item for item in app.button if item.label == "Build CFB PAPER card from this snapshot").click().run()
    assert not app.exception
    runs = history.runs(status="PAPER")
    assert len(runs) == 1 and runs[0]["lines_source"] == "bluecoins.ag screenshot"
    assert runs[0]["menu_book"] == "bluecoins.ag"  # inherited from the screenshot's sportsbook
    assert runs[0]["teaser_price_sources"] == {"2_team": "default", "3_team": "default"}
    assert any("standard default menu, not observed" in item.value for item in app.caption)
    assert any("PAPER — NOT REAL MONEY" in item.value for item in app.warning)
    assert all("PLACE" not in item.label.upper() for item in app.button)
    assert history.live_placements() == []
