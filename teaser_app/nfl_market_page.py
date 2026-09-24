"""Review saved NFL market snapshots before an explicit model build or recheck."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from teaser_app.inputs import slate_fingerprint
from teaser_app.market_data import LocalHistory, snapshot_role
from teaser_app.nfl_market import _stored_prices, execution_book, prepare_nfl_snapshot
from teaser_app.presentation import signed
from teaser_app.public_cfb import DEFAULT_TEASER_BOOK


def render_saved_nfl_market(adapter, reset_result) -> bool:
    """Return True when the saved-market path owns the NFL input area."""
    st.subheader("NFL market source")
    mode = st.radio("NFL input path", ("Manual entry", "Saved market snapshot"),
                    horizontal=True, key="nfl_input_path")
    if mode == "Manual entry":
        return False
    history = LocalHistory()
    snapshots = [record for record in reversed(history.market_history(league="NFL"))
                 if snapshot_role(record) in {"REFERENCE", "EXECUTION"}]
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
        if st.button("Build proposal from saved NFL slate", type="primary", use_container_width=True):
            try:
                view = adapter.grade(prepared.slate)
            except (ValueError, RuntimeError) as exc:
                st.error(str(exc))
            else:
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
