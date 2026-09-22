"""Provider-independent market matching and literal line differences; no model decisions."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from teaser_app.market_data import snapshot_role

KICKOFF_TOLERANCE = timedelta(minutes=15)
STALE_AFTER = timedelta(hours=24)

NFL_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}


def _clean(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", name.casefold()))


NFL_ALIASES = {_clean(alias): code for code, full in NFL_NAMES.items()
               for alias in (code, full)}
NFL_ALIASES.update({_clean(alias): code for alias, code in {
    "LAR": "LA", "WSH": "WAS", "JAC": "JAX", "SFO": "SF", "TAM": "TB",
}.items()})
CFB_ALIASES = {
    _clean("Texas Longhorns"): "texas longhorns", _clean("Texas"): "texas longhorns",
    _clean("Tennessee Volunteers"): "tennessee volunteers",
    _clean("Tennessee"): "tennessee volunteers",
}


def team_key(league: str, name: str) -> str | None:
    """Recognize only explicit NFL aliases; CFB defaults to exact normalized names."""
    if not isinstance(name, str) or not name.strip():
        return None
    cleaned = _clean(name)
    if league == "NFL":
        return NFL_ALIASES.get(cleaned)
    if league == "CFB":
        return CFB_ALIASES.get(cleaned, cleaned)
    return None


def _matchup(event: dict, league: str) -> tuple[str, str] | None:
    away = team_key(league, event.get("away_team"))
    home = team_key(league, event.get("home_team"))
    return (away, home) if away and home and away != home else None


def _time(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(timezone.utc) if result.utcoffset() is not None else None


def _compatible(reference: dict, execution: dict, league: str) -> bool:
    matchup = _matchup(reference, league)
    if matchup is None or matchup != _matchup(execution, league):
        return False
    first, second = _time(reference.get("kickoff")), _time(execution.get("kickoff"))
    return first is not None and second is not None and abs(first - second) <= KICKOFF_TOLERANCE


def _reason(event: dict, others: list[dict], league: str) -> str:
    matchup = _matchup(event, league)
    if matchup is None:
        return "unrecognized team identity"
    same = [other for other in others if _matchup(other, league) == matchup]
    if same:
        if any(_time(item.get("kickoff")) is None for item in (event, *same)):
            return "kickoff missing or invalid — review required"
        return "conflicting kickoff — review required"
    if any(_matchup(other, league) == matchup[::-1] for other in others):
        return "home/away conflict — review required"
    return "no corresponding game"


def match_events(reference: list[dict], execution: list[dict], league: str) -> dict:
    """Pair only unique reciprocal matches by league, explicit team identity, and kickoff."""
    if league not in {"NFL", "CFB"}:
        raise ValueError("unsupported league")
    ref_candidates = [[j for j, other in enumerate(execution) if _compatible(event, other, league)]
                      for event in reference]
    exe_candidates = [[i for i, other in enumerate(reference) if _compatible(other, event, league)]
                      for event in execution]
    matches, matched_ref, matched_exe = [], set(), set()
    for i, candidates in enumerate(ref_candidates):
        if len(candidates) == 1 and exe_candidates[candidates[0]] == [i]:
            j = candidates[0]
            matches.append((reference[i], execution[j]))
            matched_ref.add(i)
            matched_exe.add(j)
    def unmatched(events, candidates, matched, others):
        return [{"event": event,
                 "reason": "ambiguous match — review required" if candidates[index]
                 else _reason(event, others, league)}
                for index, event in enumerate(events) if index not in matched]
    return {"matches": matches,
            "reference_unmatched": unmatched(reference, ref_candidates, matched_ref, execution),
            "execution_unmatched": unmatched(execution, exe_candidates, matched_exe, reference)}


def line_difference(reference: str | None, execution: str | None) -> str | None:
    """Execution minus reference in line points; never a betting recommendation."""
    if reference is None or execution is None or reference == "" or execution == "":
        return None
    try:
        difference = Decimal(str(execution)) - Decimal(str(reference))
    except InvalidOperation:
        return None
    if not difference.is_finite():
        return None
    return f"{difference:+f}" if difference else "0"


def snapshot_age(snapshot: dict, now: datetime | None = None) -> dict:
    instant = _time(snapshot.get("captured_at"))
    current = now or datetime.now(timezone.utc)
    if instant is None or current.utcoffset() is None:
        return {"age_hours": None, "stale": True}
    age = current.astimezone(timezone.utc) - instant
    return {"age_hours": max(0, round(age.total_seconds() / 3600, 1)),
            "stale": age > STALE_AFTER}


def compare_snapshots(reference: dict, execution: dict, *, now: datetime | None = None) -> dict:
    if snapshot_role(reference) != "REFERENCE" or snapshot_role(execution) != "EXECUTION":
        raise ValueError("comparison requires REFERENCE and EXECUTION snapshots")
    if reference.get("league") != execution.get("league"):
        raise ValueError("comparison requires one league")
    league = reference["league"]
    comparison = match_events(reference["events"], execution["events"], league)
    comparison.update(reference_age=snapshot_age(reference, now),
                      execution_age=snapshot_age(execution, now),
                      league=league,
                      reference_snapshot_id=reference["snapshot_id"],
                      execution_snapshot_id=execution["snapshot_id"])
    return comparison
