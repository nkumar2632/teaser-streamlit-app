from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from teaser_app.market_compare import compare_snapshots
from teaser_app.market_data import LocalHistory
from teaser_app.screenshot_ingest import (
    MAX_IMAGE_BYTES,
    MAX_SCREENSHOTS,
    OCRLine,
    ScreenshotIngestError,
    extract_screenshots,
    merge_candidates,
    normalize_screenshot_review,
    parse_recognized_lines,
    reparse_review_text,
    save_confirmed_execution,
    validate_uploads,
)

ROOT = Path(__file__).resolve().parents[1]
CAPTURED = "2026-09-27T10:00:00-04:00"
KICKOFF = "2026-09-27T13:00:00-04:00"


def image_bytes(format_: str = "PNG", size=(200, 100)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, format_)
    return output.getvalue()


def checked_files(count=1):
    return validate_uploads(
        [(f"capture-{index}.png", "image/png", image_bytes(size=(200 + index, 100)))
         for index in range(count)],
        now=datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc),
    )


class FixtureExtractor:
    name = "fixture_local"

    def __init__(self, texts):
        self.texts = iter(texts)

    def extract(self, image):
        return [OCRLine(text, confidence, image.sha256) for text, confidence in next(self.texts)]


def complete_rows(preview):
    rows = [dict(row) for row in preview.candidates]
    for row in rows:
        row["kickoff"] = KICKOFF
    return rows


def test_valid_upload_is_decoded_bounded_and_hashed_without_being_stored(tmp_path):
    body = image_bytes()
    files = validate_uploads([("board.png", "image/png", body)],
                             now=datetime(2026, 9, 27, tzinfo=timezone.utc))
    assert files[0].filename == "board.png"
    assert files[0].sha256 == sha256(body).hexdigest()
    assert files[0].provenance()["uploaded_at"] == "2026-09-27T00:00:00+00:00"
    assert files[0].width == 200 and files[0].height == 100
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("uploads, message", [
    ([('lines.gif', 'image/gif', b'GIF89a')], "Only PNG"),
    ([('../lines.png', 'image/png', image_bytes())], "filename"),
    ([('..\\lines.png', 'image/png', image_bytes())], "filename"),
    ([('fake.png', 'image/png', b'not an image')], "decode"),
    ([('large.png', 'image/png', b'x' * (MAX_IMAGE_BYTES + 1))], "8 MB"),
    ([('one.png', 'image/png', image_bytes()), ('two.png', 'image/png', image_bytes())],
     "same screenshot"),
    ([(f'{i}.png', 'image/png', image_bytes(size=(200 + i, 100)))
      for i in range(MAX_SCREENSHOTS + 1)], "at most"),
])
def test_upload_rejects_unsupported_unsafe_or_oversized_input(uploads, message):
    with pytest.raises(ScreenshotIngestError, match=message):
        validate_uploads(uploads)


def test_single_screenshot_extracts_markets_kickoff_and_six_point_teaser():
    files = checked_files()
    lines = [[
        (f"JAX {KICKOFF} +2.5 -110 ML +130 O 45.5 -110", .98),
        (f"DEN {KICKOFF} -2.5 -110 ML -150 U 45.5 -110", .97),
        ("2-team 6-point teaser +100", .96),
        ("3-team 6pt teaser +170", .92),
    ]]
    preview = extract_screenshots(files, "NFL", "My Book", CAPTURED, FixtureExtractor(lines))
    row = preview.candidates[0]
    assert row["away_team"] == "JAX" and row["home_team"] == "DEN"
    assert row["kickoff"] == "2026-09-27T13:00:00-04:00"
    assert row["spread_away"] == "+2.5" and row["moneyline_home"] == "-150"
    assert row["total"] == "45.5" and row["over_price"] == "-110"
    assert preview.teaser_prices == {"2": "+100", "3": "+170"}
    assert preview.teaser_states == {"2": "high_confidence", "3": "high_confidence"}


def test_common_local_vision_spacing_is_normalized_but_remains_uncertain():
    lines = [
        OCRLine("JAX +2.5-110 ML + 130", .5, "a"),
        OCRLine("DEN -2.5-110 ML - 150", .5, "a"),
    ]
    rows, _, _, _ = parse_recognized_lines(lines, "NFL")
    assert rows[0]["spread_away"] == "+2.5"
    assert rows[0]["spread_away_price"] == "-110"
    assert rows[0]["moneyline_away"] == "+130"
    assert rows[0]["field_states"]["moneyline_away"] == "uncertain"


def test_multiple_screenshots_merge_complementary_markets_and_duplicate_games():
    files = checked_files(2)
    texts = [
        [("JAX +2.5 -110", .96), ("DEN -2.5 -110", .96)],
        [("Jacksonville Jaguars ML +130", .93), ("Denver Broncos ML -150", .93)],
    ]
    preview = extract_screenshots(files, "NFL", "My Book", CAPTURED, FixtureExtractor(texts))
    assert len(preview.candidates) == 1
    row = preview.candidates[0]
    assert row["spread_away"] == "+2.5" and row["moneyline_away"] == "+130"
    assert len(row["field_sources"]["away_team"]) == 2


def test_conflicting_lines_are_never_silently_selected():
    files = checked_files(2)
    texts = [
        [("JAX +2.5 -110", .99), ("DEN -2.5 -110", .99)],
        [("JAX +3 -110", .99), ("DEN -3 -110", .99)],
    ]
    preview = extract_screenshots(files, "NFL", "My Book", CAPTURED, FixtureExtractor(texts))
    row = preview.candidates[0]
    assert row["spread_away"] is None
    assert row["field_states"]["spread_away"] == "conflict"
    assert row["conflicts"]["spread_away"] == ["+2.5", "+3"]
    reviewed = complete_rows(preview)
    with pytest.raises(ScreenshotIngestError, match="Resolve conflicting spread away"):
        normalize_screenshot_review(preview, reviewed, {"2": "", "3": ""})
    reviewed[0]["spread_away"] = "+3"
    reviewed[0]["spread_home"] = "-3"
    assert normalize_screenshot_review(preview, reviewed, {"2": "", "3": ""})["events"][0]["spread_away"] == "+3"


def test_uncertain_missing_cutoff_and_impossible_pairing_are_flagged():
    lines = [OCRLine("JAX +2.5 -110", .61, "a"), OCRLine("DEN -3 -110", .61, "b")]
    rows, _, _, warnings = parse_recognized_lines(lines, "NFL")
    assert rows[0]["field_states"]["spread_away"] == "uncertain"
    assert rows[0]["moneyline_away"] is None
    assert "Impossible opposing spread pairing" in rows[0]["warnings"]
    assert not warnings
    rows, _, _, warnings = parse_recognized_lines([OCRLine("JAX +2.5 -110", .9, "a")], "NFL")
    assert rows == [] and any("Unpaired OCR row" in warning for warning in warnings)


def test_teaser_parser_does_not_map_special_products_or_guess_missing_price():
    lines = [
        OCRLine("2-team 10-point special teaser -130", .99, "a"),
        OCRLine("3-team 13-point teaser -120", .99, "a"),
        OCRLine("standard teaser menu", .99, "a"),
    ]
    _, prices, states, warnings = parse_recognized_lines(lines, "NFL")
    assert prices == {"2": None, "3": None} and states == {"2": "missing", "3": "missing"}
    assert sum("Ignored non-6-point" in warning for warning in warnings) == 2
    assert any("Unclassified teaser" in warning for warning in warnings)


def test_manual_text_fallback_reparses_as_uncertain_and_keeps_file_provenance():
    files = checked_files()
    preview = extract_screenshots(files, "NFL", "My Book", CAPTURED,
                                  FixtureExtractor([[]]))
    reparsed = reparse_review_text(preview, ["JAX +2.5 -110\nDEN -2.5 -110"])
    assert reparsed.files == files and len(reparsed.candidates) == 1
    assert reparsed.candidates[0]["field_states"]["spread_away"] == "uncertain"
    assert "manual_text_review" in reparsed.extractor


def test_manual_correction_and_confirmation_create_append_only_execution_snapshot(tmp_path):
    files = checked_files()
    texts = [[("JAX +2.5 -110 ML +130", .7), ("DEN -2.5 -110 ML -150", .7)]]
    preview = extract_screenshots(files, "NFL", "My Book", CAPTURED, FixtureExtractor(texts))
    rows = complete_rows(preview)
    rows[0]["spread_away_price"] = "-105"
    history = LocalHistory(tmp_path)
    saved = save_confirmed_execution(preview, rows, {"2": "-110", "3": "+170"}, history)
    assert saved["schema_version"] == 3 and saved["market_role"] == "EXECUTION"
    assert saved["source_type"] == "screenshot" and saved["source_provider"] == "fixture_local"
    assert saved["raw_screenshots_retained"] is False and saved["screenshot_count"] == 1
    assert saved["screenshot_provenance"][0]["sha256"] == files[0].sha256
    assert "content" not in saved["screenshot_provenance"][0]
    assert saved["events"][0]["review_changes"]["spread_away_price"]["confirmed"] == "-105"
    assert saved["events"][0]["teaser_3team_6pt_price"] == "+170"
    assert saved["teaser_extraction_state"]["6_point"]["2_team"] == "missing"
    assert saved["teaser_review_changes"]["2"]["confirmed"] == "-110"
    assert history.get_snapshot(saved["snapshot_id"]) == saved
    assert save_confirmed_execution(preview, rows, {"2": "-110", "3": "+170"}, history)["snapshot_id"] == saved["snapshot_id"]
    later = replace(preview, captured_at="2026-09-27T10:05:00-04:00")
    second = save_confirmed_execution(later, rows, {"2": "-110", "3": "+170"}, history)
    assert second["snapshot_id"] != saved["snapshot_id"]
    assert history.get_snapshot(saved["snapshot_id"]) == saved


def test_screenshot_execution_uses_existing_comparison_and_does_not_enter_accounting(tmp_path):
    files = checked_files()
    preview = extract_screenshots(
        files, "NFL", "My Book", CAPTURED,
        FixtureExtractor([[("JAX +2.5 -110", .99), ("DEN -2.5 -110", .99)]]),
    )
    execution = save_confirmed_execution(preview, complete_rows(preview), {"2": "", "3": ""},
                                         LocalHistory(tmp_path))
    reference = {
        "schema_version": 2, "kind": "market_snapshot", "league": "NFL",
        "captured_at": CAPTURED, "source": "reference_url", "source_type": "url",
        "source_provider": "ESPN", "source_url": "https://www.espn.com/nfl/scoreboard",
        "market_role": "REFERENCE", "sportsbook": "ESPN BET", "events": [
            {**execution["events"][0], "snapshot_id": "ignored", "source": "reference_url",
             "source_type": "url", "source_provider": "ESPN", "market_role": "REFERENCE"}
        ],
    }
    reference.pop("snapshot_id", None)
    history = LocalHistory(tmp_path)
    reference = history.save_confirmed_reference(reference)
    assert len(compare_snapshots(reference, execution)["matches"]) == 1
    assert history.market_history(role="REFERENCE") == [reference]
    assert history.market_history(role="EXECUTION") == [execution]
    assert history.runs() == [] and not (tmp_path / "results").exists()


def test_streamlit_preview_requires_confirmation_and_discard_saves_nothing(tmp_path, monkeypatch):
    import teaser_app.screenshot_import as page

    files = checked_files()
    preview = extract_screenshots(
        files, "NFL", "My Book", CAPTURED,
        FixtureExtractor([[("JAX +2.5 -110", .99), ("DEN -2.5 -110", .99)]]),
    )
    monkeypatch.setattr(page, "LocalHistory", lambda: LocalHistory(tmp_path))
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    app.session_state["screenshot_preview"] = preview
    app = app.run()
    assert not app.exception and LocalHistory(tmp_path).market_history() == []
    assert any("REVIEW REQUIRED" in item.value for item in app.warning)
    next(button for button in app.button if button.label == "Discard screenshot preview").click().run()
    assert LocalHistory(tmp_path).market_history() == []
    assert app.session_state.get("screenshot_preview") is None


def test_streamlit_confirm_button_is_the_only_ui_persistence_gate(tmp_path, monkeypatch):
    import teaser_app.screenshot_import as page

    files = checked_files()
    preview = extract_screenshots(
        files, "NFL", "My Book", CAPTURED,
        FixtureExtractor([[
            (f"JAX {KICKOFF} +2.5 -110", .99),
            (f"DEN {KICKOFF} -2.5 -110", .99),
        ]]),
    )
    monkeypatch.setattr(page, "LocalHistory", lambda: LocalHistory(tmp_path))
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    app.session_state["screenshot_preview"] = preview
    app = app.run()
    assert LocalHistory(tmp_path).market_history() == []
    next(button for button in app.button if button.label == "Confirm EXECUTION Snapshot").click().run()
    saved = LocalHistory(tmp_path).market_history()
    assert len(saved) == 1 and saved[0]["market_role"] == "EXECUTION"
    assert not app.exception


def test_confirmation_rejects_malformed_values_duplicate_events_and_reference_payload(tmp_path):
    files = checked_files()
    preview = extract_screenshots(
        files, "CFB", "My Book", CAPTURED,
        FixtureExtractor([[("Texas +2.5 -110", .99), ("Tennessee -2.5 -110", .99)]]),
    )
    rows = complete_rows(preview)
    rows[0]["moneyline_away"] = "EVEN"
    history = LocalHistory(tmp_path)
    with pytest.raises(ScreenshotIngestError, match="malformed"):
        save_confirmed_execution(preview, rows, {"2": "", "3": ""}, history)
    rows[0]["moneyline_away"] = "+120"
    with pytest.raises(ScreenshotIngestError, match="Duplicate reviewed event"):
        save_confirmed_execution(preview, rows + [dict(rows[0])], {"2": "", "3": ""}, history)
    with pytest.raises(ValueError, match="confirmed sportsbook"):
        history.save_confirmed_execution({"schema_version": 2, "market_role": "REFERENCE", "events": [{}]})
    assert history.market_history() == [] and history.runs(status="PAPER") == []
