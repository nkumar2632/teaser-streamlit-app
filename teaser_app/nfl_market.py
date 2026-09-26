"""Reviewed NFL market snapshots to model inputs; no betting-model decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from teaser_app.inputs import slate_fingerprint
from teaser_app.market_compare import team_key
from teaser_app.market_data import snapshot_role
from teaser_app.public_cfb import DEFAULT_TEASER_BOOK

UNKNOWN_BOOK = "UNKNOWN"
LEGACY_BOOK = "USER_SPORTSBOOK_SCREENSHOT"
MENU_FRESHNESS = timedelta(minutes=30)
EXECUTION_BOARD_FRESHNESS = timedelta(minutes=30)


@dataclass(frozen=True)
class PreparedNFL:
    slate: dict
    included: tuple[dict, ...]
    excluded: tuple[dict, ...]
    context: dict


def _aware(value: object, label: str) -> datetime:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if when.utcoffset() is None:
            raise ValueError
        return when.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} needs a timezone-aware timestamp") from exc


def _book(value: object) -> str:
    return str(value or "").strip()


def execution_book(snapshot: dict) -> str:
    """Read legacy screenshot attribution without changing the immutable record."""
    book = _book(snapshot.get("sportsbook"))
    if book and book != LEGACY_BOOK:
        return book
    if book != LEGACY_BOOK:
        return UNKNOWN_BOOK
    references = [snapshot.get("source_reference")]
    references.extend(event.get("source_reference") for event in snapshot.get("events", [])
                      if isinstance(event, dict))
    found = set()
    for reference in references:
        candidate = reference.get("sportsbook") or reference.get("book") if isinstance(reference, dict) else reference
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        match = re.fullmatch(r"(?:sportsbook|book)\s*[:=]\s*(.{1,60})", candidate, re.I)
        if match:
            candidate = match.group(1).strip()
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}", candidate):
            continue
        if (not candidate or candidate.upper() in {LEGACY_BOOK, "UNKNOWN", "USER SPORTSBOOK SCREENSHOT",
                                                   "SCREENSHOT", "USER SCREENSHOT"}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}", candidate)):
            continue
        found.add(candidate)
    return found.pop() if len(found) == 1 else UNKNOWN_BOOK


def _stored_prices(snapshot: dict) -> dict[str, str]:
    menu = snapshot.get("teaser_prices") or {}
    six = (menu.get("6_point") or {}) if isinstance(menu, dict) else {}
    result = {}
    for size in ("2", "3"):
        value = six.get(f"{size}_team") if isinstance(six, dict) else None
        if value is None:
            observed = {str(event.get(f"teaser_{size}team_6pt_price")) for event in
                        snapshot.get("events", []) if isinstance(event, dict) and
                        event.get(f"teaser_{size}team_6pt_price") not in (None, "")}
            value = next(iter(observed)) if len(observed) == 1 else None
        result[size] = str(value).strip() if value not in (None, "") else ""
    return result


def _default_prices(snapshot: dict) -> set[str]:
    """Sizes whose confirmed price was the prefilled standard menu, never an observed quote."""
    sources = snapshot.get("teaser_price_sources") or {}
    return {size for size in ("2", "3")
            if isinstance(sources, dict) and sources.get(f"{size}_team") == "default"}


def _confirmed_execution(snapshot: dict) -> bool:
    return (snapshot_role(snapshot) == "EXECUTION" and
            snapshot.get("source_type") == "screenshot" and
            snapshot.get("schema_version") == 3)


def prepare_nfl_snapshot(snapshot: dict, adapter, *, now: datetime,
                         season: int | None = None, week: int | None = None,
                         prices: dict[str, str] | None = None, menu_book: str | None = None,
                         menu_verification: dict | None = None) -> PreparedNFL:
    """Exclude bad games individually and preserve the source-independent NFL IDs."""
    role = snapshot_role(snapshot)
    if snapshot.get("league") != "NFL" or role not in {"REFERENCE", "EXECUTION"} or not snapshot.get("snapshot_id"):
        raise ValueError("Choose a saved NFL REFERENCE or EXECUTION market snapshot")
    current = _aware(now.isoformat(), "current time")
    captured = _aware(snapshot.get("captured_at"), "snapshot capture time")
    if captured > current:
        raise ValueError("Snapshot capture time cannot be in the future")
    season = snapshot.get("season") if season is None else season
    week = snapshot.get("week") if week is None else week
    if type(season) is not int or not 2000 <= season <= 2100:
        raise ValueError("Confirm the NFL season for this slate")
    if type(week) is not int or not 1 <= week <= 18:
        raise ValueError("Confirm the NFL week for this slate")
    line_book = (execution_book(snapshot) if role == "EXECUTION"
                 else _book(snapshot.get("sportsbook")) or "ESPN reference")
    menu_book = _book(menu_book) if menu_book is not None else (
        line_book if role == "EXECUTION" and line_book != UNKNOWN_BOOK else DEFAULT_TEASER_BOOK)
    if not menu_book or len(menu_book) > 60 or len(line_book) > 60:
        raise ValueError("Choose a teaser-menu sportsbook")
    stored = _stored_prices(snapshot)
    supplied = prices or {"2": "", "3": ""}
    if not isinstance(supplied, dict) or set(supplied) != {"2", "3"}:
        raise ValueError("Teaser menu needs 2-team and 3-team fields")
    offered = {}
    for size in ("2", "3"):
        entered = str(supplied[size] or "").strip()
        if stored[size] and entered and entered != stored[size]:
            raise ValueError("A saved teaser price cannot be changed in this proposal")
        offered[size] = stored[size] or entered
    verification = menu_verification or {}
    verified = (verification.get("snapshot_id") == snapshot["snapshot_id"] and
                verification.get("book") == menu_book and
                verification.get("prices") == offered)
    verified_at = _aware(verification["observed_at"], "menu verification") if verified else None
    defaults = _default_prices(snapshot)
    observed = {size: (verified_at.isoformat() if offered[size] and verified_at else
                       captured.isoformat() if stored[size] and _confirmed_execution(snapshot)
                       and menu_book.casefold() == line_book.casefold() and size not in defaults
                       else None)
                for size in ("2", "3")}

    candidates, excluded, conflicts = {}, [], set()
    events = snapshot.get("events")
    if not isinstance(events, list):
        raise ValueError("Saved market snapshot has no event list")
    for event in events:
        if not isinstance(event, dict):
            excluded.append({"game": "unknown", "reason": "INVALID_VALUE", "detail": "game is not an object"})
            continue
        label = f"{event.get('away_team') or '?'} at {event.get('home_team') or '?'}"
        try:
            away = team_key("NFL", event.get("away_team"))
            home = team_key("NFL", event.get("home_team"))
            if not away or not home or away == home:
                raise ValueError("unrecognized or duplicate NFL teams")
            kickoff = _aware(event.get("kickoff"), "kickoff")
            if (kickoff <= current or captured >= kickoff or
                    str(event.get("event_state") or "pre").lower() != "pre"):
                excluded.append({"game": label, "reason": "STARTED", "detail": "not a new pregame quote"})
                continue
            home_spread, away_spread = event.get("spread_home"), event.get("spread_away")
            if home_spread in (None, "") and away_spread in (None, ""):
                excluded.append({"game": label, "reason": "NO_ODDS", "detail": "spread missing"})
                continue
            if event.get("total") in (None, ""):
                excluded.append({"game": label, "reason": "MISSING_TOTAL", "detail": "game total missing"})
                continue
            if home_spread in (None, ""):
                home_spread = str(-Decimal(str(away_spread)))
            sides = adapter.nfl_market_sides(
                away=away, home=home, kickoff=kickoff.isoformat(),
                home_spread=str(home_spread),
                away_spread=str(away_spread) if away_spread not in (None, "") else None,
                total=str(event["total"]),
            )
            identity = (away, home, kickoff.isoformat())
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
    for identity, game in sorted(candidates.items(), key=lambda item: (item[0][2], item[0][0], item[0][1])):
        if any(team in duplicate_teams for team in identity[:2]):
            excluded.append({"game": game["game"], "reason": "CONFLICT",
                             "detail": "team appears in multiple games"})
        else:
            included.append(game)
    slate = {"schema_version": 2, "league": "NFL",
             "source": "reference_source" if role == "REFERENCE" else "user_screenshot",
             "ingestion_method": "confirmed_reference_url" if role == "REFERENCE" else "confirmed_screenshot",
             "source_snapshot_id": snapshot["snapshot_id"],
             "source_market_role": role,
             "line_label": "current_pregame", "season": season, "week": week,
             "sportsbook": line_book, "price_sportsbook": menu_book,
             "price_captured_at": verified_at.isoformat() if verified_at else captured.isoformat(),
             "price_label": "OPERATOR_RECONFIRMED" if verified_at else "",
             "price_source_reference": (
                 f"operator verified unchanged; parent market {snapshot['snapshot_id']}"
                 if verified_at else
                 "saved screenshot menu" if role == "EXECUTION" else "operator-entered screening menu"),
             "price_reconfirmation_id": verification.get("verification_id") if verified_at else None,
             "captured_at": captured.isoformat(), "prices": offered,
             "rows": [side for game in included for side in game["sides"]]}
    context = {"snapshot_id": snapshot["snapshot_id"], "role": role,
               "source_type": snapshot.get("source_type"), "confirmed_execution": _confirmed_execution(snapshot),
               "captured_at": captured.isoformat(), "line_book": line_book,
               "menu_book": menu_book, "menu_observed_at": observed,
               "fingerprint": slate_fingerprint(slate),
               "lines_label": f"{snapshot.get('source_provider') or 'public'}/{line_book}"
               if role == "REFERENCE" else line_book}
    return PreparedNFL(slate, tuple(included), tuple(excluded), context)


def execution_status(card_context: dict | None, recheck_context: dict | None,
                     view, recheck, current_slate: dict, *, now: datetime) -> tuple[bool, str]:
    """App-level placement gate; model selection flags never grant execution rights."""
    if not view.selected_tickets or view.historical:
        return False, "No current selected NFL ticket is available for placement review"
    if not card_context or not card_context.get("confirmed_execution"):
        return False, "Proposal lines are not a confirmed sportsbook EXECUTION snapshot"
    if card_context.get("line_book") in (None, "", UNKNOWN_BOOK):
        return False, "Execution sportsbook is unknown"
    if card_context["menu_book"].casefold() != card_context["line_book"].casefold():
        return False, "Original teaser menu belongs to a different sportsbook"
    if not recheck_context or not recheck_context.get("confirmed_execution"):
        return False, "A newer confirmed sportsbook EXECUTION recheck is required"
    if recheck_context.get("line_book", "").casefold() != card_context["line_book"].casefold():
        return False, "Recheck sportsbook differs from the proposal sportsbook"
    if recheck_context.get("menu_book", "").casefold() != card_context["line_book"].casefold():
        return False, "Recheck teaser menu belongs to a different sportsbook"
    if recheck_context.get("snapshot_id") == card_context["snapshot_id"] or (
            _aware(recheck_context["captured_at"], "recheck capture") <=
            _aware(card_context["captured_at"], "proposal capture")):
        return False, "Recheck needs a newer confirmed sportsbook snapshot"
    recheck_captured = _aware(recheck_context["captured_at"], "recheck capture")
    if recheck_captured <= _aware(view.graded_at, "card grading time"):
        return False, "Recheck sportsbook capture must postdate card grading"
    if recheck_context.get("fingerprint") != slate_fingerprint(current_slate):
        return False, "Recheck inputs changed after the confirmed snapshot was selected"
    if not recheck or recheck.verdict != "VALIDATED" or recheck.original_card_id != view.card_id:
        return False, "The frozen model recheck has not validated this proposal"
    instant = _aware(now.isoformat(), "current time")
    checked_at = _aware(recheck.rechecked_at, "recheck time")
    if not recheck_captured <= checked_at <= instant:
        return False, "Recheck capture and validation must precede placement review"
    if instant > recheck_captured + EXECUTION_BOARD_FRESHNESS:
        return False, "Confirmed sportsbook market capture is no longer fresh"
    if not checked_at <= instant <= checked_at + MENU_FRESHNESS:
        return False, "Model recheck is no longer fresh"
    selected_legs = {leg_id for ticket in view.selected_tickets for leg_id in ticket["leg_ids"]}
    selected_teams = set()
    for leg in view.qualifying_legs:
        if leg["leg_id"] in selected_legs:
            selected_teams.add(leg["team"])
            if instant >= _aware(leg["kickoff"], "kickoff"):
                return False, "A selected NFL game has already started"
    current_rows = {row["team"]: row for row in current_slate.get("rows", [])}
    for team in selected_teams:
        row = current_rows.get(team)
        if row is None or instant >= _aware(row["kickoff"], "recheck kickoff"):
            return False, "A selected NFL game is missing or started on the recheck slate"
    for size in {str(ticket["n_legs"]) for ticket in view.selected_tickets}:
        observed = recheck_context.get("menu_observed_at", {}).get(size)
        if not observed:
            return False, f"{size}-team teaser menu needs explicit current verification"
        seen = _aware(observed, "menu observation time")
        if not seen <= instant <= seen + MENU_FRESHNESS:
            return False, f"{size}-team teaser menu is not fresh"
    return True, "Confirmed sportsbook EXECUTION slate and fresh validated recheck"
