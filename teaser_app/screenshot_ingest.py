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
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePath
from typing import Protocol
from unicodedata import normalize
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from teaser_app.market_compare import NFL_NAMES, _clean, team_key
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
    known_teams: tuple[str, ...] = ()


class ScreenshotExtractor(Protocol):
    name: str

    def extract(self, image: ScreenshotFile) -> list[OCRLine]: ...


def _coordinate(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 1 else None


# Fragments whose top edges differ by at most this fraction of the image height share a visual row.
ROW_TOLERANCE = 0.012


# Board layout: team names sit in a left column; PROPS buttons and market cells sit to the right.
SPREAD_CELL = re.compile(r"^[+-]\s?\d{1,2}\S{0,3}\s+[+-]\d{3}")
PROPS_CELL = re.compile(r"^PRO(?:PS?)?$", re.I)
MARKET_CELL = re.compile(r"[+-]\d{3}|^[OU0]\s?\d", re.I)
# Thresholds are fractions of the measured row pitch (spacing of consecutive team rows), so they hold
# for any screenshot height or zoom level.
LINE_TOLERANCE = 0.35    # fragments of one left-column line (logo text + team name) share a top edge
ASSIGN_LIMIT = 0.9       # a market cell farther than this from every left-column line is not placed
ASSIGN_MARGIN = 0.15     # the nearest line must beat the runner-up by this much, else the cell is ambiguous


def _row_pitch(tops: list[float]) -> float | None:
    gaps = sorted(b - a for a, b in zip(tops, tops[1:]) if b - a > 0.001)
    return gaps[len(gaps) // 4] if gaps else None


def board_rows(lines: list[OCRLine]) -> tuple[list[OCRLine], list[OCRLine]]:
    """Rebuild sportsbook rows from positioned fragments of one screenshot.

    Returns (rows, unplaced market cells). When a spread column is visible, every left-column
    line (team, game header, date header) becomes one row and each cell to its right joins the
    line whose top edge is nearest; a cell that is too far or equally near two lines is not
    guessed onto either. Without a visible spread column, fragments are grouped by top edge.
    Unpositioned input is returned unchanged.
    """
    if not lines or any(line.x is None or line.y is None for line in lines):
        return list(lines), []
    spread_cells = [line for line in lines if SPREAD_CELL.match(line.text.strip())]
    props_x = sorted(line.x for line in lines if PROPS_CELL.match(line.text.strip()))
    columns = [line.x for line in spread_cells] or props_x
    team_cells = [line for line in lines if (RECORD.search(line.text) or RANK.search(line.text))
                  and line.x < min(columns, default=0)]
    pitch = _row_pitch(sorted(line.y for line in (spread_cells if len(spread_cells) >= 2 else team_cells)))
    if not columns or pitch is None or (len(spread_cells) < 2 and len(team_cells) < 2):
        return _tolerance_rows(lines), []
    spread_x = sorted(line.x for line in spread_cells) or [1.0]
    boundary = min(spread_x[len(spread_x) // 2],
                   props_x[len(props_x) // 2] if props_x else 1.0) - 0.01
    # Team names share one left column; short text starting clearly left of it is a logo read as text.
    name_x = sorted(line.x for line in team_cells)
    name_column = name_x[len(name_x) // 2] if name_x else None
    groups: list[list[OCRLine]] = []
    for line in sorted((item for item in lines if item.x < boundary), key=lambda item: (-item.y, item.x)):
        if groups and abs(groups[-1][0].y - line.y) <= LINE_TOLERANCE * pitch:
            groups[-1].append(line)
        else:
            groups.append([line])
    if not groups:
        return _tolerance_rows(lines), []
    tops = [max(group, key=lambda item: len(item.text.strip())).y for group in groups]
    # Odds never sit on game-time or date header rows, nor on stray logo text: market cells may
    # only join a left-column line that reads like a team name.
    joined = [" ".join(item.text for item in group) for group in groups]
    team_lines = [index for index, text in enumerate(joined)
                  if re.search(r"[A-Za-z]{3,}", text) and not GAME_HEADER.search(text)
                  and not DATE_HEADER.search(text)]
    attached: list[list[OCRLine]] = [[] for _ in groups]
    unplaced = []
    for cell in (item for item in lines if item.x >= boundary):
        market = MARKET_CELL.search(cell.text) is not None
        ranked = sorted((abs(tops[index] - cell.y), index)
                        for index in (team_lines if market else range(len(groups))))
        if not ranked or ranked[0][0] > ASSIGN_LIMIT * pitch or (
                len(ranked) > 1 and ranked[1][0] - ranked[0][0] < ASSIGN_MARGIN * pitch):
            if market:
                unplaced.append(cell)
            continue
        attached[ranked[0][1]].append(cell)
    rows = []
    for index, (group, top, cells) in enumerate(zip(groups, tops, attached)):
        if name_column is not None and index in team_lines and any(
                item.x >= name_column - 0.005 for item in group):
            group = [item for item in group if not (
                item.x < name_column - 0.01 and len(re.sub(r"\W", "", item.text)) <= 4)] or group
        parts = sorted(group, key=lambda item: item.x) + sorted(cells, key=lambda item: item.x)
        rows.append(OCRLine(" ".join(item.text.strip() for item in parts),
                            min(item.confidence for item in parts), group[0].source_sha256,
                            min(item.x for item in group), top))
    return rows, unplaced


def layout_rows(lines: list[OCRLine]) -> list[OCRLine]:
    """Rebuild visual rows from positioned OCR fragments of one screenshot (see board_rows)."""
    return board_rows(lines)[0]


def _tolerance_rows(lines: list[OCRLine]) -> list[OCRLine]:
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


# Bluecoins board context: a date header ("SUNDAY, SEP 27") and per-game time headers
# ("01:00 PM EST - FOX"). Board times are read as America/New_York wall-clock times.
DATE_HEADER = re.compile(r"\b(MON|TUE|WED|THU|FRI|SAT|SUN)[A-Z]*\.?,?\s+"
                         r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(\d{1,2})\b", re.I)
GAME_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\s*(EST|EDT|ET)\b", re.I)
BOARD_ZONE = ZoneInfo("America/New_York")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _half_points(text: str) -> str:
    """Bluecoins prints half points as ½ (-7½, 42½, +½); parse them as exact .5 values."""
    text = re.sub(r"(?<=\d)\s?½", ".5", text)
    return re.sub(r"(?<![\d.])½", "0.5", text)


def _board_date(match: re.Match, reference: datetime | None) -> date | None:
    """Resolve "SUNDAY, SEP 27" against the capture date; None unless the weekday agrees."""
    if reference is None:
        return None
    month, day = MONTHS.index(match.group(2).upper()[:3]) + 1, int(match.group(3))
    options = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            options.append(date(year, month, day))
        except ValueError:
            continue
    if not options:
        return None
    chosen = min(options, key=lambda option: abs((option - reference.date()).days))
    return chosen if chosen.strftime("%a").upper() == match.group(1).upper()[:3] else None


def _header_kickoff(text: str, board_date: date | None) -> str | None:
    match = GAME_TIME.search(text)
    if not match or board_date is None:
        return None
    hour, minute = int(match.group(1)) % 12 + (12 if match.group(3).upper() == "PM" else 0), int(match.group(2))
    if minute > 59 or int(match.group(1)) > 12:
        return None
    return datetime(board_date.year, board_date.month, board_date.day, hour, minute,
                    tzinfo=BOARD_ZONE).isoformat()


def _nfl_teams_in(name: str) -> list[str]:
    padded = f" {_clean(name)} "
    return [full for full in NFL_NAMES.values() if f" {_clean(full)} " in padded]


# CFB team cells carry Bluecoins UI noise around the school name: logo OCR text, a ranking
# ("#13"), a record ("(3-0)", often read with square brackets), the PROPS button and its counter.
RANK = re.compile(r"#\s?\d{1,2}\b")
RECORD = re.compile(r"[(\[{]\s*\d{1,2}\s*-\s*\d{1,2}\s*[)\]}]?")
BUTTON_TOKEN = re.compile(r"(?i)pro\w*|\d{2,5}\+?|[v⌄˅]")


def _team_tokens(name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", normalize("NFKC", name).casefold().replace("&", " and "))
    return ["st" if token == "state" else token for token in tokens]


def _logo_noise(token: str) -> bool:
    return (not re.search(r"[A-Za-z]", token) or re.search(r"[^A-Za-z&.'-]", token) is not None
            or len(token) == 1 or (token[0].islower() and len(token) <= 3)
            or (len(token) == 2 and not token.isupper()))


def _clean_cfb_name(raw: str, known: frozenset[tuple[str, ...]]) -> tuple[str, str | None]:
    """Return (school name, review note or None). Never invents a name that is not in the text."""
    text = normalize("NFKC", raw)
    ranks = list(RANK.finditer(text))
    if ranks:  # anything before the ranking is logo text
        text = text[ranks[-1].end():]
    record = RECORD.search(text)
    if record:  # the record ends the school name; PROPS/counters follow it
        text = text[:record.start()]
    tokens = text.split()
    while tokens and BUTTON_TOKEN.fullmatch(tokens[-1]):
        tokens.pop()
    if not tokens:
        return raw.strip(), "CFB team not recognized in OCR row; review team"
    if known:
        for start in range(len(tokens)):
            suffix = tokens[start:]
            if tuple(_team_tokens(" ".join(suffix))) in known and all(_logo_noise(t) or len(t) <= 3 for t in tokens[:start]):
                return " ".join(suffix), None
    stripped = []
    while len(tokens) > 1 and _logo_noise(tokens[0]):
        stripped.append(tokens.pop(0))
    name = " ".join(tokens).strip(" -–|@")
    # Symbols, digits or a lowercase start can never begin a school name; a short capitalized
    # word ("Bg", "W", "St") could, so removing one is flagged for review.
    if any(re.fullmatch(r"[A-Z][A-Za-z]?", token) for token in stripped):
        return name, "CFB team name cleaned from logo OCR text; verify team"
    if not ranks and len(tokens) > 1 and len(tokens[0]) <= 3 and (tokens[0].isupper() or len(tokens) > 2):
        return name, "Possible logo text in CFB team name; verify team"
    if known and tuple(_team_tokens(name)) not in known:
        return name, "CFB team not found in saved ESPN teams; verify team"
    return name, None


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
    over = (re.search(r"\bO(?:VER)?\s*([0-9]+(?:\.5)?)(?![0-9./%,½])\s*([+-]\d{3,5})?", text, re.I)
            # Bluecoins' O renders like a zero ("0 42.5 -110", or glued "042.5 -110"): accept it
            # only as a whole total cell with a two-digit total and a price.
            or re.search(r"(?<![\w.+-])0\s?([1-9][0-9](?:\.5)?)(?![0-9./%,½])\s+([+-]\d{3,5})", text))
    under = re.search(r"\bU(?:NDER)?\s*([0-9]+(?:\.5)?)(?![0-9./%,½])\s*([+-]\d{3,5})?", text, re.I)
    # A row has one game-total cell; later total-like cells belong to other markets (e.g. halves).
    main = min((match for match in (over, under) if match), key=lambda match: match.start(), default=None)
    total = main or re.search(r"\bTOTAL\s*([0-9]+(?:\.5)?)", text, re.I)
    if total:
        values["total"] = total.group(1)
    if main is over and over and over.group(2):
        values["over_price"] = over.group(2)
    if main is under and under and under.group(2):
        values["under_price"] = under.group(2)
    return values


def parse_recognized_lines(lines: list[OCRLine], league: str, *, captured_at: str | None = None,
                           known_teams: tuple[str, ...] = ()) -> tuple[list[dict], dict, dict, list[str]]:
    """Conservative layout-agnostic parser; uncertain output always remains reviewable."""
    warnings, team_lines, ignored = [], [], []
    block = 0
    known = frozenset(tuple(_team_tokens(name)) for name in known_teams if name and _team_tokens(name))
    source, screen, date_screen = None, -1, None
    block_screen: dict[int, int] = {}
    try:
        reference = datetime.fromisoformat(captured_at).astimezone(BOARD_ZONE) if captured_at else None
    except (TypeError, ValueError):
        reference = None
    board_date = None
    block_kickoffs: dict[int, tuple[str | None, OCRLine]] = {}
    teaser_prices = {"2": None, "3": None}
    teaser_states = {"2": "missing", "3": "missing"}
    for line in lines:
        if line.source_sha256 != source:
            # A new screenshot never continues the previous screenshot's pairing block.
            source, screen = line.source_sha256, screen + 1
            block += 1
        block_screen[block] = screen
        text = " ".join(line.text.split())
        # Vision commonly inserts a space after a sign or joins spread and price.
        text = _half_points(text)
        text = re.sub(r"(?<=\d)[v⌄˅](?=\s|$)", "", text)  # dropdown chevron glued to a price
        text = re.sub(r"([+-])\s+(?=\d)", r"\1", text)
        text = re.sub(r"([+-]\d{1,2}(?:\.\d+)?)([+-]\d{3,5})(?!\d)", r"\1 \2", text)
        lower = text.casefold()
        teaser_points = re.search(r"\b(6|10|13)(?:\s*[- ]?point|pt)\b", lower)
        teaser_size = re.search(r"\b([23])\s*[- ]?team\b", lower)
        odds = re.findall(r"[+-]\d{3,5}", text)
        if re.search(r"\b(?:straight|parlay|if bet|reverse)\b", lower):
            continue  # sportsbook navigation tabs, not a teaser menu
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
        dated = DATE_HEADER.search(text)
        if dated:
            board_date, date_screen = _board_date(dated, reference), screen
            if board_date is None:
                warnings.append(f"Board date could not be resolved; enter kickoffs in review: {dated.group(0)}")
        name = _team_text(text)
        if GAME_HEADER.search(name):
            block += 1  # a new game header: never pair a team row across it
            block_screen[block] = screen
            block_kickoffs[block] = (_header_kickoff(text, board_date), line,
                                     board_date is not None and date_screen != screen)
            continue
        if dated:
            continue
        if len(name) >= 2 and _numbers(text):
            if _board_furniture(name):
                ignored.append(name)
                continue
            note = None
            if league == "NFL" and not team_key("NFL", name):
                found = _nfl_teams_in(name)
                if len(found) == 1:
                    name = found[0]
                else:
                    note = ("No NFL team recognized in OCR row; review team" if not found
                            else "Several NFL teams in one OCR row; review team")
            elif league == "CFB":
                name, note = _clean_cfb_name(name, known)
            team_lines.append((name, _market_values(text), line, block, note))
    if ignored:
        warnings.append(f"Ignored {len(ignored)} non-team OCR fragment(s): {', '.join(ignored[:5])}")
    pairs = []
    for current in sorted({entry[3] for entry in team_lines}):
        members = [entry for entry in team_lines if entry[3] == current]
        pairs.extend(zip(members[0::2], members[1::2]))
        if len(members) % 2:
            warnings.append(f"Unpaired OCR row requires manual review: {members[-1][0]}")
    candidates, inherited_rows = [], []
    for (away, away_values, away_line, pair_block, away_note), (home, home_values, home_line, _, home_note) in pairs:
        if away.casefold() == home.casefold():
            warnings.append(f"Duplicate team OCR row ignored: {away}")
            continue
        confidence = min(away_line.confidence, home_line.confidence)
        row = _candidate(away, home, away_line.source_sha256, confidence)
        row["field_sources"]["home_team"] = [home_line.source_sha256]
        for side, note in (("away_team", away_note), ("home_team", home_note)):
            if note:
                row["field_states"][side] = "uncertain"
                row["warnings"].append(note)
        kickoffs = {value for value in (_kickoff(away_line.text), _kickoff(home_line.text)) if value}
        if len(kickoffs) == 1:
            row["kickoff"] = next(iter(kickoffs))
            row["field_states"]["kickoff"] = "high_confidence" if confidence >= .85 else "uncertain"
            row["field_sources"]["kickoff"] = list({away_line.source_sha256, home_line.source_sha256})
        elif len(kickoffs) > 1:
            row["field_states"]["kickoff"] = "conflict"
            row["conflicts"]["kickoff"] = sorted(kickoffs)
            row["warnings"].append("Conflicting kickoff")
        elif pair_block in block_kickoffs:
            header_kickoff, header_line, inherited = block_kickoffs[pair_block]
            if header_kickoff:
                row["kickoff"] = header_kickoff
                row["field_states"]["kickoff"] = ("high_confidence" if header_line.confidence >= .85
                                                  else "uncertain")
                row["field_sources"]["kickoff"] = [header_line.source_sha256]
                if inherited:
                    inherited_rows.append((block_screen[pair_block], row))
            else:
                row["warnings"].append("Kickoff header could not be resolved; enter kickoff in review")
        else:
            row["warnings"].append("Game header not visible in this screenshot; enter kickoff in review")
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
    _check_carried_dates(candidates, inherited_rows, block_screen, pairs, league, warnings)
    if not candidates:
        warnings.append("No complete games were recognized; add rows manually in review")
    return candidates, teaser_prices, teaser_states, warnings


def _check_carried_dates(candidates, inherited_rows, block_screen, pairs, league, warnings) -> None:
    """A later screenshot without its own date header reuses the previous board date only when it
    overlaps the previous screenshot (a shared game proves the board is continuous)."""
    if not inherited_rows:
        return
    by_screen: dict[int, set] = {}
    for (away, _, _, pair_block, _), (home, *_ ) in pairs:
        key = (team_key(league, away) or away.casefold(), team_key(league, home) or home.casefold())
        by_screen.setdefault(block_screen[pair_block], set()).add(key)
    unproven = {screen for screen, _ in inherited_rows
                if not by_screen.get(screen, set()) & by_screen.get(screen - 1, set())}
    for screen, row in inherited_rows:
        if screen in unproven:
            row["field_states"]["kickoff"] = "uncertain"
            row["warnings"].append("Board date carried from a previous screenshot without overlap; verify kickoff")
    if unproven:
        warnings.append("A screenshot without its own date header does not overlap the previous one; "
                        "upload screenshots in board order with one shared game")


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


def known_cfb_teams(history) -> tuple[str, ...]:
    """School names already on file: ESPN CFB REFERENCE snapshots and operator-confirmed CFB rows."""
    names = set()
    for snapshot in history.market_history(league="CFB"):
        for event in snapshot.get("events", []):
            if not isinstance(event, dict):
                continue
            for field in ("away_school", "home_school", "away_team", "home_team"):
                value = event.get(field)
                if isinstance(value, str) and value.strip() and len(value) <= 80:
                    names.add(" ".join(value.split()))
    return tuple(sorted(names))


def extract_screenshots(files: tuple[ScreenshotFile, ...], league: str, sportsbook: str,
                        captured_at: str, extractor: ScreenshotExtractor, *,
                        known_teams: tuple[str, ...] = ()) -> ScreenshotPreview:
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
    known_teams = tuple(str(name) for name in known_teams if name)
    for image in files:
        lines, unplaced = board_rows(extractor.extract(image))
        all_lines.extend(lines)
        recognized.append("\n".join(line.text for line in lines))
        if not lines:
            warnings.append(f"No text recognized in {image.filename}")
        if unplaced:
            warnings.append(f"{len(unplaced)} market cell(s) in {image.filename} could not be placed on a "
                            f"single row and were not used: {', '.join(cell.text for cell in unplaced[:4])}")
    candidates, teaser_prices, teaser_states, parsed_warnings = parse_recognized_lines(
        all_lines, league, captured_at=captured.isoformat(), known_teams=known_teams)
    warnings.extend(parsed_warnings)
    return ScreenshotPreview(league, sportsbook.strip(), captured.isoformat(), datetime.now(timezone.utc).isoformat(),
                             extractor.name, files, tuple(merge_candidates(candidates, league)),
                             teaser_prices, teaser_states, tuple(warnings), tuple(recognized), known_teams)


def reparse_review_text(preview: ScreenshotPreview, texts: list[str]) -> ScreenshotPreview:
    """Parse user-corrected OCR text without changing the uploaded-image provenance."""
    if len(texts) != len(preview.files):
        raise ScreenshotIngestError("Review text must correspond to each uploaded screenshot")
    lines = [OCRLine(line.strip(), .5, image.sha256)
             for image, text in zip(preview.files, texts)
             for line in str(text).splitlines() if line.strip()]
    candidates, prices, states, parse_warnings = parse_recognized_lines(
        lines, preview.league, captured_at=preview.captured_at, known_teams=preview.known_teams)
    warnings_out = [warning for warning in preview.warnings
                    if not warning.startswith(("No text recognized", "No complete games", "Unpaired OCR"))]
    warnings_out.extend(parse_warnings)
    provider = preview.extractor
    if "manual_text_review" not in provider:
        provider = f"{provider}+manual_text_review"
    return ScreenshotPreview(
        preview.league, preview.sportsbook, preview.captured_at, preview.extracted_at,
        provider, preview.files, tuple(merge_candidates(candidates, preview.league)),
        prices, states, tuple(warnings_out), tuple(str(text) for text in texts), preview.known_teams,
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


def _capture_key(payload: dict) -> tuple:
    """What was captured and confirmed, without extraction-run metadata (times, warnings)."""
    events = sorted((tuple(str(event.get(field) or "") for field in ("away_team", "home_team", "kickoff",
                                                                     *MARKET_FIELDS))
                     for event in payload.get("events", [])))
    hashes = sorted(image.get("sha256", "") for image in payload.get("screenshot_provenance", []))
    menu = (payload.get("teaser_prices") or {}).get("6_point") or {}
    return (payload.get("league"), payload.get("sportsbook"), payload.get("captured_at"),
            tuple(hashes), tuple(events), menu.get("2_team"), menu.get("3_team"))


def save_confirmed_execution(preview: ScreenshotPreview, edited_rows: list[dict],
                             teaser_prices: dict, history) -> dict:
    """Save once per confirmed capture: re-confirming the same screenshots and values reuses it."""
    payload = normalize_screenshot_review(preview, edited_rows, teaser_prices)
    key = _capture_key(payload)
    for existing in history.market_history(league=payload["league"], role="EXECUTION"):
        if existing.get("source_type") == "screenshot" and _capture_key(existing) == key:
            return existing
    return history.save_confirmed_execution(payload)
