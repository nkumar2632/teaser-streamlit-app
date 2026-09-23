"""Review ESPN finals before joining them to frozen teaser runs."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from teaser_app.live_history import live_performance
from teaser_app.market_data import LocalHistory
from teaser_app.paper_history import paper_performance
from teaser_app.result_ingest import (
    ResultReviewError, confirm_results, fetch_espn_results, manual_preview,
    review_results,
)

COLUMNS = ("source_event_id", "away_team", "home_team", "kickoff", "status",
           "away_score", "home_score")


def render_results(adapter) -> None:
    history = LocalHistory()
    with st.expander("Results · review and settle stored cards"):
        league = st.selectbox("Results league", ("NFL", "CFB"), key="results_league")
        period = st.radio("Fetch by", ("Date", "Week"), horizontal=True, key="results_period")
        now = datetime.now(ZoneInfo("America/Detroit"))
        if period == "Date":
            game_date = st.date_input("Results date", value=now.date(), key="results_date")
            season = week = None
        else:
            game_date = None
            season = st.number_input("Results season", min_value=2000, max_value=2100,
                                     value=now.year, key="results_season")
            week = st.number_input("Results week", min_value=1,
                                   max_value=18 if league == "NFL" else 20,
                                   value=1, key="results_week")
        if st.button("Fetch ESPN Results", width="stretch"):
            st.session_state.pop("results_preview", None)
            st.session_state.pop("results_saved", None)
            st.session_state.pop("result_manual_links", None)
            try:
                st.session_state.results_preview = fetch_espn_results(
                    league, game_date=game_date, season=season, week=week)
            except ResultReviewError as exc:
                st.error(str(exc))
            else:
                st.rerun()
        if st.button("Enter results manually", width="stretch"):
            st.session_state.results_preview = manual_preview(league)
            st.session_state.pop("results_saved", None)
            st.session_state.pop("result_manual_links", None)
            st.rerun()

        if league == "NFL":
            perf = live_performance(history)
            roi = f"{float(perf['roi']):+.1%}" if perf["roi"] is not None else "—"
            st.caption(f"NFL LIVE · {perf['placed']} operator-reported placed · {perf['settled']} settled · "
                       f"{perf['wins']} W / {perf['losses']} L / {perf['pushes']} P · "
                       f"{perf['units_staked']}u staked · {perf['net_units']}u net · ROI {roi}")
        else:
            perf = paper_performance(history, adapter)
            roi = f"{float(perf['roi']):+.1%}" if perf["roi"] is not None else "—"
            st.caption(f"CFB PAPER · {perf['settled']} settled · {perf['wins']} W / "
                       f"{perf['losses']} L / {perf['pushes']} P · "
                       f"{perf['hypothetical_units_staked']} hypothetical u staked · "
                       f"{perf['net_units']} hypothetical u net · ROI {roi}")

        saved = st.session_state.get("results_saved")
        if saved:
            st.success(f"Confirmed immutable result snapshot {saved['result_snapshot_id']}. Stored cards and original lines were not changed.")
        preview = st.session_state.get("results_preview")
        if preview is None:
            return
        st.warning(f"REVIEW REQUIRED · {preview.league} {preview.provider} results. Only FINAL games with two reviewed scores may settle tickets.")
        st.caption(f"Fetched {preview.fetched_at} · {preview.source_url or 'manual entry'}")
        for warning in preview.warnings:
            st.caption(warning)
        source_rows = [{key: row.get(key) if row.get(key) is not None else ""
                        for key in COLUMNS} for row in preview.rows]
        if not source_rows:
            source_rows = [{key: "" for key in COLUMNS}]
            source_rows[0]["status"] = "FINAL"
        edited = st.data_editor(pd.DataFrame(source_rows, columns=COLUMNS),
                                hide_index=True, num_rows="dynamic", width="stretch",
                                disabled=["source_event_id"] if preview.provider == "ESPN" else [],
                                key=f"results_review_{preview.fetched_at}")
        edited_rows = [row for row in edited.to_dict("records")
                       if any(str(row.get(field) or "").strip() for field in COLUMNS)]
        st.caption("Correct names, kickoff, status, and scores here. Edited values are retained beside the ESPN values. Remove false or duplicate rows before confirming.")
        try:
            review = review_results(preview, edited_rows, history, adapter)
        except ResultReviewError as exc:
            st.info(str(exc))
            review = None
        if review is not None:
            for index, row in enumerate(review["rows"]):
                label = f"{row['away_team']} at {row['home_team']} · {row['status']}"
                st.markdown(f"**{label}** · {row['away_score'] if row['away_score'] is not None else '—'}–{row['home_score'] if row['home_score'] is not None else '—'} · {row['match_status']}")
                if row["matches"]:
                    st.caption("Stored game: " + ", ".join(
                        f"{item['run_id']} / {item['game_id']} ({item['method']})"
                        for item in row["matches"]))
                if row["review_changes"]:
                    st.caption("Edited from provider: " + ", ".join(row["review_changes"]))
                if row["match_status"] in {"AMBIGUOUS", "KICKOFF_CONFLICT", "UNMATCHED"}:
                    choices = ["Leave unmatched"] + [
                        f"{game['run_id']}|{game['game_id']}" for game in review["available_games"]
                    ]
                    chosen = st.selectbox(f"Review stored event for row {index + 1}", choices,
                                          key=f"result_link_{preview.fetched_at}_{index}")
                    if chosen != "Leave unmatched":
                        st.session_state.setdefault("result_manual_links", {})[index] = chosen
            manual_links = st.session_state.get("result_manual_links", {})
            if manual_links:
                review = review_results(preview, edited_rows, history, adapter,
                                        manual_links=manual_links)
            with st.expander("Proposed outcomes from stored terms", expanded=True):
                for leg in review["legs"]:
                    st.caption(f"Original card: {leg['team']} {leg['spread']} → {leg['teased_spread']} · "
                               f"{leg['result']} · {leg['away_score'] if leg['away_score'] is not None else '—'}–"
                               f"{leg['home_score'] if leg['home_score'] is not None else '—'} · {leg['game_id']}")
                for ticket in review["tickets"]:
                    st.caption(f"{ticket['track']} · {ticket['ticket_key']} · stored legs {', '.join(ticket['leg_ids'])} · "
                               f"{ticket['result']}" +
                               (f" · model {ticket['model_result']}" if ticket['track'] == 'LIVE' else "") +
                               (" · previously settled" if ticket.get("already_settled") else "") +
                               (f" · {ticket['reason']}" if ticket.get("reason") else ""))
                    if ticket["track"] == "LIVE":
                        st.caption("Placed terms: " + ", ".join(
                            f"{leg['team']} {leg['spread']} → {leg['teased_spread']}"
                            for leg in ticket["leg_terms"]) +
                            f" · {ticket['offered_american']} · {ticket['stake_units']}u")
                if not review["tickets"]:
                    st.caption("No stored selected PAPER ticket or operator-reported LIVE placement matches these games yet.")
            for conflict in review["conflicts"] + review["blockers"]:
                st.error(conflict)
            allow_conflicts = st.checkbox(
                "I reviewed this correction to confirmed scores or sportsbook settlement",
                key=f"results_allow_correction_{preview.fetched_at}") if (
                    review["conflicts"] or any(ticket.get("already_settled") for ticket in review["tickets"])) else False
            book_results = {}
            for ticket in review["tickets"]:
                if ticket["track"] != "LIVE" or ticket["model_result"] not in {"WIN", "LOSS"}:
                    continue
                placement_id = ticket["placement_id"]
                selected = st.selectbox(f"Sportsbook settlement · {ticket['ticket_key']}",
                                        ("Unconfirmed", "WIN", "LOSS", "PUSH", "VOID", "CANCELLED"),
                                        key=f"book_result_{preview.fetched_at}_{placement_id}")
                if selected != "Unconfirmed":
                    profit = st.text_input(f"Book P/L units override · {ticket['ticket_key']} (optional)",
                                           key=f"book_profit_{preview.fetched_at}_{placement_id}")
                    book_results[placement_id] = (selected, profit or None)
            if st.button("Confirm Results", type="primary", width="stretch"):
                try:
                    result = confirm_results(preview, review, history, adapter,
                                             allow_conflicts=allow_conflicts,
                                             book_results=book_results)
                except (ResultReviewError, ValueError, OSError) as exc:
                    st.error(str(exc))
                else:
                    st.session_state.results_saved = result
                    st.session_state.pop("results_preview", None)
                    st.session_state.pop("result_manual_links", None)
                    st.rerun()
        if st.button("Discard results preview", width="stretch"):
            st.session_state.pop("results_preview", None)
            st.session_state.pop("result_manual_links", None)
            st.rerun()
