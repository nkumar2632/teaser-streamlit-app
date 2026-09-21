"""App-owned view records. No source-model classes cross into the UI."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from teaser_app.strategy import Strategy


def frozen_row(values: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class CardView:
    card_id: str
    season: int
    week: int
    sportsbook: str
    status: str
    historical: bool
    market_snapshot_id: str
    price_snapshot_id: str
    graded_at: str
    games_scanned: int
    qualifying_legs: tuple[Mapping[str, Any], ...]
    top_legs: tuple[Mapping[str, Any], ...]
    tickets: tuple[Mapping[str, Any], ...]
    selected_ticket_keys: tuple[str, ...]
    exposure: Mapping[str, int]
    research: tuple[Mapping[str, Any], ...]

    @property
    def selected_tickets(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(t for t in self.tickets if t["selected"])


@dataclass(frozen=True)
class RecheckView:
    original_card_id: str
    verdict: str
    tickets: tuple[Mapping[str, Any], ...]
    new_market_snapshot_id: str
    new_price_snapshot_id: str
    rechecked_at: str
    rebuilt_card: CardView | None


@dataclass(frozen=True)
class PaperView:
    run_id: str
    strategy: Strategy
    season: int
    week: int
    sportsbook: str
    captured_at: str
    snapshot_id: str
    historical: bool
    legs: tuple[Mapping[str, Any], ...]
    qualifying_legs: tuple[Mapping[str, Any], ...]
    secondary_legs: tuple[Mapping[str, Any], ...]
    tickets: tuple[Mapping[str, Any], ...]
    selected_ticket_keys: tuple[str, ...]
    exposure: Mapping[str, int]

    @property
    def selected_tickets(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(ticket for ticket in self.tickets if ticket["selected"])
