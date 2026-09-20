# NFL Teaser v1.0 — local Streamlit app

A mobile-first manual interface for the immutable [Teaser Model v1.0](https://github.com/nkumar2632/Football-Teasers-v1.0). Enter one NFL sportsbook slate and its **actual observed** 2-team/3-team 6-point teaser prices. The pinned model grades qualifying PRIMARY/LIVE legs, builds the complete ticket board, chooses the proposed card, and supplies probability, EV and exposure. Nothing here places a wager.

## Run locally

From `/Users/Nitin/Developer/teaser-streamlit-app`, the existing local environment can run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/streamlit run app.py --server.address 127.0.0.1
```

Open `http://127.0.0.1:8501` on this computer. The server is bound to loopback, not the LAN. To recreate the environment later, use Python 3.12: `python3.12 -m venv .venv`, `.venv/bin/python -m pip install -r requirements-dev.txt`, then `python3.12 scripts/setup_model.py` to clone or verify the disposable pinned source. The setup script refuses to overwrite a dirty or mismatched existing checkout. Network access is needed only for the specified GitHub clone and normal dependency installation.

## Enter a slate

1. Open **Slate input**. Set season, week, sportsbook, and capture time as ISO 8601 with an explicit UTC offset. Enter only observed American teaser prices (`-110`, `+170`); leave unobserved sizes blank.
2. Under **Add a quoted side**, select away/home teams and the quoted side. Enter that side's signed spread, game total and timezone-aware kickoff. Add a second row if you also want the opposing side evaluated. Lines and totals must be exact half-point grid values. Review/edit entered sides in the collapsed list.
3. Tap **Build proposal from entered slate**, then **↑ View proposed card**. The status, selected tickets, offered price, P_ticket, break-even, EV, stake, proposed exposure and all qualifying legs appear first. The complete ticket board, research-only legs and precision/provenance are below in expanders.

You can import/export a bounded version-1 JSON slate. **Load verified Week 2 historical example** runs the original sportsbook snapshot from the pinned model; it remains visibly historical. To recheck a live proposal, capture a genuinely later slate time, update current lines/prices, and use **Recheck selected card**. A discard can be adopted as a new proposal pending another recheck. A **PLACED** annotation is an operator's session-only report of an action completed elsewhere; it is not a model placement designation or proof of a transaction.

## Boundary and limitations

`app.py` → display records/formatters in `teaser_app/` → `TeaserModelAdapter` → the actual source package in ignored `.model_reference/src`. Only the adapter imports the model. It checks the full commit `94e412ed5bedc03a60b0b86f1146ff80c9f9e195`, a clean checkout, no ignored executable additions, and disabled pushing before use. See [MODEL_PIN.md](MODEL_PIN.md) and [SECURITY.md](SECURITY.md). App tests compare adapter outputs with source APIs and the original Week 2 snapshot.

This is a local, manual, in-memory workspace: no live odds feed, OCR, durable event ledger, sportsbook account, certified placement gate, or public deployment. Closing the session loses unsaved edits and operator annotations; download JSON to retain input. Research scores for secondary/paper geometry are not comparable with live P_est; push settlement is not modeled for push-capable research legs. Recheck is limited to the source's card recheck and does not validate kickoff, current market freshness, or cumulative actual placements. Independently verify prices and conditions before making any outside decision.
