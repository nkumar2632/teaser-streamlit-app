"""App identity for a model run; operational status is per strategy."""

from dataclasses import dataclass

STATUSES = frozenset({"LIVE", "PAPER", "CHALLENGER"})
BET_TYPES = frozenset({"TEASER", "ATS", "ML", "ATS_PARLAY", "ML_PARLAY"})


@dataclass(frozen=True)
class Strategy:
    league: str
    bet_type: str
    model_version: str
    status: str

    def __post_init__(self) -> None:
        if self.league not in {"NFL", "CFB"}:
            raise ValueError("unsupported league")
        if self.bet_type not in BET_TYPES or not self.model_version or self.status not in STATUSES:
            raise ValueError("invalid strategy identity")

    def to_dict(self) -> dict[str, str]:
        return {"league": self.league, "bet_type": self.bet_type,
                "model_version": self.model_version, "status": self.status}


NFL_TEASER = Strategy("NFL", "TEASER", "teaser_v1.0", "LIVE")
CFB_TEASER = Strategy("CFB", "TEASER", "teaser_v1.0", "PAPER")
