"""App-owned record of operator-reported NFL placements and derived season totals."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from teaser_app.integrity import PIN
from teaser_app.market_data import _identity
from teaser_app.nfl_market import EXECUTION_BOARD_FRESHNESS, _confirmed_execution, execution_book
from teaser_app.strategy import NFL_TEASER


def run_record(view, slate: dict, snapshot_id: str, recheck, recheck_slate: dict, adapter) -> dict:
    if view.historical or recheck.verdict != "VALIDATED" or recheck.original_card_id != view.card_id:
        raise ValueError("only a validated current NFL proposal can be recorded")
    record = {
        "schema_version": 1, "kind": "model_run", "strategy": NFL_TEASER.to_dict(),
        "card_id": view.card_id, "season": view.season, "week": view.week,
        "captured_at": slate["captured_at"], "sportsbook": view.sportsbook,
        "snapshot_id": snapshot_id, "source_market_snapshot_id": view.market_snapshot_id,
        "price_snapshot_id": view.price_snapshot_id, "model_sha": PIN,
        "slate": slate, "legs": [dict(row) for row in view.qualifying_legs],
        "tickets": [dict(row) for row in view.tickets],
        "selected_ticket_keys": list(view.selected_ticket_keys),
        "recheck": {"verdict": recheck.verdict, "rechecked_at": recheck.rechecked_at,
                    "market_snapshot_id": recheck.new_market_snapshot_id,
                    "price_snapshot_id": recheck.new_price_snapshot_id,
                    "captured_at": recheck_slate["captured_at"],
                    "placement_legs": adapter.placement_terms(view, recheck_slate),
                    "offered_prices": dict(recheck_slate["prices"])},
    }
    if slate.get("source_snapshot_id"):
        record["confirmed_execution_snapshot_id"] = slate["source_snapshot_id"]
    if recheck_slate.get("source_snapshot_id"):
        record["recheck"]["confirmed_execution_snapshot_id"] = recheck_slate["source_snapshot_id"]
    return record


def placement_record(run: dict, ticket_key: str, *, placed_at: str) -> dict:
    try:
        instant = datetime.fromisoformat(placed_at.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            raise ValueError
    except (AttributeError, ValueError) as exc:
        raise ValueError("placement time needs an ISO timestamp with UTC offset") from exc
    rechecked_at = datetime.fromisoformat(run["recheck"]["rechecked_at"])
    captured_at = datetime.fromisoformat(run["recheck"]["captured_at"])
    if not captured_at <= rechecked_at <= instant <= rechecked_at + timedelta(minutes=30):
        raise ValueError("reported placement requires a preceding recent recheck and capture")
    if ticket_key not in run["selected_ticket_keys"]:
        raise ValueError("only a selected ticket can be reported placed")
    ticket = next(row for row in run["tickets"] if row["ticket_key"] == ticket_key)
    legs = run["recheck"].get("placement_legs", {})
    if any(key not in legs for key in ticket["leg_ids"]):
        raise ValueError("ticket needs stored rechecked leg terms")
    if any(instant >= datetime.fromisoformat(legs[key]["kickoff"]) for key in ticket["leg_ids"]):
        raise ValueError("reported placement time must precede every kickoff")
    offered = run["recheck"]["offered_prices"].get(str(ticket["n_legs"]))
    if not offered or not ticket["stake_units"]:
        raise ValueError("ticket needs stored price and stake")
    record = {
        "schema_version": 1, "kind": "operator_reported_placement",
        "designation": "OPERATOR_REPORTED", "run_id": run["run_id"],
        "card_id": run["card_id"], "season": run["season"], "week": run["week"],
        "ticket_key": ticket_key, "leg_ids": list(ticket["leg_ids"]),
        "leg_terms": [legs[key] for key in ticket["leg_ids"]],
        "sportsbook": run["sportsbook"], "offered_american": offered,
        "stake_units": ticket["stake_units"], "placed_at": instant.isoformat(),
        "source_market_snapshot_id": run["source_market_snapshot_id"],
        "price_snapshot_id": run["recheck"]["price_snapshot_id"],
        "recheck_market_snapshot_id": run["recheck"]["market_snapshot_id"],
    }
    if run.get("confirmed_execution_snapshot_id"):
        record["confirmed_execution_snapshot_id"] = run["confirmed_execution_snapshot_id"]
    if run["recheck"].get("confirmed_execution_snapshot_id"):
        record["recheck_confirmed_execution_snapshot_id"] = run["recheck"]["confirmed_execution_snapshot_id"]
    return record


def record_reported_placements(history, view, original_slate: dict, recheck_slate: dict,
                               recheck, ticket_keys: list[str], placed_at: str, adapter) -> list[dict]:
    if not ticket_keys or len(set(ticket_keys)) != len(ticket_keys):
        raise ValueError("Select distinct placed tickets")
    source_id = original_slate.get("source_snapshot_id")
    recheck_id = recheck_slate.get("source_snapshot_id")
    if not source_id or not recheck_id or source_id == recheck_id:
        raise ValueError("placement needs distinct confirmed execution snapshots")
    market = history.get_snapshot(source_id)
    current = history.get_snapshot(recheck_id)
    original_book = execution_book(market)
    recheck_book = execution_book(current)
    if (not _confirmed_execution(market) or not _confirmed_execution(current)
            or original_book.casefold() != recheck_book.casefold()
            or original_book.casefold() != original_slate["sportsbook"].casefold()
            or recheck_book.casefold() != recheck_slate["sportsbook"].casefold()
            or original_book.casefold() != original_slate.get("price_sportsbook", "").casefold()
            or recheck_book.casefold() != recheck_slate.get("price_sportsbook", "").casefold()):
        raise ValueError("placement snapshots must be confirmed sportsbook EXECUTION data")
    if datetime.fromisoformat(recheck_slate["captured_at"]) <= datetime.fromisoformat(view.graded_at):
        raise ValueError("placement recheck capture must postdate card grading")
    record = run_record(view, original_slate, market["snapshot_id"], recheck, recheck_slate, adapter)
    record["run_id"] = _identity("run", record)
    placements = [placement_record(record, key, placed_at=placed_at) for key in ticket_keys]
    if (datetime.fromisoformat(placements[0]["placed_at"]) >
            datetime.fromisoformat(recheck_slate["captured_at"]) + EXECUTION_BOARD_FRESHNESS):
        raise ValueError("confirmed sportsbook market capture is no longer fresh")
    prior = {(row["card_id"], row["ticket_key"]) for row in history.live_placements()}
    if any((record["card_id"], key) in prior for key in ticket_keys):
        raise ValueError("One selected ticket is already recorded as placed")
    adapter.validate_live_exposure(history.live_placements(), placements,
                                   season=view.season, week=view.week)
    saved = history.save_run({key: value for key, value in record.items() if key != "run_id"})
    if saved["run_id"] != record["run_id"]:
        raise ValueError("frozen run identity changed")
    return [history.save_operator_placement(item) for item in placements]


def live_performance(history, *, season: int | None = None) -> dict:
    placements = history.live_placements(season=season)
    latest = {row["placement_id"]: row for row in history.live_settlements()}
    wins = losses = pushes = voids = 0
    staked = settled_staked = net = Decimal(0)
    for placement in placements:
        staked += Decimal(str(placement["stake_units"]))
        settlement = latest.get(placement["placement_id"])
        if settlement is None:
            continue
        status = settlement["book_settlement"]
        if status == "WIN":
            wins += 1
        elif status == "LOSS":
            losses += 1
        elif status == "PUSH":
            pushes += 1
        else:
            voids += 1
        settled_staked += Decimal(str(placement["stake_units"]))
        net += Decimal(str(settlement["profit_loss_units"]))
    return {"placed": len(placements), "settled": wins + losses + pushes + voids,
            "wins": wins, "losses": losses, "pushes": pushes, "voids": voids,
            "units_staked": str(staked), "settled_units_staked": str(settled_staked),
            "net_units": str(net.normalize()),
            "roi": str(net / settled_staked) if settled_staked else None}
