"""Append-only local market and run records. No provider or model arithmetic here."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "market_data"
ID = re.compile(r"^[a-z]+_[0-9a-f]{20}$")
FIELDS = (
    "snapshot_id", "event_id", "league", "home_team", "away_team", "kickoff",
    "captured_at", "source", "sportsbook", "spread_home", "spread_home_price",
    "spread_away", "spread_away_price", "moneyline_home", "moneyline_away",
    "total", "over_price", "under_price", "teaser_2team_6pt_price",
    "teaser_3team_6pt_price",
)
MARKET_ROLES = frozenset({"REFERENCE", "EXECUTION", "UNCLASSIFIED"})


def snapshot_role(record: dict) -> str:
    """Classify old records only when their recorded provenance is conclusive."""
    explicit = record.get("market_role")
    if explicit in MARKET_ROLES:
        return explicit
    if explicit is not None:
        return "UNCLASSIFIED"
    source = record.get("source")
    if source == "manual_sportsbook" and record.get("sportsbook"):
        return "EXECUTION"
    if source in {"reference_source", "future_odds_api", "reference_url"}:
        return "REFERENCE"
    return "UNCLASSIFIED"


def _canonical(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _identity(prefix: str, record: dict) -> str:
    if prefix == "mkt":
        record = {**record, "events": [{key: value for key, value in event.items()
                                         if key != "snapshot_id"} for event in record["events"]]}
    return f"{prefix}_{hashlib.sha256(_canonical(record)).hexdigest()[:20]}"


def _read(folder: Path, record_id: str) -> dict:
    if not ID.fullmatch(record_id):
        raise ValueError("invalid record id")
    return json.loads((folder / f"{record_id}.json").read_text(encoding="utf-8"))


def _save(folder: Path, prefix: str, record: dict) -> dict:
    record_id = _identity(prefix, record)
    field = {"mkt": "snapshot_id", "run": "run_id", "res": "result_id",
             "rst": "result_snapshot_id", "plc": "placement_id",
             "stl": "settlement_id"}[prefix]
    saved = json.loads(_canonical({field: record_id, **record}))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record_id}.json"
    try:
        with path.open("x", encoding="utf-8") as output:
            json.dump(saved, output, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            output.write("\n")
    except FileExistsError:
        if _read(folder, record_id) != saved:
            raise ValueError("immutable record identity collision")
    return saved


def normalize_market(slate: dict) -> dict:
    """Preserve each observed side and optional market as one immutable snapshot."""
    league = slate.get("league", "NFL")
    source = slate.get("source", "unspecified")
    if league not in {"NFL", "CFB"}:
        raise ValueError("unsupported league")
    events: dict[tuple[str, str, str], dict] = {}
    for row in slate["rows"]:
        away, home = row["away_team"], row["home_team"]
        key = (away, home, row["kickoff"])
        if key not in events:
            event_id = _identity("evt", {"league": league, "away": away, "home": home,
                                         "kickoff": row["kickoff"]})
            events[key] = {field: None for field in FIELDS}
            events[key].update(event_id=event_id, league=league, home_team=home,
                               away_team=away, kickoff=row["kickoff"],
                               captured_at=slate["captured_at"],
                               source=source,
                               sportsbook=slate["sportsbook"], total=row["total"],
                               teaser_2team_6pt_price=slate["prices"]["2"] or None,
                               teaser_3team_6pt_price=slate["prices"]["3"] or None)
        side = "home" if row["team"] == home else "away"
        events[key][f"spread_{side}"] = row["spread"]
    ordered = sorted(events.values(), key=lambda event: event["event_id"])
    payload = {"schema_version": 1, "kind": "market_snapshot", "league": league,
               "season": slate["season"], "week": slate["week"],
               "captured_at": slate["captured_at"], "source": source,
               "line_label": slate.get("line_label"),
               "sportsbook": slate["sportsbook"], "events": ordered}
    if slate.get("schema_version") == 2:
        role = snapshot_role(payload)
        payload.update(schema_version=2, market_role=role,
                       source_type="manual_entry" if source != "user_screenshot"
                       else "screenshot_transcription",
                       source_provider=None, source_url=None)
        for event in ordered:
            event.update(market_role=role, source_type=payload["source_type"],
                         source_provider=None, source_url=None)
    snapshot_id = _identity("mkt", payload)
    for event in ordered:
        event["snapshot_id"] = snapshot_id
    return payload


class LocalHistory:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root

    def ingest_snapshot(self, slate: dict) -> dict:
        payload = normalize_market(slate)
        return _save(self.root / "normalized", "mkt", payload)

    def save_confirmed_reference(self, payload: dict) -> dict:
        if (payload.get("schema_version") != 2 or payload.get("market_role") != "REFERENCE"
                or payload.get("source_type") != "url" or not payload.get("events")):
            raise ValueError("expected a confirmed URL reference snapshot")
        return _save(self.root / "normalized", "mkt", payload)

    def save_confirmed_execution(self, payload: dict) -> dict:
        """Persist only a human-confirmed sportsbook screenshot as EXECUTION data."""
        if (payload.get("schema_version") != 3 or payload.get("market_role") != "EXECUTION"
                or payload.get("source_type") != "screenshot" or not payload.get("events")
                or any(event.get("market_role") != "EXECUTION"
                       or event.get("source_type") != "screenshot"
                       for event in payload["events"])):
            raise ValueError("expected a confirmed sportsbook screenshot snapshot")
        return _save(self.root / "normalized", "mkt", payload)

    def get_snapshot(self, snapshot_id: str) -> dict:
        return _read(self.root / "normalized", snapshot_id)

    def market_history(self, *, league: str | None = None, role: str | None = None) -> list[dict]:
        if role is not None and role not in MARKET_ROLES:
            raise ValueError("unsupported market role")
        folder = self.root / "normalized"
        records = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("mkt_*.json")]
        return sorted((record for record in records if
                       (league is None or record["league"] == league) and
                       (role is None or snapshot_role(record) == role)),
                      key=lambda record: (datetime.fromisoformat(record["captured_at"]).timestamp(), record["snapshot_id"]))

    def latest_market(self, league: str, *, role: str | None = None) -> dict | None:
        matches = self.market_history(league=league, role=role)
        return matches[-1] if matches else None

    def save_run(self, record: dict) -> dict:
        return _save(self.root / "runs", "run", record)

    def get_run(self, run_id: str) -> dict:
        return _read(self.root / "runs", run_id)

    def runs(self, *, league: str | None = None, status: str | None = None,
             bet_type: str | None = None, model_version: str | None = None) -> list[dict]:
        folder = self.root / "runs"
        records = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("run_*.json")]
        return sorted((r for r in records if
                       (league is None or r["strategy"]["league"] == league) and
                       (status is None or r["strategy"]["status"] == status) and
                       (bet_type is None or r["strategy"]["bet_type"] == bet_type) and
                       (model_version is None or r["strategy"]["model_version"] == model_version)),
                      key=lambda r: (r["captured_at"], r["run_id"]), reverse=True)

    def save_paper_results(self, run_id: str, scores: dict[str, dict[str, int]],
                           *, recorded_at: str, result_snapshot_id: str | None = None,
                           source_provider: str | None = None) -> dict:
        run = self.get_run(run_id)
        if run["strategy"]["status"] != "PAPER":
            raise ValueError("results here are paper-only")
        when = datetime.fromisoformat(recorded_at)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("result time requires UTC offset")
        game_ids = {leg["game_id"] for leg in run["legs"]}
        if not scores or set(scores) - game_ids:
            raise ValueError("scores must reference games in the paper run")
        for score in scores.values():
            if set(score) != {"home", "away"} or any(type(v) is not int or v < 0 or v > 200 for v in score.values()):
                raise ValueError("scores require nonnegative integer home and away values")
        payload = {"schema_version": 1, "kind": "paper_final_scores",
                   "run_id": run_id, "recorded_at": recorded_at, "scores": scores}
        if result_snapshot_id is not None:
            self.get_result_snapshot(result_snapshot_id)
            payload.update(result_snapshot_id=result_snapshot_id, source_provider=source_provider)
        return _save(self.root / "results", "res", payload)

    def latest_paper_results(self, run_id: str) -> dict | None:
        folder = self.root / "results"
        records = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("res_*.json")]
        matching = [record for record in records if record["run_id"] == run_id]
        return max(matching, key=lambda r: (datetime.fromisoformat(r["recorded_at"]).timestamp(),
                                            r["result_id"])) if matching else None

    def save_result_snapshot(self, payload: dict) -> dict:
        if (payload.get("kind") != "confirmed_game_results" or payload.get("league") not in {"NFL", "CFB"}
                or payload.get("provider") not in {"ESPN", "MANUAL"}
                or not isinstance(payload.get("events"), list) or not payload["events"]):
            raise ValueError("expected confirmed football results")
        return _save(self.root / "result_snapshots", "rst", payload)

    def result_snapshots(self, *, league: str | None = None) -> list[dict]:
        records = [json.loads(path.read_text(encoding="utf-8"))
                   for path in (self.root / "result_snapshots").glob("rst_*.json")]
        return sorted((record for record in records if league is None or record["league"] == league),
                      key=lambda record: (record["confirmed_at"], record["result_snapshot_id"]))

    def get_result_snapshot(self, snapshot_id: str) -> dict:
        return _read(self.root / "result_snapshots", snapshot_id)

    def live_placements(self, *, season: int | None = None) -> list[dict]:
        records = [json.loads(path.read_text(encoding="utf-8"))
                   for path in (self.root / "placements").glob("plc_*.json")]
        return sorted((record for record in records if season is None or record["season"] == season),
                      key=lambda record: (record["placed_at"], record["placement_id"]))

    def save_operator_placement(self, payload: dict) -> dict:
        run = self.get_run(payload["run_id"])
        if (run["strategy"] != {"league": "NFL", "bet_type": "TEASER",
                                 "model_version": "teaser_v1.0", "status": "LIVE"}
                or payload.get("kind") != "operator_reported_placement"
                or payload.get("card_id") != run.get("card_id")
                or payload.get("ticket_key") not in run["selected_ticket_keys"]):
            raise ValueError("placement must identify a selected NFL LIVE ticket")
        ticket = next(item for item in run["tickets"] if item["ticket_key"] == payload["ticket_key"])
        if (payload.get("leg_ids") != ticket["leg_ids"]
                or payload.get("leg_terms") != [run["recheck"]["placement_legs"][key]
                                                 for key in ticket["leg_ids"]]
                or payload.get("offered_american") != run["recheck"]["offered_prices"].get(str(ticket["n_legs"]))
                or payload.get("stake_units") != ticket["stake_units"]):
            raise ValueError("placement terms must match the frozen ticket")
        prior = [item for item in self.live_placements() if item["card_id"] == payload["card_id"]
                 and item["ticket_key"] == payload["ticket_key"]]
        if prior:
            expected = {key: value for key, value in prior[0].items() if key != "placement_id"}
            if expected == payload:
                return prior[0]
            raise ValueError("ticket already has a placement record; review it before adding another")
        return _save(self.root / "placements", "plc", payload)

    def live_settlements(self) -> list[dict]:
        records = [json.loads(path.read_text(encoding="utf-8"))
                   for path in (self.root / "live_settlements").glob("stl_*.json")]
        return sorted(records, key=lambda record: (
            datetime.fromisoformat(record["settled_at"]).timestamp(), record["settlement_id"]))

    def save_live_settlement(self, payload: dict, *, allow_correction: bool = False) -> dict:
        if payload.get("kind") != "operator_confirmed_settlement":
            raise ValueError("expected operator-confirmed settlement")
        if payload.get("placement_id") not in {p["placement_id"] for p in self.live_placements()}:
            raise ValueError("cannot settle an unrecorded placement")
        prior = [row for row in self.live_settlements() if row["placement_id"] == payload["placement_id"]]
        if prior:
            latest = prior[-1]
            comparable = {key: value for key, value in latest.items()
                          if key not in {"settlement_id", "settled_at", "result_snapshot_id", "supersedes"}}
            candidate = {key: value for key, value in payload.items()
                         if key not in {"settled_at", "result_snapshot_id", "supersedes"}}
            if comparable == candidate:
                return latest
            if not allow_correction or payload.get("supersedes") != latest["settlement_id"]:
                raise ValueError("conflicting settled ticket requires explicit correction review")
        return _save(self.root / "live_settlements", "stl", payload)
