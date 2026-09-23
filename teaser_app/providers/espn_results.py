"""Parse public ESPN football scores without treating partial scores as finals."""

from __future__ import annotations

import json
from datetime import datetime


class ESPNResultError(ValueError):
    pass


def _status(value: object) -> str:
    if not isinstance(value, dict):
        return "UNKNOWN"
    kind = value.get("type")
    if not isinstance(kind, dict):
        return "UNKNOWN"
    name = str(kind.get("name") or "").upper()
    state = str(kind.get("state") or "").lower()
    if any(part in name for part in ("CANCEL", "POSTPON", "SUSPEND")):
        return "CANCELLED" if "CANCEL" in name else "POSTPONED"
    if kind.get("completed") is True and state == "post" and name.startswith("STATUS_FINAL"):
        return "FINAL"
    if state == "in":
        return "IN_PROGRESS"
    if state == "pre":
        return "SCHEDULED"
    return "UNKNOWN"


def _score(value: object) -> int | None:
    if isinstance(value, dict):
        value = value.get("displayValue")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text.isdigit():
        return None
    number = int(text)
    return number if 0 <= number <= 200 else None


def parse_results(raw: bytes, league: str) -> tuple[list[dict], list[str]]:
    if league not in {"NFL", "CFB"}:
        raise ESPNResultError("Choose NFL or CFB")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ESPNResultError("ESPN returned malformed scoreboard JSON") from exc
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise ESPNResultError("ESPN scoreboard structure changed")
    if len(events) > 200:
        raise ESPNResultError("ESPN returned too many games for one review")
    rows, warnings, ids, matchups = [], [], set(), set()
    for index, event in enumerate(events, 1):
        try:
            competition = event["competitions"][0]
            competitors = {side["homeAway"]: side for side in competition["competitors"]}
            away = competitors["away"]["team"]["displayName"].strip()
            home = competitors["home"]["team"]["displayName"].strip()
            kickoff = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            if (not away or not home or away == home or kickoff.utcoffset() is None):
                raise ValueError
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            raise ESPNResultError(f"ESPN game {index} is missing matchup or kickoff") from exc
        event_id = str(event.get("id") or "").strip() or None
        status = _status(event.get("status") or competition.get("status"))
        away_score = _score(competitors["away"].get("score")) if status == "FINAL" else None
        home_score = _score(competitors["home"].get("score")) if status == "FINAL" else None
        issues = []
        if status == "FINAL" and (away_score is None or home_score is None):
            issues.append("final status has missing or invalid scores")
            status = "UNKNOWN"
            away_score = home_score = None
        identity = (away.casefold(), home.casefold(), kickoff.isoformat())
        if (event_id and event_id in ids) or identity in matchups:
            issues.append("duplicate ESPN game")
        if event_id:
            ids.add(event_id)
        matchups.add(identity)
        if issues:
            warnings.append(f"{away} at {home}: {', '.join(issues)}")
        rows.append({"source_event_id": event_id, "league": league,
                     "away_team": away, "home_team": home, "kickoff": kickoff.isoformat(),
                     "status": status, "away_score": away_score, "home_score": home_score,
                     "issues": issues})
    return rows, warnings
