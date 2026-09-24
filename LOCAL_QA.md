# Saturday CFB local QA

The Claude verification pack is unavailable in this workspace. Its exact 6/6 golden-fixture verification is **pending local QA tonight**. Do not recreate expected outputs or change the frozen model to make that pack pass.

1. Start the app locally: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/streamlit run app.py --server.address 127.0.0.1`.
2. Select **CFB · PAPER**. In **Public Lines · fetch and review ESPN**, choose the Saturday date, fetch the CFB scoreboard, review lines/kickoffs, and confirm the REFERENCE snapshot.
3. In **Public CFB lines**, review usable and excluded games, select the saved snapshot, and click **Use this slate for proposal**. Confirm that no manual quoted-side entry is needed.
4. Build once with missing teaser prices. Check ranked legs, constructed tickets, unavailable EV for unpriced sizes, the public-line and `bluecoins.ag` menu labels, and the absence of any placement control.
5. Enter only the missing observed 6-point teaser-menu prices, build again, and check the priced PAPER card. Ordinary Streamlit reruns must retain its run ID and must not create another saved run.
6. Run the Claude pack's own 6/6 golden-fixture instructions when the pack is available locally, compare exact outputs without changing expectations, and record the result separately. This has **not** been verified in the remote workspace.

The public ESPN response and actual sportsbook menu should be reviewed by the operator. This is a PAPER workflow; it does not authorize or place wagers.

# Sunday NFL local QA

1. In **NFL · LIVE**, fetch and confirm a real ESPN NFL scoreboard. Choose its saved REFERENCE snapshot, use it for a proposal, and build without entering quoted sides. Check canonical teams, excluded games, the public/reference screening label, and the absence of the placement form. Missing teaser prices must leave leg grading available.
2. Upload one or more current sportsbook screenshots on the local Mac. Review Apple Vision extraction, correct teams/lines/times/prices, and explicitly confirm an EXECUTION snapshot. Raw screenshots should not be saved in the app data area. Choose the saved EXECUTION snapshot and build; the proposal must still be unplaced.
3. Capture and confirm a **newer** screenshot after the proposal is built. Select it for recheck. If the 6-point teaser menu is stale or missing, use **I verified these teaser prices are unchanged now** only after checking the actual sportsbook menu. Recheck the card; confirm that the operator-reported placement form appears only for a fresh, validated, same-book EXECUTION recheck before kickoff.
4. If a wager was actually placed outside the app, rehearse the explicit placement report and verify the saved record references both confirmed snapshot IDs. Otherwise leave the card PROPOSED. A public/reference card and a mixed-book card must never expose the placement form.

Apple Vision and real sportsbook browser behavior require the local Mac and were not verified remotely. The Claude 6/6 golden-fixture pack remains pending local QA; do not infer its expected values from app tests.
