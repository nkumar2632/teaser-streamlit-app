"""Reviewed ESPN scores joined to frozen app runs, with append-only confirmation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from teaser_app.market_compare import KICKOFF_TOLERANCE, _time, team_key
from teaser_app.providers.espn_results import ESPNResultError, parse_results
from teaser_app.url_import import espn_dated_url
from teaser_app.url_ingest import URLIngestError, fetch_url, identify_provider

STATUSES = frozenset({"SCHEDULED", "IN_PROGRESS", "FINAL", "POSTPONED", "CANCELLED", "UNKNOWN"})
BOOK_RESULTS = frozenset({"WIN", "LOSS", "PUSH", "VOID", "CANCELLED"})


class ResultReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ResultPreview:
    league: str
    source_url: str | None
    fetched_url: str | None
    provider: str
    fetched_at: str
    rows: tuple[dict, ...]
    warnings: tuple[str, ...]


def espn_week_url(league: str, season: int, week: int) -> str:
    if league not in {"NFL", "CFB"} or type(season) is not int or not 2000 <= season <= 2100:
        raise ResultReviewError("Choose NFL or CFB and a season from 2000 through 2100")
    if type(week) is not int or not 1 <= week <= (18 if league == "NFL" else 20):
        raise ResultReviewError("Choose a supported football week")
    path = "nfl" if league == "NFL" else "college-football"
    extra = "&groups=80" if league == "CFB" else ""
    return f"https://www.espn.com/{path}/scoreboard?dates={season}&week={week}&seasontype=2{extra}"


def fetch_espn_results(league: str, *, game_date: date | None = None,
                       season: int | None = None, week: int | None = None,
                       client=None) -> ResultPreview:
    if (game_date is None) == (week is None):
        raise ResultReviewError("Choose either a date or a season/week")
    try:
        url = espn_dated_url(league, game_date) if game_date is not None else espn_week_url(league, season, week)
        spec = identify_provider(url)
        raw = fetch_url(spec, client=client)
        rows, warnings = parse_results(raw, league)
    except (URLIngestError, ESPNResultError) as exc:
        raise ResultReviewError(str(exc)) from exc
    if not rows:
        raise ResultReviewError("No games found for the selected ESPN date or week")
    return ResultPreview(league, spec.source_url, spec.fetch_url, "ESPN",
                         datetime.now(timezone.utc).isoformat(), tuple(rows), tuple(warnings))


def manual_preview(league: str) -> ResultPreview:
    if league not in {"NFL", "CFB"}:
        raise ResultReviewError("Choose NFL or CFB")
    return ResultPreview(league, None, None, "MANUAL", datetime.now(timezone.utc).isoformat(),
                         (), ("Manual result entry: add games and verify final scores before confirming.",))


def _checked_score(value, name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool) or not str(value).strip().isdigit():
        raise ResultReviewError(f"{name} must be a whole-number score")
    number = int(str(value).strip())
    if number > 200:
        raise ResultReviewError(f"{name} is outside the supported score range")
    return number


def _reviewed_rows(preview: ResultPreview, edited_rows: list[dict]) -> list[dict]:
    if not edited_rows or len(edited_rows) > 200:
        raise ResultReviewError("Review 1 to 200 football games")
    original_by_id = {row["source_event_id"]: row for row in preview.rows if row.get("source_event_id")}
    rows = []
    for index, edited in enumerate(edited_rows, 1):
        away, home = (str(edited.get(field) or "").strip() for field in ("away_team", "home_team"))
        if not away or not home or away.casefold() == home.casefold() or len(away) > 80 or len(home) > 80:
            raise ResultReviewError(f"Row {index} needs distinct away and home teams")
        instant = _time(edited.get("kickoff"))
        if instant is None:
            raise ResultReviewError(f"Row {index} needs a kickoff with UTC offset")
        status = str(edited.get("status") or "").strip().upper()
        if status not in STATUSES:
            raise ResultReviewError(f"Row {index} has an unsupported game status")
        away_score = _checked_score(edited.get("away_score"), f"Row {index} away score")
        home_score = _checked_score(edited.get("home_score"), f"Row {index} home score")
        if status == "FINAL" and (away_score is None or home_score is None):
            raise ResultReviewError(f"Row {index} needs both final scores")
        if status != "FINAL":
            away_score = home_score = None
        source_id = str(edited.get("source_event_id") or "").strip() or None
        original = original_by_id.get(source_id)
        compared = {"away_team": away, "home_team": home, "kickoff": instant.isoformat(),
                    "status": status, "away_score": away_score, "home_score": home_score}
        changes = {field: {"fetched": original.get(field), "confirmed": compared[field]}
                   for field in compared if original is not None and original.get(field) != compared[field]}
        rows.append({"source_event_id": source_id, **compared,
                     "review_changes": changes, "entry_type": "edited" if changes else preview.provider.lower()})
    return rows


def _run_games(run: dict) -> list[dict]:
    games = {}
    for leg in run["legs"]:
        row = next((item for item in run["slate"]["rows"]
                    if item["team"] == leg["team"] and item["kickoff"] == leg["kickoff"]), None)
        if row is None:
            continue
        games[leg["game_id"]] = {"run_id": run["run_id"], "game_id": leg["game_id"],
                                  "league": run["strategy"]["league"],
                                  "away_team": row["away_team"], "home_team": row["home_team"],
                                  "kickoff": row["kickoff"]}
    return list(games.values())


def _same_teams(row: dict, game: dict, league: str) -> bool:
    left = (team_key(league, row["away_team"]), team_key(league, row["home_team"]))
    right = (team_key(league, game["away_team"]), team_key(league, game["home_team"]))
    return None not in left and left[0] != left[1] and left == right


def _prior_id_links(history, league: str) -> dict[tuple[str, str], str]:
    links = {}
    for snapshot in history.result_snapshots(league=league):
        for event in snapshot["events"]:
            if event.get("source_event_id"):
                for match in event.get("matches", []):
                    links[(event["source_event_id"], match["run_id"])] = match["game_id"]
    for reference in history.market_history(league=league, role="REFERENCE"):
        for event in reference["events"]:
            source_id = event.get("source_event_id")
            if not source_id:
                continue
            for run in history.runs(league=league, bet_type="TEASER", model_version="teaser_v1.0"):
                candidates = [game for game in _run_games(run) if _same_teams(event, game, league)
                              and _time(event.get("kickoff")) is not None
                              and abs(_time(event["kickoff"]) - _time(game["kickoff"])) <= KICKOFF_TOLERANCE]
                if len(candidates) == 1:
                    links.setdefault((source_id, run["run_id"]), candidates[0]["game_id"])
    return links


def review_results(preview: ResultPreview, edited_rows: list[dict], history, adapter,
                   *, manual_links: dict[int, str] | None = None) -> dict:
    rows = _reviewed_rows(preview, edited_rows)
    runs = history.runs(league=preview.league, bet_type="TEASER", model_version="teaser_v1.0")
    runs = [run for run in runs if run["strategy"]["status"] in {"LIVE", "PAPER"}]
    games = [game for run in runs for game in _run_games(run)]
    links = _prior_id_links(history, preview.league)
    manual_links = manual_links or {}
    latest_ids, latest_matchups = {}, {}
    for snapshot in history.result_snapshots(league=preview.league):
        for event in snapshot["events"]:
            if event["status"] != "FINAL":
                continue
            if event.get("source_event_id"):
                latest_ids[event["source_event_id"]] = (snapshot["confirmed_at"], event)
            latest_matchups[(team_key(preview.league, event["away_team"]),
                             team_key(preview.league, event["home_team"]),
                             _time(event["kickoff"]))] = (snapshot["confirmed_at"], event)
    seen_ids, seen_games, conflicts, blockers = set(), set(), [], []
    for index, row in enumerate(rows):
        identity = (team_key(preview.league, row["away_team"]),
                    team_key(preview.league, row["home_team"]), _time(row["kickoff"]))
        if (row["source_event_id"] and row["source_event_id"] in seen_ids) or identity in seen_games:
            row["match_status"] = "DUPLICATE"
            row["matches"] = []
            blockers.append(f"Row {index + 1}: duplicate result; remove or correct the row")
            continue
        if row["source_event_id"]:
            seen_ids.add(row["source_event_id"])
        seen_games.add(identity)
        prior_records = [record for record in (
            latest_ids.get(row["source_event_id"]) if row["source_event_id"] else None,
            latest_matchups.get(identity)) if record]
        prior = max(prior_records, key=lambda record: record[0])[1] if prior_records else None
        if row["status"] == "FINAL" and prior and (
                (prior["away_score"], prior["home_score"]) != (row["away_score"], row["home_score"])
                or not _same_teams(row, prior, preview.league)):
            row["result_conflict"] = True
            conflicts.append(f"{row['away_team']} at {row['home_team']}: prior confirmed final differs")
        else:
            row["result_conflict"] = False
        if row["status"] != "FINAL":
            row["match_status"] = row["status"]
            row["matches"] = []
            continue
        matches, issues = [], []
        for run in runs:
            candidates = [game for game in games if game["run_id"] == run["run_id"]]
            manual = manual_links.get(index)
            if manual:
                linked = [game for game in candidates if f"{game['run_id']}|{game['game_id']}" == manual]
                if linked:
                    matches.extend({"run_id": game["run_id"], "game_id": game["game_id"],
                                    "method": "manual_review"} for game in linked)
                    continue
            durable = links.get((row["source_event_id"], run["run_id"])) if row["source_event_id"] else None
            if durable:
                candidates = [game for game in candidates if game["game_id"] == durable]
            else:
                candidates = [game for game in candidates if _same_teams(row, game, preview.league)]
            close = [game for game in candidates if _time(game["kickoff"]) is not None
                     and abs(_time(game["kickoff"]) - _time(row["kickoff"])) <= KICKOFF_TOLERANCE
                     and _same_teams(row, game, preview.league)]
            if len(close) == 1:
                matches.append({"run_id": close[0]["run_id"], "game_id": close[0]["game_id"],
                                "method": "provider_id" if durable else "team_kickoff"})
            elif len(close) > 1:
                issues.append("AMBIGUOUS")
            elif candidates:
                issues.append("KICKOFF_CONFLICT")
        row["matches"] = matches
        row["match_status"] = ("AMBIGUOUS" if "AMBIGUOUS" in issues else
                               "KICKOFF_CONFLICT" if "KICKOFF_CONFLICT" in issues else
                               "MATCHED" if matches else "UNMATCHED")
        for match in matches:
            run = next(item for item in runs if item["run_id"] == match["run_id"])
            if run["strategy"]["status"] != "PAPER":
                continue
            prior_paper = history.latest_paper_results(run["run_id"])
            if (prior_paper and match["game_id"] in prior_paper["scores"]
                    and prior_paper["scores"][match["game_id"]] !=
                    {"away": row["away_score"], "home": row["home_score"]}):
                row["result_conflict"] = True
                message = f"{row['away_team']} at {row['home_team']}: prior PAPER final differs"
                if message not in conflicts:
                    conflicts.append(message)
        if row["result_conflict"]:
            row["match_status"] = "CONFLICT"
        if row["match_status"] in {"AMBIGUOUS", "KICKOFF_CONFLICT"}:
            blockers.append(f"{row['away_team']} at {row['home_team']}: {row['match_status'].lower().replace('_', ' ')} needs manual event review")
    scores_by_run = {run["run_id"]: {} for run in runs}
    for snapshot in history.result_snapshots(league=preview.league):
        for event in snapshot["events"]:
            if event["status"] == "FINAL":
                for match in event.get("matches", []):
                    scores_by_run.setdefault(match["run_id"], {})[match["game_id"]] = {
                        "away": event["away_score"], "home": event["home_score"]}
    for run in runs:
        if run["strategy"]["status"] == "PAPER":
            prior = history.latest_paper_results(run["run_id"])
            if prior:
                scores_by_run[run["run_id"]].update(prior["scores"])
    for row in rows:
        if row["status"] == "FINAL" and row["match_status"] not in {"DUPLICATE", "AMBIGUOUS", "KICKOFF_CONFLICT"}:
            for match in row["matches"]:
                scores_by_run[match["run_id"]][match["game_id"]] = {
                    "away": row["away_score"], "home": row["home_score"]}
    tickets = []
    legs = [leg for run in runs for leg in adapter.grade_stored_legs(
        run, scores_by_run[run["run_id"]])]
    for run in runs:
        scores = scores_by_run[run["run_id"]]
        if run["strategy"]["status"] == "PAPER":
            graded = adapter.grade_paper_results(run, scores)
            outcomes = graded["legs"]
            by_key = {row["ticket_key"]: row for row in graded["tickets"]}
            for ticket in run["tickets"]:
                if not ticket["selected"]:
                    continue
                result = by_key[ticket["ticket_key"]]["result"]
                if any(outcomes.get(leg_id) == "PUSH" for leg_id in ticket["leg_ids"]):
                    result = "REQUIRES_REVIEW"
                tickets.append({"run_id": run["run_id"], "ticket_key": ticket["ticket_key"],
                                "track": "PAPER", "result": result, "model_result": result,
                                "stake_units": ticket["stake_units"], "profit": ticket["profit"],
                                "leg_ids": ticket["leg_ids"], "placement_id": None})
        else:
            prior_live = {row["placement_id"]: row for row in history.live_settlements()}
            for placement in history.live_placements():
                if placement["run_id"] != run["run_id"]:
                    continue
                outcome = adapter.grade_live_placement(run, placement, scores)
                prior = prior_live.get(placement["placement_id"])
                tickets.append({"run_id": run["run_id"], "ticket_key": placement["ticket_key"],
                                "track": "LIVE", "result": prior["book_settlement"] if prior else outcome["status"],
                                "model_result": outcome["model_ticket_result"],
                                "reason": outcome["reason"], "stake_units": placement["stake_units"],
                                "offered_american": placement["offered_american"],
                                "leg_ids": placement["leg_ids"],
                                "leg_terms": placement.get("leg_terms", []),
                                "placement_id": placement["placement_id"],
                                "already_settled": prior is not None})
    return {"rows": rows, "tickets": tickets, "legs": legs, "scores_by_run": scores_by_run,
            "conflicts": conflicts, "blockers": blockers, "runs": runs,
            "available_games": games}


def confirm_results(preview: ResultPreview, review: dict, history, adapter,
                    *, allow_conflicts: bool = False,
                    book_results: dict[str, tuple[str, str | None]] | None = None) -> dict:
    if review["blockers"]:
        raise ResultReviewError("Resolve duplicate or ambiguous results before confirmation")
    if review["conflicts"] and not allow_conflicts:
        raise ResultReviewError("Conflicting confirmed final scores require explicit correction review")
    book_results = book_results or {}
    prior_settlements = {row["placement_id"]: row for row in history.live_settlements()}
    for row in review["rows"]:
        if not row["result_conflict"]:
            continue
        affected = {match["run_id"] for match in row["matches"]}
        for placement in history.live_placements():
            if placement["run_id"] in affected and placement["placement_id"] in prior_settlements and any(
                    leg["game_id"] == match["game_id"] for match in row["matches"]
                    for leg in history.get_run(placement["run_id"])["legs"]
                    if leg["leg_id"] in placement["leg_ids"]):
                if placement["placement_id"] not in book_results:
                    raise ResultReviewError("A settled LIVE ticket has changed scores; review its sportsbook outcome")
    for run in review["runs"]:
        if run["strategy"]["status"] != "PAPER":
            continue
        prior = history.latest_paper_results(run["run_id"])
        if prior and any(game_id in prior["scores"] and prior["scores"][game_id] != score
                         for game_id, score in review["scores_by_run"][run["run_id"]].items()):
            if not allow_conflicts:
                raise ResultReviewError("A prior PAPER final differs; review the correction explicitly")
    for placement_id, (book_status, raw_profit) in book_results.items():
        if book_status not in BOOK_RESULTS:
            raise ResultReviewError("Choose a supported sportsbook settlement")
        if raw_profit is not None and str(raw_profit).strip():
            try:
                number = Decimal(str(raw_profit).strip())
                if not number.is_finite() or abs(number) > 100000:
                    raise ValueError
            except (InvalidOperation, ValueError) as exc:
                raise ResultReviewError("Book P/L must be a finite number of units") from exc
        if not any(ticket["placement_id"] == placement_id and ticket["model_result"] in {"WIN", "LOSS"}
                   for ticket in review["tickets"]):
            raise ResultReviewError("Cannot settle a LIVE ticket until all required games are final")
    planned_settlements = []
    for placement_id, (book_status, raw_profit) in book_results.items():
        placement = next(p for p in history.live_placements() if p["placement_id"] == placement_id)
        run = history.get_run(placement["run_id"])
        numeric_profit = float(Decimal(str(raw_profit))) if raw_profit is not None and str(raw_profit).strip() else None
        settlement_time = datetime.now(timezone.utc).isoformat()
        graded = adapter.grade_live_placement(run, placement, review["scores_by_run"][run["run_id"]],
                                              book_settlement=book_status,
                                              settled_at=settlement_time,
                                              book_profit_loss_units=numeric_profit)
        prior = prior_settlements.get(placement_id)
        if prior and not allow_conflicts and any(
                prior[key] != graded[key] for key in
                ("model_ticket_result", "book_settlement", "profit_loss_units", "legs")):
            raise ResultReviewError("A changed sportsbook settlement requires explicit correction review")
        planned_settlements.append((placement_id, graded, settlement_time))
    events = [{key: value for key, value in row.items() if key != "result_conflict"}
              for row in review["rows"]]
    payload = {"schema_version": 1, "kind": "confirmed_game_results",
               "league": preview.league, "provider": preview.provider,
               "source_url": preview.source_url, "fetched_url": preview.fetched_url,
               "fetched_at": preview.fetched_at,
               "confirmed_at": datetime.now(timezone.utc).isoformat(),
               "correction_reviewed": bool(review["conflicts"] and allow_conflicts),
               "events": events}
    def semantic(items):
        return [{**{key: value for key, value in event.items() if key != "match_status"},
                 "matches": [{key: value for key, value in match.items() if key != "method"}
                             for match in event.get("matches", [])]}
                for event in items]
    previous = history.result_snapshots(league=preview.league)
    latest = previous[-1] if previous else None
    existing = latest if (not review["conflicts"] and latest and
                          latest["provider"] == preview.provider and
                          latest["source_url"] == preview.source_url and
                          semantic(latest["events"]) == semantic(events)) else None
    saved = existing or history.save_result_snapshot(payload)
    for run in review["runs"]:
        if run["strategy"]["status"] != "PAPER":
            continue
        scores = review["scores_by_run"][run["run_id"]]
        if not scores:
            continue
        prior = history.latest_paper_results(run["run_id"])
        if prior and prior["scores"] == scores:
            continue
        history.save_paper_results(run["run_id"], scores,
                                   recorded_at=saved["confirmed_at"],
                                   result_snapshot_id=saved["result_snapshot_id"],
                                   source_provider=preview.provider)
    for placement_id, graded, settlement_time in planned_settlements:
        prior = prior_settlements.get(placement_id)
        history.save_live_settlement({"schema_version": 1, "kind": "operator_confirmed_settlement",
                                      "placement_id": placement_id,
                                      "result_snapshot_id": saved["result_snapshot_id"],
                                      "settled_at": settlement_time,
                                      "model_ticket_result": graded["model_ticket_result"],
                                      "book_settlement": graded["book_settlement"],
                                      "profit_loss_units": graded["profit_loss_units"],
                                      "legs": graded["legs"],
                                      "supersedes": prior["settlement_id"] if prior else None},
                                     allow_correction=bool(prior and allow_conflicts))
    return saved
