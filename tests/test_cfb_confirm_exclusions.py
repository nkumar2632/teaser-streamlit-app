"""CFB (PAPER) screenshot confirmation excludes unusable rows instead of failing; teaser defaults
are labelled defaults and never count as an observed NFL quote."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.market_data import LocalHistory
from teaser_app.nfl_market import prepare_nfl_snapshot
from teaser_app.public_cfb import build_cfb_public_board
from teaser_app.screenshot_ingest import (DEFAULT_TEASER_MENU, OCRLine, ScreenshotIngestError,
                                          extract_screenshots, normalize_screenshot_review,
                                          save_confirmed_execution, teaser_price_source, validate_uploads)

APP = Path(__file__).resolve().parents[1] / "app.py"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
KICKOFF = (NOW + timedelta(days=2)).isoformat()
CAPTURED = (NOW - timedelta(hours=1)).isoformat()

# The real Apple Vision cases from the 2026-09-25/26 Bluecoins CFB board, as reviewed rows.
CFB_LINES = [
    # Conflicting total: the first-half column (O 35½) was read as the home total.
    f"Northwestern (2-0) {KICKOFF} +20½ -110 O 50 -110", f"#5 Indiana (3-0) {KICKOFF} -20½ -110 U 35½ -120",
    # One spread side unreadable (Maryland).
    f"UCLA (3-0) {KICKOFF} -1½ -110 O 56½ -110", f"Maryland (2-1) {KICKOFF} U 56½ -110",
    # Over price missing.
    f"Illinois (2-1) {KICKOFF} +26 -110 O 54", f"#7 Ohio St (2-1) {KICKOFF} -26 -110 U 54 -110",
    f"Wisconsin (2-1) {KICKOFF} +10 -110 O 44 -110", f"#13 Penn St (3-0) {KICKOFF} -10 -110 U 44 -110",
]


def _files():
    buffer = BytesIO()
    Image.new("RGB", (200, 100), "white").save(buffer, format="PNG")
    return validate_uploads([("cfb.png", "image/png", buffer.getvalue())], now=NOW)


def _preview(lines=CFB_LINES, league="CFB"):
    class Extractor:
        name = "fixture"

        def extract(self, image):
            return [OCRLine(text, .95, image.sha256) for text in lines]

    return extract_screenshots(_files(), league, "bluecoins.ag", CAPTURED, Extractor())


def _rows(preview):
    return [dict(row) for row in preview.candidates]


def test_conflicting_total_is_excluded_without_blocking_the_cfb_snapshot(tmp_path):
    preview = _preview()
    assert preview.candidates[0]["field_states"]["total"] == "conflict"
    saved = save_confirmed_execution(preview, _rows(preview), dict(DEFAULT_TEASER_MENU), LocalHistory(tmp_path))
    assert [event["away_team"] for event in saved["events"]] == ["UCLA", "Illinois", "Wisconsin"]
    assert saved["excluded_review_rows"] == [
        {"row": 1, "game": "Northwestern at Indiana", "reason": "Resolve conflicting total in row 1"}]
    assert "35.5" not in {event["total"] for event in saved["events"]}  # nothing inferred


def test_missing_spread_side_is_kept_for_the_shown_side_only(tmp_path):
    history = LocalHistory(tmp_path)
    preview = _preview()
    saved = save_confirmed_execution(preview, _rows(preview), dict(DEFAULT_TEASER_MENU), history)
    ucla = next(event for event in saved["events"] if event["away_team"] == "UCLA")
    assert (ucla["spread_away"], ucla["spread_home"]) == ("-1.5", None)
    _, prepared, _ = build_cfb_public_board(history, TeaserModelAdapter(), saved, now=NOW, season=2026, week=4)
    ucla_sides = [side for side in prepared.slate["rows"] if "UCLA" in (side["away_team"], side["home_team"])]
    assert [(side["team"], side["spread"]) for side in ucla_sides] == [("UCLA", "-1.5")]  # Maryland not invented


def test_missing_over_price_does_not_block_or_exclude_the_game(tmp_path):
    preview = _preview()
    saved = save_confirmed_execution(preview, _rows(preview), dict(DEFAULT_TEASER_MENU), LocalHistory(tmp_path))
    illinois = next(event for event in saved["events"] if event["away_team"] == "Illinois")
    assert (illinois["total"], illinois["over_price"], illinois["under_price"]) == ("54", None, "-110")


def test_missing_or_invalid_kickoff_is_excluded_not_guessed(tmp_path):
    preview = _preview()
    rows = _rows(preview)
    rows[1]["kickoff"] = ""
    rows[2]["kickoff"] = "Saturday afternoon"
    saved = save_confirmed_execution(preview, rows, dict(DEFAULT_TEASER_MENU), LocalHistory(tmp_path))
    assert [event["away_team"] for event in saved["events"]] == ["Wisconsin"]
    reasons = {item["game"]: item["reason"] for item in saved["excluded_review_rows"]}
    assert reasons["UCLA at Maryland"] == "Row 2 kickoff requires an ISO timestamp with UTC offset"
    assert reasons["Illinois at Ohio St"] == "Row 3 kickoff requires an ISO timestamp with UTC offset"


def test_nothing_is_saved_when_no_cfb_row_is_usable(tmp_path):
    history = LocalHistory(tmp_path)
    preview = _preview(CFB_LINES[:2])
    with pytest.raises(ScreenshotIngestError, match=r"No usable reviewed rows remain \(1 excluded\)"):
        save_confirmed_execution(preview, _rows(preview), dict(DEFAULT_TEASER_MENU), history)
    assert history.market_history() == []


def test_nfl_confirmation_still_fails_closed_on_any_unresolved_row(tmp_path):
    history = LocalHistory(tmp_path)
    preview = _preview([f"Jacksonville Jaguars {KICKOFF} +2.5 -110 O 44 -110", f"Denver Broncos {KICKOFF} -2.5 -110 U 41 -110",
                        f"Atlanta Falcons {KICKOFF} +2.5 -110 O 43.5 -110", f"Carolina Panthers {KICKOFF} -2.5 -110 U 43.5 -110"],
                       league="NFL")
    with pytest.raises(ScreenshotIngestError, match="Resolve conflicting total in row 1"):
        save_confirmed_execution(preview, _rows(preview), dict(DEFAULT_TEASER_MENU), history)
    assert history.market_history() == []


def test_prefilled_default_menu_is_labelled_and_never_an_observed_nfl_quote(tmp_path):
    history = LocalHistory(tmp_path)
    preview = _preview([f"Jacksonville Jaguars {KICKOFF} +2.5 -110 O 44.5 -110",
                        f"Denver Broncos {KICKOFF} -2.5 -110 U 44.5 -110"], league="NFL")
    assert preview.teaser_prices == {"2": None, "3": None}
    assert teaser_price_source(preview, "2", "-110") == "default"
    assert teaser_price_source(preview, "3", "+150") == "operator"
    saved = save_confirmed_execution(preview, _rows(preview), dict(DEFAULT_TEASER_MENU), history)
    assert saved["teaser_price_sources"] == {"2_team": "default", "3_team": "default"}
    prepared = prepare_nfl_snapshot(saved, TeaserModelAdapter(), now=datetime.now(timezone.utc),
                                    season=2026, week=4)
    assert prepared.slate["prices"] == DEFAULT_TEASER_MENU
    assert prepared.context["menu_observed_at"] == {"2": None, "3": None}  # verification still required
    typed = save_confirmed_execution(preview, _rows(preview), {"2": "-120", "3": "+160"}, history)
    assert typed["teaser_price_sources"] == {"2_team": "operator", "3_team": "operator"}
    observed = prepare_nfl_snapshot(typed, TeaserModelAdapter(), now=datetime.now(timezone.utc), season=2026, week=4)
    assert observed.context["menu_observed_at"] == {"2": typed["captured_at"], "3": typed["captured_at"]}


def test_streamlit_cfb_review_shows_usable_and_excluded_and_confirms_without_deleting_rows(tmp_path, monkeypatch):
    import teaser_app.screenshot_import as screenshot_import

    history = LocalHistory(tmp_path)
    monkeypatch.setattr(screenshot_import, "LocalHistory", lambda: history)
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.session_state["screenshot_preview"] = _preview()
    app.run()
    assert [item.value for item in app.text_input if "6-point teaser price" in item.label] == ["-110", "+170"]
    assert any("standard default · not an observed or verified quote" in item.value for item in app.caption)
    assert any("3 usable / 1 excluded" in item.value for item in app.info)
    assert any("Northwestern at Indiana" in item.value and "conflicting total" in item.value for item in app.warning)
    next(item for item in app.button if item.label == "Confirm EXECUTION Snapshot").click().run()
    assert not app.exception and not app.error
    saved = history.market_history(league="CFB", role="EXECUTION")
    assert len(saved) == 1 and len(saved[0]["events"]) == 3 and len(saved[0]["excluded_review_rows"]) == 1
    assert any("3 usable / 1 excluded" in item.value for item in app.success)
