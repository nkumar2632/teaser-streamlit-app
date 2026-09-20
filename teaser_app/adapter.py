"""The only app boundary to the pinned source model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from teaser_app.integrity import REFERENCE, load_model_path, verify_model
from teaser_app.views import CardView, RecheckView, frozen_row

load_model_path()

from teaser_model_v1.engine.constants import UNITS_PER_TICKET  # noqa: E402
from teaser_model_v1.live.card import grade_week  # noqa: E402
from teaser_model_v1.live.market import read_market_csv  # noqa: E402
from teaser_model_v1.live.pricing import read_price_csv  # noqa: E402
from teaser_model_v1.live.recheck import recheck_card  # noqa: E402
from teaser_model_v1.live.research import teased_board_rows  # noqa: E402
from teaser_model_v1.live.schemas import (  # noqa: E402
    NFL_TEAMS, MarketQuote, MarketSnapshot, TeaserPriceQuote, TeaserPriceSnapshot,
    normalize_team,
)


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name} must be a numeric string")
    raw = str(value).strip()
    if len(raw) > 16 or "e" in raw.lower():
        raise ValueError(f"{name} must be a short plain decimal number")
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _aware(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or len(value) > 35:
        raise ValueError(f"{name} requires an ISO timestamp with offset")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} requires an ISO timestamp with offset") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} requires an explicit UTC offset")
    return result


def _text(value: Any, name: str, limit: int = 80) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} is required and must be at most {limit} characters")
    return value.strip()


def _int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class TeaserModelAdapter:
    """Session-scoped source objects and presentation mapping; never model arithmetic."""

    def __init__(self) -> None:
        verify_model()
        self._cards: dict[str, tuple[Any, Any, Any, bool]] = {}

    @property
    def teams(self) -> tuple[str, ...]:
        return tuple(sorted(NFL_TEAMS))

    def canonical_team(self, value: str) -> str:
        return normalize_team(value)

    def _snapshots(self, slate: dict) -> tuple[MarketSnapshot, TeaserPriceSnapshot | None]:
        verify_model()
        if not isinstance(slate, dict):
            raise ValueError("slate must be an object")
        season = _int(slate.get("season"), "season", 2000, 2100)
        week = _int(slate.get("week"), "week", 1, 18)
        sportsbook = _text(slate.get("sportsbook"), "sportsbook", 60)
        captured = _aware(slate.get("captured_at"), "capture time")
        rows = slate.get("rows")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
            raise ValueError("enter 1 to 64 quoted sides")
        quotes = []
        game_values: dict[str, tuple[Decimal, datetime, dict[str, Decimal]]] = {}
        team_games: dict[str, str] = {}
        for number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise ValueError(f"side {number} must be an object")
            away = normalize_team(_text(row.get("away_team"), f"side {number} away", 8))
            home = normalize_team(_text(row.get("home_team"), f"side {number} home", 8))
            team = normalize_team(_text(row.get("team"), f"side {number} team", 8))
            spread = _decimal(row.get("spread"), f"side {number} spread")
            total = _decimal(row.get("total"), f"side {number} total")
            kickoff = _aware(row.get("kickoff"), f"side {number} kickoff")
            game_id = f"{season}_{week:02d}_{away}_{home}"
            for participant in (away, home):
                if participant in team_games and team_games[participant] != game_id:
                    raise ValueError(f"side {number}: {participant} appears in two matchups")
                team_games[participant] = game_id
            prior = game_values.get(game_id)
            if prior is not None:
                if prior[0] != total:
                    raise ValueError(f"side {number} total conflicts with the other side of {game_id}")
                if prior[1] != kickoff:
                    raise ValueError(f"side {number} kickoff conflicts with the other side of {game_id}")
                if team in prior[2]:
                    raise ValueError(f"side {number} duplicates {team} in {game_id}")
                if prior[2] and spread != -next(iter(prior[2].values())):
                    raise ValueError(f"side {number} spread conflicts with the other side of {game_id}")
                prior[2][team] = spread
            else:
                game_values[game_id] = (total, kickoff, {team: spread})
            quotes.append(MarketQuote(
                game_id=game_id, season=season, week=week, kickoff=kickoff,
                home_team=home, away_team=away, team=team, spread=spread,
                total=total, sportsbook=sportsbook, captured_at=captured,
                ingestion_method="streamlit_manual",
                raw_source_value=f"{team} {spread} total {total}",
            ))
        market = MarketSnapshot(
            season=season, week=week, captured_at=captured, sportsbook=sportsbook,
            ingestion_method="streamlit_manual", quotes=tuple(quotes),
        )
        prices = slate.get("prices")
        if not isinstance(prices, dict) or set(prices) != {"2", "3"}:
            raise ValueError("prices must contain 2-team and 3-team fields")
        price_quotes = []
        for size in (2, 3):
            raw = prices[str(size)]
            if raw == "":
                continue
            price_quotes.append(TeaserPriceQuote(
                ticket_size=size, sportsbook=sportsbook, captured_at=captured,
                american_odds=_decimal(raw, f"{size}-team price"),
                source_reference="operator-entered actual menu",
            ))
        price_snapshot = TeaserPriceSnapshot(
            season=season, week=week, captured_at=captured,
            sportsbook=sportsbook, quotes=tuple(price_quotes),
        ) if price_quotes else None
        return market, price_snapshot

    def _view(self, card: Any, market: Any, prices: Any, historical: bool) -> CardView:
        research = teased_board_rows(market)
        legs = tuple(frozen_row(leg.to_dict()) for leg in card.qualifying_legs)
        top = tuple(frozen_row(leg.to_dict()) for leg in card.top_legs)
        tickets = tuple(frozen_row({
            **ticket.to_dict(),
            "stake_units": UNITS_PER_TICKET if ticket.selected else 0,
        }) for ticket in card.tickets)
        research_rows = tuple(frozen_row({
            "team": row.team,
            "opponent": row.opponent,
            "spread": str(row.spread),
            "teased_spread": str(row.teased_spread),
            "total": str(row.total),
            "p_est": row.p_est,
            "geometry_class": row.geometry_class,
            "track": row.track,
            "exclusion_reason": row.exclusion_reason,
            "can_push": row.can_push,
            "on_live_board": row.on_live_board,
        }) for row in research)
        self._cards[card.card_id] = (card, market, prices, historical)
        return CardView(
            card_id=card.card_id, season=card.season, week=card.week,
            sportsbook=card.sportsbook, status=card.status, historical=historical,
            market_snapshot_id=card.market_snapshot_id,
            price_snapshot_id=card.price_snapshot_id,
            graded_at=card.graded_at.isoformat(), games_scanned=card.games_scanned,
            qualifying_legs=legs, top_legs=top, tickets=tickets,
            selected_ticket_keys=tuple(card.selected_ticket_keys),
            exposure=MappingProxyType(dict(card.exposure)), research=research_rows,
        )

    def grade(self, slate: dict, *, historical: bool = False) -> CardView:
        market, prices = self._snapshots(slate)
        card = grade_week(market, prices)
        return self._view(card, market, prices, historical)

    def recheck(self, card_id: str, slate: dict) -> RecheckView:
        verify_model()
        if card_id not in self._cards:
            raise ValueError("original card is no longer in this session")
        original, original_market, _, historical = self._cards[card_id]
        if historical:
            raise ValueError("historical examples cannot be rechecked as live cards")
        if not original.selected_ticket_keys:
            raise ValueError("the proposed card has no selected tickets to recheck")
        market, prices = self._snapshots(slate)
        if market.captured_at <= original_market.captured_at:
            raise ValueError("recheck requires a capture time later than the original snapshot")
        result = recheck_card(original, market, prices)
        rebuilt = self._view(result.rebuilt_card, market, prices, False) if result.rebuilt_card else None
        return RecheckView(
            original_card_id=card_id, verdict=result.overall,
            tickets=tuple(frozen_row(ticket.to_dict()) for ticket in result.tickets),
            new_market_snapshot_id=result.new_market_snapshot_id,
            new_price_snapshot_id=result.new_price_snapshot_id,
            rechecked_at=result.rechecked_at.isoformat(), rebuilt_card=rebuilt,
        )

    def week2_example(self) -> tuple[dict, CardView]:
        """Original pinned sportsbook snapshot; never an editable live wager source."""
        verify_model()
        folder = REFERENCE / "data" / "live" / "input"
        market = read_market_csv(folder / "nfl_2026_week_02_market_screenshot.csv")
        prices = read_price_csv(
            folder / "nfl_2026_week_02_prices_screenshot.csv", season=2026, week=2,
        )
        slate = {
            "schema_version": 1,
            "season": market.season, "week": market.week,
            "sportsbook": market.sportsbook,
            "captured_at": market.captured_at.isoformat(),
            "prices": {str(size): str(prices.quote_for(size).american_odds) for size in (2, 3)},
            "rows": [
                {"away_team": q.away_team, "home_team": q.home_team,
                 "team": q.team, "spread": str(q.spread), "total": str(q.total),
                 "kickoff": q.kickoff.isoformat()}
                for q in market.quotes
            ],
        }
        card = grade_week(market, prices, graded_at=market.captured_at)
        return slate, self._view(card, market, prices, True)
