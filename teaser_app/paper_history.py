"""Frozen paper runs and isolated hypothetical performance accounting."""

from __future__ import annotations

from decimal import Decimal

from teaser_app.integrity import PIN
from teaser_app.views import PaperView


def run_record(view: PaperView, slate: dict, snapshot_id: str) -> dict:
    if view.strategy.status != "PAPER":
        raise ValueError("only paper runs belong in paper history")
    return {
        "schema_version": 1, "kind": "model_run", "strategy": view.strategy.to_dict(),
        "season": view.season, "week": view.week, "captured_at": view.captured_at,
        "sportsbook": view.sportsbook, "snapshot_id": snapshot_id,
        "model_sha": PIN,
        "slate": slate, "legs": [dict(row) for row in view.legs],
        "qualifying_leg_ids": [row["leg_id"] for row in view.qualifying_legs],
        "tickets": [dict(row) for row in view.tickets],
        "selected_ticket_keys": list(view.selected_ticket_keys),
        "hypothetical_exposure": dict(view.exposure),
        "actual_placements": 0, "actual_units_staked": 0,
    }


def paper_performance(history, adapter) -> dict:
    """Score only recorded final outcomes for PAPER runs; never touch a live ledger."""
    settled = wins = losses = pushes = 0
    net = Decimal(0)
    for run in history.runs(status="PAPER"):
        result_record = history.latest_paper_results(run["run_id"])
        if result_record is None:
            continue
        graded = adapter.grade_paper_results(run, result_record["scores"])
        for ticket in graded["tickets"]:
            if not ticket["selected"] or ticket["result"] == "PENDING":
                continue
            settled += 1
            if ticket["result"] == "WIN":
                wins += 1
                net += Decimal(ticket["profit"]) * ticket["stake_units"]
            elif ticket["result"] == "LOSS":
                losses += 1
                net -= ticket["stake_units"]
            else:
                pushes += 1
    return {"settled": settled, "wins": wins, "losses": losses,
            "pushes": pushes, "net_units": str(net), "actual_placements": 0,
            "actual_units_staked": 0}
