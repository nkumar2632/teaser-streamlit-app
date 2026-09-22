"""Read-only reference-versus-sportsbook display; never feeds a model card."""

from __future__ import annotations

import streamlit as st

from teaser_app.market_compare import compare_snapshots, line_difference
from teaser_app.market_data import LocalHistory
from teaser_app.presentation import h


def _label(snapshot: dict) -> str:
    source = snapshot.get("source_provider") or snapshot.get("sportsbook") or snapshot.get("source") or "unspecified"
    return f"{snapshot['captured_at']} · {source} · {snapshot['snapshot_id']}"


def _display(value) -> str:
    return h(value) if value is not None and value != "" else "—"


def _snapshot_caption(label: str, snapshot: dict, age: dict) -> None:
    provider = snapshot.get("source_provider") or snapshot.get("source") or "unspecified"
    book = snapshot.get("sportsbook") or "unspecified"
    age_text = f"{age['age_hours']}h old" if age["age_hours"] is not None else "age unavailable"
    st.caption(f"{label}: {h(provider)} · book {h(book)} · captured {h(snapshot['captured_at'])} · {age_text} · {h(snapshot['snapshot_id'])}")


def _default_reference(references: list[dict], execution: dict | None) -> int:
    if execution is None:
        return 0
    for index, record in enumerate(references):
        if (record.get("season"), record.get("week")) == (execution.get("season"), execution.get("week")):
            return index
    return 0


def render_market_comparison(league: str) -> None:
    history = LocalHistory()
    references = list(reversed(history.market_history(league=league, role="REFERENCE")))
    executions = list(reversed(history.market_history(league=league, role="EXECUTION")))
    unknown_count = len(history.market_history(league=league, role="UNCLASSIFIED"))
    with st.expander("Compare reference vs my sportsbook"):
        st.caption(f"{league} · read-only line comparison. Reference prices are never copied into a model card or placement record.")
        if unknown_count:
            st.caption(f"{unknown_count} older or uncertain snapshot(s) are unclassified and excluded from comparison.")
        if not references:
            st.info("No confirmed REFERENCE snapshot yet. Fetch ESPN lines and confirm the review table first.")
            return
        if not executions:
            st.info("No confirmed EXECUTION snapshot yet. Build or import a slate with an identified actual sportsbook. Reference lines cannot stand in for your sportsbook lines.")
            return
        execution = st.selectbox("My EXECUTION snapshot", executions, format_func=_label,
                                 key=f"comparison_execution_{league}")
        reference = st.selectbox("REFERENCE snapshot", references, format_func=_label,
                                 index=_default_reference(references, execution),
                                 key=f"comparison_reference_{league}")
        result = compare_snapshots(reference, execution)
        _snapshot_caption("REFERENCE", reference, result["reference_age"])
        _snapshot_caption("EXECUTION", execution, result["execution_age"])
        if result["reference_age"]["stale"]:
            st.warning("Reference snapshot is over 24 hours old or has no valid capture time.")
        if result["execution_age"]["stale"]:
            st.warning("Execution snapshot is over 24 hours old or has no valid capture time. Recheck your actual sportsbook before any outside action.")
        st.caption("Δ is EXECUTION minus REFERENCE in points; it is a line difference, not EV or a betting recommendation. — means unavailable.")
        if not result["matches"]:
            st.info("No games matched safely across these snapshots.")
        for reference_event, execution_event in result["matches"]:
            game = f"{reference_event['away_team']} at {reference_event['home_team']}"
            with st.container(border=True):
                st.markdown(f"**{h(game)}** · kickoff {h(reference_event.get('kickoff') or 'unavailable')}")
                st.caption(f"Reference quote: {h(reference_event.get('sportsbook') or 'unspecified')} · My book: {h(execution_event.get('sportsbook') or 'unspecified')}")
                if reference_event.get("kickoff") != execution_event.get("kickoff"):
                    st.caption(f"Execution kickoff: {h(execution_event.get('kickoff') or 'unavailable')} (within 15-minute match tolerance)")
                for side in ("away", "home"):
                    ref_spread = reference_event.get(f"spread_{side}")
                    exe_spread = execution_event.get(f"spread_{side}")
                    delta = line_difference(ref_spread, exe_spread)
                    team = reference_event[f"{side}_team"]
                    difference = f" · **Δ {_display(delta)}**" if delta not in (None, "0") else f" · Δ {_display(delta)}"
                    st.markdown(f"**{h(team)}:** spread Ref {_display(ref_spread)} / My {_display(exe_spread)}{difference}")
                    st.caption(f"Spread price Ref {_display(reference_event.get(f'spread_{side}_price'))} / My {_display(execution_event.get(f'spread_{side}_price'))} · Moneyline Ref {_display(reference_event.get(f'moneyline_{side}'))} / My {_display(execution_event.get(f'moneyline_{side}'))}")
                ref_total, exe_total = reference_event.get("total"), execution_event.get("total")
                st.markdown(f"**Total:** Ref {_display(ref_total)} / My {_display(exe_total)} · Δ {_display(line_difference(ref_total, exe_total))}")
                st.caption(f"Over price Ref {_display(reference_event.get('over_price'))} / My {_display(execution_event.get('over_price'))} · Under price Ref {_display(reference_event.get('under_price'))} / My {_display(execution_event.get('under_price'))}")
                missing = [f"{label} {field.replace('_', ' ')}" for label, row in
                           (("Ref", reference_event), ("My", execution_event))
                           for field in ("spread_away", "spread_home", "spread_away_price",
                                         "spread_home_price", "moneyline_away", "moneyline_home",
                                         "total", "over_price", "under_price")
                           if row.get(field) is None or row.get(field) == ""]
                if missing:
                    st.caption(f"Missing markets: {', '.join(missing)}")
        for name, items in (("REFERENCE games without a safe execution match", result["reference_unmatched"]),
                            ("EXECUTION games without a safe reference match", result["execution_unmatched"])):
            if items:
                st.warning(f"{len(items)} {name.lower()}.")
                for item in items:
                    event = item["event"]
                    st.caption(f"{h(event.get('away_team') or '?')} at {h(event.get('home_team') or '?')} · {h(event.get('kickoff') or 'kickoff unavailable')} · {h(item['reason'])}")
