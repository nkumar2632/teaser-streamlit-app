"""Streamlit upload, local extraction, review, and confirmation gate for sportsbook images."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from teaser_app.market_data import LocalHistory
from teaser_app.screenshot_ingest import (
    DEFAULT_TEASER_MENU,
    AppleVisionExtractor,
    ManualTextExtractor,
    MARKET_FIELDS,
    REVIEW_FIELDS,
    ScreenshotIngestError,
    ScreenshotPreview,
    extract_screenshots,
    known_cfb_teams,
    normalize_screenshot_review,
    teaser_price_source,
    reparse_review_text,
    save_confirmed_execution,
    validate_uploads,
)

DISPLAY_COLUMNS = ("candidate_id", *REVIEW_FIELDS, "review_status")


def _review_status(row: dict) -> str:
    states = row.get("field_states", {})
    parts = [f"{field.replace('_', ' ')}: {state}"
             for field, state in states.items() if state in {"uncertain", "conflict"}]
    parts.extend(row.get("warnings", []))
    missing = [field.replace("_", " ") for field in MARKET_FIELDS if row.get(field) in (None, "")]
    if missing:
        parts.append("missing: " + ", ".join(missing))
    return " · ".join(parts) if parts else "extracted · high confidence"


def _table(preview: ScreenshotPreview) -> pd.DataFrame:
    rows = [{**{field: row.get(field) or "" for field in DISPLAY_COLUMNS[:-1]},
             "review_status": _review_status(row)} for row in preview.candidates]
    if not rows:
        rows = [{field: "" for field in DISPLAY_COLUMNS}]
        rows[0]["review_status"] = "manual row · fill teams, kickoff, and visible markets"
    return pd.DataFrame(rows, columns=DISPLAY_COLUMNS)


def _edit_summary(preview: ScreenshotPreview, rows: list[dict]) -> list[str]:
    originals = {row["candidate_id"]: row for row in preview.candidates}
    summaries = []
    for position, row in enumerate(rows, start=1):
        original = originals.get(row.get("candidate_id"))
        if original is None:
            if any(str(row.get(field) or "").strip() for field in REVIEW_FIELDS):
                summaries.append(f"Row {position}: manually added")
            continue
        changed = [field.replace("_", " ") for field in REVIEW_FIELDS
                   if str(row.get(field) or "").strip() != str(original.get(field) or "").strip()]
        if changed:
            summaries.append(f"Row {position}: edited " + ", ".join(changed))
    return summaries


def _clear(*, include_table: bool = True) -> None:
    keys = ["screenshot_preview", "screenshot_saved_snapshot"]
    if include_table:
        keys.append("screenshot_review_table")
    for key in keys:
        st.session_state.pop(key, None)


def render_screenshot_import() -> None:
    with st.expander("Import my sportsbook screenshots"):
        st.caption("Images stay on this Mac. Review and confirmation are required before an immutable EXECUTION snapshot is saved; no wager is placed.")
        sportsbook = st.text_input("Sportsbook", value="bluecoins.ag", key="screenshot_sportsbook",
                                   placeholder="Enter the sportsbook shown")
        league = st.selectbox("Screenshot league", ("NFL", "CFB"), key="screenshot_league")
        captured_at = st.text_input(
            "Lines captured at (ISO time with offset)",
            value=datetime.now(ZoneInfo("America/Detroit")).isoformat(timespec="seconds"),
            key="screenshot_captured_at",
        )
        uploads = st.file_uploader(
            "Upload PNG or JPG screenshots",
            type=("png", "jpg", "jpeg"), accept_multiple_files=True,
            max_upload_size=8, key="sportsbook_screenshots",
        )
        st.caption("Up to 6 images, 8 MB each and 24 MB combined. Raw files are validated in memory and are not retained after extraction.")

        if st.button("Extract Lines", width="stretch"):
            _clear()
            try:
                files = validate_uploads([(item.name, item.type, item.getvalue()) for item in uploads])
                known = known_cfb_teams(LocalHistory()) if league == "CFB" else ()
                try:
                    preview = extract_screenshots(files, league, sportsbook, captured_at,
                                                  AppleVisionExtractor(), known_teams=known)
                except ScreenshotIngestError as ocr_error:
                    preview = extract_screenshots(files, league, sportsbook, captured_at,
                                                  ManualTextExtractor(), known_teams=known)
                    preview = ScreenshotPreview(
                        preview.league, preview.sportsbook, preview.captured_at,
                        preview.extracted_at, preview.extractor, preview.files,
                        preview.candidates, preview.teaser_prices, preview.teaser_states,
                        (str(ocr_error), *preview.warnings), preview.recognized_text, preview.known_teams,
                    )
                st.session_state.screenshot_preview = preview
            except ScreenshotIngestError as exc:
                st.error(str(exc))
            else:
                st.rerun()

        saved = st.session_state.get("screenshot_saved_snapshot")
        if saved:
            excluded = saved.get("excluded_review_rows") or []
            st.success(f"Saved immutable EXECUTION snapshot {saved['snapshot_id']} · {saved['league']} · {saved['sportsbook']} · "
                       f"{len(saved['events'])} usable / {len(excluded)} excluded. No betting card or placement record was changed.")
            for item in excluded:
                st.caption(f"Excluded · {item['game']} · {item['reason']}")

        preview = st.session_state.get("screenshot_preview")
        if preview is None:
            return

        st.warning("REVIEW REQUIRED · SCREENSHOT EXTRACTION IS UNVERIFIED. Correct every uncertain value before confirmation.")
        st.caption(f"{preview.league} · {preview.sportsbook} · captured {preview.captured_at} · {len(preview.files)} screenshot(s) · extractor {preview.extractor}")
        for image in preview.files:
            st.caption(f"{image.filename} · {image.width}×{image.height} · SHA-256 {image.sha256[:16]}…")
        with st.expander("Uploaded source images"):
            for image in preview.files:
                st.image(image.content, caption=image.filename, width="stretch")
        if preview.warnings:
            with st.expander(f"Extraction warnings · {len(preview.warnings)}"):
                for warning in preview.warnings:
                    st.caption(warning)

        with st.expander("Recognized text · correct or enter manually"):
            texts = [st.text_area(
                f"{image.filename} recognized text",
                value=preview.recognized_text[index] if index < len(preview.recognized_text) else "",
                height=140, key=f"screenshot_text_{image.sha256}_{preview.extracted_at}",
            ) for index, image in enumerate(preview.files)]
            st.caption("Use one team/market row per line. Manual text is treated as uncertain until you confirm the final table.")
            if st.button("Re-parse edited text", width="stretch"):
                try:
                    st.session_state.screenshot_preview = reparse_review_text(preview, texts)
                except ScreenshotIngestError as exc:
                    st.error(str(exc))
                else:
                    st.session_state.pop("screenshot_review_table", None)
                    st.rerun()

        table = _table(preview)
        edited = st.data_editor(
            table, hide_index=True, num_rows="dynamic", width="stretch",
            disabled=("candidate_id", "review_status"), key="screenshot_review_table",
        )
        st.caption("You may add or remove rows and correct teams, kickoff, spreads, odds, totals, or moneylines. Blank means missing. Conflicting values must be resolved explicitly.")
        edited_records = edited.drop(columns=["review_status"], errors="ignore").to_dict("records")
        changes = _edit_summary(preview, edited_records)
        if changes:
            st.info("User edits in this review: " + " · ".join(changes))

        two, three = st.columns(2)
        price_key = preview.files[0].sha256[:16] + preview.extracted_at
        # Without a teaser menu on the screenshot, the standard menu is prefilled as a default only.
        with two:
            price_two = st.text_input("2-team 6-point teaser price",
                                      value=preview.teaser_prices.get("2") or DEFAULT_TEASER_MENU["2"],
                                      key=f"screenshot_teaser_2_{price_key}")
        with three:
            price_three = st.text_input("3-team 6-point teaser price",
                                        value=preview.teaser_prices.get("3") or DEFAULT_TEASER_MENU["3"],
                                        key=f"screenshot_teaser_3_{price_key}")
        labels = {"screenshot": "read from the screenshot", "operator": "entered by you",
                  "default": "standard default · not an observed or verified quote"}
        st.caption(" · ".join(f"{size}-team: {labels[source]}" for size, value in (("2", price_two), ("3", price_three))
                              if (source := teaser_price_source(preview, size, value))))
        if any(state in {"uncertain", "conflict"} for state in preview.teaser_states.values()):
            st.warning("Teaser pricing was uncertain or conflicting. Confirm only the standard 6-point football teaser; leave unrelated products blank.")

        rows = [row for row in edited_records if any(str(row.get(field) or "").strip()
                                                     for field in REVIEW_FIELDS)]
        prices = {"2": price_two, "3": price_three}
        if preview.league == "CFB" and rows:
            try:
                planned = normalize_screenshot_review(preview, rows, prices)
            except ScreenshotIngestError as exc:
                st.error(f"Nothing can be saved yet: {exc}")
            else:
                excluded = planned.get("excluded_review_rows") or []
                st.info(f"CFB PAPER · {len(planned['events'])} usable / {len(excluded)} excluded. Excluded games "
                        "are not saved and nothing is guessed; fix a row above to include it.")
                for item in excluded:
                    st.warning(f"Excluded · {item['game']} · {item['reason']}")
        if preview.league == "NFL" and _render_nfl_actions(preview, rows, prices):
            return
        if st.button("Confirm EXECUTION Snapshot", type="primary" if preview.league != "NFL" else "secondary",
                     width="stretch"):
            try:
                result = save_confirmed_execution(preview, rows, prices, LocalHistory())
            except (ScreenshotIngestError, ValueError, OSError) as exc:
                st.error(str(exc))
            else:
                st.session_state.screenshot_saved_snapshot = result
                st.session_state.pop("screenshot_preview", None)
                st.rerun()

        if st.button("Discard screenshot preview", width="stretch"):
            _clear(include_table=False)
            st.rerun()


def _render_nfl_actions(preview: ScreenshotPreview, rows: list[dict], prices: dict) -> bool:
    """One-click NFL LIVE actions. Returns True after handling a click (the page reruns)."""
    from teaser_app.nfl_market_page import _now, adopt_proposal, adopt_recheck
    from teaser_app.nfl_workflow import (DEFAULT_TEASER_MENU, active_proposal, build_active_proposal,
                                         recheck_active_proposal)

    history = LocalHistory()
    adapter = st.session_state.get("adapter")
    active = active_proposal(history)
    if active is None:
        st.caption(f"Confirm & Build saves this EXECUTION snapshot and builds the LIVE proposal. Missing "
                   f"teaser prices use the standard menu (2-team {DEFAULT_TEASER_MENU['2']} · 3-team "
                   f"{DEFAULT_TEASER_MENU['3']}) as defaults only; they are verified separately before any "
                   "placement report.")
        clicked = st.button("Confirm & Build Proposal", type="primary", width="stretch")
        verify = False
    else:
        menu = active["card_slate"]["prices"]
        st.caption(f"Active proposal {active['card_id']} · this newer screenshot rechecks it with the same "
                   f"teaser menu (2-team {menu.get('2') or '—'} · 3-team {menu.get('3') or '—'}).")
        verify = st.checkbox(f"I verified these teaser prices on {active['card_context']['menu_book']} "
                             "are unchanged now", key="screenshot_verify_menu")
        clicked = st.button("Confirm & Recheck Active Proposal", type="primary", width="stretch")
    if not clicked:
        return False
    try:
        snapshot = save_confirmed_execution(preview, rows, prices, history)
        if active is None:
            view, prepared, _ = build_active_proposal(history, adapter, snapshot, now=_now())
            adopt_proposal(view, prepared.slate, prepared.context)
        else:
            view = st.session_state.get("card")
            if view is None or view.card_id != active["card_id"]:
                raise ValueError("Open the active proposal first (it is restored on the NFL page)")
            recheck, prepared, _ = recheck_active_proposal(history, adapter, view, active, snapshot,
                                                           now=_now(), verify_menu=verify)
            adopt_recheck(recheck, prepared)
    except (ScreenshotIngestError, ValueError, RuntimeError, OSError) as exc:
        st.error(str(exc))
        return True
    st.session_state.screenshot_saved_snapshot = snapshot
    st.session_state.pop("screenshot_preview", None)
    st.rerun()
    return True
