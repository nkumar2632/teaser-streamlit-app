# Football Teasers v1.0 — local Streamlit app

A mobile-first manual interface for the immutable [Teaser Model v1.0](https://github.com/nkumar2632/Football-Teasers-v1.0). NFL teasers use the model's LIVE proposal path; CFB teasers use its full PAPER path. Each view shows league, bet type, model version, and operational status. Enter actual observed 2-team/3-team 6-point teaser prices for EV and selection. Nothing here places a wager.

## Run locally

From `/Users/Nitin/Developer/teaser-streamlit-app`, the existing local environment can run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/streamlit run app.py --server.address 127.0.0.1
```

Open `http://127.0.0.1:8501` on this computer. The server is bound to loopback, not the LAN. To recreate the environment later, use Python 3.12: `python3.12 -m venv .venv`, `.venv/bin/python -m pip install -r requirements-dev.txt`, then `python3.12 scripts/setup_model.py` to clone or verify the disposable pinned source. The setup script refuses to overwrite a dirty or mismatched existing checkout. Network access is used for the specified GitHub clone, normal dependency installation, and user-triggered public ESPN imports.

## Enter a slate

1. Select **NFL · LIVE** or **CFB · PAPER**. Set season, week, sportsbook/source, and capture time as ISO 8601 with an explicit UTC offset. Enter only observed American teaser prices (`-110`, `+170`); leave unobserved sizes blank.
2. Add one quoted side per observed line, with signed spread, game total and timezone-aware kickoff. NFL uses canonical team choices; CFB accepts college names. Add the opposing side separately when observed. CFB also requires a pregame provenance label. Current and timestamped CFB captures must precede kickoff.
3. Build the card. NFL shows the live proposal and recheck workflow. CFB shows a **PAPER — NOT REAL MONEY** hypothetical selection, qualifying and excluded legs, all constructed tickets, market transcription, offered price, P_ticket, break-even, EV, and hypothetical exposure. CFB final scores can be recorded after the run is frozen; settled paper tickets and cumulative paper units appear in the paper history.

You can import/export bounded JSON slates. Version-1 NFL slates remain supported; version 2 adds league and provenance. **Load verified Week 2 historical example** runs the original NFL sportsbook snapshot unchanged. The original pinned CFB paper artifact remains readable in the CFB history section. NFL recheck and **PLACED** operator annotation remain session-only and unchanged. CFB paper outcomes never become live placements or live bankroll units.

## Import public reference lines

Open **Import public reference lines by URL**, paste a public [ESPN NFL scoreboard](https://www.espn.com/nfl/scoreboard) or [ESPN college-football scoreboard](https://www.espn.com/college-football/scoreboard) URL, then select **Fetch Lines**. Dated and week-style scoreboard links are supported. The app reads ESPN's public scoreboard JSON; the standalone odds page may require JavaScript verification and is not supported. VegasInsider URLs are not supported yet.

Review the editable table, especially missing or malformed fields, displayed book, and kickoff. Make corrections, then select **Confirm Market Snapshot**. Fetching alone writes nothing. Confirmation saves a new, immutable **REFERENCE** snapshot with the original URL, ESPN fetch URL, capture time, source event IDs, displayed book, and any corrections. It does not populate the NFL execution slate, build a model card, update CFB paper performance, or record a placement. Lines at your own sportsbook must still be entered separately. No teaser payout is inferred from a reference page.

## Boundary and limitations

`app.py` → display records/formatters in `teaser_app/` → `TeaserModelAdapter` → the actual source package in ignored `.model_reference/src`. Only the adapter imports the model. The adapter invokes source CFB paper grading, pricing, ticket construction, selection, and outcome grading; no teaser formulas are copied into the app. Strategy identity is a four-part record (`league`, `bet_type`, `model_version`, `status`), leaving ATS/ML and CHALLENGER as future configurations without implementing those models. The pin is `94e412ed5bedc03a60b0b86f1146ff80c9f9e195`. See [MODEL_PIN.md](MODEL_PIN.md), [SECURITY.md](SECURITY.md), and [market snapshot schema](market_data/schemas/market_snapshot_v1.md).

Manual and confirmed URL-reference market snapshots, plus CFB paper runs/results, are append-only local JSON in `market_data/`. The files are ignored by Git and each newer line or result receives a new ID; older records remain individually retrievable. The app never rewrites historical source artifacts in `.model_reference/`. Paper performance is calculated only from settled hypothetical selections and is separate from real-money accounting.

This is a local manual workspace with on-demand public ESPN reference fetching, not a live odds feed, OCR system, sportsbook account, certified placement gate, or public deployment. ESPN's public response shape and availability can change; a failed fetch or incomplete odds leaves stored snapshots unchanged. NFL operator annotations and unsaved form edits are session-only; download JSON to retain input. CFB prices and final scores are manually reported, so paper performance is only as accurate as that transcription. No price means no EV or hypothetical selection for that ticket size. Secondary research is never promoted into the card. NFL recheck does not validate kickoff, current market freshness, or cumulative actual placements. Independently verify prices and conditions before making any outside decision.
