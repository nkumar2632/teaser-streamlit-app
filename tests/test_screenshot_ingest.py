from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

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
    layout_rows,
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
    lines = [OCRLine("JAX +2.5 -110", .61, "a"), OCRLine("DEN -3 -110", .61, "a")]
    rows, _, _, warnings = parse_recognized_lines(lines, "NFL")
    assert rows[0]["field_states"]["spread_away"] == "uncertain"
    assert rows[0]["moneyline_away"] is None
    assert "Impossible opposing spread pairing" in rows[0]["warnings"]
    assert not warnings
    rows, _, _, warnings = parse_recognized_lines([OCRLine("JAX +2.5 -110", .9, "a")], "NFL")
    assert rows == [] and any("Unpaired OCR row" in warning for warning in warnings)
    # Rows from two different screenshots are never paired into one game.
    rows, _, _, warnings = parse_recognized_lines(
        [OCRLine("JAX +2.5 -110", .9, "a"), OCRLine("DEN -2.5 -110", .9, "b")], "NFL")
    assert rows == [] and sum("Unpaired OCR row" in warning for warning in warnings) == 2


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


def _bluecoins_board(sha: str = "board") -> list[OCRLine]:
    """Positioned Apple Vision fragments shaped like a Bluecoins NFL board (row-major, top first)."""
    fragments = [
        (.90, [("01:00 PM EST - FOX", .05), ("$2,000", .80)]),
        (.86, [("451 Dallas Cowboys", .05), ("+2.5 -110", .45), ("O 44 -110", .62), ("+115", .80), ("932+", .93)]),
        (.82, [("452 New York Giants", .05), ("-2.5 -110", .45), ("U 44 -110", .62), ("-135", .80)]),
        (.76, [("04:25 PM EST - CBS", .05), ("$2,000", .80)]),
        (.72, [("453 Kansas City Chiefs", .05), ("-3.5 -110", .45), ("O 46.5 -110", .62), ("-175", .80), ("411+", .93)]),
        (.68, [("454 Denver Broncos", .05), ("+3.5 -110", .45), ("U 46.5 -110", .62), ("+150", .80)]),
    ]
    return [OCRLine(text, .95, sha, x, y) for y, row in fragments for text, x in row]


def test_positioned_bluecoins_fragments_rebuild_rows_and_never_pair_headers_limits_or_counters():
    rows = layout_rows(_bluecoins_board())
    assert rows[1].text == "451 Dallas Cowboys +2.5 -110 O 44 -110 +115 932+"
    candidates, _, _, warnings = parse_recognized_lines(rows, "NFL")
    assert [(row["away_team"], row["home_team"]) for row in candidates] == [
        ("Dallas Cowboys", "New York Giants"), ("Kansas City Chiefs", "Denver Broncos")]
    first, second = candidates
    assert (first["spread_away"], first["spread_home"], first["total"]) == ("+2.5", "-2.5", "44")
    assert (first["over_price"], first["under_price"]) == ("-110", "-110")
    assert (second["spread_away"], second["spread_home"], second["total"]) == ("-3.5", "+3.5", "46.5")
    names = " ".join(row[side] for row in candidates for side in ("away_team", "home_team"))
    assert not any(junk in names for junk in ("PM", "EST", "FOX", "$", "2,000", "932", "411", "451"))
    assert not any("Unpaired" in warning for warning in warnings)


def test_unpositioned_board_fragments_are_not_team_names():
    lines = [OCRLine(text, .95, "a") for text in (
        "01:00 PM EST - FOX", "$2,000", "932+",
        "Dallas Cowboys +2.5 -110", "New York Giants -2.5 -110", "Total 44", "O 44 -110")]
    candidates, _, _, warnings = parse_recognized_lines(lines, "NFL")
    assert [(row["away_team"], row["home_team"]) for row in candidates] == [("Dallas Cowboys", "New York Giants")]
    assert not any("Unpaired" in warning for warning in warnings)
    assert any("Ignored" in warning and "932+" in warning for warning in warnings)


def test_team_rows_are_never_paired_across_a_game_header():
    lines = [OCRLine(text, .95, "a") for text in (
        "01:00 PM EST - FOX", "Dallas Cowboys +2.5 -110",
        "04:25 PM EST - CBS", "Kansas City Chiefs -3.5 -110", "Denver Broncos +3.5 -110")]
    candidates, _, _, warnings = parse_recognized_lines(lines, "NFL")
    assert [(row["away_team"], row["home_team"]) for row in candidates] == [("Kansas City Chiefs", "Denver Broncos")]
    assert any("Unpaired OCR row requires manual review: Dallas Cowboys" in warning for warning in warnings)


def test_apple_vision_extractor_keeps_coordinates_and_removes_temporary_image(monkeypatch):
    import json as _json
    import os as _os
    import teaser_app.screenshot_ingest as ingest

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["path"] = argv, argv[-1]
        assert _os.path.exists(argv[-1])
        payload = [{"text": "451 Dallas Cowboys", "confidence": .9, "x": .05, "y": .86},
                   {"text": "no position", "confidence": .8}]
        return SimpleNamespace(returncode=0, stdout=_json.dumps(payload), stderr="")

    monkeypatch.setattr(ingest.subprocess, "run", fake_run)
    image = checked_files(1)[0]
    lines = ingest.AppleVisionExtractor().extract(image)
    assert seen["argv"][:2] == ["/usr/bin/xcrun", "swift"]
    assert not _os.path.exists(seen["path"])
    assert (lines[0].x, lines[0].y) == (.05, .86)
    assert (lines[1].x, lines[1].y) == (None, None)


# Minimal Bluecoins-style NFL board (synthetic; mirrors the observed layout, not a real capture).
BOARD_CAPTURED = "2026-09-27T10:05:00-04:00"
BLUECOINS_BOARD = [
    ("SUNDAY, SEP 27", None),
    ("01:00 PM EST - FOX", None),
    ("_ Seattle Seahawks (2-0)", ("PROPS", "-7½ -110", "-375", "O 40 -110")),
    ("W Washington Commanders (0-2)", ("932+", "+7½ -110", "+305", "U 40 -110")),
    ("01:00 PM EST - CBS", None),
    ("1E/ Cincinnati Bengals (2-0)", ("PROPS", "-3½ -110", "-185", "O 42½ -110")),
    ("Pittsburgh Steelers (1-1)", ("958+", "+3½ -110", "+155", "U 42½ -110")),
    ("New England Patriots (1-1)", ("PROPS", "+3 -110", "+135", "0 46 -110")),
    ("Jacksonville Jaguars (1-1)", ("1942+", "-3 -110", "-155", "U 46 -110")),
    ("04:05 PM EST - FOX", None),
    ("Arizona Cardinals (1-1)", ("PROPS", "+8½ -110", "+335", "0 48 -110")),
    ("San Francisco 49ers (2-0)", ("978+", "-8½ -110", "-425", "U 48 -110")),
    ("04:25 PM EST - @ MARACANA STADIUM - RIO DE JANEIRO, BRAZIL (CBS)", None),
    ("Baltimore Ravens (1-1)", ("PROPS", "-3 -125", "-180", "O 53 -110")),
    ("Dallas Cowboys (1-1)", ("1630+", "+3 +105", "+160", "U 53 -110")),
    ("08:20 PM EST - NBC", None),
    ("Los Angeles Rams (1-1)", ("PROPS", "-2½ -110", "-140", "O 44½ -110")),
    ("Denver Broncos (1-1)", ("1161+", "+2½ -110", "+120", "U 44½ -110")),
    ("MONDAY, SEP 28", None),
    ("08:15 PM EST - ABC/ESPN", None),
    ("Philadelphia Eagles (2-0)", ("PROPS", "-4½ -110", "-225", "O 41 -110")),
    ("Chicago Bears (1-1)", ("770+", "+4½ -110", "+185", "U 41 -110")),
]
BOARD_EXPECTED = [
    ("Seattle Seahawks", "Washington Commanders", "2026-09-27T13:00:00-04:00", "-7.5", "+7.5", "40"),
    ("Cincinnati Bengals", "Pittsburgh Steelers", "2026-09-27T13:00:00-04:00", "-3.5", "+3.5", "42.5"),
    ("New England Patriots", "Jacksonville Jaguars", "2026-09-27T13:00:00-04:00", "+3", "-3", "46"),
    ("Arizona Cardinals", "San Francisco 49ers", "2026-09-27T16:05:00-04:00", "+8.5", "-8.5", "48"),
    ("Baltimore Ravens", "Dallas Cowboys", "2026-09-27T16:25:00-04:00", "-3", "+3", "53"),
    ("Los Angeles Rams", "Denver Broncos", "2026-09-27T20:20:00-04:00", "-2.5", "+2.5", "44.5"),
    ("Philadelphia Eagles", "Chicago Bears", "2026-09-28T20:15:00-04:00", "-4.5", "+4.5", "41"),
]


def _positioned_board(sha="board"):
    lines, top = [], .98
    for label, cells in BLUECOINS_BOARD:
        lines.append(OCRLine(label, .95, sha, .03, top))
        for cell, x in zip(cells or (), (.35, .43, .61, .76)):
            lines.append(OCRLine(cell, .95, sha, x, top + .002))
        top -= .022
    return lines


def _board_rows(candidates):
    return [(row["away_team"], row["home_team"], row["kickoff"], row["spread_away"], row["spread_home"],
             row["total"]) for row in candidates]


def test_bluecoins_board_yields_canonical_teams_half_points_and_header_kickoffs():
    candidates, _, _, warnings = parse_recognized_lines(layout_rows(_positioned_board()), "NFL",
                                                        captured_at=BOARD_CAPTURED)
    assert _board_rows(candidates) == BOARD_EXPECTED
    assert all(row["moneyline_away"] is None and row["moneyline_home"] is None for row in candidates)
    assert all(row["field_states"]["kickoff"] == "high_confidence" for row in candidates)
    assert not any(row["warnings"] for row in candidates)
    assert not any("Unpaired" in warning for warning in warnings)


def test_bluecoins_board_extracts_and_confirms_without_manual_edits(tmp_path):
    class BoardExtractor:
        name = "positioned_fixture"

        def extract(self, image):
            return _positioned_board(image.sha256)

    preview = extract_screenshots(checked_files(1), "NFL", "bluecoins.ag", BOARD_CAPTURED, BoardExtractor())
    assert _board_rows(preview.candidates) == BOARD_EXPECTED
    assert "SUNDAY, SEP 27" in preview.recognized_text[0]
    snapshot = normalize_screenshot_review(preview, [dict(row) for row in preview.candidates], {"2": "", "3": ""})
    assert [(e["spread_away"], e["total"]) for e in snapshot["events"]][:2] == [("-7.5", "40"), ("-3.5", "42.5")]


@pytest.mark.parametrize("unreadable", ["-3% -110 O 42% -110", "-31/2 -110 O 421/2 -110"])
def test_unreadable_half_points_stay_blank_never_rounded(unreadable):
    lines = [OCRLine(text, .95, "a") for text in (
        "SUNDAY, SEP 27", "01:00 PM EST - CBS", f"Cincinnati Bengals (2-0) PROPS {unreadable}",
        "Pittsburgh Steelers (1-1) 958+ +3½ -110 U 42½ -110")]
    candidates, _, _, _ = parse_recognized_lines(lines, "NFL", captured_at=BOARD_CAPTURED)
    row = candidates[0]
    assert row["spread_away"] is None and row["spread_home"] == "+3.5"
    assert row["total"] == "42.5" and row["over_price"] is None  # only the readable under side


def test_team_rows_with_zero_or_several_nfl_teams_stay_for_review():
    lines = [OCRLine(text, .95, "a") for text in (
        "Cincinati Bengal (2-0) PROPS -3½ -110", "Pittsburgh Steelers Cleveland Browns 958+ +3½ -110")]
    row = parse_recognized_lines(lines, "NFL")[0][0]
    assert row["away_team"] == "Cincinati Bengal (2-0) PROPS"
    assert row["home_team"] == "Pittsburgh Steelers Cleveland Browns 958+"
    assert row["field_states"]["away_team"] == row["field_states"]["home_team"] == "uncertain"
    assert "No NFL team recognized in OCR row; review team" in row["warnings"]
    assert "Several NFL teams in one OCR row; review team" in row["warnings"]


@pytest.mark.parametrize("headers, captured", [
    (["01:00 PM EST - CBS"], BOARD_CAPTURED),                        # no board date
    (["SUNDAY, SEP 27", "12:00 PM CT - CBS"], BOARD_CAPTURED),       # non-Eastern label
    (["SATURDAY, SEP 27", "01:00 PM EST - CBS"], BOARD_CAPTURED),    # weekday disagrees
    (["SUNDAY, SEP 27", "01:00 PM EST - CBS"], None),                # no capture year
])
def test_kickoff_is_left_blank_when_board_headers_cannot_be_resolved(headers, captured):
    lines = [OCRLine(text, .95, "a") for text in (
        *headers, "Cincinnati Bengals (2-0) -3½ -110", "Pittsburgh Steelers (1-1) +3½ -110")]
    row = parse_recognized_lines(lines, "NFL", captured_at=captured)[0][0]
    assert row["kickoff"] is None and row["field_states"]["kickoff"] == "missing"
    assert "Kickoff header could not be resolved; enter kickoff in review" in row["warnings"]


# Synthetic Bluecoins CFB board (mirrors the observed layout: ~25 px rows on a 2000 px capture,
# market cells a few px below the team text, logo OCR text, rankings, records, PROPS/counters,
# O read as 0 and glued to the total, and chevrons glued to prices). No real capture is stored.
CFB_CAPTURED = "2026-09-25T18:00:00-04:00"
CFB_FRIDAY = [("08:06 PM EST - FOX",
               ("Northwestern (2-0)", "PROPS", "+20½ -110", None, "050 -110v", "0 13½ -150"),
               ("#5 Indiana (3-0)", "470", "-20½ -110", None, "U 50 -110v", "0 35½ -120"))]
CFB_SATURDAY = [
    ("12:00 PM EST - ESPN", ("Wake Forest (2-1)", "PROPS", "+12½ -110", "+375", "057 -110v", None, "WF"),
     ("> #16 Louisville (2-1)", "1423", "-12½ -110", "-550", "U 57 -110v", None)),
    ("12:00 PM EST - ABC", ("y #1 Texas (3-0)", "PROPS", "-5 -110", "-200", "0 54½ -110v", None),
     ("#14 Tennessee (3-0)", "698", "+5 -110", "+170", "U 54½ -110v", None, "T")),
    ("12:00 PM EST", ("Illinois (2-1)", "PROPS", "+26 -110", None, "0 54 -110v", None),
     ("#7 Ohio St (2-1)", "1088", "-26 -110", None, "U 54 -110v", None)),
    ("05:00 PM EST", ("Wisconsin (2-1)", "PROPS", "+10 -110", "+310", "0 44 -110v", None),
     ("#13 Penn St (3-0)", "722", "-10 -110", "-380", "U 44 -110v", None)),
]
CFB_EXPECTED = [
    ("Northwestern", "Indiana", "2026-09-25T20:06:00-04:00", "+20.5", "-20.5", "50"),
    ("Wake Forest", "Louisville", "2026-09-26T12:00:00-04:00", "+12.5", "-12.5", "57"),
    ("Texas", "Tennessee", "2026-09-26T12:00:00-04:00", "-5", "+5", "54.5"),
    ("Illinois", "Ohio St", "2026-09-26T12:00:00-04:00", "+26", "-26", "54"),
    ("Wisconsin", "Penn St", "2026-09-26T17:00:00-04:00", "+10", "-10", "44"),
]


def _cfb_fragments(sections, sha="cfb", pitch=.0125, top=.97):
    """sections: [(date header or None, [(game header, away row, home row), ...])]."""
    lines, y = [], top

    def row(name, button, spread, moneyline, total, extra, logo=None):
        lines.append(OCRLine(name, .96, sha, .055, y))
        if logo:  # Vision often returns the team logo as its own short text observation
            lines.append(OCRLine(logo, .5, sha, .033, y + .1 * pitch))
        for text, x in ((button, .35), (spread, .40), (moneyline, .58), (total, .72), (extra, .89)):
            if text:
                lines.append(OCRLine(text, .95, sha, x, y - .25 * pitch))

    for date_header, games in sections:
        if date_header:
            lines.append(OCRLine(date_header, .97, sha, .034, y))
            lines.append(OCRLine("MAX:", .97, sha, .36, y))
            y -= pitch
        for header, away, home in games:
            lines.append(OCRLine(header, .95, sha, .05, y))
            y -= pitch
            row(*away)
            y -= pitch
            row(*home)
            y -= pitch
    return lines


def _positioned_extractor(boards):
    class Extractor:
        name = "positioned_fixture"

        def __init__(self):
            self.boards = iter(boards)

        def extract(self, image):
            return next(self.boards)
    return Extractor()


def test_cfb_board_on_tight_rows_pairs_every_game_with_clean_names_and_markets():
    board = _cfb_fragments([("FRIDAY, SEP 25", CFB_FRIDAY), ("SATURDAY, SEP 26", CFB_SATURDAY)])
    preview = extract_screenshots(checked_files(1), "CFB", "bluecoins.ag", CFB_CAPTURED,
                                  _positioned_extractor([board]))
    assert _board_rows(preview.candidates) == CFB_EXPECTED
    by_away = {row["away_team"]: row for row in preview.candidates}
    # The first-half total column never replaces the game total.
    assert by_away["Northwestern"]["under_price"] == "-110" and not by_away["Northwestern"]["conflicts"]
    assert by_away["Illinois"]["moneyline_away"] is None
    assert not by_away["Wake Forest"]["warnings"] and by_away["Texas"]["home_team"] == "Tennessee"
    assert not any("could not be placed" in warning for warning in preview.warnings)
    snapshot = normalize_screenshot_review(preview, [dict(row) for row in preview.candidates], {"2": "", "3": ""})
    assert snapshot["league"] == "CFB" and snapshot["market_role"] == "EXECUTION"


def test_cfb_board_layout_scales_with_screenshot_height():
    board = _cfb_fragments([("SATURDAY, SEP 26", CFB_SATURDAY)], pitch=.052, top=.99)
    preview = extract_screenshots(checked_files(1), "CFB", "bluecoins.ag", CFB_CAPTURED,
                                  _positioned_extractor([board]))
    assert _board_rows(preview.candidates) == CFB_EXPECTED[1:]


def test_cfb_names_drop_rank_record_props_counter_and_logo_text_but_flag_uncertain_prefixes():
    lines = [OCRLine(text, .95, "a") for text in (
        "SATURDAY, SEP 26", "03:30 PM EST - CBS",
        "(@y #17 Iowa (3-0) PROPS +5½ -110 0 38½ -110", "[a N #18 Michigan (3-0) 1930 -5½ -110 U 38½ -110",
        "03:30 PM EST", "Bg UNLV (1-2) PROPS -13½ -110 0 52½ -110", "RK Rice (1-2) 386 +13½ -110 U 52½ -110",
        "04:00 PM EST", "ef Clemson (2-1) PROPS -2½ -110 0 51½ -110", "&* South Alabama (2-1) 424 +2½ -110 U 51½ -110")]
    rows = parse_recognized_lines(lines, "CFB", captured_at=CFB_CAPTURED)[0]
    assert [(row["away_team"], row["home_team"]) for row in rows] == [
        ("Iowa", "Michigan"), ("UNLV", "RK Rice"), ("Clemson", "South Alabama")]
    assert rows[2]["warnings"] == []  # symbol/lowercase logo text is removed with certainty
    assert rows[0]["field_states"]["away_team"] == rows[0]["field_states"]["home_team"] == "high_confidence"
    assert "CFB team name cleaned from logo OCR text; verify team" in rows[1]["warnings"]
    assert "Possible logo text in CFB team name; verify team" in rows[1]["warnings"]
    assert rows[1]["field_states"]["home_team"] == "uncertain"


def test_saved_team_names_resolve_logo_prefixes_without_shortening_real_names():
    known = ("Rice", "UL Lafayette", "Ohio", "Miami (OH)", "Miami Ohio")
    lines = [OCRLine(text, .95, "a") for text in (
        "SATURDAY, SEP 26", "03:30 PM EST", "RK Rice (1-2) +13 -110 0 44 -110",
        "UL Lafayette (2-1) -13 -110 U 44 -110", "04:00 PM EST", "Connecticut (2-1) +3½ -110 0 52 -110",
        "BN Miami Ohio 965 -3½ -110 U 52 -110")]
    rows = parse_recognized_lines(lines, "CFB", captured_at=CFB_CAPTURED, known_teams=known)[0]
    assert [(row["away_team"], row["home_team"]) for row in rows] == [
        ("Rice", "UL Lafayette"), ("Connecticut", "Miami Ohio")]
    assert rows[0]["warnings"] == [] or all("team" not in warning for warning in rows[0]["warnings"])
    # "Miami" is a real word, so "Miami Ohio" is never shortened to the known school "Ohio".
    lines[-1] = OCRLine("Miami Ohio 965 -3½ -110 U 52 -110", .95, "a")
    rows = parse_recognized_lines(lines, "CFB", captured_at=CFB_CAPTURED, known_teams=("Ohio",))[0]
    assert rows[1]["home_team"] == "Miami Ohio"
    assert "CFB team not found in saved ESPN teams; verify team" in rows[1]["warnings"]


def test_continuous_cfb_screenshots_merge_overlap_and_never_pair_across_images():
    first = _cfb_fragments([("SATURDAY, SEP 26", CFB_SATURDAY[:3])], sha="one")
    # The second screenshot starts with the last home row of the previous game (orphan), then
    # repeats the Illinois game and continues without its own date header.
    second = [OCRLine("#14 Tennessee (3-0) 698 +5 -110 U 54½ -110v", .95, "two", .055, .99)]
    second += _cfb_fragments([(None, CFB_SATURDAY[2:])], sha="two", top=.975)
    preview = extract_screenshots(checked_files(2), "CFB", "bluecoins.ag", CFB_CAPTURED,
                                  _positioned_extractor([first, second]))
    assert _board_rows(preview.candidates) == CFB_EXPECTED[1:]
    assert any("Unpaired OCR row requires manual review: Tennessee" in warning for warning in preview.warnings)
    assert all(row["field_states"]["kickoff"] == "high_confidence" for row in preview.candidates)


def test_screenshot_without_date_or_overlap_keeps_carried_kickoff_uncertain():
    first = _cfb_fragments([("SATURDAY, SEP 26", CFB_SATURDAY[:1])], sha="one")
    second = _cfb_fragments([(None, CFB_SATURDAY[3:])], sha="two")
    preview = extract_screenshots(checked_files(2), "CFB", "bluecoins.ag", CFB_CAPTURED,
                                  _positioned_extractor([first, second]))
    penn = next(row for row in preview.candidates if row["home_team"] == "Penn St")
    assert penn["kickoff"] == "2026-09-26T17:00:00-04:00"
    assert penn["field_states"]["kickoff"] == "uncertain"
    assert any("without overlap" in warning for warning in penn["warnings"])


def test_ambiguous_or_unreadable_cfb_cells_are_left_blank_not_guessed():
    board = _cfb_fragments([("SATURDAY, SEP 26", CFB_SATURDAY[3:])])
    wisconsin = next(line for line in board if line.text.startswith("Wisconsin"))
    penn = next(line for line in board if line.text.startswith("#13 Penn"))
    midway = (wisconsin.y + penn.y) / 2
    board = [line for line in board if line.text != "+10 -110"]
    board.append(OCRLine("+10 -110", .95, "cfb", .40, midway))           # equally near both rows
    board = [OCRLine("-B% -110", .95, "cfb", .40, line.y) if line.text == "-10 -110" else line for line in board]
    preview = extract_screenshots(checked_files(1), "CFB", "bluecoins.ag", CFB_CAPTURED,
                                  _positioned_extractor([board]))
    row = preview.candidates[0]
    assert (row["spread_away"], row["spread_home"], row["total"]) == (None, None, "44")
    assert any("could not be placed" in warning and "+10 -110" in warning for warning in preview.warnings)


def test_reconfirming_the_same_capture_reuses_one_snapshot(tmp_path):
    history = LocalHistory(tmp_path)
    texts = [[("JAX 2026-09-27T13:00:00-04:00 +2.5 -110", .99), ("DEN 2026-09-27T13:00:00-04:00 -2.5 -110", .99)]]
    files = checked_files(1)
    first = extract_screenshots(files, "NFL", "My Book", CAPTURED, FixtureExtractor(list(texts)))
    second = extract_screenshots(files, "NFL", "My Book", CAPTURED, FixtureExtractor(list(texts)))
    assert first.extracted_at != second.extracted_at or first is not second
    saved_first = save_confirmed_execution(first, [dict(row) for row in first.candidates], {"2": "", "3": ""}, history)
    saved_again = save_confirmed_execution(second, [dict(row) for row in second.candidates], {"2": "", "3": ""}, history)
    assert saved_again["snapshot_id"] == saved_first["snapshot_id"]
    assert len(history.market_history(league="NFL", role="EXECUTION")) == 1
    changed = [dict(row) for row in second.candidates]
    changed[0]["spread_away"], changed[0]["spread_home"] = "+3", "-3"
    assert save_confirmed_execution(second, changed, {"2": "", "3": ""}, history)["snapshot_id"] != saved_first["snapshot_id"]
