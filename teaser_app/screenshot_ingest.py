"""Bounded sportsbook-image validation, local OCR, review merging, and persistence."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePath
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from teaser_app.market_compare import team_key
from teaser_app.market_data import FIELDS, _identity

MAX_SCREENSHOTS = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
ALLOWED_MIME = {"image/png": "PNG", "image/jpeg": "JPEG"}
MARKET_FIELDS = (
    "spread_away", "spread_away_price", "spread_home", "spread_home_price",
    "moneyline_away", "moneyline_home", "total", "over_price", "under_price",
)
REVIEW_FIELDS = ("away_team", "home_team", "kickoff", *MARKET_FIELDS)
AMERICAN = re.compile(r"^[+-][0-9]{2,5}$")
NUMBER = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$")
KICKOFF = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})\b")
VISION_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vision_ocr.swift"


class ScreenshotIngestError(ValueError):
    pass


@dataclass(frozen=True)
class ScreenshotFile:
    filename: str
    mime_type: str
    sha256: str
    size_bytes: int
    width: int
    height: int
    uploaded_at: str
    content: bytes

    def provenance(self) -> dict:
        return {key: getattr(self, key) for key in
                ("filename", "mime_type", "sha256", "size_bytes", "width", "height", "uploaded_at")}


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: float
    source_sha256: str
    # Apple Vision normalized position (0..1): left edge and top edge, origin bottom-left.
    # None when the extractor supplies no layout (manual text, fixtures, reparsed review text).
    x: float | None = None
    y: float | None = None


@dataclass(frozen=True)
class ScreenshotPreview:
    league: str
    sportsbook: str
    captured_at: str
    extracted_at: str
    extractor: str
    files: tuple[ScreenshotFile, ...]
    candidates: tuple[dict, ...]
    teaser_prices: dict
    teaser_states: dict
    warnings: tuple[str, ...]
    recognized_text: tuple[str, ...]


class ScreenshotExtractor(Protocol):
    name: str

    def extract(self, image: ScreenshotFile) -> list[OCRLine]: ...


def _coordinate(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 1 else None


# Fragments whose top edges differ by at most this fraction of the image height share a visual row.
ROW_TOLERANCE = 0.012


def layout_rows(lines: list[OCRLine]) -> list[OCRLine]:
    """Rebuild visual rows from positioned OCR fragments of one screenshot.

    A sportsbook board is read column by column (team, spread, total, moneyline), so each
    row is re-joined left to right before parsing. Unpositioned input is returned unchanged.
    """
    if not lines or any(line.x is None or line.y is None for line in lines):
        return list(lines)
    rows: list[list[OCRLine]] = []
    for line in sorted(lines, key=lambda item: (-item.y, item.x)):
        if rows and abs(rows[-1][0].y - line.y) <= ROW_TOLERANCE:
            rows[-1].append(line)
        else:
            rows.append([line])
    merged = []
    for row in rows:
        row.sort(key=lambda item: item.x)
        merged.append(OCRLine(" ".join(item.text.strip() for item in row),
                              min(item.confidence for item in row), row[0].source_sha256, row[0].x, row[0].y))
    return merged


class AppleVisionExtractor:
    """Optional local macOS Vision OCR. It never sends an image over the network."""
    name = "apple_vision_local"

    def extract(self, image: ScreenshotFile) -> list[OCRLine]:
        suffix = ".png" if image.mime_type == "image/png" else ".jpg"
        path = None
        try:
            module_cache = Path(tempfile.gettempdir()) / "teaser-swift-module-cache"
            module_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                temporary.write(image.content)
                path = temporary.name
            result = subprocess.run(
                ["/usr/bin/xcrun", "swift", str(VISION_SCRIPT), path],
                check=False, capture_output=True, text=True, timeout=45,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": tempfile.gettempdir(),
                     "CLANG_MODULE_CACHE_PATH": str(module_cache),
                     "SWIFT_MODULECACHE_PATH": str(module_cache)},
            )
            if result.returncode:
                raise ScreenshotIngestError("Local Apple Vision OCR is unavailable; use the manual review fallback")
            payload = json.loads(result.stdout)
            if not isinstance(payload, list) or len(payload) > 1000:
                raise ScreenshotIngestError("Local OCR returned an invalid result")
            return [OCRLine(str(item["text"])[:500], float(item["confidence"]), image.sha256,
                            _coordinate(item.get("x")), _coordinate(item.get("y")))
                    for item in payload if isinstance(item, dict) and item.get("text")]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ScreenshotIngestError):
                raise
            raise ScreenshotIngestError("Local Apple Vision OCR is unavailable; use the manual review fallback") from exc
        finally:
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass


class ManualTextExtractor:
    """Functional fallback when local OCR is unavailable; review text is entered by the user."""
    name = "manual_text_review"

    def extract(self, image: ScreenshotFile) -> list[OCRLine]:
        return []


def validate_uploads(uploads: list[tuple[str, str, bytes]], *, now: datetime | None = None) -> tuple[ScreenshotFile, ...]:
    if not uploads:
        raise ScreenshotIngestError("Upload at least one PNG or JPEG screenshot")
    if len(uploads) > MAX_SCREENSHOTS:
        raise ScreenshotIngestError(f"Upload at most {MAX_SCREENSHOTS} screenshots per capture")
    total = sum(len(content) for _, _, content in uploads)
    if total > MAX_TOTAL_BYTES:
        raise ScreenshotIngestError("Combined screenshots exceed the 24 MB limit")
    uploaded_at = (now or datetime.now(timezone.utc)).isoformat()
    checked = []
    for filename, mime_type, content in uploads:
        if mime_type not in ALLOWED_MIME:
            raise ScreenshotIngestError("Only PNG and JPG/JPEG screenshots are supported")
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise ScreenshotIngestError("Each screenshot must be nonempty and at most 8 MB")
        name = PurePath(filename or "").name
        if (name != filename or not name or name in {".", ".."} or "\\" in name
                or len(name) > 160 or any(ord(ch) < 32 for ch in name)):
            raise ScreenshotIngestError("Screenshot filename is invalid")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    if image.format != ALLOWED_MIME[mime_type]:
                        raise ScreenshotIngestError("Image content does not match its PNG/JPEG type")
                    width, height = image.size
                    if width < 50 or height < 50 or width * height > MAX_IMAGE_PIXELS:
                        raise ScreenshotIngestError("Screenshot dimensions are outside the supported range")
                    image.verify()
        except (UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning, OSError) as exc:
            raise ScreenshotIngestError("Screenshot cannot be decoded safely") from exc
        checked.append(ScreenshotFile(name, mime_type, hashlib.sha256(content).hexdigest(),
                                      len(content), width, height, uploaded_at, content))
    if len({item.sha256 for item in checked}) != len(checked):
        raise ScreenshotIngestError("The same screenshot was uploaded more than once")
    return tuple(checked)


def _candidate(away: str, home: str, source: str, confidence: float) -> dict:
    values = {field: None for field in REVIEW_FIELDS}
    values.update(away_team=away, home_team=home)
    states = {field: "missing" for field in REVIEW_FIELDS}
    level = "high_confidence" if confidence >= 0.85 else "uncertain"
    states.update(away_team=level, home_team=level)
    return {**values, "field_states": states, "field_sources": {"away_team": [source], "home_team": [source]},
            "conflicts": {}, "warnings": [], "candidate_id": _identity("cand", {"away": away, "home": home, "source": source})}


def _numbers(text: str) -> list[str]:
    return re.findall(r"(?<![\w.])[+-]\d{2,5}|(?<![\w.])[+-]?\d+(?:\.\d+)?", text)


def _team_text(text: str) -> str:
    marker = re.search(r"(?:\s|^)(?:[+-]\d|ML\b|O\s*\d|U\s*\d|TOTAL\b)", text, re.I)
    name = text[:marker.start()] if marker else text
    name = KICKOFF.sub("", name).strip(" -–|@")
    return ROTATION.sub("", name).strip(" -–|@")


# Board furniture that carries digits but is never a team: game times / TV headers
# ("01:00 PM EST - FOX"), limits ("$2,000"), prop counters ("932+") and column headers.
GAME_HEADER = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b|\b(?:EST|EDT|CST|CDT|MST|MDT|PST|PDT)\b", re.I)
ROTATION = re.compile(r"^\d{3,4}\s+(?=[A-Za-z])")
COLUMN_HEADERS = {"spread", "spreads", "total", "totals", "moneyline", "money line", "ml", "over", "under",
                  "game lines", "game", "handicap", "team", "teams", "more bets", "props"}


def _board_furniture(name: str) -> bool:
    return (not re.search(r"[A-Za-z]{2,}", name) or "$" in name or re.search(r"\d,\d{3}", name) is not None
            or GAME_HEADER.search(name) is not None or name.casefold() in COLUMN_HEADERS)


def _kickoff(text: str) -> str | None:
    match = KICKOFF.search(text)
    if not match:
        return None
    try:
        value = datetime.fromisoformat(match.group(0).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.isoformat()


def _market_values(text: str) -> dict:
    values: dict[str, str] = {}
    spread = re.search(r"(?<!\w)([+-]\d{1,2}(?:\.5)?)\s+([+-]\d{3,5})(?!\d)", text)
    if spread:
        values["spread"] = spread.group(1)
        values["spread_price"] = spread.group(2)
    ml = re.search(r"\bML\s*([+-]\d{3,5})(?!\d)", text, re.I)
    if ml:
        values["moneyline"] = ml.group(1)
    over = re.search(r"\bO(?:VER)?\s*([0-9]+(?:\.5)?)\s*([+-]\d{3,5})?", text, re.I)
    under = re.search(r"\bU(?:NDER)?\s*([0-9]+(?:\.5)?)\s*([+-]\d{3,5})?", text, re.I)
    total = over or under or re.search(r"\bTOTAL\s*([0-9]+(?:\.5)?)", text, re.I)
    if total:
        values["total"] = total.group(1)
    if over and over.group(2):
        values["over_price"] = over.group(2)
    if under and under.group(2):
        values["under_price"] = under.group(2)
    return values


def parse_recognized_lines(lines: list[OCRLine], league: str) -> tuple[list[dict], dict, dict, list[str]]:
    """Conservative layout-agnostic parser; uncertain output always remains reviewable."""
    warnings, team_lines, ignored = [], [], []
    block = 0
    teaser_prices = {"2": None, "3": None}
    teaser_states = {"2": "missing", "3": "missing"}
    for line in lines:
        text = " ".join(line.text.split())
        # Vision commonly inserts a space after a sign or joins spread and price.
        text = re.sub(r"([+-])\s+(?=\d)", r"\1", text)
        text = re.sub(r"([+-]\d{1,2}(?:\.\d+)?)([+-]\d{3,5})(?!\d)", r"\1 \2", text)
        lower = text.casefold()
        teaser_points = re.search(r"\b(6|10|13)(?:\s*[- ]?point|pt)\b", lower)
        teaser_size = re.search(r"\b([23])\s*[- ]?team\b", lower)
        odds = re.findall(r"[+-]\d{3,5}", text)
        if "teaser" in lower:
            if teaser_points and teaser_points.group(1) != "6":
                warnings.append(f"Ignored non-6-point teaser product: {text[:120]}")
            elif teaser_points and teaser_size and odds:
                size = teaser_size.group(1)
                value, state = odds[-1], "high_confidence" if line.confidence >= .85 else "uncertain"
                if teaser_prices[size] not in (None, value):
                    teaser_prices[size], teaser_states[size] = None, "conflict"
                    warnings.append(f"Conflicting {size}-team 6-point teaser prices require review")
                else:
                    teaser_prices[size], teaser_states[size] = value, state
            else:
                warnings.append(f"Unclassified teaser text requires review: {text[:120]}")
            continue
        name = _team_text(text)
        if GAME_HEADER.search(name):
            block += 1  # a new game header: never pair a team row across it
            continue
        if len(name) >= 2 and _numbers(text):
            if _board_furniture(name):
                ignored.append(name)
            else:
                team_lines.append((name, _market_values(text), line, block))
    if ignored:
        warnings.append(f"Ignored {len(ignored)} non-team OCR fragment(s): {', '.join(ignored[:5])}")
    pairs = []
    for current in sorted({entry[3] for entry in team_lines}):
        members = [entry for entry in team_lines if entry[3] == current]
        pairs.extend(zip(members[0::2], members[1::2]))
        if len(members) % 2:
            warnings.append(f"Unpaired OCR row requires manual review: {members[-1][0]}")
    candidates = []
    for (away, away_values, away_line, _), (home, home_values, home_line, _) in pairs:
        if away.casefold() == home.casefold():
            warnings.append(f"Duplicate team OCR row ignored: {away}")
            continue
        confidence = min(away_line.confidence, home_line.confidence)
        row = _candidate(away, home, away_line.source_sha256, confidence)
        row["field_sources"]["home_team"] = [home_line.source_sha256]
        kickoffs = {value for value in (_kickoff(away_line.text), _kickoff(home_line.text)) if value}
        if len(kickoffs) == 1:
            row["kickoff"] = next(iter(kickoffs))
            row["field_states"]["kickoff"] = "high_confidence" if confidence >= .85 else "uncertain"
            row["field_sources"]["kickoff"] = list({away_line.source_sha256, home_line.source_sha256})
        elif len(kickoffs) > 1:
            row["field_states"]["kickoff"] = "conflict"
            row["conflicts"]["kickoff"] = sorted(kickoffs)
            row["warnings"].append("Conflicting kickoff")
        for side, values, source_line in (("away", away_values, away_line), ("home", home_values, home_line)):
            for raw, field in (("spread", f"spread_{side}"), ("spread_price", f"spread_{side}_price"),
                               ("moneyline", f"moneyline_{side}")):
                if raw in values:
                    row[field] = values[raw]
                    row["field_states"][field] = "high_confidence" if source_line.confidence >= .85 else "uncertain"
                    row["field_sources"][field] = [source_line.source_sha256]
        for field in ("total", "over_price", "under_price"):
            values = [(source_line, markets[field]) for source_line, markets in
                      ((away_line, away_values), (home_line, home_values)) if field in markets]
            unique = {value for _, value in values}
            if len(unique) == 1:
                row[field] = next(iter(unique))
                row["field_states"][field] = "high_confidence" if all(line.confidence >= .85 for line, _ in values) else "uncertain"
                row["field_sources"][field] = list({line.source_sha256 for line, _ in values})
            elif len(unique) > 1:
                row["field_states"][field] = "conflict"
                row["conflicts"][field] = sorted(unique)
                row["warnings"].append(f"Conflicting {field.replace('_', ' ')}")
        if not away_values or not home_values:
            row["warnings"].append("Partially read or cut-off market row")
        if row.get("spread_away") is not None and row.get("spread_home") is not None:
            if Decimal(row["spread_away"]) + Decimal(row["spread_home"]) != 0:
                row["warnings"].append("Impossible opposing spread pairing")
                row["field_states"]["spread_away"] = "uncertain"
                row["field_states"]["spread_home"] = "uncertain"
        candidates.append(row)
    if not candidates:
        warnings.append("No complete games were recognized; add rows manually in review")
    return candidates, teaser_prices, teaser_states, warnings


def _same_event(left: dict, right: dict, league: str) -> bool:
    left_teams = (team_key(league, left.get("away_team")), team_key(league, left.get("home_team")))
    right_teams = (team_key(league, right.get("away_team")), team_key(league, right.get("home_team")))
    if None in left_teams or left_teams != right_teams:
        return False
    left_time, right_time = left.get("kickoff"), right.get("kickoff")
    return not left_time or not right_time or left_time == right_time


def merge_candidates(candidates: list[dict], league: str) -> list[dict]:
    merged: list[dict] = []
    for candidate in candidates:
        matches = [row for row in merged if _same_event(row, candidate, league)]
        if len(matches) > 1:
            candidate["warnings"].append("Ambiguous duplicate game; kept separate")
            merged.append(candidate)
            continue
        if not matches:
            merged.append(candidate)
            continue
        target = matches[0]
        for field in REVIEW_FIELDS:
            current, incoming = target.get(field), candidate.get(field)
            sources = candidate.get("field_sources", {}).get(field, [])
            if incoming in (None, ""):
                continue
            if (field in {"away_team", "home_team"} and current not in (None, "")
                    and team_key(league, current) == team_key(league, incoming)):
                target["field_sources"].setdefault(field, []).extend(
                    source for source in sources if source not in target["field_sources"].get(field, []))
                if candidate["field_states"].get(field) == "uncertain":
                    target["field_states"][field] = "uncertain"
                continue
            if current in (None, ""):
                target[field] = incoming
                target["field_states"][field] = candidate["field_states"].get(field, "uncertain")
                target["field_sources"][field] = sources
            elif current == incoming:
                target["field_sources"].setdefault(field, []).extend(
                    source for source in sources if source not in target["field_sources"].get(field, []))
            else:
                target[field] = None
                target["field_states"][field] = "conflict"
                target["conflicts"][field] = sorted({str(current), str(incoming)})
                target["warnings"].append(f"Conflicting {field.replace('_', ' ')}")
        target["warnings"].extend(w for w in candidate.get("warnings", []) if w not in target["warnings"])
    return merged


def extract_screenshots(files: tuple[ScreenshotFile, ...], league: str, sportsbook: str,
                        captured_at: str, extractor: ScreenshotExtractor) -> ScreenshotPreview:
    if league not in {"NFL", "CFB"}:
        raise ScreenshotIngestError("Choose NFL or CFB")
    if not isinstance(sportsbook, str) or not sportsbook.strip() or len(sportsbook.strip()) > 60:
        raise ScreenshotIngestError("Enter the sportsbook shown in these screenshots")
    try:
        captured = datetime.fromisoformat(captured_at)
        if captured.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ScreenshotIngestError("Capture time requires an ISO timestamp with UTC offset") from exc
    all_lines, recognized, warnings = [], [], []
    for image in files:
        lines = layout_rows(extractor.extract(image))
        all_lines.extend(lines)
        recognized.append("\n".join(line.text for line in lines))
        if not lines:
            warnings.append(f"No text recognized in {image.filename}")
    candidates, teaser_prices, teaser_states, parsed_warnings = parse_recognized_lines(all_lines, league)
    warnings.extend(parsed_warnings)
    return ScreenshotPreview(league, sportsbook.strip(), captured.isoformat(), datetime.now(timezone.utc).isoformat(),
                             extractor.name, files, tuple(merge_candidates(candidates, league)),
                             teaser_prices, teaser_states, tuple(warnings), tuple(recognized))


def reparse_review_text(preview: ScreenshotPreview, texts: list[str]) -> ScreenshotPreview:
    """Parse user-corrected OCR text without changing the uploaded-image provenance."""
    if len(texts) != len(preview.files):
        raise ScreenshotIngestError("Review text must correspond to each uploaded screenshot")
    lines = [OCRLine(line.strip(), .5, image.sha256)
             for image, text in zip(preview.files, texts)
             for line in str(text).splitlines() if line.strip()]
    candidates, prices, states, parse_warnings = parse_recognized_lines(lines, preview.league)
    warnings_out = [warning for warning in preview.warnings
                    if not warning.startswith(("No text recognized", "No complete games", "Unpaired OCR"))]
    warnings_out.extend(parse_warnings)
    provider = preview.extractor
    if "manual_text_review" not in provider:
        provider = f"{provider}+manual_text_review"
    return ScreenshotPreview(
        preview.league, preview.sportsbook, preview.captured_at, preview.extracted_at,
        provider, preview.files, tuple(merge_candidates(candidates, preview.league)),
        prices, states, tuple(warnings_out), tuple(str(text) for text in texts),
    )


def _checked(value, field: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    odds = field.endswith("_price") or field.startswith("moneyline_")
    if not (AMERICAN if odds else NUMBER).fullmatch(text):
        raise ScreenshotIngestError(f"{field.replace('_', ' ')} is malformed")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ScreenshotIngestError(f"{field.replace('_', ' ')} is malformed") from exc
    if not number.is_finite() or (odds and abs(number) < 100) or (field == "total" and number <= 0):
        raise ScreenshotIngestError(f"{field.replace('_', ' ')} is outside the supported range")
    return text


def normalize_screenshot_review(preview: ScreenshotPreview, edited_rows: list[dict],
                                teaser_prices: dict) -> dict:
    if not edited_rows:
        raise ScreenshotIngestError("Add at least one reviewed market row")
    if len(edited_rows) > 100:
        raise ScreenshotIngestError("Review has too many market rows")
    events, seen, teams_at_time = [], set(), set()
    originals = {row["candidate_id"]: row for row in preview.candidates}
    for position, edited in enumerate(edited_rows, start=1):
        candidate_id = edited.get("candidate_id")
        original = originals.get(candidate_id)
        away, home = (str(edited.get(field) or "").strip() for field in ("away_team", "home_team"))
        if not away or not home or away.casefold() == home.casefold() or len(away) > 80 or len(home) > 80:
            raise ScreenshotIngestError(f"Row {position} needs distinct away and home teams")
        kickoff = str(edited.get("kickoff") or "").strip()
        try:
            instant = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            if instant.utcoffset() is None:
                raise ValueError
        except ValueError as exc:
            raise ScreenshotIngestError(f"Row {position} kickoff requires an ISO timestamp with UTC offset") from exc
        identity = (team_key(preview.league, away) or away.casefold(),
                    team_key(preview.league, home) or home.casefold(), instant.isoformat())
        if identity in seen:
            raise ScreenshotIngestError("Duplicate reviewed event")
        seen.add(identity)
        for team in identity[:2]:
            team_time = (team, identity[2])
            if team_time in teams_at_time:
                raise ScreenshotIngestError(f"Row {position} repeats a team at the same kickoff")
            teams_at_time.add(team_time)
        record = {field: None for field in FIELDS}
        record.update(event_id=_identity("evt", {"league": preview.league, "away": away,
                                                   "home": home, "kickoff": instant.isoformat()}),
                      league=preview.league, away_team=away, home_team=home,
                      kickoff=instant.isoformat(), captured_at=preview.captured_at,
                      source="user_screenshot", sportsbook=preview.sportsbook,
                      source_type="screenshot", source_provider=preview.extractor,
                      source_url=None, market_role="EXECUTION")
        for field in MARKET_FIELDS:
            record[field] = _checked(edited.get(field), field)
            if original and original.get("field_states", {}).get(field) == "conflict" and record[field] is None:
                raise ScreenshotIngestError(f"Resolve conflicting {field.replace('_', ' ')} in row {position}")
        if record["spread_away"] is not None and record["spread_home"] is not None:
            if Decimal(record["spread_away"]) + Decimal(record["spread_home"]) != 0:
                raise ScreenshotIngestError(
                    f"Row {position} has opposing spreads that do not pair; correct or clear one side")
        changes = {}
        if original:
            for field in REVIEW_FIELDS:
                if str(original.get(field) or "") != str(record.get(field, edited.get(field)) or ""):
                    changes[field] = {"extracted": original.get(field),
                                      "confirmed": record.get(field, edited.get(field))}
        record["review_changes"] = changes
        record["field_extraction_state"] = original.get("field_states", {}) if original else {
            field: "manual" for field in REVIEW_FIELDS}
        record["source_screenshot_hashes"] = sorted({value for values in
            (original.get("field_sources", {}) if original else {}).values() for value in values})
        events.append(record)
    prices = {size: _checked(teaser_prices.get(size), f"teaser_{size}team_6pt_price")
              for size in ("2", "3")}
    payload = {
        "schema_version": 3, "kind": "market_snapshot", "league": preview.league,
        "season": None, "week": None, "captured_at": preview.captured_at,
        "source": "user_screenshot", "source_type": "screenshot",
        "source_provider": preview.extractor, "source_url": None,
        "market_role": "EXECUTION", "sportsbook": preview.sportsbook,
        "line_label": "screenshot_reviewed_execution", "events": events,
        "teaser_prices": {"6_point": {"2_team": prices["2"], "3_team": prices["3"]}},
        "teaser_extraction_state": {"6_point": {
            "2_team": preview.teaser_states.get("2", "missing"),
            "3_team": preview.teaser_states.get("3", "missing"),
        }},
        "teaser_review_changes": {size: {"extracted": preview.teaser_prices.get(size),
                                          "confirmed": prices[size]}
                                  for size in ("2", "3")
                                  if preview.teaser_prices.get(size) != prices[size]},
        "screenshot_provenance": [image.provenance() for image in preview.files],
        "screenshot_count": len(preview.files), "extracted_at": preview.extracted_at,
        "extractor": preview.extractor, "extraction_warnings": list(preview.warnings),
        "raw_screenshots_retained": False,
    }
    for event in events:
        event["teaser_2team_6pt_price"] = prices["2"]
        event["teaser_3team_6pt_price"] = prices["3"]
    snapshot_id = _identity("mkt", payload)
    for event in events:
        event["snapshot_id"] = snapshot_id
    return payload


def save_confirmed_execution(preview: ScreenshotPreview, edited_rows: list[dict],
                             teaser_prices: dict, history) -> dict:
    return history.save_confirmed_execution(normalize_screenshot_review(preview, edited_rows, teaser_prices))
