from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from teaser_app.adapter import TeaserModelAdapter
from teaser_app.live_history import live_performance, placement_record
from teaser_app.market_data import LocalHistory
from teaser_app.paper_history import paper_performance
from teaser_app.providers.espn_results import ESPNResultError, parse_results
from teaser_app.result_ingest import (
    ResultPreview, ResultReviewError, confirm_results, espn_week_url,
    fetch_espn_results, manual_preview, review_results,
)
from teaser_app.strategy import CFB_TEASER, NFL_TEASER

ROOT = Path(__file__).resolve().parents[1]
KICKOFF = "2026-09-20T13:00:00-04:00"


def fixture(name):
    return (ROOT / "tests" / "fixtures" / name).read_bytes()


class Response:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size):
        yield self.body


class Client:
    def __init__(self, body):
        self.body = body
        self.urls = []

    def get(self, url, **options):
        self.urls.append((url, options))
        return Response(self.body)


def nfl_preview(body=None):
    return fetch_espn_results("NFL", game_date=date(2026, 9, 20),
                              client=Client(body or fixture("espn_nfl_results.json")))


def cfb_preview(body=None):
    return fetch_espn_results("CFB", game_date=date(2026, 9, 26),
                              client=Client(body or fixture("espn_cfb_results.json")))


def edited(preview):
    return [{key: row.get(key) for key in
             ("source_event_id", "away_team", "home_team", "kickoff", "status",
              "away_score", "home_score")} for row in preview.rows]


def live_run(history, *, two_legs=True):
    legs = [{"leg_id": "TB-leg", "game_id": "tb-cle", "team": "TB",
             "teased_spread": "-2.5", "spread": "-8.5", "kickoff": KICKOFF}]
    rows = [{"away_team": "CLE", "home_team": "TB", "team": "TB",
             "spread": "-8.5", "total": "45.5", "kickoff": KICKOFF}]
    if two_legs:
        legs.append({"leg_id": "ATL-leg", "game_id": "car-atl", "team": "ATL",
                     "teased_spread": "+8.5", "spread": "+2.5", "kickoff": KICKOFF})
        rows.append({"away_team": "CAR", "home_team": "ATL", "team": "ATL",
                     "spread": "+2.5", "total": "44.5", "kickoff": KICKOFF})
    placement_legs = {leg["leg_id"]: {
        "leg_id": leg["leg_id"], "game_id": leg["game_id"], "team": leg["team"],
        "away_team": row["away_team"], "home_team": row["home_team"],
        "kickoff": leg["kickoff"], "spread": leg["spread"],
        "teased_spread": leg["teased_spread"]}
        for leg, row in zip(legs, rows)}
    ticket = {"ticket_key": "TB|ATL", "leg_ids": [leg["leg_id"] for leg in legs],
              "n_legs": 2, "selected": True, "stake_units": 1,
              "offered_american": "+170", "net_profit_per_unit": "1.7"}
    run = {"schema_version": 1, "kind": "model_run", "strategy": NFL_TEASER.to_dict(),
           "card_id": "card_test", "season": 2026, "week": 2,
           "captured_at": "2026-09-20T10:00:00-04:00", "sportsbook": "Test Book",
           "snapshot_id": "mkt_test", "source_market_snapshot_id": "src_test",
           "price_snapshot_id": "price_test", "model_sha": "pinned",
           "slate": {"rows": rows}, "legs": legs, "tickets": [ticket],
           "selected_ticket_keys": ["TB|ATL"],
           "recheck": {"verdict": "VALIDATED", "rechecked_at": "2026-09-20T10:55:00-04:00",
                       "captured_at": "2026-09-20T10:50:00-04:00", "market_snapshot_id": "later_market",
                       "price_snapshot_id": "later_price", "offered_prices": {"2": "+170", "3": ""},
                       "placement_legs": placement_legs}}
    return history.save_run(run)


def paper_run(history, *, push=False):
    kickoff = "2026-09-26T12:00:00-04:00"
    legs = [
        {"leg_id": "texas-leg", "game_id": "texas-tenn", "team": "Texas",
         "opponent": "Tennessee", "teased_spread": "+7" if push else "+7.5",
         "spread": "+1", "kickoff": kickoff},
        {"leg_id": "beta-leg", "game_id": "alpha-beta", "team": "Beta",
         "opponent": "Alpha State", "teased_spread": "-1.5",
         "spread": "-7.5", "kickoff": "2026-09-26T15:30:00-04:00"},
    ]
    rows = [
        {"away_team": "Texas", "home_team": "Tennessee", "team": "Texas",
         "spread": "+1", "total": "45.5", "kickoff": kickoff},
        {"away_team": "Alpha State", "home_team": "Beta", "team": "Beta",
         "spread": "-7.5", "total": "50.5", "kickoff": "2026-09-26T15:30:00-04:00"},
    ]
    return history.save_run({"schema_version": 1, "kind": "model_run",
                             "strategy": CFB_TEASER.to_dict(), "season": 2026, "week": 4,
                             "captured_at": "2026-09-25T10:00:00-04:00",
                             "sportsbook": "Paper Book", "snapshot_id": "mkt_paper",
                             "slate": {"rows": rows}, "legs": legs,
                             "tickets": [{"ticket_key": "texas|beta", "leg_ids": ["texas-leg", "beta-leg"],
                                          "selected": True, "stake_units": 1, "profit": "1.7"}],
                             "selected_ticket_keys": ["texas|beta"]})


def test_espn_nfl_cfb_scores_statuses_and_bounded_existing_fetch():
    nfl = parse_results(fixture("espn_nfl_results.json"), "NFL")[0]
    assert [row["status"] for row in nfl] == ["FINAL", "IN_PROGRESS", "SCHEDULED", "POSTPONED"]
    assert (nfl[0]["away_score"], nfl[0]["home_score"]) == (10, 24)
    assert all(row["away_score"] is None for row in nfl[1:])
    cfb = parse_results(fixture("espn_cfb_results.json"), "CFB")[0]
    assert [row["status"] for row in cfb] == ["FINAL", "CANCELLED"]
    client = Client(fixture("espn_cfb_results.json"))
    preview = fetch_espn_results("CFB", game_date=date(2026, 9, 26), client=client)
    assert preview.provider == "ESPN" and preview.source_url.endswith("dates=20260926&groups=80")
    assert client.urls[0][0].endswith("dates=20260926&groups=80")
    assert client.urls[0][1]["allow_redirects"] is False


def test_result_provider_rejects_bad_or_duplicate_and_never_guesses_final():
    raw = json.loads(fixture("espn_nfl_results.json"))
    raw["events"][0]["status"]["type"]["completed"] = False
    rows, _ = parse_results(json.dumps(raw).encode(), "NFL")
    assert rows[0]["status"] == "UNKNOWN" and rows[0]["home_score"] is None
    raw["events"][0]["status"]["type"]["completed"] = True
    raw["events"][0]["competitions"][0]["competitors"][0]["score"] = "bad"
    rows, warnings = parse_results(json.dumps(raw).encode(), "NFL")
    assert rows[0]["status"] == "UNKNOWN" and any("invalid scores" in value for value in warnings)
    raw["events"].append(deepcopy(raw["events"][1]))
    rows, _ = parse_results(json.dumps(raw).encode(), "NFL")
    assert "duplicate ESPN game" in rows[-1]["issues"]
    with pytest.raises(ESPNResultError, match="malformed"):
        parse_results(b"not json", "NFL")


def test_date_week_and_invalid_input_use_espn_allowlist():
    assert espn_week_url("NFL", 2026, 2).endswith("dates=2026&week=2&seasontype=2")
    assert espn_week_url("CFB", 2026, 4).endswith("dates=2026&week=4&seasontype=2&groups=80")
    for league, year, week in (("NBA", 2026, 2), ("NFL", 2026, 0), ("CFB", 2026, 21)):
        with pytest.raises(ResultReviewError):
            espn_week_url(league, year, week)
    with pytest.raises(ResultReviewError):
        fetch_espn_results("NFL", game_date=date(2026, 9, 20), week=2, season=2026)
    with pytest.raises(ResultReviewError, match="No games"):
        fetch_espn_results("NFL", game_date=date(2026, 9, 20), client=Client(b'{"events": []}'))


def test_provider_id_and_team_kickoff_match_fallback_and_unmatched(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = live_run(history)
    placement = history.save_operator_placement(placement_record(run, "TB|ATL",
                                                                placed_at="2026-09-20T11:00:00-04:00"))
    preview = nfl_preview()
    review = review_results(preview, edited(preview), history, adapter)
    assert review["rows"][0]["match_status"] == "MATCHED"
    assert review["rows"][0]["matches"][0]["method"] == "team_kickoff"
    assert review["rows"][1]["status"] == "IN_PROGRESS"
    assert next(leg for leg in review["legs"] if leg["team"] == "TB")["result"] == "WIN"
    assert next(leg for leg in review["legs"] if leg["team"] == "ATL")["result"] == "PENDING"
    assert review["tickets"][0]["result"] == "PENDING"
    assert history.live_settlements() == []
    first = confirm_results(preview, review, history, adapter)
    assert history.get_result_snapshot(first["result_snapshot_id"]) == first
    assert first["events"][0]["matches"][0]["game_id"] == "tb-cle"
    review_again = review_results(preview, edited(preview), history, adapter)
    assert review_again["rows"][0]["matches"][0]["method"] == "provider_id"
    assert confirm_results(preview, review_again, history, adapter)["result_snapshot_id"] == first["result_snapshot_id"]
    assert history.live_settlements() == [] and live_performance(history)["settled"] == 0
    assert placement["offered_american"] == "+170"
    manual = manual_preview("NFL")
    rows = [{"source_event_id": "", "away_team": "Unknown", "home_team": "Other",
             "kickoff": KICKOFF, "status": "FINAL", "away_score": "7", "home_score": "10"}]
    assert review_results(manual, rows, history, adapter)["rows"][0]["match_status"] == "UNMATCHED"


def test_duplicate_ambiguous_and_kickoff_conflict_require_review(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = live_run(history)
    preview = nfl_preview()
    rows = edited(preview)
    rows.append(deepcopy(rows[0]))
    review = review_results(preview, rows, history, adapter)
    assert review["rows"][-1]["match_status"] == "DUPLICATE"
    with pytest.raises(ResultReviewError, match="duplicate"):
        confirm_results(preview, review, history, adapter)
    rows = edited(preview)
    rows[0]["kickoff"] = "2026-09-20T14:00:00-04:00"
    review = review_results(preview, rows, history, adapter)
    assert review["rows"][0]["match_status"] == "KICKOFF_CONFLICT"
    with pytest.raises(ResultReviewError):
        confirm_results(preview, review, history, adapter)
    assert history.result_snapshots() == []
    duplicate_run = {key: value for key, value in run.items() if key != "run_id"}
    duplicate_run["legs"] = [dict(leg) for leg in run["legs"]]
    duplicate_run["legs"].append({**duplicate_run["legs"][0], "leg_id": "duplicate-leg",
                                  "game_id": "duplicate-game"})
    duplicate_run["tickets"] = []
    duplicate_run["selected_ticket_keys"] = []
    history.save_run(duplicate_run)
    review = review_results(preview, edited(preview), history, adapter)
    assert review["rows"][0]["match_status"] == "AMBIGUOUS"


def test_final_two_leg_live_requires_book_review_then_uses_stored_payout(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = live_run(history)
    placement = history.save_operator_placement(placement_record(run, "TB|ATL",
                                                                placed_at="2026-09-20T11:00:00-04:00"))
    body = json.loads(fixture("espn_nfl_results.json"))
    body["events"][1]["status"]["type"] = {"name": "STATUS_FINAL", "state": "post", "completed": True}
    body["events"][1]["competitions"][0]["competitors"][0]["score"] = "21"
    body["events"][1]["competitions"][0]["competitors"][1]["score"] = "20"
    preview = nfl_preview(json.dumps(body).encode())
    review = review_results(preview, edited(preview), history, adapter)
    ticket = review["tickets"][0]
    assert ticket["model_result"] == "WIN" and ticket["result"] == "REQUIRES_REVIEW"
    assert history.live_settlements() == []
    confirm_results(preview, review, history, adapter,
                    book_results={placement["placement_id"]: ("WIN", None)})
    settlement = history.live_settlements()[0]
    assert settlement["model_ticket_result"] == settlement["book_settlement"] == "WIN"
    assert settlement["profit_loss_units"] == "1.7"
    assert live_performance(history)["net_units"] == "1.7"
    assert live_performance(history)["roi"] == "1.7"
    confirm_results(preview, review_results(preview, edited(preview), history, adapter), history, adapter,
                    book_results={placement["placement_id"]: ("WIN", None)})
    assert len(history.live_settlements()) == 1
    with pytest.raises(ResultReviewError, match="sportsbook settlement"):
        confirm_results(preview, review_results(preview, edited(preview), history, adapter),
                        history, adapter, book_results={placement["placement_id"]: ("LOSS", None)})
    assert len(history.live_settlements()) == 1
    confirm_results(preview, review_results(preview, edited(preview), history, adapter),
                    history, adapter, allow_conflicts=True,
                    book_results={placement["placement_id"]: ("LOSS", None)})
    assert len(history.live_settlements()) == 2
    assert live_performance(history)["net_units"] == "-1"


def test_half_point_loss_and_unplaced_proposal_do_not_create_live_accounting(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = live_run(history, two_legs=False)
    preview = nfl_preview()
    review = review_results(preview, edited(preview), history, adapter)
    assert review["tickets"] == []
    confirm_results(preview, review, history, adapter)
    assert live_performance(history)["placed"] == 0
    assert history.live_settlements() == []
    placement = history.save_operator_placement(placement_record(run, "TB|ATL",
                                                                placed_at="2026-09-20T11:00:00-04:00"))
    body = json.loads(fixture("espn_nfl_results.json"))
    body["events"][0]["competitions"][0]["competitors"][0]["score"] = "10"
    body["events"][0]["competitions"][0]["competitors"][1]["score"] = "24"
    changed = nfl_preview(json.dumps(body).encode())
    review = review_results(changed, edited(changed), history, adapter)
    assert review["tickets"][0]["model_result"] == "LOSS"
    assert review["legs"][0]["result"] == "LOSS"
    with pytest.raises(ResultReviewError, match="Conflicting"):
        confirm_results(changed, review, history, adapter)
    confirm_results(changed, review, history, adapter, allow_conflicts=True,
                    book_results={placement["placement_id"]: ("LOSS", None)})
    assert live_performance(history)["net_units"] == "-1"


def test_cfb_paper_final_pending_push_and_separate_performance(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = paper_run(history)
    preview = cfb_preview()
    review = review_results(preview, edited(preview), history, adapter)
    assert review["rows"][0]["match_status"] == "MATCHED"
    assert review["tickets"][0]["result"] == "PENDING"
    saved = confirm_results(preview, review, history, adapter)
    assert history.latest_paper_results(run["run_id"])["result_snapshot_id"] == saved["result_snapshot_id"]
    assert paper_performance(history, adapter)["settled"] == 0
    assert live_performance(history)["placed"] == 0
    manual = manual_preview("CFB")
    rows = [{"source_event_id": "", "away_team": "Alpha State", "home_team": "Beta",
             "kickoff": "2026-09-26T15:30:00-04:00", "status": "FINAL",
             "away_score": "7", "home_score": "21"}]
    review2 = review_results(manual, rows, history, adapter)
    assert review2["tickets"][0]["result"] == "WIN"
    confirm_results(manual, review2, history, adapter)
    perf = paper_performance(history, adapter)
    assert perf["settled"] == 1 and perf["wins"] == 1 and perf["net_units"] == "1.7"
    assert perf["hypothetical_units_staked"] == "1"
    assert history.live_placements() == []

    push_history = LocalHistory(tmp_path / "push")
    push_run = paper_run(push_history, push=True)
    push_history.save_paper_results(push_run["run_id"],
                                    {"texas-tenn": {"away": 21, "home": 28},
                                     "alpha-beta": {"away": 7, "home": 21}},
                                    recorded_at="2026-09-27T12:00:00-04:00")
    push_perf = paper_performance(push_history, adapter)
    assert push_perf["pushes"] == push_perf["requires_review"] == 1
    assert push_perf["settled"] == 0 and push_perf["net_units"] == "0"


def test_manual_correction_preserves_prior_snapshot_and_conflict_review(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    paper_run(history)
    first = cfb_preview()
    original = confirm_results(first, review_results(first, edited(first), history, adapter), history, adapter)
    manual = manual_preview("CFB")
    rows = [{"source_event_id": "", "away_team": "Texas", "home_team": "Tennessee",
             "kickoff": "2026-09-26T12:00:00-04:00", "status": "FINAL",
             "away_score": "10", "home_score": "21"}]
    review = review_results(manual, rows, history, adapter)
    assert review["conflicts"]
    with pytest.raises(ResultReviewError, match="Conflicting"):
        confirm_results(manual, review, history, adapter)
    corrected = confirm_results(manual, review, history, adapter, allow_conflicts=True)
    assert corrected["result_snapshot_id"] != original["result_snapshot_id"]
    assert history.get_result_snapshot(original["result_snapshot_id"]) == original
    repeated = review_results(manual, rows, history, adapter)
    assert repeated["conflicts"] == []
    assert confirm_results(manual, repeated, history, adapter)["result_snapshot_id"] == corrected["result_snapshot_id"]
    older_espn = review_results(first, edited(first), history, adapter)
    assert older_espn["conflicts"]
    assert history.latest_paper_results(history.runs(status="PAPER")[0]["run_id"])["scores"]["texas-tenn"] == {
        "away": 10, "home": 21}


def test_incomplete_stored_live_terms_require_review_not_accounting(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = live_run(history, two_legs=False)
    placement = placement_record(run, "TB|ATL", placed_at="2026-09-20T11:00:00-04:00")
    scores = {"tb-cle": {"away": 10, "home": 24}}
    for field in ("offered_american", "stake_units", "leg_ids"):
        incomplete = {key: value for key, value in placement.items() if key != field}
        outcome = adapter.grade_live_placement(run, incomplete, scores)
        assert outcome["status"] == "REQUIRES_REVIEW"
    incomplete = {**placement, "leg_ids": ["unknown-leg"]}
    assert adapter.grade_live_placement(run, incomplete, scores)["status"] == "REQUIRES_REVIEW"
    assert live_performance(history)["net_units"] == "0"


def test_live_grade_uses_placement_line_instead_of_original_proposal(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = live_run(history, two_legs=False)
    placement = placement_record(run, "TB|ATL", placed_at="2026-09-20T11:00:00-04:00")
    scores = {"tb-cle": {"away": 10, "home": 24}}
    assert adapter.grade_live_placement(run, placement, scores)["model_ticket_result"] == "WIN"
    changed = deepcopy(placement)
    changed["leg_terms"][0]["teased_spread"] = "-20.5"
    assert adapter.grade_live_placement(run, changed, scores)["model_ticket_result"] == "LOSS"


def test_conflict_after_live_settlement_requires_book_review_and_supersedes_once(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = live_run(history, two_legs=False)
    placement = history.save_operator_placement(placement_record(run, "TB|ATL",
                                                                placed_at="2026-09-20T11:00:00-04:00"))
    first = nfl_preview()
    confirm_results(first, review_results(first, edited(first), history, adapter), history, adapter,
                    book_results={placement["placement_id"]: ("WIN", None)})
    assert live_performance(history)["net_units"] == "1.7"
    body = json.loads(fixture("espn_nfl_results.json"))
    body["events"][0]["competitions"][0]["competitors"][0]["score"] = "10"
    body["events"][0]["competitions"][0]["competitors"][1]["score"] = "24"
    corrected = nfl_preview(json.dumps(body).encode())
    review = review_results(corrected, edited(corrected), history, adapter)
    assert review["conflicts"] and review["tickets"][0]["model_result"] == "LOSS"
    with pytest.raises(ResultReviewError, match="book outcome"):
        confirm_results(corrected, review, history, adapter, allow_conflicts=True)
    confirm_results(corrected, review, history, adapter, allow_conflicts=True,
                    book_results={placement["placement_id"]: ("LOSS", None)})
    settlements = history.live_settlements()
    assert len(settlements) == 2
    assert settlements[-1]["supersedes"] == settlements[0]["settlement_id"]
    assert live_performance(history)["settled"] == 1
    assert live_performance(history)["net_units"] == "-1"


def test_prior_manual_paper_score_conflict_is_flagged_before_confirmation(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    run = paper_run(history)
    history.save_paper_results(run["run_id"], {"texas-tenn": {"away": 20, "home": 28}},
                               recorded_at="2026-09-27T10:00:00-04:00")
    preview = cfb_preview()
    review = review_results(preview, edited(preview), history, adapter)
    assert review["rows"][0]["match_status"] == "CONFLICT"
    assert any("PAPER" in conflict for conflict in review["conflicts"])
    with pytest.raises(ResultReviewError, match="Conflicting"):
        confirm_results(preview, review, history, adapter)
    assert history.result_snapshots() == []


def test_operator_reported_live_terms_are_frozen_and_not_duplicated(tmp_path):
    history = LocalHistory(tmp_path)
    run = live_run(history)
    placed_at = "2026-09-20T10:57:00-04:00"
    candidate = placement_record(run, run["selected_ticket_keys"][0], placed_at=placed_at)
    saved = history.save_operator_placement(candidate)
    assert saved["offered_american"] == "+170"
    assert history.get_run(saved["run_id"])["recheck"]["captured_at"] == "2026-09-20T10:50:00-04:00"
    assert live_performance(history)["placed"] == 1
    assert history.save_operator_placement(candidate)["placement_id"] == saved["placement_id"]


def test_streamlit_discard_and_confirm_review_gate(tmp_path, monkeypatch):
    import teaser_app.results_page as page
    import teaser_app.cfb_page as cfb_page
    import teaser_app.market_compare_page as compare_page
    history = LocalHistory(tmp_path)
    monkeypatch.setattr(page, "LocalHistory", lambda: history)
    monkeypatch.setattr(cfb_page, "LocalHistory", lambda: history)
    monkeypatch.setattr(compare_page, "LocalHistory", lambda: history)
    monkeypatch.setattr(page, "fetch_espn_results", lambda *args, **kwargs: nfl_preview())
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    next(button for button in app.button if button.label == "Fetch ESPN Results").click().run()
    assert history.result_snapshots() == []
    assert any("REVIEW REQUIRED" in item.value for item in app.warning)
    next(button for button in app.button if button.label == "Discard results preview").click().run()
    assert history.result_snapshots() == []
    next(button for button in app.button if button.label == "Fetch ESPN Results").click().run()
    next(button for button in app.button if button.label == "Confirm Results").click().run()
    assert len(history.result_snapshots()) == 1
    assert not app.exception


def test_week2_and_historical_market_are_unchanged(tmp_path):
    adapter, history = TeaserModelAdapter(), LocalHistory(tmp_path)
    slate, original = adapter.week2_example()
    assert adapter.grade(slate).selected_ticket_keys == original.selected_ticket_keys
    market = history.ingest_snapshot(slate)
    before = history.get_snapshot(market["snapshot_id"])
    preview = nfl_preview()
    confirm_results(preview, review_results(preview, edited(preview), history, adapter), history, adapter)
    assert history.get_snapshot(market["snapshot_id"]) == before
    assert history.runs() == [] and history.live_placements() == []
