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
