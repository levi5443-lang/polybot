"""
consensus_logic.py — shared data models + consensus computation.
Used by both consensus_bot.py (alerting) and webapp/main.py (dashboard).
"""

from dataclasses import dataclass, field

MIN_POSITION_SIZE_USD = 100
CONSENSUS_THRESHOLD = 5


@dataclass
class Position:
    wallet: str
    market_id: str
    market_question: str
    outcome: str
    size_usd: float


@dataclass
class ConsensusSignal:
    market_id: str
    market_question: str
    outcome: str
    agreeing_wallets: list = field(default_factory=list)
    total_size_usd: float = 0.0

    @property
    def count(self) -> int:
        return len(self.agreeing_wallets)

    def to_dict(self, total_tracked: int) -> dict:
        return {
            "market_id": self.market_id,
            "market_question": self.market_question,
            "outcome": self.outcome,
            "agree_count": self.count,
            "total_tracked": total_tracked,
            "total_size_usd": round(self.total_size_usd, 2),
        }


def parse_positions(wallet: str, raw: list[dict], min_size: float = MIN_POSITION_SIZE_USD) -> list[Position]:
    out = []
    for p in raw:
        try:
            size_usd = float(p.get("currentValue") or p.get("size", 0))
        except (TypeError, ValueError):
            size_usd = 0.0
        if size_usd < min_size:
            continue
        out.append(Position(
            wallet=wallet,
            market_id=p.get("conditionId") or p.get("marketId", "unknown"),
            market_question=p.get("title") or p.get("question", "unknown market"),
            outcome=p.get("outcome", "unknown"),
            size_usd=size_usd,
        ))
    return out


def compute_consensus(all_positions: list[Position], threshold: int = CONSENSUS_THRESHOLD) -> list[ConsensusSignal]:
    grouped: dict[tuple, ConsensusSignal] = {}
    for pos in all_positions:
        key = (pos.market_id, pos.outcome)
        if key not in grouped:
            grouped[key] = ConsensusSignal(pos.market_id, pos.market_question, pos.outcome)
        signal = grouped[key]
        if pos.wallet not in signal.agreeing_wallets:
            signal.agreeing_wallets.append(pos.wallet)
            signal.total_size_usd += pos.size_usd
    return [s for s in grouped.values() if s.count >= threshold]
