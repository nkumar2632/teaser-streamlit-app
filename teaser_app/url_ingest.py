"""Allowlisted public-URL fetch, isolated parsing, and explicit review confirmation."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from teaser_app.market_data import FIELDS, _identity
from teaser_app.providers.espn import ESPNParseError, FIELDS as ODDS_FIELDS, parse_scoreboard

MAX_URL = 600
MAX_RESPONSE = 5_000_000
PAGE_PATH = re.compile(r"^/(nfl|college-football)/scoreboard(?:/_/week/([0-9]{1,2})/year/([0-9]{4})/seasontype/([123]))?/?$")
API_PATH = re.compile(r"^/apis/site/v2/sports/football/(nfl|college-football)/scoreboard/?$")
LINE = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$")
AMERICAN = re.compile(r"^[+-][0-9]{2,5}$")


class URLIngestError(ValueError):
    pass


@dataclass(frozen=True)
class RequestSpec:
    source_url: str
    fetch_url: str
    provider: str
    league: str


@dataclass(frozen=True)
class MarketPreview:
    spec: RequestSpec
    captured_at: str
    rows: tuple[dict, ...]
    warnings: tuple[str, ...]


def identify_provider(url: str) -> RequestSpec:
    """Accept only HTTPS ESPN football scoreboards, never arbitrary fetch targets."""
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_URL or url != url.strip():
        raise URLIngestError("Enter a short public ESPN scoreboard URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        invalid_authority = parsed.username or parsed.password or parsed.port not in (None, 443)
    except ValueError as exc:
        raise URLIngestError("Malformed scoreboard URL") from exc
    if (parsed.scheme != "https" or invalid_authority
            or parsed.fragment or parsed.hostname not in {"www.espn.com", "site.api.espn.com"}
            or "%" in parsed.path or "\\" in parsed.path):
        raise URLIngestError("Unsupported URL: use a public HTTPS ESPN football scoreboard")
    host = parsed.hostname
    page = PAGE_PATH.fullmatch(parsed.path) if host == "www.espn.com" else None
    api = API_PATH.fullmatch(parsed.path) if host == "site.api.espn.com" else None
    if not page and not api:
        raise URLIngestError("Unsupported ESPN URL: use an NFL or college-football scoreboard")
    try:
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise URLIngestError("Malformed scoreboard URL query") from exc
    if len(pairs) != len(dict(pairs)):
        raise URLIngestError("Duplicate scoreboard URL parameters")
    params = dict(pairs)
    if set(params) - {"dates", "week", "seasontype", "groups", "group"}:
        raise URLIngestError("Unsupported scoreboard URL parameters")
    if "group" in params and "groups" in params:
        raise URLIngestError("Specify only one college-football group")
    if page:
        kind, week, year, season_type = page.groups()
        if week:
            if "dates" in params or "week" in params or "seasontype" in params:
                raise URLIngestError("Conflicting ESPN week parameters")
            params.update(dates=year, week=week, seasontype=season_type)
    else:
        kind = api.group(1)
    if "group" in params:
        params["groups"] = params.pop("group")
    if "groups" in params and kind != "college-football":
        raise URLIngestError("Groups apply only to college football")
    if ("dates" in params and not re.fullmatch(r"[0-9]{4}(?:[0-9]{4})?", params["dates"])) or (
        "week" in params and not (params["week"].isdigit() and 1 <= int(params["week"]) <= 20)
    ) or ("seasontype" in params and params["seasontype"] not in {"1", "2", "3"}) or (
        "groups" in params and not re.fullmatch(r"[0-9]{1,5}", params["groups"])
    ):
        raise URLIngestError("Malformed ESPN scoreboard date, week, or group")
    endpoint = f"https://site.api.espn.com/apis/site/v2/sports/football/{kind}/scoreboard"
    if params:
        endpoint += "?" + urllib.parse.urlencode(sorted(params.items()))
    return RequestSpec(source_url=url, fetch_url=endpoint, provider="ESPN",
                       league="NFL" if kind == "nfl" else "CFB")


def fetch_url(spec: RequestSpec, *, client=None) -> bytes:
    """One bounded JSON GET to the validated ESPN host; redirects are refused."""
    if identify_provider(spec.fetch_url).fetch_url != spec.fetch_url:
        raise URLIngestError("Invalid provider request")
    try:
        with (client or requests).get(
            spec.fetch_url, headers={"Accept": "application/json"}, timeout=10,
            allow_redirects=False, stream=True,
        ) as response:
            if response.status_code != 200:
                raise URLIngestError("Could not fetch ESPN scoreboard (network, block, or changed endpoint)")
            if "json" not in response.headers.get("Content-Type", "").lower():
                raise URLIngestError("ESPN did not return JSON; request may be blocked")
            length = response.headers.get("Content-Length", "0")
            if length.isdigit() and int(length) > MAX_RESPONSE:
                raise URLIngestError("ESPN response exceeds the 5 MB limit")
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65_536):
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise URLIngestError("ESPN response exceeds the 5 MB limit")
                chunks.append(chunk)
            return b"".join(chunks)
    except (requests.RequestException, OSError) as exc:
        raise URLIngestError("Could not fetch ESPN scoreboard (network, block, or changed endpoint)") from exc


def preview_url(url: str, *, client=None) -> MarketPreview:
    spec = identify_provider(url)
    content = fetch_url(spec, client=client)
    try:
        rows, warnings = parse_scoreboard(content, spec.league)
    except ESPNParseError as exc:
        raise URLIngestError(str(exc)) from exc
    return MarketPreview(spec, datetime.now(timezone.utc).isoformat(), tuple(rows), tuple(warnings))


def _checked_number(value, name: str, *, odds: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise URLIngestError(f"{name} must be a number or blank")
    text = str(value).strip()
    if len(text) > 16 or not (AMERICAN if odds else LINE).fullmatch(text):
        raise URLIngestError(f"{name} is malformed; correct it or leave it blank")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise URLIngestError(f"{name} is malformed") from exc
    if not number.is_finite() or (odds and abs(number) < 100) or (name == "total" and number <= 0):
        raise URLIngestError(f"{name} is outside the supported range")
    return text


def normalize_review(preview: MarketPreview, edited_rows: list[dict]) -> dict:
    """Validate the edited transcription and retain original values as provenance."""
    if len(edited_rows) != len(preview.rows):
        raise URLIngestError("The review must keep each fetched event")
    events, seen_ids, seen_matchups = [], set(), set()
    for original, edited in zip(preview.rows, edited_rows, strict=True):
        if edited.get("source_event_id") != original["source_event_id"]:
            raise URLIngestError("Provider event identity cannot be changed")
        source_id = original["source_event_id"]
        if source_id in seen_ids:
            raise URLIngestError("Duplicate provider events")
        seen_ids.add(source_id)
        away, home = (str(edited.get(field) or "").strip() for field in ("away_team", "home_team"))
        if not away or not home or away.casefold() == home.casefold() or len(away) > 80 or len(home) > 80:
            raise URLIngestError("Each game needs distinct away and home teams")
        kickoff = str(edited.get("kickoff") or "").strip()
        try:
            instant = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            if instant.utcoffset() is None:
                raise ValueError("offset required")
        except ValueError as exc:
            raise URLIngestError("Kickoff requires an ISO timestamp with UTC offset") from exc
        matchup = (away.casefold(), home.casefold(), instant.isoformat())
        if matchup in seen_matchups:
            raise URLIngestError("Duplicate matchup and kickoff in review")
        seen_matchups.add(matchup)
        record = {field: None for field in FIELDS}
        record.update(event_id=_identity("evt", {"league": preview.spec.league,
                                                    "away": away, "home": home,
                                                    "kickoff": instant.isoformat()}),
                      league=preview.spec.league, away_team=away, home_team=home,
                      kickoff=instant.isoformat(), captured_at=preview.captured_at,
                      source="reference_url", sportsbook=original["sportsbook"],
                      source_event_id=source_id, source_provider=preview.spec.provider,
                      source_url=preview.spec.source_url, source_type="url",
                      market_role="REFERENCE")
        for field in ODDS_FIELDS:
            record[field] = _checked_number(edited.get(field), field,
                                            odds=field.endswith("_price") or field.startswith("moneyline_"))
        record["teaser_2team_6pt_price"] = None
        record["teaser_3team_6pt_price"] = None
        record["review_changes"] = {field: {"fetched": original.get(field), "confirmed": record.get(field, edited.get(field))}
                                    for field in ("away_team", "home_team", "kickoff", *ODDS_FIELDS)
                                    if str(original.get(field) or "") != str(record.get(field, edited.get(field)) or "")}
        events.append(record)
    seasons = {row["season"] for row in preview.rows if type(row.get("season")) is int}
    weeks = {row["week"] for row in preview.rows if type(row.get("week")) is int}
    books = {row["sportsbook"] for row in preview.rows if row.get("sportsbook")}
    payload = {
        "schema_version": 2, "kind": "market_snapshot", "league": preview.spec.league,
        "season": next(iter(seasons)) if len(seasons) == 1 else None,
        "week": next(iter(weeks)) if len(weeks) == 1 else None,
        "captured_at": preview.captured_at, "source": "reference_url", "source_type": "url",
        "source_provider": preview.spec.provider, "source_url": preview.spec.source_url,
        "fetched_url": preview.spec.fetch_url, "market_role": "REFERENCE",
        "reference_source": preview.spec.provider,
        "sportsbook": next(iter(books)) if len(books) == 1 else None,
        "line_label": "reference_at_fetch", "events": events,
    }
    snapshot_id = _identity("mkt", payload)
    for event in events:
        event["snapshot_id"] = snapshot_id
    return payload


def save_confirmed_snapshot(preview: MarketPreview, edited_rows: list[dict], history) -> dict:
    """Only this explicit call persists a URL preview; never invokes a model."""
    return history.save_confirmed_reference(normalize_review(preview, edited_rows))
