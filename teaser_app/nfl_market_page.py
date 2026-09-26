"""Review saved NFL market snapshots before an explicit model build or recheck."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from teaser_app.inputs import slate_fingerprint
from teaser_app.market_data import LocalHistory, snapshot_role
from teaser_app.nfl_market import _stored_prices, execution_book, execution_status, prepare_nfl_snapshot
from teaser_app.nfl_workflow import (activate_proposal, active_proposal, infer_nfl_week, latest_execution,
                                     recheck_active_proposal, recheck_candidate, release_active, active_context,
                                     restore_active_proposal, workflow_steps)
from teaser_app.presentation import signed
from teaser_app.public_cfb import DEFAULT_TEASER_BOOK


def _now() -> datetime:
    return datetime.now(ZoneInfo("America/Detroit"))


def adopt_proposal(view, prepared_slate: dict, context: dict) -> None:
    """Show a built or restored proposal as the session's card."""
    for key in ("recheck", "recheck_fingerprint", "reported_placed", "nfl_recheck_context"):
        st.session_state.pop(key, None)
    st.session_state.slate = deepcopy(prepared_slate)
    st.session_state.card = view
    st.session_state.card_slate = deepcopy(prepared_slate)
    st.session_state.built_fingerprint = slate_fingerprint(prepared_slate)
    st.session_state.nfl_card_context = context
    st.session_state.nfl_current_context = context
    st.session_state.origin = "saved_market"


def adopt_recheck(recheck, prepared) -> None:
    st.session_state.slate = prepared.slate
    st.session_state.nfl_current_context = prepared.context
    st.session_state.recheck = recheck
    st.session_state.recheck_fingerprint = slate_fingerprint(prepared.slate)
    st.session_state.nfl_recheck_context = prepared.context
    st.session_state.nfl_recheck_snapshot_id = prepared.context["snapshot_id"]


def run_active_recheck(adapter, history, active: dict, snapshot: dict, *, verify_menu: bool) -> None:
    view = st.session_state.get("card")
    try:
        recheck, prepared, _ = recheck_active_proposal(history, adapter, view, active, snapshot,
                                                       now=_now(), verify_menu=verify_menu)
    except (ValueError, RuntimeError, OSError) as exc:
        st.error(str(exc))
        return
    adopt_recheck(recheck, prepared)
    st.rerun()


def ensure_active_restored(adapter) -> None:
    """After a restart, re-create the active proposal (same card ID and grading time) before any
    recheck can run. Never re-grades it at a new time."""
    if st.session_state.get("card") is not None or st.session_state.get("nfl_restore_failed"):
        return
    history = LocalHistory()
    active = active_proposal(history)
    if active is None:
        return
    try:
        restored = restore_active_proposal(history, adapter)
    except (ValueError, RuntimeError) as exc:
        st.session_state.nfl_restore_failed = str(exc)
        return
    if restored:
        adopt_proposal(restored[0], active["card_slate"], active_context(active))
        st.session_state.nfl_restored = True


def render_nfl_workflow(adapter) -> None:
    """Active proposal baseline, latest screenshot and a simple step indicator for NFL LIVE."""
    history = LocalHistory()
    active = active_proposal(history)
    view = st.session_state.get("card")
    if st.session_state.pop("nfl_restored", False):
        st.caption("Restored the active proposal from local history with its original grading time.")
    if st.session_state.get("nfl_restore_failed"):
        st.error(st.session_state.nfl_restore_failed)
    recheck = st.session_state.get("recheck")
    is_active = bool(active and view is not None and view.card_id == active["card_id"])
    executable = False
    if is_active and recheck is not None:
        executable, _ = execution_status(st.session_state.get("nfl_card_context"),
                                         st.session_state.get("nfl_recheck_context"), view, recheck,
                                         st.session_state.slate, now=_now())
    steps = workflow_steps(active if is_active else None, recheck if is_active else None, executable,
                           bool(st.session_state.get("reported_placed")))
    st.markdown(" → ".join(f"{index} {label} {'✓' if done else '·'}"
                           for index, (label, done) in enumerate(steps, start=1)))
    if active is None:
        st.caption("No active proposal. Upload a Bluecoins screenshot in Import my sportsbook screenshots "
                   "and use Confirm & Build Proposal.")
        return
    st.markdown(f"**ACTIVE PROPOSAL / BASELINE** · `{active['card_id']}` · "
                f"{active['season']} Week {active['week']} · {active['card_context']['line_book']}")
    st.caption(f"Baseline capture {active['baseline_captured_at']} · proposal graded {active['graded_at']} · "
               f"snapshot {active['snapshot_id']}")
    latest = latest_execution(history)
    eligible, reason = recheck_candidate(active, latest)
    if latest is not None:
        st.markdown(f"**LATEST EXECUTION SNAPSHOT** · `{latest['snapshot_id']}` · captured {latest['captured_at']}")
    st.caption(("Eligible for recheck · " if eligible else "Not eligible for recheck · ") + reason)
    if not is_active:
        return
    rechecked_id = st.session_state.get("nfl_recheck_snapshot_id")
    if eligible and rechecked_id != latest["snapshot_id"]:
        if st.button("Recheck active proposal with latest screenshot", use_container_width=True):
            run_active_recheck(adapter, history, active, latest, verify_menu=False)
    if recheck is not None and rechecked_id and not executable:
        context = st.session_state.get("nfl_recheck_context") or {}
        needs_menu = any(not (context.get("menu_observed_at") or {}).get(str(ticket["n_legs"]))
                         for ticket in view.selected_tickets)
        if needs_menu and recheck.verdict == "VALIDATED":
            prices = st.session_state.slate.get("prices", {})
            st.info(f"Teaser menu for this recheck is a default, not a verified quote: 2-team {prices.get('2') or '—'} · "
                    f"3-team {prices.get('3') or '—'}. Check the sportsbook menu, then verify.")
            if st.button("I verified these teaser prices are unchanged now", key="nfl_workflow_verify_menu",
                         use_container_width=True):
                run_active_recheck(adapter, history, active, history.get_snapshot(rechecked_id), verify_menu=True)


def render_saved_nfl_market(adapter, reset_result) -> bool:
    """Return True when the saved-market path owns the NFL input area."""
    st.subheader("Advanced · saved snapshots, manual entry and audit")
    history = LocalHistory()
    snapshots = [record for record in reversed(history.market_history(league="NFL"))
                 if snapshot_role(record) in {"REFERENCE", "EXECUTION"}]
    mode = st.radio("NFL input path", ("Manual entry", "Saved market snapshot"),
                    index=1 if snapshots else 0, horizontal=True, key="nfl_input_path")
    if mode == "Manual entry":
        return False
    if not snapshots:
        st.info("Confirm public lines or a sportsbook screenshot above, then select the saved snapshot here.")
        return True
    by_id = {record["snapshot_id"]: record for record in snapshots}
    chosen = st.selectbox("Saved NFL market snapshot", tuple(by_id),
                          format_func=lambda key: (
                              f"{snapshot_role(by_id[key])} · {by_id[key]['captured_at']} · "
                              f"{by_id[key].get('source_provider') or by_id[key].get('sportsbook') or 'unknown'} · {key}"),
                          key="nfl_saved_choice")
    snapshot = by_id[chosen]
    role = snapshot_role(snapshot)
    source_book = execution_book(snapshot) if role == "EXECUTION" else (snapshot.get("sportsbook") or "ESPN reference")
    st.caption(f"{role} · {snapshot.get('source_type') or 'legacy'} · captured {snapshot['captured_at']}")
    season = snapshot.get("season")
    week = snapshot.get("week")
    if type(season) is not int or type(week) is not int:
        inferred_season, inferred_week = infer_nfl_week(snapshot)
        season = season if type(season) is int else inferred_season
        week = week if type(week) is int else inferred_week
        if inferred_week is not None:
            st.caption(f"NFL week inferred from kickoffs: {inferred_season} Week {inferred_week}")
    if type(season) is not int or not 2000 <= season <= 2100:
        season = int(st.number_input("NFL season for saved slate", 2000, 2100,
                                     datetime.now(ZoneInfo("America/Detroit")).year,
                                     key=f"nfl_saved_season_{chosen}"))
    if type(week) is not int or not 1 <= week <= 18:
        week = int(st.number_input("NFL week for saved slate", 1, 18, 1,
                                   key=f"nfl_saved_week_{chosen}"))
    default_menu = source_book if role == "EXECUTION" and source_book != "UNKNOWN" else DEFAULT_TEASER_BOOK
    menu_book = st.text_input("Teaser-menu sportsbook", value=default_menu,
                              key=f"nfl_menu_book_{chosen}").strip()
    stored = _stored_prices(snapshot)
    for size in ("2", "3"):
        if stored[size] not in (None, ""):
            st.caption(f"{size}-team 6-point teaser price: {stored[size]} · saved with snapshot")
        else:
            stored[size] = st.text_input(f"Missing {size}-team 6-point teaser price · optional",
                                          placeholder="Leave blank if not observed",
                                          key=f"nfl_menu_{size}_{chosen}").strip()
    prices = {size: str(stored[size] or "").strip() for size in ("2", "3")}
    if role == "EXECUTION" and st.button("I verified these teaser prices are unchanged now",
                                          key=f"nfl_verify_menu_{chosen}", use_container_width=True):
        if not menu_book or menu_book.casefold() != source_book.casefold() or not any(prices.values()):
            st.error("Verify a named execution sportsbook and at least one observed 6-point teaser price.")
        else:
            try:
                saved = history.save_menu_verification(
                    snapshot_id=chosen, sportsbook=menu_book, prices=prices,
                    observed_at=datetime.now(ZoneInfo("America/Detroit")).isoformat())
            except (ValueError, OSError) as exc:
                st.error(str(exc))
            else:
                verifications = dict(st.session_state.get("nfl_menu_verifications", {}))
                verifications[chosen] = {"snapshot_id": chosen, "book": menu_book,
                                         "prices": prices, "observed_at": saved["observed_at"],
                                         "verification_id": saved["verification_id"]}
                st.session_state.nfl_menu_verifications = verifications
                st.rerun()
    if role == "EXECUTION":
        st.caption("A saved menu keeps its original observation time. Reusing an old price is not a fresh quote; verify it here, then select this slate again for recheck.")
    now = datetime.now(ZoneInfo("America/Detroit"))
    try:
        prepared = prepare_nfl_snapshot(
            snapshot, adapter, now=now, season=season, week=week,
            prices=prices, menu_book=menu_book,
            menu_verification=st.session_state.get("nfl_menu_verifications", {}).get(chosen))
    except (ValueError, TypeError) as exc:
        st.error(str(exc))
        return True
    if role == "REFERENCE":
        st.warning(f"Public/reference lines + {menu_book} teaser pricing — NOT AN EXECUTABLE {menu_book.upper()} SLATE")
    elif not prepared.context["confirmed_execution"]:
        st.warning("Saved EXECUTION data can be screened, but this legacy/manual record is not a confirmed screenshot placement source.")
    else:
        st.info(f"Confirmed {source_book} EXECUTION lines · proposal only. Placement needs a newer confirmed recheck and explicit operator report.")
    with st.expander(f"Review saved NFL slate · {len(prepared.included)} usable / {len(prepared.excluded)} excluded"):
        for game in prepared.included:
            st.caption(f"{game['game']} · home {signed(game['sides'][1]['spread'])} · "
                       f"total {game['sides'][0]['total']} · {game['kickoff']}")
        for game in prepared.excluded:
            st.warning(f"{game['game']}: {game['reason']} · {game['detail']}")
    if not prepared.included:
        st.info("No usable pregame NFL games remain in this snapshot.")
        return True
    if st.button("Use this slate for proposal", use_container_width=True):
        st.session_state.nfl_saved_selected_id = chosen
        st.session_state.slate = prepared.slate
        st.session_state.nfl_current_context = prepared.context
        st.rerun()
    selected = st.session_state.get("nfl_saved_selected_id") == chosen
    if selected:
        st.caption("Selected slate is ready. Building is a separate explicit action; no ticket is placed.")
        active = active_proposal(history)
        replaces = active is not None and active["snapshot_id"] != chosen
        replace_ok = True
        if replaces:
            st.warning("An active proposal baseline exists. Building here would replace it; to recheck it, "
                       "confirm a newer screenshot instead.")
            replace_ok = st.checkbox("Replace the active proposal baseline", key=f"nfl_replace_active_{chosen}")
        if st.button("Build proposal from saved NFL slate", type="primary", use_container_width=True,
                     disabled=not replace_ok):
            if (st.session_state.get("origin") == "saved_market"
                    and st.session_state.get("card") is not None
                    and st.session_state.get("nfl_card_context", {}).get("snapshot_id") == chosen
                    and st.session_state.get("built_fingerprint") == slate_fingerprint(prepared.slate)):
                st.info("This exact saved slate already has a proposal in this session.")
            else:
                try:
                    view = adapter.grade(prepared.slate)
                    if prepared.context["confirmed_execution"]:
                        activate_proposal(history, view, prepared.slate, prepared.context, now=_now(),
                                          replace=replaces, reason="built from saved snapshot")
                    elif replaces:
                        release_active(history, now=_now(), reason="replaced by a non-executable proposal")
                except (ValueError, RuntimeError, OSError) as exc:
                    st.error(str(exc))
                    return True
                reset_result()
                st.session_state.slate = prepared.slate
                st.session_state.card = view
                st.session_state.card_slate = deepcopy(prepared.slate)
                st.session_state.built_fingerprint = slate_fingerprint(prepared.slate)
                st.session_state.nfl_card_context = prepared.context
                st.session_state.nfl_current_context = prepared.context
                st.session_state.origin = "saved_market"
                st.rerun()
    card_context = st.session_state.get("nfl_card_context")
    if (card_context and card_context.get("confirmed_execution") and
            role == "EXECUTION" and chosen != card_context["snapshot_id"]):
        if st.button("Use this EXECUTION slate for recheck", use_container_width=True):
            st.session_state.slate = prepared.slate
            st.session_state.nfl_current_context = prepared.context
            st.session_state.pop("recheck", None)
            st.session_state.pop("recheck_fingerprint", None)
            st.rerun()
    return True
