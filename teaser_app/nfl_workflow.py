"""NFL LIVE operator workflow: one active proposal baseline and its newer-screenshot recheck.

The app manages the chronology so the operator does not have to: a confirmed sportsbook
screenshot becomes the proposal baseline, a later confirmed screenshot rechecks it. Every model
decision stays in the pinned model and every placement gate stays in nfl_market.execution_status;
nothing here places or records a wager.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from teaser_app.inputs import slate_fingerprint
from teaser_app.market_data import snapshot_role
from teaser_app.nfl_market import (UNKNOWN_BOOK, _aware, _confirmed_execution, _stored_prices,
                                   execution_book, prepare_nfl_snapshot)

DEFAULT_MENU_BOOK = "bluecoins.ag"
# Standard Bluecoins 6-point menu. These are defaults for pricing a proposal, never an observed
# or fresh quote: the execution gate still requires an explicit current verification.
DEFAULT_TEASER_MENU = {"2": "-110", "3": "+170"}
BOARD_ZONE = ZoneInfo("America/New_York")


def _week_one_tuesday(season: int) -> date:
    labor_day = next(date(season, 9, day) for day in range(1, 8) if date(season, 9, day).weekday() == 0)
    return labor_day + timedelta(days=1)


def infer_nfl_week(snapshot: dict) -> tuple[int | None, int | None]:
    """Season and week from the kickoff dates (NFL weeks run Tuesday to Monday from the week
    after Labor Day). Returns (None, None) when the games do not fall in one regular-season week."""
    weeks = set()
    for event in snapshot.get("events", []):
        try:
            day = _aware(event.get("kickoff"), "kickoff").astimezone(BOARD_ZONE).date()
        except (ValueError, AttributeError):
            continue
        season = day.year if day.month >= 3 else day.year - 1
        week = (day - _week_one_tuesday(season)).days // 7 + 1
        weeks.add((season, week) if 1 <= week <= 18 else None)
    return next(iter(weeks)) if len(weeks) == 1 and None not in weeks else (None, None)


def active_proposal(history) -> dict | None:
    """The proposal most recently activated and not released since."""
    log = history.activations()
    if not log or log[-1]["action"] != "activate":
        return None
    return history.get_proposal(log[-1]["proposal_id"])


def _log(history, action: str, proposal_id: str | None, *, now: datetime, reason: str) -> dict:
    return history.save_activation({
        "schema_version": 1, "kind": "nfl_active_proposal_event", "action": action,
        "proposal_id": proposal_id, "at": _aware(now.isoformat(), "event time").isoformat(),
        "sequence": len(history.activations()) + 1, "reason": reason,
    })


def release_active(history, *, now: datetime, reason: str) -> None:
    active = active_proposal(history)
    if active is not None:
        _log(history, "release", active["proposal_id"], now=now, reason=reason)


def activate_proposal(history, view, card_slate: dict, context: dict, *, now: datetime,
                      replace: bool = False, reason: str = "proposal built") -> dict:
    """Persist a confirmed-EXECUTION proposal and make it the active baseline.

    An existing different baseline is only replaced with replace=True (an explicit operator action)."""
    if not context or not context.get("confirmed_execution"):
        raise ValueError("Only a confirmed sportsbook EXECUTION proposal can be the active baseline")
    active = active_proposal(history)
    if active is not None and active["card_id"] != view.card_id and not replace:
        raise ValueError("An active proposal baseline already exists; recheck it with a newer screenshot, "
                         "or explicitly replace it")
    record = history.save_proposal({
        "schema_version": 1, "kind": "nfl_proposal_baseline", "league": "NFL",
        "card_id": view.card_id, "graded_at": view.graded_at, "season": view.season, "week": view.week,
        "snapshot_id": context["snapshot_id"], "baseline_captured_at": context["captured_at"],
        # The slate fingerprint is derived (bytes); it is recomputed from card_slate on restore.
        "card_slate": card_slate, "card_context": {key: value for key, value in context.items()
                                                   if key != "fingerprint"},
    })
    if active is None or active["proposal_id"] != record["proposal_id"]:
        _log(history, "activate", record["proposal_id"], now=now, reason=reason)
    return record


def active_context(active: dict) -> dict:
    return {**active["card_context"], "fingerprint": slate_fingerprint(active["card_slate"])}


def restore_active_proposal(history, adapter):
    """Re-create the active proposal with its original grading time. The card ID must match the
    saved one exactly; otherwise nothing is restored (the chronology cannot be trusted)."""
    active = active_proposal(history)
    if active is None:
        return None
    view = adapter.grade(active["card_slate"], graded_at=active["graded_at"])
    if view.card_id != active["card_id"] or view.graded_at != active["graded_at"]:
        raise ValueError("The saved active proposal could not be re-created exactly; it was not restored")
    return view, active


def _menu_entries(stored: dict, defaults: dict) -> dict:
    return {size: "" if stored[size] else str(defaults.get(size) or "") for size in ("2", "3")}


def build_active_proposal(history, adapter, snapshot: dict, *, now: datetime, replace: bool = False):
    """Confirmed EXECUTION screenshot -> proposal -> active baseline, in one step."""
    if snapshot.get("league") != "NFL" or not _confirmed_execution(snapshot):
        raise ValueError("Build the LIVE proposal from a confirmed NFL sportsbook screenshot")
    season, week = infer_nfl_week(snapshot)
    if season is None:
        raise ValueError("Could not infer one NFL week from the kickoffs; use Advanced to set it")
    book = execution_book(snapshot)
    prepared = prepare_nfl_snapshot(
        snapshot, adapter, now=now, season=season, week=week,
        prices=_menu_entries(_stored_prices(snapshot), DEFAULT_TEASER_MENU),
        menu_book=book if book != UNKNOWN_BOOK else DEFAULT_MENU_BOOK)
    if not prepared.included:
        raise ValueError("No usable pregame NFL games remain in this screenshot")
    active = active_proposal(history)
    if active is not None and not replace:
        raise ValueError("An active proposal baseline already exists; use Confirm & Recheck, "
                         "or explicitly replace the baseline")
    view = adapter.grade(prepared.slate)
    record = activate_proposal(history, view, prepared.slate, prepared.context, now=now, replace=replace,
                               reason="one-click proposal from confirmed screenshot")
    return view, prepared, record


def recheck_candidate(active: dict | None, snapshot: dict | None) -> tuple[bool, str]:
    """Whether a saved snapshot can recheck the active baseline (mirrors the execution gate)."""
    if active is None:
        return False, "No active proposal baseline"
    if snapshot is None:
        return False, "Waiting for a newer confirmed sportsbook screenshot"
    if snapshot.get("league") != "NFL" or not _confirmed_execution(snapshot):
        return False, "Only a confirmed NFL sportsbook screenshot can recheck the proposal"
    if snapshot["snapshot_id"] == active["snapshot_id"]:
        return False, "This is the baseline screenshot; capture a newer one"
    captured = _aware(snapshot.get("captured_at"), "recheck capture")
    if captured <= _aware(active["baseline_captured_at"], "baseline capture"):
        return False, "Screenshot is not newer than the baseline capture"
    if captured <= _aware(active["graded_at"], "proposal grading"):
        return False, "Screenshot was captured before the proposal was graded"
    if execution_book(snapshot).casefold() != active["card_context"]["line_book"].casefold():
        return False, "Screenshot sportsbook differs from the proposal sportsbook"
    return True, "Newer confirmed screenshot from the same sportsbook"


def latest_execution(history) -> dict | None:
    records = [record for record in history.market_history(league="NFL", role="EXECUTION")
               if _confirmed_execution(record)]
    return records[-1] if records else None


def recheck_active_proposal(history, adapter, view, active: dict, snapshot: dict, *, now: datetime,
                            verify_menu: bool = False):
    """Newer confirmed screenshot -> frozen-model recheck of the active proposal.

    The teaser menu is inherited from the baseline. With verify_menu=True the operator's explicit
    'unchanged now' assertion is saved first, so the recheck uses a fresh menu observation."""
    eligible, reason = recheck_candidate(active, snapshot)
    if not eligible:
        raise ValueError(reason)
    if view.card_id != active["card_id"]:
        raise ValueError("The displayed proposal is not the active baseline")
    context = active["card_context"]
    stored = _stored_prices(snapshot)
    entries = _menu_entries(stored, active["card_slate"]["prices"])
    offered = {size: stored[size] or entries[size] for size in ("2", "3")}
    verification = None
    if verify_menu:
        saved = history.save_menu_verification(
            snapshot_id=snapshot["snapshot_id"], sportsbook=context["menu_book"], prices=offered,
            observed_at=_aware(now.isoformat(), "verification time").isoformat())
        verification = {"snapshot_id": snapshot["snapshot_id"], "book": context["menu_book"],
                        "prices": offered, "observed_at": saved["observed_at"],
                        "verification_id": saved["verification_id"]}
    prepared = prepare_nfl_snapshot(snapshot, adapter, now=now, season=active["season"], week=active["week"],
                                    prices=entries, menu_book=context["menu_book"],
                                    menu_verification=verification)
    recheck = adapter.recheck(view.card_id, prepared.slate)
    return recheck, prepared, verification


def workflow_steps(active: dict | None, recheck, executable: bool, reported: bool) -> list[tuple[str, bool]]:
    return [
        ("Proposal built", active is not None),
        ("Newer screenshot rechecked", recheck is not None),
        ("Recheck validated", bool(recheck is not None and recheck.verdict == "VALIDATED")),
        ("Ready for operator placement report", executable and not reported),
    ]


def as_utc(value: str) -> datetime:
    return _aware(value, "timestamp").astimezone(timezone.utc)
