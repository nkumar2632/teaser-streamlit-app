from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

from teaser_app.market_data import LocalHistory
from teaser_app.providers.espn import ESPNParseError, parse_scoreboard
from teaser_app.url_ingest import (
    MarketPreview, URLIngestError, fetch_url, identify_provider,
    normalize_review, preview_url, save_confirmed_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"
APP = Path(__file__).resolve().parents[1] / "app.py"
NFL_URL = "https://www.espn.com/nfl/scoreboard/_/week/3/year/2026/seasontype/2"
CFB_URL = "https://www.espn.com/college-football/scoreboard?dates=20260926&group=80"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _Response:
    headers = {"Content-Type": "application/json"}
    status_code = 200

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size: int):
        yield from (self.body[i:i + chunk_size] for i in range(0, len(self.body), chunk_size))


class _Client:
    def __init__(self, body: bytes):
        self.body = body
        self.requests = []

    def get(self, url, *, headers, timeout, allow_redirects, stream):
        self.requests.append((url, timeout, allow_redirects, stream))
        return _Response(self.body)


def test_provider_detection_canonicalizes_supported_espn_pages():
    nfl = identify_provider(NFL_URL)
    assert (nfl.league, nfl.provider) == ("NFL", "ESPN")
    assert nfl.fetch_url == ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
                             "?dates=2026&seasontype=2&week=3")
    cfb = identify_provider(CFB_URL)
    assert cfb.league == "CFB"
    assert cfb.fetch_url.endswith("?dates=20260926&groups=80")
    assert identify_provider(cfb.fetch_url).fetch_url == cfb.fetch_url


@pytest.mark.parametrize("url", [
    "http://www.espn.com/nfl/scoreboard",
    "https://localhost/nfl/scoreboard",
    "https://www.espn.com.evil.example/nfl/scoreboard",
    "https://user:password@www.espn.com/nfl/scoreboard",
    "https://www.espn.com:8443/nfl/scoreboard",
    "https://www.espn.com/nfl/odds",
    "https://www.espn.com/nfl/scoreboard?url=http://127.0.0.1",
    "https://www.espn.com/nfl/scoreboard?dates=20260927&dates=20260928",
    "https://www.espn.com/nfl/scoreboard/../odds",
    "https://www.vegasinsider.com/nfl/odds/las-vegas/",
])
def test_unsupported_or_unsafe_url_is_rejected(url):
    with pytest.raises(URLIngestError):
        identify_provider(url)


def test_fetch_is_bounded_and_network_failure_does_not_persist(tmp_path):
    client = _Client(fixture("espn_nfl_scoreboard.json"))
    preview = preview_url(NFL_URL, client=client)
    assert len(preview.rows) == 2
    assert client.requests == [(identify_provider(NFL_URL).fetch_url, 10, False, True)]
    assert list(tmp_path.iterdir()) == []

    class Broken:
        def get(self, *_args, **_kwargs):
            raise requests.ConnectionError("blocked")

    with pytest.raises(URLIngestError, match="Could not fetch"):
        preview_url(NFL_URL, client=Broken())
    with pytest.raises(URLIngestError, match="5 MB"):
        fetch_url(identify_provider(NFL_URL), client=_Client(b"x" * 5_000_001))
    class RedirectClient(_Client):
        def get(self, *args, **kwargs):
            response = super().get(*args, **kwargs)
            response.status_code = 302
            return response

    redirect = RedirectClient(b"{}")
    with pytest.raises(URLIngestError, match="Could not fetch"):
        preview_url(NFL_URL, client=redirect)
    assert redirect.requests[0][2] is False
    assert list(tmp_path.iterdir()) == []


def test_nfl_and_cfb_fields_missing_values_and_duplicate_events():
    nfl, warnings = parse_scoreboard(fixture("espn_nfl_scoreboard.json"), "NFL")
    assert nfl[0]["spread_home"] == "-7"
    assert nfl[0]["spread_away_price"] == "-110"
    assert nfl[0]["moneyline_away"] == "+270"
    assert nfl[0]["over_price"] == "-110"
    assert nfl[0]["total"] == "50.5"
    assert nfl[1]["spread_home"] is None and nfl[1]["moneyline_home"] is None
    assert any("missing" in warning for warning in warnings)
    cfb, _ = parse_scoreboard(fixture("espn_cfb_scoreboard.json"), "CFB")
    assert cfb[0]["away_team"] == "Texas Longhorns"
    assert cfb[0]["spread_away"] == "-4"
    payload = json.loads(fixture("espn_nfl_scoreboard.json"))
    payload["events"].append(payload["events"][0])
    with pytest.raises(ESPNParseError, match="Duplicate"):
        parse_scoreboard(json.dumps(payload).encode(), "NFL")
    with pytest.raises(ESPNParseError, match="No games"):
        parse_scoreboard(b'{"events": []}', "NFL")
    with pytest.raises(ESPNParseError, match="malformed"):
        parse_scoreboard(b"not json", "NFL")
    bad = json.loads(fixture("espn_nfl_scoreboard.json"))
    bad["events"][0]["competitions"][0]["odds"][0]["moneyline"]["away"]["close"]["odds"] = "EVEN"
    bad["events"][0]["season"] = None
    bad["events"][0]["week"] = None
    _, warnings = parse_scoreboard(json.dumps(bad).encode(), "NFL")
    assert any("malformed moneyline_away" in warning for warning in warnings)


def test_review_edit_confirm_provenance_immutability_and_no_live_accounting(tmp_path):
    history = LocalHistory(tmp_path)
    preview = preview_url(NFL_URL, client=_Client(fixture("espn_nfl_scoreboard.json")))
    assert history.market_history() == []
    edited = [dict(row) for row in preview.rows]
    edited[0]["spread_home"] = "-6.5"
    edited[1]["moneyline_away"] = "+180"
    first = save_confirmed_snapshot(preview, edited, history)
    assert first["schema_version"] == 2
    assert first["market_role"] == "REFERENCE"
    assert first["source_type"] == "url" and first["source_provider"] == "ESPN"
    assert first["source_url"] == NFL_URL
    assert first["sportsbook"] == "DraftKings"
    assert first["events"][0]["teaser_2team_6pt_price"] is None
    assert first["events"][0]["review_changes"]["spread_home"]["fetched"] == "-7"
    assert all(event["market_role"] == "REFERENCE" for event in first["events"])
    assert history.get_snapshot(first["snapshot_id"]) == first
    assert history.save_confirmed_reference(normalize_review(preview, edited))["snapshot_id"] == first["snapshot_id"]
    alternate_url = replace(preview, spec=identify_provider(identify_provider(NFL_URL).fetch_url))
    alternate_source = save_confirmed_snapshot(alternate_url, edited, history)
    assert alternate_source["snapshot_id"] != first["snapshot_id"]
    assert alternate_source["events"][0]["event_id"] == first["events"][0]["event_id"]
    later = replace(preview, captured_at="2026-09-25T21:00:00+00:00")
    second = save_confirmed_snapshot(later, edited, history)
    assert second["snapshot_id"] != first["snapshot_id"]
    assert [event["event_id"] for event in second["events"]] == [event["event_id"] for event in first["events"]]
    assert history.get_snapshot(first["snapshot_id"]) == first
    assert len(history.market_history(league="NFL")) == 3
    assert history.runs() == []
    assert not (tmp_path / "results").exists()


def test_cfb_reference_save_is_separate_from_paper_runs(tmp_path):
    preview = preview_url(CFB_URL, client=_Client(fixture("espn_cfb_scoreboard.json")))
    history = LocalHistory(tmp_path)
    saved = save_confirmed_snapshot(preview, [dict(row) for row in preview.rows], history)
    assert saved["league"] == "CFB"
    assert saved["events"][0]["moneyline_home"] == "+154"
    assert saved["events"][0]["teaser_3team_6pt_price"] is None
    assert saved["market_role"] == "REFERENCE"
    assert history.runs(status="PAPER") == []


def _cfb_with_off_board_moneylines(sentinel: str = "OFF") -> bytes:
    payload = json.loads(fixture("espn_cfb_scoreboard.json"))
    moneyline = payload["events"][0]["competitions"][0]["odds"][0]["moneyline"]
    moneyline["away"]["close"]["odds"] = sentinel
    moneyline["home"]["close"]["odds"] = sentinel
    return json.dumps(payload).encode()


@pytest.mark.parametrize("sentinel", ["OFF", "off"])
def test_espn_off_board_moneyline_is_not_offered_rather_than_malformed(sentinel):
    rows, warnings = parse_scoreboard(_cfb_with_off_board_moneylines(sentinel), "CFB")
    assert rows[0]["moneyline_away"] is None
    assert rows[0]["moneyline_home"] is None
    assert (rows[0]["spread_away"], rows[0]["spread_home"], rows[0]["total"]) == ("-4", "+4", "56.5")
    assert (rows[0]["spread_away_price"], rows[0]["over_price"]) == ("-108", "-108")
    assert not any("malformed" in warning for warning in warnings)


def test_espn_off_board_moneylines_confirm_without_manual_edits(tmp_path):
    history = LocalHistory(tmp_path)
    preview = preview_url(CFB_URL, client=_Client(_cfb_with_off_board_moneylines()))
    assert not any("malformed" in warning for warning in preview.warnings)
    saved = save_confirmed_snapshot(preview, [dict(row) for row in preview.rows], history)
    assert saved["market_role"] == "REFERENCE" and saved["league"] == "CFB"
    assert saved["events"][0]["moneyline_away"] is None
    assert saved["events"][0]["moneyline_home"] is None
    assert saved["events"][0]["spread_home"] == "+4" and saved["events"][0]["total"] == "56.5"
    assert "OFF" not in json.dumps(saved)
    assert history.get_snapshot(saved["snapshot_id"]) == saved


def test_malformed_price_or_duplicate_review_cannot_create_snapshot(tmp_path):
    history = LocalHistory(tmp_path)
    preview = preview_url(NFL_URL, client=_Client(fixture("espn_nfl_scoreboard.json")))
    edited = [dict(row) for row in preview.rows]
    edited[0]["spread_home_price"] = "EVEN"
    with pytest.raises(URLIngestError, match="malformed"):
        save_confirmed_snapshot(preview, edited, history)
    edited[0]["spread_home_price"] = "-110"
    edited[1]["home_team"] = edited[0]["home_team"]
    edited[1]["away_team"] = edited[0]["away_team"]
    edited[1]["kickoff"] = edited[0]["kickoff"]
    with pytest.raises(URLIngestError, match="Duplicate matchup"):
        save_confirmed_snapshot(preview, edited, history)
    assert history.market_history() == []


def test_streamlit_fetch_preview_requires_confirm_and_leaves_week2_intact(tmp_path, monkeypatch):
    import teaser_app.url_import as url_import
    monkeypatch.setattr(url_import, "preview_url", lambda _url: preview_url(NFL_URL, client=_Client(fixture("espn_nfl_scoreboard.json"))))
    monkeypatch.setattr(url_import, "LocalHistory", lambda: LocalHistory(tmp_path))
    app = AppTest.from_file(APP, default_timeout=30).run()
    next(button for button in app.button if button.label == "Load verified Week 2 historical example").click().run()
    before = app.session_state["card"].card_id
    next(button for button in app.button if button.label == "Fetch Lines").click().run()
    assert not app.exception
    assert history_count(tmp_path) == 0
    assert any("REVIEW REQUIRED" in w.value for w in app.warning)
    assert app.session_state["card"].card_id == before
    next(button for button in app.button if button.label == "Confirm Market Snapshot").click().run()
    assert not app.exception
    assert history_count(tmp_path) == 1
    assert app.session_state["card"].card_id == before
    assert not app.session_state.get("reported_placed")


def test_espn_presets_use_the_same_review_and_confirm_gate(tmp_path, monkeypatch):
    import teaser_app.url_import as url_import
    assert url_import.espn_preset_url("NFL", date(2026, 9, 21)).endswith("dates=20260927")
    assert url_import.espn_preset_url("CFB", date(2026, 9, 21)).endswith("dates=20260926&groups=80")
    requested = []
    def fixture_preview(url):
        requested.append(url)
        name = "espn_cfb_scoreboard.json" if "college-football" in url else "espn_nfl_scoreboard.json"
        return preview_url(url, client=_Client(fixture(name)))
    monkeypatch.setattr(url_import, "preview_url", fixture_preview)
    monkeypatch.setattr(url_import, "LocalHistory", lambda: LocalHistory(tmp_path))
    app = AppTest.from_file(APP, default_timeout=30).run()
    next(button for button in app.button if button.label == "Fetch ESPN NFL").click().run()
    assert requested[0] == url_import.espn_preset_url("NFL")
    assert history_count(tmp_path) == 0
    assert any("REVIEW REQUIRED" in item.value for item in app.warning)
    next(button for button in app.button if button.label == "Confirm Market Snapshot").click().run()
    assert history_count(tmp_path) == 1
    next(button for button in app.button if button.label == "Fetch ESPN CFB").click().run()
    assert requested[-1] == url_import.espn_preset_url("CFB")
    assert history_count(tmp_path) == 1
    assert any("CFB reference market" in item.value for item in app.warning)
    assert not app.exception


def test_dated_espn_urls_validate_league_and_calendar_date():
    from teaser_app.url_import import espn_dated_url
    nfl = espn_dated_url("NFL", date(2026, 9, 27))
    cfb = espn_dated_url("CFB", date(2026, 9, 26))
    assert nfl == "https://www.espn.com/nfl/scoreboard?dates=20260927"
    assert cfb == "https://www.espn.com/college-football/scoreboard?dates=20260926&groups=80"
    assert identify_provider(nfl).league == "NFL"
    assert identify_provider(cfb).league == "CFB"
    for league, day in (("NBA", date(2026, 9, 27)), ("NFL", "2026-09-27"),
                        ("NFL", None), ("CFB", date(1999, 12, 31)),
                        ("CFB", date(2101, 1, 1))):
        with pytest.raises(URLIngestError):
            espn_dated_url(league, day)


def test_dated_fetch_uses_existing_review_gate_and_preserves_selected_date(tmp_path, monkeypatch):
    import teaser_app.url_import as url_import
    requested = []
    def fixture_preview(url):
        requested.append(url)
        return preview_url(url, client=_Client(fixture("espn_cfb_scoreboard.json")))
    monkeypatch.setattr(url_import, "preview_url", fixture_preview)
    monkeypatch.setattr(url_import, "LocalHistory", lambda: LocalHistory(tmp_path))
    app = AppTest.from_file(APP, default_timeout=30).run()
    next(widget for widget in app.selectbox if widget.label == "ESPN league").set_value("CFB").run()
    next(widget for widget in app.date_input if widget.label == "ESPN scoreboard date").set_value(date(2026, 9, 26)).run()
    next(button for button in app.button if button.label == "Fetch ESPN Lines").click().run()
    expected = url_import.espn_dated_url("CFB", date(2026, 9, 26))
    assert requested == [expected]
    assert history_count(tmp_path) == 0
    assert app.session_state["url_preview"].spec.source_url == expected
    assert any("REVIEW REQUIRED" in item.value for item in app.warning)
    next(button for button in app.button if button.label == "Confirm Market Snapshot").click().run()
    saved = app.session_state["url_saved_snapshot"]
    assert saved["market_role"] == "REFERENCE"
    assert saved["source_type"] == "url" and saved["source_url"] == expected
    assert saved["fetched_url"].endswith("dates=20260926&groups=80")
    assert history_count(tmp_path) == 1
    assert not app.exception


def test_dated_fetch_no_games_shows_error_and_saves_nothing(tmp_path, monkeypatch):
    import teaser_app.url_import as url_import
    def empty_preview(url):
        return preview_url(url, client=_Client(b'{"events": []}'))
    monkeypatch.setattr(url_import, "preview_url", empty_preview)
    monkeypatch.setattr(url_import, "LocalHistory", lambda: LocalHistory(tmp_path))
    app = AppTest.from_file(APP, default_timeout=30).run()
    next(button for button in app.button if button.label == "Fetch ESPN Lines").click().run()
    assert any("No games found" in item.value for item in app.error)
    assert history_count(tmp_path) == 0
    assert not app.session_state.get("url_preview")
    assert not app.exception


def history_count(path: Path) -> int:
    return len(list((path / "normalized").glob("*.json")))
