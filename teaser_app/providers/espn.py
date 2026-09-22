"""Parse ESPN's public football scoreboard JSON; never follow sportsbook links."""

from __future__ import annotations

import json
import re
from datetime import datetime


class ESPNParseError(ValueError):
    pass


FIELDS = (
    "spread_away", "spread_away_price", "spread_home", "spread_home_price",
    "moneyline_away", "moneyline_home", "total", "over_price", "under_price",
)


def _as_text(value) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _nested(record: dict, *keys):
    for key in keys:
        if not isinstance(record, dict):
            return None
        record = record.get(key)
    return record


def parse_scoreboard(raw: bytes, league: str) -> tuple[list[dict], list[str]]:
    """Return transcription rows and warnings, without selection or persistence."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ESPNParseError("ESPN returned malformed scoreboard JSON") from exc
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not events:
        raise ESPNParseError("No games found in the ESPN scoreboard response")
    if len(events) > 200:
        raise ESPNParseError("ESPN returned too many games for one review")
    rows, warnings = [], []
    event_ids, matchups = set(), set()
    for event in events:
        try:
            source_id = str(event["id"])
            competition = event["competitions"][0]
            competitors = {side["homeAway"]: side for side in competition["competitors"]}
            away = competitors["away"]["team"]["displayName"].strip()
            home = competitors["home"]["team"]["displayName"].strip()
            kickoff = event["date"]
            if not source_id or not away or not home or away == home:
                raise ValueError("missing or duplicate teams")
            when = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            if when.utcoffset() is None:
                raise ValueError("kickoff lacks offset")
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            raise ESPNParseError("ESPN event is missing matchup, kickoff, or event identity") from exc
        if source_id in event_ids or (away.casefold(), home.casefold(), when.isoformat()) in matchups:
            raise ESPNParseError("Duplicate events in ESPN response")
        event_ids.add(source_id)
        matchups.add((away.casefold(), home.casefold(), when.isoformat()))
        odds_list = competition.get("odds") or []
        if not isinstance(odds_list, list):
            raise ESPNParseError("ESPN odds structure changed")
        if len(odds_list) > 1:
            warnings.append(f"{away} at {home}: multiple displayed odds sets; review the first provider carefully")
        odds = odds_list[0] if odds_list else {}
        if not isinstance(odds, dict):
            raise ESPNParseError("ESPN odds structure changed")
        sportsbook = _as_text(_nested(odds, "provider", "displayName") or _nested(odds, "provider", "name"))
        row = {
            "source_event_id": source_id, "league": league,
            "away_team": away, "home_team": home, "kickoff": when.isoformat(),
            "sportsbook": sportsbook,
            "spread_away": _as_text(_nested(odds, "pointSpread", "away", "close", "line")),
            "spread_away_price": _as_text(_nested(odds, "pointSpread", "away", "close", "odds")),
            "spread_home": _as_text(_nested(odds, "pointSpread", "home", "close", "line")),
            "spread_home_price": _as_text(_nested(odds, "pointSpread", "home", "close", "odds")),
            "moneyline_away": _as_text(_nested(odds, "moneyline", "away", "close", "odds")),
            "moneyline_home": _as_text(_nested(odds, "moneyline", "home", "close", "odds")),
            "total": _as_text(odds.get("overUnder")),
            "over_price": _as_text(_nested(odds, "total", "over", "close", "odds")),
            "under_price": _as_text(_nested(odds, "total", "under", "close", "odds")),
            "season": _nested(event, "season", "year"),
            "week": _nested(event, "week", "number"),
        }
        missing = [field for field in FIELDS if row[field] is None]
        if missing:
            warnings.append(f"{away} at {home}: missing {', '.join(missing)}")
        malformed = [field for field in FIELDS if row[field] is not None and not re.fullmatch(
            r"[+-][0-9]{2,5}" if field.endswith("_price") or field.startswith("moneyline_")
            else r"[+-]?[0-9]+(?:\.[0-9]+)?", row[field]
        )]
        if malformed:
            warnings.append(f"{away} at {home}: malformed {', '.join(malformed)}; correct before confirming")
        rows.append(row)
    return rows, warnings
