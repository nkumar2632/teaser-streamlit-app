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
    field = {"mkt": "snapshot_id", "run": "run_id", "res": "result_id"}[prefix]
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

    def get_snapshot(self, snapshot_id: str) -> dict:
        return _read(self.root / "normalized", snapshot_id)

    def market_history(self, *, league: str | None = None) -> list[dict]:
        folder = self.root / "normalized"
        records = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("mkt_*.json")]
        return sorted((record for record in records if league is None or record["league"] == league),
                      key=lambda record: (datetime.fromisoformat(record["captured_at"]).timestamp(), record["snapshot_id"]))

    def latest_market(self, league: str) -> dict | None:
        matches = self.market_history(league=league)
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
                           *, recorded_at: str) -> dict:
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
        return _save(self.root / "results", "res", {"schema_version": 1, "kind": "paper_final_scores",
                                                      "run_id": run_id, "recorded_at": recorded_at,
                                                      "scores": scores})

    def latest_paper_results(self, run_id: str) -> dict | None:
        folder = self.root / "results"
        records = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("res_*.json")]
        matching = [record for record in records if record["run_id"] == run_id]
        return max(matching, key=lambda r: (r["recorded_at"], r["result_id"])) if matching else None
