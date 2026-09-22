# Normalized market snapshot v1

`teaser_app.market_data.normalize_market` emits one snapshot with `schema_version`,
`snapshot_id`, league, season, week, capture time, source, sportsbook, and `events`.
Each event has every field below; unavailable markets are `null`, never guessed:

`snapshot_id`, `event_id`, `league`, `home_team`, `away_team`, `kickoff`,
`captured_at`, `source`, `sportsbook`, `spread_home`, `spread_home_price`,
`spread_away`, `spread_away_price`, `moneyline_home`, `moneyline_away`,
`total`, `over_price`, `under_price`, `teaser_2team_6pt_price`,
`teaser_3team_6pt_price`.

Spreads and prices are exact input strings. `source` identifies manual sportsbook
entry, user screenshot, reference source, or a future odds API. Older v1
slates without a source field retain `unspecified` rather than acquiring
a guessed provenance. `sportsbook`
identifies the quoted book, if known. Source and book are never inferred from
each other. An event ID identifies the league/matchup/kickoff; a snapshot ID
identifies the full observed state and capture time. Files are created once in
`market_data/normalized/`, never updated. Later lines receive new snapshot IDs.

`LocalHistory` exposes `ingest_snapshot`, `get_snapshot`, `latest_market`, and
`market_history`. Future providers should normalize into this record and use the
same append-only store. The app does not yet ingest ATS, moneyline, or total
prices from manual teaser slates, though the record reserves those fields.

Confirmed public URL imports use snapshot `schema_version: 2` with the same event
market columns plus `market_role: REFERENCE`, `source_type: url`, `source_provider`,
`source_url`, `fetched_url`, `reference_source`, `captured_at`, and per-event
`source_event_id`, displayed `sportsbook`, and `review_changes` (fetched and
confirmed values). Missing odds and teaser payouts are `null`. An ESPN URL
snapshot is never an execution-market slate. The v1 format and IDs remain
readable unchanged; multiple v2 snapshots can reference the same event.

Newly saved version-2 manual snapshots retain the same market columns and add
`market_role`, `source_type`, `source_provider`, and `source_url` (the last two
remain `null` for manual entry). An identified `manual_sportsbook` source with
book is `EXECUTION`; explicit reference-source records are `REFERENCE`;
uncertain screenshot transcription is `UNCLASSIFIED`. Existing v1 files are
never rewritten: old source-less records remain unclassified, while an old
record with explicit manual-sportsbook provenance and book can be read as
execution. Role-filtered history and comparison are read-only. Matching uses
league, exact or explicitly registered team aliases, home/away orientation,
and kickoff within 15 minutes; conflicts are flagged, not paired. Snapshot
selection preserves all older records for later line-history work.

Confirmed sportsbook screenshot imports use `schema_version: 3`,
`market_role: EXECUTION`, `source_type: screenshot`, and
`source: user_screenshot`. They retain the operator-supplied sportsbook,
league and capture time, the local/manual extractor name, extraction warnings,
field confidence states, conflict resolutions and review edits. Top-level
`screenshot_provenance` contains each safe filename, MIME type, byte size,
dimensions, upload timestamp and SHA-256 hash. Raw image bytes are not stored.
The optional `teaser_prices.6_point` object preserves separately reviewed
2-team and 3-team prices and mirrors them into the existing event fields;
unrelated teaser products remain absent. Version-3 records use the same event
IDs, role-filtered history and comparison path as earlier formats. Saving them
does not create a model run or accounting record, and no older snapshot is
rewritten.
