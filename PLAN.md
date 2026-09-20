# Implementation plan

## Scope and architecture

Local NFL app, iPhone portrait first. Streamlit calls presentation components and app-owned frozen view records; only `teaser_app/adapter.py` imports source model APIs. The adapter invokes `live.card.grade_week`, `live.research.teased_board_rows`, and `live.recheck.recheck_card`, mapping their outputs without model calculations. Input conversion invokes source schema validation. Stake comes from the source's `UNITS_PER_TICKET`. No database, APIs, account connections, placement transactions, or public deployment.

## Layout and dependency loading

- `app.py`: page composition and session workflow.
- `teaser_app/adapter.py`: single model service; validates inputs, builds source snapshots, grades, rechecks, maps outputs.
- `teaser_app/views.py`: small immutable app data records independent of source classes.
- `teaser_app/inputs.py`: app input records, strict JSON import/export and structural validation; no model logic.
- `teaser_app/presentation.py`, `styles.css`: signed display formatting and reusable compact vertical cards.
- `teaser_app/integrity.py`: verify exact Git SHA, clean tracked files, disabled push, and source import location before loading; refuse failures.
- `scripts/setup_model.py`: repeatable clone/fetch/pin setup inside this workspace only; never silently delete a dirty reference.
- `tests/`: validation, source contracts, Week 2 regression, architecture guards, operational state, Streamlit smoke.

Load the actual pinned package from `.model_reference/src`, after integrity verification, without editable installation or modifying source files. Reject untracked executable files in model `src`, foreign preloaded model modules, and tracked changes; disable bytecode writes. Isolated `.venv`; Streamlit, SciPy, and the source's other runtime dependencies plus pytest. Pin resolved dependencies for reproducibility. Do not persist uploaded files or operational events automatically.

## Data flow and entry

Typed slate includes season, week, sportsbook, timezone-aware capture time, actual 2/3-team American prices (optional, no default live prices), and quotes with away/home teams, quoted side, spread, total, and timezone-aware kickoff. Store exact numeric strings/Decimal, never round or average. Canonical team choices come from source schema through adapter. Reject nonfinite/off-grid values, invalid metadata, duplicate sides, contradictory quotes for the same game, and team/game inconsistencies. No arbitrary file paths or executable deserialization.

One-column manual entry: compact slate settings expander and one side at a time form; list/edit/remove quoted sides; explicit Build proposal action. JSON import/export uses an explicit schema version, bounded size/rows/fields, duplicate-key rejection, and strict parsing. Read original Week 2 source CSVs through the adapter for an explicitly labeled historical example, separate from manual live input. Escape input-derived text in HTML.

## Mobile hierarchy

Header with season/week/book, provenance/freshness, placement and recheck shown independently. An above-the-fold card/exposure summary comes first; then all qualifying PRIMARY/LIVE legs (rank, signed spreads, total, P_est), selected tickets (signed price, P_ticket, break-even, signed EV, source stake and status), aggregate exposure. Full board, operations, research, and audit use expandable details. No normal-use wide tables. Tap controls target at least 44px; responsive text wraps and no horizontal overflow.

Research maps model-supplied primary/research labels, push flags and reasons. Separate cards/value labels; never feed research into grading or recheck.

## Operational workflow

New proposals are SHADOW — NOT PLACED and PENDING RECHECK. Manual input changes invalidate the current displayed proposal until rebuilt/rechecked. Recheck requires explicitly captured new data and calls the source recheck API against the original selected card. Display model verdict and updated ticket values separately from grading-time values. Discards show reasons and allow adopting the source rebuilt card as a new pending proposal. Adapter keeps opaque source card/market/price objects per Streamlit session; UI never reconstructs from formatted values. A manual session-only PLACED annotation means an operator reports an already completed action; it is explicitly unverified and is never a source model placement designation. Proposed exposure is labeled as proposal exposure, not actual placed exposure. Never promote an empty source recheck result to VALIDATED. Historical examples remain visibly historical and cannot be recorded as placed.

## Verification gates

1. Safety preflight and clean pin complete before implementation.
2. Four independent read-only plan reviews (architecture, security, iPhone UX, tests/model isolation); schedule in two batches because only three reviewer slots are available. Wait for all before build; record consequential changes in DECISIONS.md.
3. Small vertical slice first; execute original Week 2 inputs through adapter and source independently. Stop on qualifier/card/exposure identity disagreement. Capture exact source numerical values in regression fixture, not hand-derived formulas.
4. Source-vs-adapter tests compare every leg/ticket, order, classification, precision, selection and exposure; additional cases: fewer legs, >4 qualifiers, missing prices, negative EV, guardrail/geometry boundaries, research isolation, invalid data.
5. Architecture AST/import guards plus delegation sentinel tests catch forbidden model imports and accidental duplicate arithmetic. No tests reproduce model equations.
6. Independent slice reviews, then full board/status/research/audit additions with test and pin checks after each meaningful step.
7. Run source tests unchanged, app tests, Streamlit AppTest, localhost startup and actual browser QA at iPhone portrait viewport if available. Confirm no horizontal overflow, signed values, touch targets, readable cards, import/manual workflow, expansion and errors.
8. Final security/model-isolation review; document actual QA and limitations in README/SECURITY. Verify pin, tracked diff, disabled push and ignored secrets again.

## Future changes

Model upgrade is an explicit new pin and adapter contract review, primarily changing the adapter. UI depends on app view records, not source implementation. Deployment is deferred; later hosting needs explicit approval, authentication/privacy decisions, immutable dependency distribution and persistent operational design. Do not build those now.

## Progress

- Phase 0: complete; full pin and initial repository state recorded in MODEL_PIN.md.
- Phase 1: public live APIs, schemas, research, recheck, original inputs and tests inspected read-only.
- Phase 2: initial plan ready for independent critiques.
- Phase 3: architecture, security, mobile, and testing reviewers delivered findings. The testing reviewer message arrived before its turn hit a usage limit; the findings are recorded above. The primary agent owns follow-through.
- Phase 4: revised plan incorporates the consequential findings. Build may proceed.
- Phase 5: smallest vertical slice is implemented and the original Week 2 source fixture reproduces exact legs, ticket metrics, selected-card identities, and exposure. Nine focused app/contract tests pass.
- Phase 6–7: compact selected card, full ticket board, status/recheck display, research view, and audit/provenance are implemented. Independent post-build review and final documentation remain to do.
- Phase 8: local Streamlit startup and a 390×844 browser inspection passed; no horizontal overflow in normal or expanded research views. A final mobile pass after the latest tap-target CSS change remains.
- Integrity incident: the full source suite passed but one source test rewrote a tracked rehearsal report timestamp. The disposable checkout was deleted and cloned afresh at the identical pin, with pushing disabled and a clean tracked diff. Do not rerun that write-producing source test in the pinned checkout.
- Safe pause checkpoint: current app tests and Streamlit AppTest passed; localhost server stopped. Next task: finish security/input review, add README.md and SECURITY.md, then run final focused regression and mobile QA while preserving the clean pin.
