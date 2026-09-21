"""CFB paper workspace. Streamlit sees display records, never source model objects."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json

import streamlit as st

from teaser_app.inputs import decode_slate, encode_slate, slate_fingerprint
from teaser_app.market_data import LocalHistory
from teaser_app.paper_history import paper_performance, run_record
from teaser_app.presentation import card as html_card, h, percent, signed
from teaser_app.strategy import CFB_TEASER


def initial_cfb_slate() -> dict:
    now = datetime.now(ZoneInfo("America/Detroit"))
    return {"schema_version": 2, "league": "CFB", "source": "manual_sportsbook",
            "line_label": "current_pregame", "season": now.year, "week": 1,
            "sportsbook": "", "captured_at": now.isoformat(timespec="seconds"),
            "prices": {"2": "", "3": ""}, "rows": []}


def _render_ticket(ticket: dict, selected: bool = False) -> None:
    body = (f"<strong>{h(signed(ticket['offered_american']))}</strong>"
            f"{' · ' + h(ticket['stake_units']) + ' hypothetical u' if selected else ''}"
            f" · P_ticket {h(percent(ticket['p_ticket']))}"
            f"<br>Break-even {h(percent(ticket['break_even']))}"
            f" · EV {h(percent(ticket['ev_per_unit'], sign=True))}"
            f"<br>{h(ticket['status'].replace('_', ' '))}")
    if ticket["offered_american"] == "UNAVAILABLE":
        body += f"<br>Fair break-even net profit: {h(ticket['fair_break_even_profit'])}u per 1u"
    st.markdown(html_card(" / ".join(h(t) for t in ticket["teams"]),
                          "PAPER · hypothetical selection" if selected else "PAPER · constructed ticket",
                          body, accent="paper" if selected else ""), unsafe_allow_html=True)


def _render_board(view, history, adapter, slate: dict) -> None:
    st.warning("PAPER — NOT REAL MONEY · CFB | TEASER | teaser_v1.0")
    if view.historical:
        st.info("Historical paper run. Its original lines and timestamps remain unchanged.")
    elif slate_fingerprint(slate) != st.session_state.get("cfb_built_fingerprint"):
        st.warning("INPUTS CHANGED — this paper board uses the previous saved slate. Build again to update it.")
    st.caption(f"{view.season} · Week {view.week} · {view.sportsbook} · captured {view.captured_at}")
    st.subheader("Hypothetical paper card")
    st.caption("Frozen v1.0 greedy selector applied to actual entered prices. These are hypothetical units, never placements.")
    if view.selected_tickets:
        for ticket in view.selected_tickets:
            _render_ticket(ticket, selected=True)
    else:
        st.info("No paper tickets selected. Actual 2/3-team prices are required for EV and selection.")
    by_id = {leg["leg_id"]: leg["team"] for leg in view.legs}
    st.markdown("**Hypothetical leg exposure**")
    st.write(" · ".join(f"{by_id[leg_id]} {units}u" for leg_id, units in view.exposure.items()) or "0u")
    st.caption("Actual placements: 0 · actual units staked: 0")

    st.subheader("Qualifying PRIMARY / PAPER legs")
    if not view.qualifying_legs:
        st.info("No primary CFB leg qualifies on this slate.")
    for leg in view.qualifying_legs:
        body = (f"{h(signed(leg['spread']))} → {h(signed(leg['teased_spread']))}"
                f" · Total {h(leg['total'])}<br>P_est <strong>{h(percent(leg['p_est']))}</strong>")
        st.markdown(html_card(h(leg["team"]), f"#{leg['rank']} · PRIMARY / PAPER", body, accent="paper"),
                    unsafe_allow_html=True)

    with st.expander(f"Full paper ticket board · {len(view.tickets)} tickets"):
        st.caption("All top-four primary 2-team and 3-team combinations from the pinned model.")
        for ticket in view.tickets:
            _render_ticket(ticket)
    with st.expander(f"Market snapshot and all sides · {len(view.legs)}"):
        st.caption(f"Snapshot {view.snapshot_id or 'not saved'} · source {slate['source']} · line provenance {slate['line_label']}")
        for leg in view.legs:
            body = (f"vs {h(leg['opponent'])} · {h(signed(leg['spread']))} → {h(signed(leg['teased_spread']))}"
                    f" · Total {h(leg['total'])}<br>P_est {h(percent(leg['p_est']))}"
                    f" · {h(leg['exclusion_reason'])}")
            st.markdown(html_card(h(leg["team"]), f"{h(leg['geometry_class'])} / PAPER", body),
                        unsafe_allow_html=True)
    with st.expander(f"Secondary research · {len(view.secondary_legs)}"):
        st.caption("Secondary geometry is research only; it never enters the paper card selector. Push-capable scores are not full win probabilities because push settlement is not modeled here.")
        for leg in view.secondary_legs:
            st.write(f"#{leg['secondary_rank']} {leg['team']} {signed(leg['spread'])} → {signed(leg['teased_spread'])} · research score {percent(leg['p_est'])}{' · can push' if leg['can_push'] else ''}")
    with st.expander("Audit details"):
        st.code(f"run={view.run_id}\nmarket={view.snapshot_id}\nmodel=94e412ed5bedc03a60b0b86f1146ff80c9f9e195")
        st.caption("Original model precision is retained in the saved local run record.")

    if not view.run_id:
        return
    run = history.get_run(view.run_id)
    prior = history.latest_paper_results(view.run_id)
    scores = dict(prior["scores"]) if prior else {}
    with st.expander("Record final scores · paper only"):
        st.caption("Enter final scores after games finish. Scores are joined after the frozen board; they cannot change rankings or selection. Each submission creates a new immutable result record.")
        games = {}
        for row in run["slate"]["rows"]:
            leg = next((l for l in run["legs"] if l["team"] == row["team"] and
                        l["opponent"] in {row["home_team"], row["away_team"]}), None)
            if leg:
                games[leg["game_id"]] = (row["away_team"], row["home_team"])
        with st.form(f"cfb_scores_{view.run_id}"):
            submitted = {}
            for game_id, (away, home) in games.items():
                st.markdown(f"**{away} at {home}**")
                use = st.checkbox("Final", value=game_id in scores, key=f"final_{game_id}")
                away_score = st.number_input(f"{away} final score", min_value=0, max_value=200,
                                             value=scores.get(game_id, {}).get("away", 0), key=f"away_{game_id}")
                home_score = st.number_input(f"{home} final score", min_value=0, max_value=200,
                                             value=scores.get(game_id, {}).get("home", 0), key=f"home_{game_id}")
                if use:
                    submitted[game_id] = {"away": int(away_score), "home": int(home_score)}
            save = st.form_submit_button("Save final paper results", use_container_width=True)
        if save:
            try:
                history.save_paper_results(view.run_id, submitted,
                                           recorded_at=datetime.now(ZoneInfo("America/Detroit")).isoformat(timespec="microseconds"))
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.rerun()
        if prior:
            graded = adapter.grade_paper_results(run, scores)
            for ticket in graded["tickets"]:
                if ticket["selected"]:
                    st.write(f"{ticket['ticket_key']}: {ticket['result']}")
            with st.expander("All constructed ticket results"):
                for ticket in graded["tickets"]:
                    st.caption(f"{ticket['ticket_key']}: {ticket['result']}")


def render_cfb_page(adapter) -> None:
    history = LocalHistory()
    if "cfb_slate" not in st.session_state:
        st.session_state.cfb_slate = initial_cfb_slate()
    slate = st.session_state.cfb_slate
    view = st.session_state.get("cfb_view")
    st.title("College football teasers")
    st.caption(f"{CFB_TEASER.league} | {CFB_TEASER.bet_type} | {CFB_TEASER.model_version} | {CFB_TEASER.status} · local model workspace")
    if view is None:
        st.info("Enter pregame CFB lines and actual sportsbook teaser prices to build a full paper card.")
    else:
        _render_board(view, history, adapter, slate)

    st.subheader("Enter CFB market")
    with st.expander("Slate settings and prices", expanded=not bool(slate["sportsbook"])):
        with st.form("cfb_settings"):
            season = st.number_input("Season", min_value=2000, max_value=2100, value=int(slate["season"]), key="cfb_season")
            week = st.number_input("Week", min_value=1, max_value=20, value=int(slate["week"]), key="cfb_week")
            book = st.text_input("Sportsbook or source name", value=slate["sportsbook"], key="cfb_book")
            capture = st.text_input("Captured at · ISO with UTC offset", value=slate["captured_at"], key="cfb_capture")
            sources = ("manual_sportsbook", "user_screenshot", "reference_source", "future_odds_api")
            source = st.selectbox("Line source", sources, index=sources.index(slate["source"]))
            label = st.selectbox("Pregame provenance", ("current_pregame", "true_timestamped_pregame", "archived_pregame_reference"),
                                 index=("current_pregame", "true_timestamped_pregame", "archived_pregame_reference").index(slate["line_label"]))
            two = st.text_input("Observed 2-team 6-point price", value=slate["prices"]["2"], placeholder="-110")
            three = st.text_input("Observed 3-team 6-point price", value=slate["prices"]["3"], placeholder="+170")
            st.caption("Leave an unobserved price blank. EV and paper selection then remain unavailable for that size.")
            save = st.form_submit_button("Save CFB settings", use_container_width=True)
        if save:
            slate.update(season=int(season), week=int(week), sportsbook=book.strip(),
                         captured_at=capture.strip(), source=source, line_label=label,
                         prices={"2": two.strip(), "3": three.strip()})
            st.rerun()

    st.caption(f"{len(slate['rows'])} quoted side(s). Enter both sides when both were observed.")
    with st.expander("Review entered sides"):
        for index, row in enumerate(list(slate["rows"])):
            st.write(f"{row['team']} {signed(row['spread'])} · {row['away_team']} at {row['home_team']} · total {row['total']}")
            if st.button("Remove side", key=f"cfb_remove_{index}", use_container_width=True):
                slate["rows"].pop(index)
                st.rerun()
    with st.expander("Add a CFB quoted side", expanded=not bool(slate["rows"])):
        with st.form("cfb_add_side", clear_on_submit=True):
            away = st.text_input("Away team", placeholder="College name")
            home = st.text_input("Home team", placeholder="College name")
            side = st.radio("Quoted side", ("Away", "Home"), horizontal=True)
            spread = st.text_input("Signed spread", placeholder="+2.5 or -7.5")
            total = st.text_input("Game total", placeholder="48.5")
            default_kickoff = (datetime.now(ZoneInfo("America/Detroit")) + timedelta(days=7)).replace(hour=12, minute=0, second=0, microsecond=0)
            kickoff = st.text_input("Kickoff · ISO with UTC offset", value=default_kickoff.isoformat(timespec="seconds"))
            add = st.form_submit_button("Add CFB side", use_container_width=True)
        if add:
            slate["rows"].append({"away_team": away.strip(), "home_team": home.strip(),
                                  "team": away.strip() if side == "Away" else home.strip(),
                                  "spread": spread.strip(), "total": total.strip(), "kickoff": kickoff.strip()})
            st.rerun()

    if st.button("Build PAPER card from entered slate", type="primary", use_container_width=True):
        try:
            paper = adapter.grade_cfb_paper(slate)
            snapshot = history.ingest_snapshot(slate)
            record = history.save_run(run_record(paper, slate, snapshot["snapshot_id"]))
        except (ValueError, RuntimeError, OSError) as exc:
            st.error(str(exc))
        else:
            st.session_state.cfb_view = replace(paper, run_id=record["run_id"],
                                                 snapshot_id=snapshot["snapshot_id"])
            st.session_state.cfb_built_fingerprint = slate_fingerprint(slate)
            st.rerun()
    if view is not None:
        st.markdown("[↑ View paper card](#hypothetical-paper-card)")

    with st.expander("Import, export, or reset CFB slate"):
        uploaded = st.file_uploader("Import CFB slate JSON", type="json", max_upload_size=1)
        if st.button("Import CFB slate", disabled=uploaded is None, use_container_width=True):
            try:
                parsed = decode_slate(uploaded.getvalue())
                if parsed.get("league") != "CFB":
                    raise ValueError("imported slate is not CFB")
                adapter.grade_cfb_paper(parsed)
            except (ValueError, RuntimeError) as exc:
                st.error(str(exc))
            else:
                st.session_state.cfb_slate = parsed
                st.session_state.pop("cfb_view", None)
                st.rerun()
        st.download_button("Download CFB slate JSON", encode_slate(slate), "cfb_slate.json",
                           mime="application/json", use_container_width=True)
        if st.button("Start blank CFB slate", use_container_width=True):
            st.session_state.cfb_slate = initial_cfb_slate()
            st.session_state.pop("cfb_view", None)
            st.rerun()

    st.subheader("Paper history")
    perf = paper_performance(history, adapter)
    st.markdown(f"**Cumulative paper:** {perf['settled']} settled · {perf['wins']} W / {perf['losses']} L / {perf['pushes']} P · {signed(perf['net_units'])} hypothetical units")
    st.caption("Paper performance is separate from live bankroll and placement totals.")
    with st.expander("Filter saved runs"):
        league = st.selectbox("League", ("All", "NFL", "CFB"), index=2)
        bet_type = st.selectbox("Bet type", ("All", "TEASER", "ATS", "ML", "ATS_PARLAY", "ML_PARLAY"), index=1)
        version = st.text_input("Model version", value="teaser_v1.0")
        status = st.selectbox("Status", ("All", "LIVE", "PAPER", "CHALLENGER"), index=2)
        week_filter = st.number_input("Week · 0 for all", min_value=0, max_value=20, value=0)
        date_filter = st.text_input("Capture date · blank for all", value="", placeholder="YYYY-MM-DD")
    runs = history.runs(league=None if league == "All" else league,
                        bet_type=None if bet_type == "All" else bet_type,
                        model_version=version or None, status=None if status == "All" else status)
    for run in runs:
        if (week_filter and run["week"] != week_filter) or (date_filter and not run["captured_at"].startswith(date_filter)):
            continue
        st.caption(f"{run['captured_at']} · {run['strategy']['league']} | {run['strategy']['bet_type']} | {run['strategy']['model_version']} | {run['strategy']['status']} · {run['run_id']}")
        if st.button("Open saved paper run", key=f"open_{run['run_id']}", use_container_width=True):
            st.session_state.cfb_slate = run["slate"]
            paper = adapter.grade_cfb_paper(run["slate"], historical=True)
            st.session_state.cfb_view = replace(paper, run_id=run["run_id"], snapshot_id=run["snapshot_id"])
            st.session_state.cfb_built_fingerprint = slate_fingerprint(run["slate"])
            st.rerun()

    with st.expander("Legacy pinned CFB paper artifact"):
        path = Path(__file__).resolve().parents[1] / ".model_reference" / "data" / "live" / "cards" / "cfb_paper_board_2026-09-19.json"
        if st.button("Read original 2026-09-19 paper board", use_container_width=True):
            from teaser_app.integrity import verify_model
            verify_model()
            old = json.loads(path.read_text(encoding="utf-8"))
            st.session_state.cfb_legacy = old
        old = st.session_state.get("cfb_legacy")
        if old:
            st.caption(f"{old['board_id']} · frozen {old['frozen_at']} · {old['games']} games · actual units {old['actual_units_staked']}")
            for leg in old["primary_qualifiers"]:
                st.write(f"#{leg['rank']} {leg['team']} {signed(leg['spread'])} → {signed(leg['teased_spread'])} · P_est {percent(leg['p_est'])}")
            st.caption(old["ev_note"])
