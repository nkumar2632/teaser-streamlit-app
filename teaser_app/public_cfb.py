"""Convert a reviewed CFB REFERENCE snapshot into the existing PAPER input shape."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from unicodedata import normalize

from teaser_app.market_data import _canonical, snapshot_role

DEFAULT_TEASER_BOOK = "bluecoins.ag"


@dataclass(frozen=True)
class PreparedCFB:
    slate: dict
    included: tuple[dict, ...]
    excluded: tuple[dict, ...]
    lines_label: str
    menu_book: str


def _name(value: object) -> str:
    return " ".join(normalize("NFKC", str(value or "")).split())


def _aware(value: object, label: str) -> datetime:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if when.utcoffset() is None:
            raise ValueError
        return when.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} needs a timezone-aware timestamp") from exc


def _missing(value: object) -> bool:
    return value is None or str(value).strip() == ""


def prepare_cfb_reference(snapshot: dict, adapter, *, now: datetime,
                          season: int | None = None, week: int | None = None,
                          prices: dict[str, str] | None = None,
                          menu_book: str = DEFAULT_TEASER_BOOK) -> PreparedCFB:
    """Exclude bad games individually; never change the confirmed source snapshot."""
    if (snapshot.get("league") != "CFB" or snapshot_role(snapshot) != "REFERENCE"
            or snapshot.get("source_type") != "url" or not snapshot.get("snapshot_id")):
        raise ValueError("Choose a confirmed public CFB market snapshot")
    current = _aware(now.isoformat(), "current time")
    captured = _aware(snapshot.get("captured_at"), "snapshot capture time")
    if captured > current:
        raise ValueError("Snapshot capture time cannot be in the future")
    season = snapshot.get("season") if season is None else season
    week = snapshot.get("week") if week is None else week
    if type(season) is not int or not 2000 <= season <= 2100:
        raise ValueError("Confirm the CFB season for this public slate")
    if type(week) is not int or not 1 <= week <= 20:
        raise ValueError("Confirm the CFB week for this public slate")
    menu_book = _name(menu_book)
    if not menu_book or len(menu_book) > 60:
        raise ValueError("Choose a teaser-menu sportsbook")
    offered = prices or {"2": "", "3": ""}
    if not isinstance(offered, dict) or set(offered) != {"2", "3"}:
        raise ValueError("Teaser menu needs 2-team and 3-team fields")
    provider = _name(snapshot.get("source_provider") or "ESPN")
    line_book = _name(snapshot.get("sportsbook") or "reference market")
    lines_label = f"{provider}/{line_book}"
    candidates, excluded = {}, []
    conflicts = set()
    for event in snapshot.get("events", []):
        if not isinstance(event, dict):
            excluded.append({"game": "unknown", "reason": "INVALID_VALUE", "detail": "game is not an object"})
            continue
        away = _name(event.get("away_school") or event.get("away_team"))
        home = _name(event.get("home_school") or event.get("home_team"))
        label = f"{away or '?'} at {home or '?'}"
        try:
            kickoff = _aware(event.get("kickoff"), "kickoff")
            if not away or not home or away.casefold() == home.casefold():
                raise ValueError("missing or duplicate teams")
            if kickoff <= current or captured >= kickoff or str(event.get("event_state") or "pre").lower() != "pre":
                excluded.append({"game": label, "reason": "STARTED", "detail": "not a new pregame quote"})
                continue
            home_spread = event.get("spread_home")
            away_spread = event.get("spread_away")
            if _missing(home_spread) and _missing(away_spread):
                excluded.append({"game": label, "reason": "NO_ODDS", "detail": "spread missing"})
                continue
            if _missing(event.get("total")):
                excluded.append({"game": label, "reason": "MISSING_TOTAL", "detail": "game total missing"})
                continue
            if _missing(home_spread):
                home_spread = str(-Decimal(str(away_spread)))
            sides = adapter.cfb_public_sides(
                away=away, home=home, kickoff=kickoff.isoformat(),
                home_spread=str(home_spread),
                away_spread=str(away_spread) if not _missing(away_spread) else None,
                total=str(event["total"]),
            )
            identity = (away.casefold(), home.casefold(), kickoff.isoformat())
            if identity in conflicts:
                excluded.append({"game": label, "reason": "CONFLICT", "detail": "duplicate game"})
            elif identity in candidates:
                if candidates[identity]["sides"] == sides:
                    excluded.append({"game": label, "reason": "DUPLICATE", "detail": "identical game merged"})
                else:
                    candidates.pop(identity)
                    conflicts.add(identity)
                    excluded.append({"game": label, "reason": "CONFLICT", "detail": "conflicting duplicate game"})
            else:
                candidates[identity] = {"game": label, "sides": sides,
                                        "kickoff": kickoff.isoformat()}
        except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
            excluded.append({"game": label, "reason": "INVALID_VALUE", "detail": str(exc)})
    team_games = {}
    for identity in candidates:
        for team in identity[:2]:
            team_games.setdefault(team, set()).add(identity)
    duplicate_teams = {team for team, games in team_games.items() if len(games) > 1}
    included = []
    for identity, game in sorted(candidates.items(), key=lambda item: item[0][2:3] + item[0][:2]):
        if any(team in duplicate_teams for team in identity[:2]):
            excluded.append({"game": game["game"], "reason": "CONFLICT",
                             "detail": "team appears in multiple games"})
        else:
            included.append(game)
    slate = {"schema_version": 2, "league": "CFB", "source": "reference_source",
             "line_label": "current_pregame", "season": season, "week": week,
             "sportsbook": line_book, "captured_at": captured.isoformat(),
             "prices": {key: str(offered[key] or "").strip() for key in ("2", "3")},
             "rows": [side for game in included for side in game["sides"]]}
    return PreparedCFB(slate, tuple(included), tuple(excluded), lines_label, menu_book)


def build_cfb_public_board(history, adapter, snapshot: dict, *, now: datetime,
                           season: int | None = None, week: int | None = None,
                           prices: dict[str, str] | None = None,
                           menu_book: str = DEFAULT_TEASER_BOOK):
    """Build and save once on an explicit operator action, outside the model checkout."""
    from teaser_app.paper_history import run_record

    prepared = prepare_cfb_reference(snapshot, adapter, now=now, season=season, week=week,
                                     prices=prices, menu_book=menu_book)
    if not prepared.included:
        raise ValueError("No usable pregame CFB games remain; review the excluded games")
    paper = adapter.grade_cfb_paper(prepared.slate)
    record = run_record(paper, prepared.slate, snapshot["snapshot_id"])
    record.update(board_kind="cfb_paper_board", frozen_at=_aware(now.isoformat(), "build time").isoformat(),
                  source_snapshot_id=snapshot["snapshot_id"],
                  lines_source=prepared.lines_label,
                  lines_book=prepared.slate["sportsbook"], menu_book=prepared.menu_book,
                  excluded_games=list(prepared.excluded))
    saved = next((prior for prior in reversed(history.runs(league="CFB", status="PAPER"))
                  if prior.get("board_kind") == "cfb_paper_board"
                  and prior.get("source_snapshot_id") == snapshot["snapshot_id"]
                  and prior.get("model_sha") == record["model_sha"]
                  and prior.get("slate") == record["slate"]
                  and prior.get("menu_book") == record["menu_book"]
                  and _canonical(prior.get("legs")) == _canonical(record["legs"])
                  and _canonical(prior.get("tickets")) == _canonical(record["tickets"])), None)
    if saved is None:
        saved = history.save_run(record)
    return replace(paper, run_id=saved["run_id"], snapshot_id=snapshot["snapshot_id"]), prepared, saved
