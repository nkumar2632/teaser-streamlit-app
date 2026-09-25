# Pre-live hardening audit

Audit branch: `codex/nfl-checkpoint-b`  
Starting commit: `664d4b28194dc34a7218a5ce70e4ccbeba6fdb04`  
Frozen model: `94e412ed5bedc03a60b0b86f1146ff80c9f9e195`

This audit covers the app integration and persistence boundary. It does not change or reimplement model calculations. The Claude 6/6 golden pack, real ESPN browser flow, Apple Vision screenshot flow, and real sportsbook placement rehearsal remain local operator QA.

## R-01 through R-15

| ID | Result | Evidence |
| --- | --- | --- |
| R-01 | **ALREADY HANDLED** | `app.py` routes CFB to `render_cfb_page()` and stops before the NFL workspace. Public CFB calls `TeaserModelAdapter.grade_cfb_paper()`; `grade()` rejects non-NFL input. Covered by `test_public_cfb_snapshot_builds_paper_card_without_manual_sides` and the complete suite. |
| R-02 | **ALREADY HANDLED** | CFB uses the `CFB_TEASER` PAPER strategy, its page has no placement action, and `LocalHistory.save_operator_placement()` accepts only the NFL LIVE strategy. `test_cfb_public_streamlit_rerun_keeps_saved_board_and_no_placement` and `test_cfb_paper_run_never_consumes_nfl_exposure` cover both UI and accounting. |
| R-03 | **FIXED** | Saved NFL proposals now ignore a repeated Build for the same snapshot and fingerprint. Public CFB builds reuse a semantically identical persisted run even after restart. Covered by `test_streamlit_saved_nfl_reference_and_execution_builds_require_no_manual_sides` and `test_repeated_cfb_public_build_reuses_persisted_run_after_restart`. |
| R-04 | **ALREADY HANDLED** | `prepare_nfl_snapshot()` and `prepare_cfb_reference()` derive source-independent identities from normalized teams and kickoff, sort before engine input, and discard provider IDs. Covered by source-ID, source-order, and provenance-invariance tests in `test_nfl_market_proposal.py` and `test_cfb_public_proposal.py`. |
| R-05 | **ALREADY HANDLED** | NFL input passes through the pinned model's `normalize_team()` before model-facing IDs are constructed. Existing alias tests cover LA/LAR, Washington, JAX/JAC, SF/SFO, and TB/TAM forms. |
| R-06 | **ALREADY HANDLED** | `prepare_nfl_snapshot()` merges identical games, excludes conflicting duplicate games, and excludes any remaining same-team conflicts before invoking the adapter. Covered by `test_duplicate_invalid_started_and_conflicting_games_are_isolated`. |
| R-07 | **ALREADY HANDLED** | Both NFL and CFB preparation catch per-event validation failures and retain valid events. Non-object CFB events are now isolated as `INVALID_VALUE`. Covered by the invalid-extra and bad-event tests. |
| R-08 | **FIXED** | CFB validates a half-point spread/total grid, positive totals, opposing spreads, duplicate/conflicting teams, pregame state, timezone-aware kickoff, and future capture without applying NFL spread/total bounds. Covered by the CFB validation and isolation tests. |
| R-09 | **FIXED** | One strict app-boundary American-odds parser now requires an integer string, 3–5 digits plus optional sign, and absolute value at least 100. NFL and CFB both use it. Parameterized tests reject 0, ±50, ±99, decimal-style values, booleans, floats, and malformed strings while accepting valid American odds. |
| R-10 | **ALREADY HANDLED** | A stored menu keeps its source capture time. `LocalHistory.save_menu_verification()` creates a separate append-only record only after explicit operator reconfirmation, and the snapshot is unchanged. Covered by `test_explicit_menu_verification_is_append_only_and_rejects_bad_american_odds`. |
| R-11 | **FIXED** | The UI and write handler require confirmed same-book EXECUTION source and a distinct newer confirmed EXECUTION recheck, post-grading capture, current same-book menu observations, a VALIDATED model recheck, pre-kickoff rows, explicit operator confirmation, and a market/recheck age within 30 minutes. Covered by the execution state matrix and direct-handler tests. |
| R-12 | **ALREADY HANDLED** | Snapshot, kickoff, menu, recheck, placement, and result paths reject naive timestamps. The new future-capture checks also use aware UTC comparisons. Existing input, proposal, menu, placement, and result tests cover these boundaries. |
| R-13 | **ALREADY HANDLED** | `LocalHistory` writes only under the app data root. `.model_reference/` is ignored, verified against the pinned SHA at adapter startup, and has push disabled. No app runtime writes target the model checkout. |
| R-14 | **FIXED** | `execution_status()` remains the UI gate and the durable write handler now independently rejects missing/unconfirmed source snapshots. Model selection, EV, PROPOSED, and model `placement_eligible` cannot create a placement. Covered by the invalid-state matrix and `test_manual_or_reference_handler_calls_cannot_create_placements`. |
| R-15 | **ALREADY HANDLED** | Missing or partial menus still produce graded legs and constructed tickets. Price-dependent fields remain unavailable only for the missing size. Covered for NFL and CFB by their missing-price and partial-menu tests plus the price-independence metamorphic tests. |

No R-item remains open in the remotely testable app boundary.

## Weekly exposure audit

The app passes persisted operator-reported NFL placements plus the proposed placement records to the pinned model's `exposure_after()` and `exposure_violations()` helpers through `TeaserModelAdapter.validate_live_exposure()`. It does not copy the cap or calculate exposure independently.

Regression tests cover first and second use, the first prohibited use, two tickets in one action, separate actions, restart and durable JSON reload, duplicate submission, rejected attempts, all WIN/LOSS/PUSH/VOID settlement states, another week, another season, a different weekly leg identity, and CFB PAPER isolation. Settlement does not erase exposure because the frozen helper's input remains the durable placement ledger; the app does not infer a release policy.

## Placement state machine

Only this sequence can reach `LocalHistory.save_operator_placement()`:

1. Build from a confirmed NFL sportsbook EXECUTION snapshot with a known sportsbook and same-book teaser menu.
2. Select a distinct newer same-book confirmed EXECUTION snapshot.
3. Explicitly verify any menu values that are not fresh observations on that snapshot.
4. Receive a VALIDATED pinned-model recheck for the original card before kickoff.
5. Report tickets that were actually placed outside the app.

REFERENCE, manual, legacy/UNKNOWN-book, CFB, mixed-book, stale, out-of-order, same-snapshot, post-kickoff, duplicate, and over-exposure variants fail closed in the UI gate or durable write boundary. The app submits no wager.

## Persistence and invariance

Temporary-directory tests reconstruct fresh `LocalHistory` objects and verify immutable snapshots, append-only menu verification, restart-safe placements, exposure after restart, duplicate protection, settlement without a second placement, stable saved CFB run identity, and no write on ordinary Streamlit reruns.

Metamorphic tests compare adapter outputs rather than duplicating model mathematics. Equivalent REFERENCE and EXECUTION data, accepted NFL aliases, reversed source order, added invalid games, absent teaser prices, and changed provenance preserve canonical game/leg model fields and ticket composition where the frozen model says they should. Executability and price-dependent economics remain allowed to differ.

## Public repository and security review

- `.gitignore` excludes model checkouts, virtual environments, caches, runtime market/results/placement data, menu-verification records, screenshots, and raw imports.
- Reachable Git object names and revision contents were checked for common secret/key signatures and sensitive artifact types. No credential, private key, screenshot, database, or runtime market JSON was found in reachable history.
- README instructions are repository-relative and document Python 3.12, dependency installation, pinned-model setup, tests, loopback-only launch, CFB PAPER isolation, and the absence of sportsbook-account integration or automated betting.
- `.github/workflows/tests.yml` installs the pinned dependencies, obtains the exact read-only model pin with `scripts/setup_model.py`, and runs the Linux-compatible app suite without secrets or write permissions.
- The workflow deliberately does not claim Apple Vision, real ESPN/browser, golden-pack, or actual sportsbook QA.

## Verification record

- App suite during development: **120 passed**.
- Frozen-model checkout: exact pinned SHA, clean tracked and untracked state, push disabled.
- Final clean-process suite repetitions, selected read-only source tests, and Streamlit health check are recorded in the completion report for this audit commit.

## Required local gates

Follow [LOCAL_QA.md](LOCAL_QA.md) before relying on a real weekend workflow. The remote audit does not replace human review of current markets, extracted sportsbook screenshots, prices, game times, or an outside wager.
