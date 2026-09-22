from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from teaser_app.market_compare import (
    compare_snapshots, line_difference, match_events, snapshot_age, team_key,
)
from teaser_app.market_data import LocalHistory, normalize_market, snapshot_role
from teaser_app.providers.espn import parse_scoreboard
from teaser_app.url_ingest import MarketPreview, identify_provider, save_confirmed_snapshot

ROOT = Path(__file__).resolve().parents[1]
NFL_URL = "https://www.espn.com/nfl/scoreboard/_/week/3/year/2026/seasontype/2"
KICKOFF = "2026-09-27T13:00:00-04:00"


def manual_slate(*, league="NFL", source="manual_sportsbook", sportsbook="My Book"):
    return {"schema_version": 2, "league": league, "source": source,
            "line_label": "true_timestamped_pregame", "season": 2026, "week": 3,
            "sportsbook": sportsbook, "captured_at": "2026-09-27T10:00:00-04:00",
            "prices": {"2": "-110", "3": "+170"},
            "rows": [{"away_team": "JAX", "home_team": "TB", "team": "JAX",
                      "spread": "+3", "total": "45.5", "kickoff": KICKOFF}]}


def event(away="Jacksonville Jaguars", home="Tampa Bay Buccaneers", kickoff=KICKOFF, **markets):
    return {"league": "NFL", "away_team": away, "home_team": home,
            "kickoff": kickoff, "sportsbook": "ESPN quote", "spread_away": "+2.5",
            "spread_away_price": "-110", "spread_home": "-2.5",
            "spread_home_price": "-110", "moneyline_away": "+120",
            "moneyline_home": "-140", "total": "45", "over_price": "-110",
            "under_price": "-110", **markets}


def reference_snapshot(events=None):
    return {"schema_version": 2, "snapshot_id": "mkt_reference",
            "league": "NFL", "season": 2026, "week": 3,
            "source": "reference_url", "source_type": "url", "source_provider": "ESPN",
            "source_url": NFL_URL, "sportsbook": "ESPN quote", "market_role": "REFERENCE",
            "captured_at": "2026-09-27T09:00:00-04:00",
            "events": events if events is not None else [event()]}


def test_roles_and_history_keep_reference_execution_and_uncertain_legacy_apart(tmp_path):
    history = LocalHistory(tmp_path)
    execution = history.ingest_snapshot(manual_slate())
    assert execution["schema_version"] == 2
    assert execution["market_role"] == execution["events"][0]["market_role"] == "EXECUTION"
    assert execution["source_type"] == "manual_entry"
    assert execution["source_provider"] is None and execution["source_url"] is None
    assert history.market_history(league="NFL", role="EXECUTION") == [execution]

    rows, warnings = parse_scoreboard((ROOT / "tests/fixtures/espn_nfl_scoreboard.json").read_bytes(), "NFL")
    preview = MarketPreview(identify_provider(NFL_URL), "2026-09-27T09:00:00-04:00", tuple(rows), tuple(warnings))
    reference = save_confirmed_snapshot(preview, [dict(row) for row in rows], history)
    assert reference["market_role"] == "REFERENCE"
    assert history.market_history(league="NFL", role="REFERENCE") == [reference]
    assert history.latest_market("NFL", role="EXECUTION") == execution
    assert history.get_snapshot(execution["snapshot_id"]) == execution
    assert history.get_snapshot(reference["snapshot_id"]) == reference
    assert history.runs() == []

    old = deepcopy(manual_slate())
    old.pop("league")
    old.pop("source")
    old.pop("line_label")
    old["schema_version"] = 1
    old_snapshot = history.ingest_snapshot(old)
    assert old_snapshot["schema_version"] == 1
    assert old_snapshot["source"] == "unspecified"
    assert "market_role" not in old_snapshot
    assert snapshot_role(old_snapshot) == "UNCLASSIFIED"
    assert history.market_history(league="NFL", role="UNCLASSIFIED") == [old_snapshot]
    assert history.market_history(league="NFL", role="EXECUTION") == [execution]

    uncertain = normalize_market(manual_slate(source="user_screenshot"))
    assert uncertain["market_role"] == "UNCLASSIFIED"
    reference_manual = normalize_market(manual_slate(source="reference_source"))
    assert reference_manual["market_role"] == "REFERENCE"


def test_exact_alias_and_kickoff_matching_with_literal_market_differences():
    execution = normalize_market(manual_slate())
    execution["snapshot_id"] = "mkt_execution"
    execution["events"][0]["moneyline_away"] = "+130"
    execution["events"][0]["spread_away_price"] = "-105"
    result = compare_snapshots(reference_snapshot(), execution,
                               now=datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc))
    assert len(result["matches"]) == 1
    assert result["reference_unmatched"] == result["execution_unmatched"] == []
    reference_event, execution_event = result["matches"][0]
    assert team_key("NFL", "JAX") == team_key("NFL", "Jacksonville Jaguars")
    assert team_key("NFL", "LA") == team_key("NFL", "Los Angeles Rams")
    assert team_key("NFL", "LAC") != team_key("NFL", "Los Angeles Rams")
    assert team_key("CFB", "Texas") == team_key("CFB", "Texas Longhorns")
    assert team_key("CFB", "Texas Tech") != team_key("CFB", "Texas")
    assert line_difference(reference_event["spread_away"], execution_event["spread_away"]) == "+0.5"
    assert line_difference(reference_event["total"], execution_event["total"]) == "+0.5"
    assert reference_event["moneyline_away"] == "+120"
    assert execution_event["moneyline_away"] == "+130"
    assert reference_event["spread_away_price"] == "-110"
    assert execution_event["spread_away_price"] == "-105"
    assert not result["reference_age"]["stale"] and not result["execution_age"]["stale"]


def test_ambiguous_conflicting_and_missing_games_stay_unmatched():
    reference = [event()]
    execution = [event(away="JAX", home="TB"), event(away="JAX", home="TB")]
    result = match_events(reference, execution, "NFL")
    assert result["matches"] == []
    assert all("ambiguous" in item["reason"] for item in
               result["reference_unmatched"] + result["execution_unmatched"])
    later = [event(away="JAX", home="TB", kickoff="2026-09-28T13:00:00-04:00")]
    result = match_events(reference, later, "NFL")
    assert not result["matches"]
    assert "conflicting kickoff" in result["reference_unmatched"][0]["reason"]
    result = match_events(reference, [event(away="TB", home="JAX")], "NFL")
    assert "home/away conflict" in result["reference_unmatched"][0]["reason"]
    result = match_events(reference, [event(away="Unknown Team", home="TB")], "NFL")
    assert not result["matches"]
    assert line_difference(None, "+3") is None
    assert line_difference("EVEN", "+3") is None
    assert snapshot_age(reference_snapshot(), datetime(2026, 9, 29, tzinfo=timezone.utc))["stale"]
    with pytest.raises(ValueError, match="REFERENCE and EXECUTION"):
        compare_snapshots(reference_snapshot(), reference_snapshot())


def test_cfb_exact_aliases_match_without_fuzzy_pairing():
    reference = [{"away_team": "Texas Longhorns", "home_team": "Tennessee Volunteers",
                  "kickoff": "2026-09-26T16:00:00+00:00"}]
    execution = [{"away_team": "Texas", "home_team": "Tennessee",
                  "kickoff": "2026-09-26T12:00:00-04:00"}]
    matched = match_events(reference, execution, "CFB")
    assert len(matched["matches"]) == 1
    execution[0]["away_team"] = "Texas Tech"
    assert not match_events(reference, execution, "CFB")["matches"]


def test_comparison_ui_displays_sources_times_missing_markets_and_does_not_change_card(tmp_path, monkeypatch):
    import teaser_app.market_compare_page as page
    from teaser_app.adapter import TeaserModelAdapter
    history = LocalHistory(tmp_path)
    execution = history.ingest_snapshot(manual_slate())
    reference = reference_snapshot()
    reference["events"][0]["spread_home_price"] = None
    reference.pop("snapshot_id")
    history.save_confirmed_reference(reference)
    monkeypatch.setattr(page, "LocalHistory", lambda: history)
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    assert not app.exception
    text = " ".join(item.value for item in [*app.caption, *app.markdown, *app.warning])
    assert "ESPN" in text and "My Book" in text
    assert "captured" in text and "Δ +0.5" in text and "Moneyline" in text
    assert "—" in text and "Missing markets:" in text
    assert "REFERENCE minus" not in text
    next(button for button in app.button if button.label == "Load verified Week 2 historical example").click().run()
    card_id = app.session_state["card"].card_id
    assert app.session_state["card"].card_id == card_id
    assert history.get_snapshot(execution["snapshot_id"]) == execution
    assert history.runs() == []
