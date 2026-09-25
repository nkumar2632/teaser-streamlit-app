# Gate A — Saturday CFB PAPER

The Claude verification pack is unavailable in this workspace. Its exact 6/6 golden-fixture verification is **pending local QA tonight**. Do not recreate expected outputs or change the frozen model to make that pack pass.

1. Start locally: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/streamlit run app.py --server.address 127.0.0.1`.
2. Fetch the real Saturday ESPN CFB board, review it, and confirm the REFERENCE snapshot. **Pass:** no data is saved before Confirm.
3. Select the saved slate and build unpriced. **Pass:** no manual sides, legs/tickets display, EV is unavailable only for missing sizes, and no placement control exists.
4. Enter the observed 6-point prices and build priced. Refresh, then restart the app and reopen history. **Pass:** PAPER provenance and the saved run persist; reruns do not add duplicate runs; actual placements remain zero.

# Gate B — Claude golden pack

Run the pack's exact 6/6 instructions when available. **Pass:** all six exact fixtures agree without changing expected values. This remains pending local QA and was not reconstructed remotely.

# Gate C — Apple Vision

Upload real sportsbook screenshots, run local extraction, review every value, and confirm EXECUTION. **Pass:** corrected sportsbook, capture time, lines, totals and 6-point menu persist; raw images do not; no proposal is automatically PLACED.

# Gate D — Sunday NFL rehearsal

1. Build an NFL REFERENCE screening proposal. **Pass:** no manual sides and no placement form, even with valid Bluecoins menu prices.
2. Build from a confirmed same-book EXECUTION snapshot. **Pass:** it remains PROPOSED and unplaced.
3. Confirm a distinct newer EXECUTION board, verify the current same-book menu if needed, and recheck. **Pass:** stale/wrong-book/same-snapshot cases remain blocked; only the fresh validated sequence exposes the operator-report form.
4. Rehearse eligibility without reporting a wager. **Pass:** no placement JSON is created. Only use the report action after a wager was actually placed outside the app.

Real ESPN/browser behavior, Apple Vision, the Claude pack, and an actual sportsbook recheck require the local Mac. None is claimed as remotely verified.
