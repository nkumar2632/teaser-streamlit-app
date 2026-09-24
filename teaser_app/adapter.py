"""The only app boundary to the pinned source model."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any

from teaser_app.integrity import REFERENCE, load_model_path, verify_model
from teaser_app.strategy import CFB_TEASER
from teaser_app.views import CardView, PaperView, RecheckView, frozen_row

load_model_path()

from teaser_model_v1.engine.constants import UNITS_PER_TICKET  # noqa: E402
from teaser_model_v1.engine.geometry import passes_total_guardrail, secondary_reason_for, teased_spread  # noqa: E402
from teaser_model_v1.engine.numeric import is_whole_number, on_half_point_grid  # noqa: E402
from teaser_model_v1.engine.pricing import profit_from_american_odds  # noqa: E402
from teaser_model_v1.engine.tickets import select_live_tickets  # noqa: E402
from teaser_model_v1.analysis.grading import grade_teased_leg  # noqa: E402
from teaser_model_v1.live.card import grade_week, ticket_key_for  # noqa: E402
from teaser_model_v1.live.market import read_market_csv  # noqa: E402
from teaser_model_v1.live.pricing import read_price_csv  # noqa: E402
from teaser_model_v1.live.paper import (  # noqa: E402
    CURRENT_PREGAME, ARCHIVED_PREGAME_REFERENCE, TRUE_TIMESTAMPED_PREGAME,
    PregameLineInput, build_cfb_paper_legs, build_cfb_paper_tickets,
    primary_paper_legs, secondary_paper_legs,
)
from teaser_model_v1.live.recheck import recheck_card  # noqa: E402
from teaser_model_v1.live.settlement import (  # noqa: E402
    PrimaryPushInSettlement, grade_leg_settlement, settle_ticket,
)
from teaser_model_v1.live.research import teased_board_rows  # noqa: E402
from teaser_model_v1.live.schemas import (  # noqa: E402
    NFL_TEAMS, MarketQuote, MarketSnapshot, TeaserPriceQuote, TeaserPriceSnapshot,
    normalize_team, validate_spread, validate_total,
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


def _american_price(value: Any, name: str) -> Decimal:
    price = _decimal(value, name)
    if price != price.to_integral_value() or abs(price) < 100:
        raise ValueError(f"{name} requires American odds of at least +100 or at most -100")
    return price


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

    def nfl_market_sides(self, *, away: str, home: str, kickoff: str,
                         home_spread: str, away_spread: str | None,
                         total: str) -> list[dict]:
        """Validate one normalized market game with frozen NFL input guards."""
        verify_model()
        away, home = normalize_team(away), normalize_team(home)
        if away == home:
            raise ValueError("NFL game needs distinct teams")
        when = _aware(kickoff, "NFL kickoff").astimezone(timezone.utc)
        spread = validate_spread(_decimal(home_spread, "NFL home spread"))
        game_total = validate_total(_decimal(total, "NFL total"))
        if away_spread is not None and validate_spread(
                _decimal(away_spread, "NFL away spread")) != -spread:
            raise ValueError("NFL opposing spreads conflict")
        common = {"away_team": away, "home_team": home,
                  "total": str(game_total), "kickoff": when.isoformat()}
        return [{**common, "team": away, "spread": str(-spread)},
                {**common, "team": home, "spread": str(spread)}]

    def cfb_public_sides(self, *, away: str, home: str, kickoff: str,
                         home_spread: str, away_spread: str | None,
                         total: str) -> list[dict]:
        """Validate one public CFB game and expand its quote through the model boundary."""
        verify_model()
        away = _text(away, "CFB away team")
        home = _text(home, "CFB home team")
        if away.casefold() == home.casefold():
            raise ValueError("CFB game needs distinct teams")
        when = _aware(kickoff, "CFB kickoff").astimezone(timezone.utc)
        spread = _decimal(home_spread, "CFB home spread")
        game_total = _decimal(total, "CFB total")
        if (not on_half_point_grid(spread) or not on_half_point_grid(game_total)
                or game_total <= 0):
            raise ValueError("CFB spread and total need half-point-grid values and a positive total")
        if away_spread is not None and _decimal(away_spread, "CFB away spread") != -spread:
            raise ValueError("CFB opposing spreads conflict")
        common = {"away_team": away, "home_team": home,
                  "total": str(game_total), "kickoff": when.isoformat()}
        return [{**common, "team": away, "spread": str(-spread)},
                {**common, "team": home, "spread": str(spread)}]

    def placement_terms(self, view: CardView, recheck_slate: dict) -> dict[str, dict]:
        """Freeze the rechecked quote and source-derived teaser line for settlement."""
        verify_model()
        terms = {}
        for leg in view.qualifying_legs:
            rows = [row for row in recheck_slate["rows"] if row["team"] == leg["team"]]
            if len(rows) != 1:
                raise ValueError("rechecked placement leg must have one quoted side")
            row = rows[0]
            spread = _decimal(row["spread"], "rechecked spread")
            terms[leg["leg_id"]] = {
                "leg_id": leg["leg_id"], "game_id": leg["game_id"], "team": leg["team"],
                "away_team": row["away_team"], "home_team": row["home_team"],
                "kickoff": row["kickoff"], "spread": str(spread),
                "teased_spread": str(teased_spread(spread)),
            }
        return terms

    def _snapshots(self, slate: dict) -> tuple[MarketSnapshot, TeaserPriceSnapshot | None]:
        verify_model()
        if not isinstance(slate, dict):
            raise ValueError("slate must be an object")
        season = _int(slate.get("season"), "season", 2000, 2100)
        week = _int(slate.get("week"), "week", 1, 18)
        sportsbook = _text(slate.get("sportsbook"), "sportsbook", 60)
        ingestion_method = _text(slate.get("ingestion_method", "streamlit_manual"),
                                 "ingestion method", 60)
        price_sportsbook = _text(slate.get("price_sportsbook", sportsbook),
                                 "teaser menu sportsbook", 60)
        captured = _aware(slate.get("captured_at"), "capture time")
        price_captured = _aware(slate.get("price_captured_at", slate.get("captured_at")),
                                "teaser menu capture time")
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
                ingestion_method=ingestion_method,
                raw_source_value=f"{team} {spread} total {total}",
            ))
        market = MarketSnapshot(
            season=season, week=week, captured_at=captured, sportsbook=sportsbook,
            ingestion_method=ingestion_method, quotes=tuple(quotes),
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
                ticket_size=size, sportsbook=price_sportsbook, captured_at=price_captured,
                american_odds=_american_price(raw, f"{size}-team price"),
                source_reference=slate.get("price_source_reference", "operator-entered actual menu"),
            ))
        price_snapshot = TeaserPriceSnapshot(
            season=season, week=week, captured_at=price_captured,
            sportsbook=price_sportsbook, quotes=tuple(price_quotes),
            label=slate.get("price_label", ""),
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
        if slate.get("league", "NFL") != "NFL":
            raise ValueError("NFL live grading requires an NFL slate")
        market, prices = self._snapshots(slate)
        card = grade_week(market, prices)
        return self._view(card, market, prices, historical)

    def grade_cfb_paper(self, slate: dict, *, historical: bool = False) -> PaperView:
        """Use only the pinned paper board and frozen selector; no app model rules."""
        verify_model()
        if not isinstance(slate, dict) or slate.get("league") != "CFB":
            raise ValueError("CFB paper grading requires a CFB slate")
        season = _int(slate.get("season"), "season", 2000, 2100)
        week = _int(slate.get("week"), "week", 1, 20)
        book = _text(slate.get("sportsbook"), "sportsbook", 60)
        captured = _aware(slate.get("captured_at"), "capture time")
        label = slate.get("line_label", CURRENT_PREGAME)
        if label not in {CURRENT_PREGAME, ARCHIVED_PREGAME_REFERENCE, TRUE_TIMESTAMPED_PREGAME}:
            raise ValueError("CFB requires a recognized pregame provenance label")
        rows = slate.get("rows")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 400:
            raise ValueError("enter 1 to 400 quoted CFB sides")
        inputs = []
        seen: dict[str, tuple[Decimal, datetime, dict[str, Decimal]]] = {}
        team_games: dict[str, str] = {}
        for number, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise ValueError(f"side {number} must be an object")
            away = _text(row.get("away_team"), f"side {number} away")
            home = _text(row.get("home_team"), f"side {number} home")
            team = _text(row.get("team"), f"side {number} team")
            if away.casefold() == home.casefold() or team not in {away, home}:
                raise ValueError(f"side {number} has invalid matchup teams")
            spread = _decimal(row.get("spread"), f"side {number} spread")
            total = _decimal(row.get("total"), f"side {number} total")
            if not on_half_point_grid(spread) or not on_half_point_grid(total) or total <= 0:
                raise ValueError(f"side {number} requires positive total and half-point-grid lines")
            kickoff = _aware(row.get("kickoff"), f"side {number} kickoff")
            if label in {CURRENT_PREGAME, TRUE_TIMESTAMPED_PREGAME} and captured >= kickoff:
                raise ValueError(f"side {number} capture time must precede kickoff")
            game_id = f"{season}_{week:02d}_{sha256(f'{away}|{home}|{kickoff.isoformat()}'.encode()).hexdigest()[:16]}"
            for participant in (away, home):
                key = participant.casefold()
                if key in team_games and team_games[key] != game_id:
                    raise ValueError(f"side {number}: {participant} appears in two matchups")
                team_games[key] = game_id
            if game_id in seen:
                prior_total, prior_kickoff, sides = seen[game_id]
                if total != prior_total or kickoff != prior_kickoff or team in sides or spread != -next(iter(sides.values())):
                    raise ValueError(f"side {number} conflicts with the other side")
                sides[team] = spread
            else:
                seen[game_id] = (total, kickoff, {team: spread})
            inputs.append(PregameLineInput(
                game_id=game_id, team=team, opponent=home if team == away else away,
                spread=spread, total=total, kickoff=kickoff.isoformat(),
                source=f"{slate.get('source', 'manual_sportsbook')}:{book}", line_label=label,
            ))
        prices = slate.get("prices")
        if not isinstance(prices, dict) or set(prices) != {"2", "3"}:
            raise ValueError("prices must contain 2-team and 3-team fields")
        offered = {}
        profit = {}
        for size in (2, 3):
            raw = prices[str(size)]
            if raw != "":
                value = _decimal(raw, f"{size}-team price")
                offered[size] = str(value)
                profit[size] = profit_from_american_odds(float(value))
        legs = build_cfb_paper_legs(inputs)
        primary = primary_paper_legs(legs)
        secondary = secondary_paper_legs(legs, limit=len(legs))
        top, tickets = build_cfb_paper_tickets(primary, profit_by_size=profit or None)
        # This is a paper counterfactual, never a placement designation or ledger entry.
        selected = select_live_tickets(tickets, eligibility=lambda ticket: ticket.is_positive_ev)
        selected_keys = tuple(ticket_key_for(t.leg_ids) for t in selected.selected)
        primary_ids = {leg.leg_id for leg in primary}
        rank = {leg.leg_id: i for i, leg in enumerate(primary, 1)}
        secondary_rank = {leg.leg_id: i for i, leg in enumerate(secondary, 1)}
        all_rows = []
        for leg in legs:
            total_ok = passes_total_guardrail("CFB", leg.total)
            if leg.leg_id in primary_ids:
                reason = "qualifies"
            elif leg.is_primary:
                reason = "total above CFB guardrail"
            else:
                reason = f"secondary geometry: {secondary_reason_for(leg.spread)}"
                if not total_ok:
                    reason += "; total above CFB guardrail"
            all_rows.append(frozen_row({
                "leg_id": leg.leg_id, "game_id": leg.game_id, "team": leg.team,
                "opponent": leg.opponent, "spread": str(leg.spread),
                "teased_spread": str(leg.teased_spread), "total": str(leg.total),
                "geometry_class": leg.geometry_class, "track": leg.track,
                "key_numbers_crossed": leg.key_numbers_crossed,
                "p_raw": repr(leg.p_raw), "bump": repr(leg.bump), "p_est": repr(leg.p_est),
                "can_push": is_whole_number(leg.teased_spread),
                "kickoff": leg.kickoff, "source": leg.source, "line_label": leg.line_label,
                "rank": rank.get(leg.leg_id), "secondary_rank": secondary_rank.get(leg.leg_id),
                "exclusion_reason": reason,
            }))
        by_id = {row["leg_id"]: row for row in all_rows}
        ticket_rows = []
        for ticket in tickets:
            key = ticket_key_for(ticket.leg_ids)
            ticket_rows.append(frozen_row({
                "ticket_key": key, "n_legs": ticket.n_legs,
                "leg_ids": tuple(ticket.leg_ids), "teams": tuple(leg.team for leg in ticket.legs),
                "p_ticket": repr(ticket.p_ticket), "offered_american": offered.get(ticket.n_legs, "UNAVAILABLE"),
                "profit": repr(ticket.profit) if ticket.profit is not None else "UNAVAILABLE",
                "break_even": repr(ticket.break_even) if ticket.break_even is not None else "UNAVAILABLE",
                "ev_per_unit": repr(ticket.ev) if ticket.ev is not None else "UNAVAILABLE",
                "fair_break_even_profit": repr(ticket.fair_break_even_profit),
                "status": "POSITIVE_EV" if ticket.is_positive_ev else "UNPRICED" if ticket.ev is None else "NON_POSITIVE_EV",
                "selected": key in selected_keys,
                "stake_units": UNITS_PER_TICKET if key in selected_keys else 0,
            }))
        return PaperView(
            run_id="", strategy=CFB_TEASER, season=season, week=week,
            sportsbook=book, captured_at=captured.isoformat(), snapshot_id="",
            historical=historical, legs=tuple(all_rows),
            qualifying_legs=tuple(by_id[leg.leg_id] for leg in primary),
            secondary_legs=tuple(by_id[leg.leg_id] for leg in secondary),
            tickets=tuple(ticket_rows), selected_ticket_keys=selected_keys,
            exposure=MappingProxyType(dict(selected.exposure)),
        )

    def grade_paper_results(self, run: dict, scores: dict[str, dict[str, int]]) -> dict:
        """Join final scores only after a frozen paper run exists."""
        verify_model()
        if run["strategy"]["status"] != "PAPER":
            raise ValueError("paper results require a paper run")
        outcomes = {}
        for leg in run["legs"]:
            score = scores.get(leg["game_id"])
            if score is None:
                continue
            market = next(row for row in run["slate"]["rows"] if
                          row["team"] == leg["team"] and
                          row["away_team"] in {leg["team"], leg["opponent"]} and
                          row["home_team"] in {leg["team"], leg["opponent"]})
            margin = (score["home"] - score["away"]) if leg["team"] == market["home_team"] else (score["away"] - score["home"])
            outcomes[leg["leg_id"]] = grade_teased_leg(margin, leg["teased_spread"]).value
        tickets = []
        for ticket in run["tickets"]:
            leg_results = [outcomes.get(leg_id) for leg_id in ticket["leg_ids"]]
            result = "PENDING" if None in leg_results else "WIN" if all(v == "WIN" for v in leg_results) else "PUSH" if "PUSH" in leg_results and "LOSS" not in leg_results else "LOSS"
            tickets.append({"ticket_key": ticket["ticket_key"], "result": result,
                            "selected": ticket["selected"], "profit": ticket["profit"],
                            "stake_units": ticket["stake_units"]})
        return {"legs": outcomes, "tickets": tickets}

    def grade_live_placement(self, run: dict, placement: dict,
                             scores: dict[str, dict[str, int]], *,
                             book_settlement: str | None = None,
                             settled_at: str | None = None,
                             book_profit_loss_units: float | None = None) -> dict:
        """Use frozen source settlement functions on stored placement terms only."""
        verify_model()
        if run["strategy"] != {"league": "NFL", "bet_type": "TEASER",
                                "model_version": "teaser_v1.0", "status": "LIVE"}:
            raise ValueError("NFL LIVE run required")
        if placement["run_id"] != run["run_id"]:
            raise ValueError("placement and run differ")
        review = {"status": "REQUIRES_REVIEW", "model_ticket_result": None,
                  "legs": [], "reason": "stored placement or leg terms are incomplete"}
        try:
            terms = placement["leg_terms"]
            if not isinstance(terms, list):
                return review
            legs = {row["leg_id"]: row for row in terms}
            stake = Decimal(str(placement["stake_units"]))
            offered = Decimal(str(placement["offered_american"]))
            leg_ids = placement["leg_ids"]
            if (not stake.is_finite() or stake <= 0 or not offered.is_finite()
                    or offered == 0 or not isinstance(leg_ids, list) or not leg_ids):
                return review
        except (KeyError, InvalidOperation, TypeError, ValueError):
            return review
        graded = []
        for leg_id in leg_ids:
            leg = legs.get(leg_id)
            if not leg or not all(leg.get(field) for field in ("game_id", "team", "kickoff", "teased_spread")):
                return review
            result = scores.get(leg["game_id"])
            if result is None:
                return {"status": "PENDING", "model_ticket_result": None, "legs": [],
                        "reason": "waiting for another final game"}
            if leg["team"] not in {leg.get("home_team"), leg.get("away_team")}:
                return review
            margin = (result["home"] - result["away"]) if leg["team"] == leg["home_team"] else (result["away"] - result["home"])
            try:
                graded.append(grade_leg_settlement(
                    leg_id=leg_id, team=leg["team"], teased_spread=leg["teased_spread"],
                    final_margin=margin, home_score=result["home"], away_score=result["away"],
                ))
            except PrimaryPushInSettlement:
                return {"status": "REQUIRES_REVIEW", "model_ticket_result": None,
                        "legs": [], "reason": "primary leg unexpectedly pushed"}
        model_result = "WIN" if all(leg.model_result == "WIN" for leg in graded) else "LOSS"
        if book_settlement is None:
            return {"status": "REQUIRES_REVIEW", "model_ticket_result": model_result,
                    "legs": [leg.to_dict() for leg in graded],
                    "reason": "confirm the sportsbook settlement before LIVE P/L"}
        if settled_at is None:
            raise ValueError("settlement time required")
        source_placement = {**placement, "placement_id": placement["placement_id"],
                            "american_odds": placement["offered_american"]}
        source = settle_ticket(
            placement=source_placement, legs=tuple(graded), book_settlement=book_settlement,
            settled_at=datetime.fromisoformat(settled_at),
            profit_loss_units=book_profit_loss_units,
        )
        return {"status": book_settlement, "model_ticket_result": source.model_ticket_result,
                "book_settlement": source.book_settlement,
                "profit_loss_units": repr(source.profit_loss_units),
                "legs": [leg.to_dict() for leg in graded], "reason": ""}

    def grade_stored_legs(self, run: dict, scores: dict[str, dict[str, int]]) -> list[dict]:
        """Read-only results display using frozen lines and the pinned leg grader."""
        verify_model()
        displayed = []
        for leg in run["legs"]:
            score = scores.get(leg["game_id"])
            result = "PENDING"
            if score is not None:
                market = next((row for row in run["slate"]["rows"]
                               if row["team"] == leg["team"] and row["kickoff"] == leg["kickoff"]), None)
                if market is None or leg["team"] not in {market.get("home_team"), market.get("away_team")} or not leg.get("teased_spread"):
                    result = "REQUIRES_REVIEW"
                else:
                    margin = (score["home"] - score["away"]) if leg["team"] == market["home_team"] else (score["away"] - score["home"])
                    result = grade_teased_leg(margin, leg["teased_spread"]).value
            displayed.append({"run_id": run["run_id"], "game_id": leg["game_id"],
                              "leg_id": leg["leg_id"], "team": leg["team"],
                              "spread": leg["spread"], "teased_spread": leg["teased_spread"],
                              "result": result, "away_score": score["away"] if score else None,
                              "home_score": score["home"] if score else None})
        return displayed

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
