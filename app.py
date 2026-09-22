"""Mobile-first local Streamlit interface for the pinned teaser model."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.cfb_page import render_cfb_page
from teaser_app.inputs import decode_slate, encode_slate, slate_fingerprint
from teaser_app.market_data import LocalHistory
from teaser_app.market_compare_page import render_market_comparison
from teaser_app.presentation import card as html_card, h, percent, signed
from teaser_app.screenshot_import import render_screenshot_import
from teaser_app.strategy import NFL_TEASER
from teaser_app.url_import import render_url_import


st.set_page_config(page_title="Football Teasers v1.0", page_icon="🏈", layout="centered", initial_sidebar_state="collapsed")
st.markdown((Path(__file__).parent / "teaser_app" / "styles.css").read_text(), unsafe_allow_html=True)


def initial_slate() -> dict:
    now = datetime.now(ZoneInfo("America/Detroit"))
    return {
        "schema_version": 2,
        "league": "NFL", "source": "manual_sportsbook", "line_label": "current_pregame",
        "season": now.year,
        "week": 1,
        "sportsbook": "",
        "captured_at": now.isoformat(timespec="seconds"),
        "prices": {"2": "", "3": ""},
        "rows": [],
    }


if "adapter" not in st.session_state:
    st.session_state.adapter = TeaserModelAdapter()
if "slate" not in st.session_state:
    st.session_state.slate = initial_slate()
if "origin" not in st.session_state:
    st.session_state.origin = "manual"
if "revision" not in st.session_state:
    st.session_state.revision = 0

adapter: TeaserModelAdapter = st.session_state.adapter
slate: dict = st.session_state.slate

mode = st.radio("League and model track", ("NFL · LIVE", "CFB · PAPER"), horizontal=True)
render_url_import()
render_screenshot_import()
render_market_comparison("CFB" if mode == "CFB · PAPER" else "NFL")
if mode == "CFB · PAPER":
    render_cfb_page(adapter)
    st.stop()


def reset_result() -> None:
    st.session_state.pop("card", None)
    st.session_state.pop("built_fingerprint", None)
    st.session_state.pop("recheck", None)
    st.session_state.pop("recheck_fingerprint", None)
    st.session_state.pop("reported_placed", None)


def show_error(exc: Exception) -> None:
    st.error(str(exc))


def render_ticket(ticket, *, selected: bool = False, recheck=None) -> None:
    title = " / ".join(h(team) for team in ticket["teams"])
    odds = h(signed(ticket["offered_american"]))
    stake = f" · {h(ticket['stake_units'])}u" if selected else ""
    body = (
        f"<strong>{odds}</strong>{stake} · P_ticket {h(percent(ticket['p_ticket']))}"
        f"<br>Break-even {h(percent(ticket['break_even']))} · EV {h(percent(ticket['ev_per_unit'], sign=True))}"
        f"<br>{h(ticket['status'].replace('_', ' '))}"
    )
    if recheck is not None:
        body += (
            f"<br><span class='teaser-muted'>Latest recheck: {h(recheck['verdict'])}"
            f" · P {h(percent(recheck['new_p_ticket']))}"
            f" · EV {h(percent(recheck['new_ev'], sign=True))}</span>"
        )
    st.markdown(html_card(title, "Selected · grading-time values" if selected else "Constructed ticket", body,
                          accent="selected" if selected else ""), unsafe_allow_html=True)


def render_leg(leg) -> None:
    body = (
        f"{h(signed(leg['spread']))} → {h(signed(leg['teased_spread']))}"
        f" · Total {h(leg['total'])}"
        f"<br>P_est <strong>{h(percent(leg['p_est']))}</strong>"
    )
    st.markdown(html_card(h(leg["team"]), f"#{leg['rank']} · PRIMARY / LIVE", body),
                unsafe_allow_html=True)


def render_exposure(view) -> None:
    teams = {leg["leg_id"]: leg["team"] for leg in view.qualifying_legs}
    ordered = sorted(view.exposure.items(), key=lambda item: next(
        (leg["rank"] for leg in view.qualifying_legs if leg["leg_id"] == item[0]), 999))
    if ordered:
        st.markdown(" · ".join(f"**{h(teams.get(leg_id, leg_id))} {units}u**" for leg_id, units in ordered),
                    unsafe_allow_html=True)
    else:
        st.caption("No selected-ticket exposure.")


st.title("NFL Teaser v1.0")
st.caption(f"{NFL_TEASER.league} | {NFL_TEASER.bet_type} | {NFL_TEASER.model_version} | {NFL_TEASER.status} · local manual workspace · no bets are placed by this app")


view = st.session_state.get("card")
if view is None:
    st.info("Enter a sportsbook slate, then build a proposal. The model will determine qualifying legs and tickets.")

else:
    current_fp = slate_fingerprint(slate)
    built_fp = st.session_state.get("built_fingerprint")
    recheck_view = st.session_state.get("recheck")
    recheck_fp = st.session_state.get("recheck_fingerprint")
    fresh = current_fp in {built_fp, recheck_fp}
    validated = bool(recheck_view and recheck_view.verdict == "VALIDATED" and current_fp == recheck_fp)
    reported_placed = st.session_state.get("reported_placed", ())

    st.divider()
    st.markdown(f"**{h(view.season)} · Week {h(view.week)} · {h(view.sportsbook)}**")
    if view.historical:
        st.warning("HISTORICAL EXAMPLE — captured Week 2 sportsbook snapshot. This is a regression example, not current betting data.")
    elif not fresh:
        st.warning("INPUTS CHANGED — this is the previous proposal. Build or recheck before using these values.")
    else:
        st.caption("Proposal from the entered sportsbook snapshot. Verify current lines and prices before any outside action.")

    placement_label = "PLACED · operator reported, session only" if reported_placed else "🔵 SHADOW — NOT PLACED"
    recheck_label = (
        "🔴 DISCARD — REBUILD" if recheck_view and recheck_view.verdict != "VALIDATED" and fresh else
        "🟢 VALIDATED · model recheck" if validated else "🟡 PENDING RECHECK"
    )
    st.markdown(f"**Placement:** {h(placement_label)}  \n**Recheck:** {h(recheck_label)}")

    st.subheader("Proposed card")
    st.caption("Model-selected subset · proposal exposure below · 1 unit per selected ticket from source model")
    if view.selected_tickets:
        latest = {t["ticket_key"]: t for t in recheck_view.tickets} if recheck_view and fresh else {}
        for ticket in view.selected_tickets:
            render_ticket(ticket, selected=True, recheck=latest.get(ticket["ticket_key"]))
    else:
        st.info("The model selected no positive-EV ticket at the supplied prices and lines.")

    st.markdown("**Proposed aggregate leg exposure**")
    render_exposure(view)

    st.subheader("Qualifying PRIMARY / LIVE legs")
    if view.qualifying_legs:
        for leg in view.qualifying_legs:
            render_leg(leg)
    else:
        st.info("No PRIMARY / LIVE leg qualifies in this slate.")

    with st.expander("Recheck and operator status"):
        st.caption("Capture a new sportsbook snapshot by editing the slate and capture time below, save it, then recheck. The model compares selected tickets with those new inputs.")
        can_recheck = bool(view.selected_tickets) and not view.historical and not reported_placed
        if st.button("Recheck selected card against current input", disabled=not can_recheck, use_container_width=True):
            try:
                result = adapter.recheck(view.card_id, slate)
            except (ValueError, RuntimeError) as exc:
                show_error(exc)
            else:
                st.session_state.recheck = result
                st.session_state.recheck_fingerprint = slate_fingerprint(slate)
                st.rerun()
        if recheck_view:
            st.markdown(f"**{h(recheck_view.verdict)}** · rechecked {h(recheck_view.rechecked_at)}")
            st.caption("Geometry, total and EV recheck only. This is not a model-designated placement approval; freshness, kickoff and cumulative actual placements are not checked here.")
            for ticket in recheck_view.tickets:
                st.markdown(f"**{h(ticket['ticket_key'])}: {h(ticket['verdict'])}**")
                for reason in ticket["reasons"]:
                    st.caption(reason)
            if recheck_view.rebuilt_card:
                st.warning("Source model rebuilt a new proposal after a discard. It is pending its own recheck.")
                if st.button("Adopt rebuilt proposal", disabled=current_fp != recheck_fp, use_container_width=True):
                    st.session_state.card = recheck_view.rebuilt_card
                    st.session_state.built_fingerprint = current_fp
                    st.session_state.pop("recheck", None)
                    st.session_state.pop("recheck_fingerprint", None)
                    st.session_state.pop("reported_placed", None)
                    st.rerun()
        if validated and not view.historical and not reported_placed:
            st.caption("If you already placed tickets outside this app, you may annotate this session. No wager is submitted and no durable record is kept.")
            selected_labels = {" / ".join(t["teams"]): t["ticket_key"] for t in view.selected_tickets}
            with st.form("reported_placement"):
                named = st.multiselect("Tickets already placed", tuple(selected_labels))
                confirm = st.checkbox("I confirm these tickets were already placed outside this app")
                report = st.form_submit_button("Record operator-reported PLACED status", use_container_width=True)
            if report:
                if not confirm or not named:
                    st.error("Select tickets and confirm the outside placement first.")
                else:
                    st.session_state.reported_placed = tuple(selected_labels[name] for name in named)
                    st.rerun()
        elif reported_placed:
            st.caption("Reported placed ticket IDs: " + ", ".join(reported_placed) + ". This annotation disappears when the session ends.")

    with st.expander(f"Full ticket board · {len(view.tickets)} constructed"):
        st.caption("All model-constructed tickets, including negative EV and unavailable-price tickets. Selected tickets are shown above.")
        for ticket in view.tickets:
            render_ticket(ticket)

    with st.expander(f"ALL 6-POINT TEASER LEGS · research view · {len(view.research)} sides"):
        st.warning("RESEARCH ONLY. These rows never change live eligibility, selected tickets, EV or exposure. Primary P_est and secondary research scores are different populations.")
        for row in view.research:
            is_live = row["on_live_board"]
            label = "P_est" if row["geometry_class"] == "PRIMARY" else "Unadjusted research score"
            badge = "PRIMARY / LIVE" if is_live else f"{row['geometry_class']} / {row['track']} · paper/excluded"
            body = (
                f"{h(signed(row['spread']))} → {h(signed(row['teased_spread']))}"
                f" · Total {h(row['total'])}"
                f"<br>{h(label)} <strong>{h(percent(row['p_est']))}</strong>"
                f"<br>{h(row['exclusion_reason'])}"
            )
            if row["can_push"] and row["geometry_class"] != "PRIMARY":
                body += "<br><strong>RESEARCH P_est — PUSH SETTLEMENT NOT MODELED</strong>"
            st.markdown(html_card(h(row["team"]), h(badge), body, accent="" if is_live else "paper"),
                        unsafe_allow_html=True)
            if row["can_push"] and row["geometry_class"] != "PRIMARY":
                st.caption("Not a fully modeled win probability when the teased line can push; sportsbook push settlement and discrete push mass are not incorporated.")

    with st.expander("Audit and model provenance"):
        st.markdown("**Model pin:** `94e412ed5bedc03a60b0b86f1146ff80c9f9e195`")
        st.markdown(f"**Card:** `{h(view.card_id)}`  \n**Market snapshot:** `{h(view.market_snapshot_id)}`  \n**Price snapshot:** `{h(view.price_snapshot_id or 'NONE')}`")
        st.caption(f"Graded at {view.graded_at}; games scanned {view.games_scanned}. Full source precision is retained.")
        for leg in view.qualifying_legs:
            st.code(f"{leg['team']} {leg['leg_id']} · P_raw={leg['p_raw']} · bump={leg['bump']} · P_est={leg['p_est']}")
        for ticket in view.tickets:
            st.code(f"{' / '.join(ticket['teams'])} · P_ticket={ticket['p_ticket']} · break_even={ticket['break_even']} · EV/unit={ticket['ev_per_unit']}")

with st.expander("Slate input · season, book, prices", expanded=not bool(slate["sportsbook"])):
    with st.form("settings"):
        season = st.number_input("Season", min_value=2000, max_value=2100, value=int(slate["season"]), step=1)
        week = st.number_input("Week", min_value=1, max_value=18, value=int(slate["week"]), step=1)
        book = st.text_input("Sportsbook", value=slate["sportsbook"], placeholder="Name of the book showing these lines")
        captured = st.text_input("Captured at · ISO with UTC offset", value=slate["captured_at"])
        two = st.text_input("Actual 2-team 6-point price · American odds", value=slate["prices"]["2"], placeholder="e.g. -110")
        three = st.text_input("Actual 3-team 6-point price · American odds", value=slate["prices"]["3"], placeholder="e.g. +170")
        st.caption("Leave an unobserved price blank. No EV is available for that ticket size.")
        save_settings = st.form_submit_button("Save slate settings", use_container_width=True)
    if save_settings:
        slate.update(season=int(season), week=int(week), sportsbook=book.strip(), captured_at=captured.strip(),
                     prices={"2": two.strip(), "3": three.strip()})
        st.session_state.revision += 1
        st.rerun()

if st.button("Build proposal from entered slate", type="primary", use_container_width=True):
    try:
        view = adapter.grade(slate, historical=st.session_state.origin == "historical")
        if st.session_state.origin != "historical":
            LocalHistory().ingest_snapshot(slate)
    except (ValueError, RuntimeError, OSError) as exc:
        show_error(exc)
    else:
        reset_result()
        st.session_state.card = view
        st.session_state.built_fingerprint = slate_fingerprint(slate)
        st.rerun()
if st.session_state.get("card") is not None:
    st.markdown("[↑ View proposed card](#proposed-card)")

st.subheader("Quoted sides")
st.caption(f"{len(slate['rows'])} side(s) entered. One row per side; enter both sides if you want both researched.")

with st.expander(f"Review or edit {len(slate['rows'])} entered sides"):
    for index, row in enumerate(list(slate["rows"])):
        label = f"{row['team']} {signed(row['spread'])} · {row['away_team']} at {row['home_team']} · {row['total']}"
        with st.expander(label):
            with st.form(f"edit_{st.session_state.revision}_{index}"):
                e_away = st.selectbox("Away team", adapter.teams, index=adapter.teams.index(adapter.canonical_team(row["away_team"])), key=f"ea_{st.session_state.revision}_{index}")
                e_home = st.selectbox("Home team", adapter.teams, index=adapter.teams.index(adapter.canonical_team(row["home_team"])), key=f"eh_{st.session_state.revision}_{index}")
                e_side = st.radio("Quoted side", ("Away", "Home"), index=0 if adapter.canonical_team(row["team"]) == adapter.canonical_team(row["away_team"]) else 1,
                                  horizontal=True, key=f"es_{st.session_state.revision}_{index}")
                e_spread = st.text_input("Spread · keep sign and half point", value=row["spread"], key=f"esp_{st.session_state.revision}_{index}")
                e_total = st.text_input("Game total", value=row["total"], key=f"et_{st.session_state.revision}_{index}")
                e_kickoff = st.text_input("Kickoff · ISO with UTC offset", value=row["kickoff"], key=f"ek_{st.session_state.revision}_{index}")
                save_row = st.form_submit_button("Save this side", use_container_width=True)
            if save_row:
                slate["rows"][index] = {
                    "away_team": e_away, "home_team": e_home, "team": e_away if e_side == "Away" else e_home,
                    "spread": e_spread.strip(), "total": e_total.strip(), "kickoff": e_kickoff.strip(),
                }
                st.session_state.revision += 1
                st.rerun()
            if st.button("Remove this side", key=f"remove_{st.session_state.revision}_{index}", use_container_width=True):
                slate["rows"].pop(index)
                st.session_state.revision += 1
                st.rerun()

with st.expander("Add a quoted side", expanded=not bool(slate["rows"])):
    with st.form(f"add_side_{st.session_state.revision}"):
        away = st.selectbox("Away team", adapter.teams, index=0)
        home = st.selectbox("Home team", adapter.teams, index=1)
        side = st.radio("Quoted side", ("Away", "Home"), horizontal=True)
        spread = st.text_input("Spread · include sign", placeholder="+2.5 or -8.5")
        total = st.text_input("Game total", placeholder="43.5")
        default_kickoff = (datetime.now(ZoneInfo("America/Detroit")) + timedelta(days=7)).replace(hour=13, minute=0, second=0, microsecond=0)
        kickoff = st.text_input("Kickoff · ISO with UTC offset", value=default_kickoff.isoformat(timespec="seconds"))
        add_side = st.form_submit_button("Add side", use_container_width=True)
    if add_side:
        slate["rows"].append({
            "away_team": away, "home_team": home, "team": away if side == "Away" else home,
            "spread": spread.strip(), "total": total.strip(), "kickoff": kickoff.strip(),
        })
        st.session_state.revision += 1
        st.rerun()

with st.expander("Import, export, or inspect verified example"):
    uploaded = st.file_uploader("Import a slate JSON file", type="json", max_upload_size=1)
    if st.button("Import and build", disabled=uploaded is None, use_container_width=True):
        try:
            parsed = decode_slate(uploaded.getvalue())
            view = adapter.grade(parsed)
            LocalHistory().ingest_snapshot(parsed)
        except (ValueError, RuntimeError, OSError) as exc:
            show_error(exc)
        else:
            slate = parsed
            st.session_state.slate = parsed
            st.session_state.origin = "imported"
            st.session_state.revision += 1
            reset_result()
            st.session_state.card = view
            st.session_state.built_fingerprint = slate_fingerprint(parsed)
            st.rerun()
    st.download_button("Download entered slate JSON", data=encode_slate(slate), file_name="teaser_slate.json",
                       mime="application/json", use_container_width=True)
    if st.button("Load verified Week 2 historical example", use_container_width=True):
        try:
            example_slate, view = adapter.week2_example()
        except (ValueError, RuntimeError) as exc:
            show_error(exc)
        else:
            slate = example_slate
            st.session_state.slate = example_slate
            st.session_state.origin = "historical"
            st.session_state.revision += 1
            reset_result()
            st.session_state.card = view
            st.session_state.built_fingerprint = slate_fingerprint(example_slate)
            st.rerun()
    if st.button("Start a new blank slate", use_container_width=True):
        st.session_state.slate = initial_slate()
        st.session_state.origin = "manual"
        st.session_state.revision += 1
        reset_result()
        st.rerun()
