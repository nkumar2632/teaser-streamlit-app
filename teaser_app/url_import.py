"""Streamlit review gate for public reference URLs; no grading or placement path."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from teaser_app.market_data import LocalHistory
from teaser_app.providers.espn import FIELDS as ODDS_FIELDS
from teaser_app.url_ingest import URLIngestError, preview_url, save_confirmed_snapshot

EDIT_COLUMNS = ("source_event_id", "away_team", "home_team", "kickoff", "sportsbook", *ODDS_FIELDS)


def espn_preset_url(league: str, today: date | None = None) -> str:
    """Fetch the next standard game day rather than ESPN's sometimes settled default week."""
    day = today or datetime.now(ZoneInfo("America/Detroit")).date()
    if league not in {"NFL", "CFB"}:
        raise ValueError("unsupported ESPN preset league")
    target = 6 if league == "NFL" else 5  # Sunday or Saturday
    game_day = day + timedelta(days=(target - day.weekday()) % 7)
    path = "nfl" if league == "NFL" else "college-football"
    extra = "&groups=80" if league == "CFB" else ""
    return f"https://www.espn.com/{path}/scoreboard?dates={game_day:%Y%m%d}{extra}"


def render_url_import() -> None:
    with st.expander("Import public reference lines by URL"):
        st.caption("ESPN NFL/college-football scoreboard URLs only. Public reference lines are separate from your actual sportsbook slate; importing never builds a model card.")
        url = st.text_input("Public scoreboard URL", placeholder="https://www.espn.com/nfl/scoreboard/_/week/3/year/2026/seasontype/2")
        nfl, cfb = st.columns(2)
        with nfl:
            nfl_preset = st.button("Fetch ESPN NFL", width="stretch")
        with cfb:
            cfb_preset = st.button("Fetch ESPN CFB", width="stretch")
        st.caption("Presets target the next NFL Sunday or CFB Saturday. Use a dated URL for other game days.")
        manual_fetch = st.button("Fetch Lines", width="stretch")
        requested_url = (espn_preset_url("NFL") if nfl_preset else
                         espn_preset_url("CFB") if cfb_preset else url if manual_fetch else None)
        if requested_url is not None:
            st.session_state.pop("url_preview", None)
            st.session_state.pop("url_saved_snapshot", None)
            st.session_state.pop("url_review_table", None)
            try:
                st.session_state.url_preview = preview_url(requested_url)
            except URLIngestError as exc:
                st.error(str(exc))
            else:
                st.rerun()
        saved = st.session_state.get("url_saved_snapshot")
        if saved:
            st.success(f"Saved immutable REFERENCE snapshot {saved['snapshot_id']} · {saved['league']} · {saved['source_provider']}. It was not applied to a betting card.")
        preview = st.session_state.get("url_preview")
        if preview is None:
            return
        st.warning(f"REVIEW REQUIRED · {preview.spec.league} reference market from {preview.spec.provider}. These are not your confirmed execution lines.")
        st.caption(f"Fetched {preview.captured_at} · {len(preview.rows)} games · {preview.spec.source_url}")
        if preview.warnings:
            st.info(f"{len(preview.warnings)} review flags. Missing fields remain blank; malformed values must be corrected before confirmation.")
            with st.expander("Missing or uncertain values"):
                for warning in preview.warnings:
                    st.caption(warning)
        table = pd.DataFrame([{field: row.get(field) or "" for field in EDIT_COLUMNS} for row in preview.rows])
        edited = st.data_editor(table, hide_index=True, num_rows="fixed", width="stretch",
                                disabled=["source_event_id", "sportsbook"], key="url_review_table")
        st.caption("Edit any transcribed line or kickoff. Blank means unavailable. The displayed book is a reference quote, not your sportsbook account. No teaser payout is inferred.")
        if st.button("Confirm Market Snapshot", type="primary", width="stretch"):
            try:
                result = save_confirmed_snapshot(preview, edited.to_dict("records"), LocalHistory())
            except (URLIngestError, ValueError, OSError) as exc:
                st.error(str(exc))
            else:
                st.session_state.url_saved_snapshot = result
                st.session_state.pop("url_preview", None)
                st.rerun()
        if st.button("Discard fetched preview", width="stretch"):
            st.session_state.pop("url_preview", None)
            st.rerun()
