"""
consensus_logic.py — shared data models + consensus computation.
Used by both consensus_bot.py (alerting) and webapp/main.py (dashboard).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

MIN_POSITION_SIZE_USD = 100
CONSENSUS_THRESHOLD = 5


@dataclass
class Position:
    wallet: str
    market_id: str
    market_question: str
    outcome: str
    size_usd: float
    event_id: str = ""
    token_id: str = ""
    end_date: str = ""     # market's expected resolution date, as reported by Polymarket
    cur_price: float = 0.0 # current market price for this specific outcome (0-1)


@dataclass
class ConsensusSignal:
    market_id: str
    market_question: str
    outcome: str
    agreeing_wallets: list = field(default_factory=list)
    total_size_usd: float = 0.0
    category: str = "Uncategorized"
    event_id: str = ""
    token_id: str = ""
    end_date: str = ""
    cur_price: float = 0.0

    @property
    def count(self) -> int:
        return len(self.agreeing_wallets)

    @property
    def expected_return_pct(self) -> float:
        """Return on a winning $1 bought at cur_price: a share always pays
        out $1 if correct, so profit per dollar invested is (1-price)/price.
        Returns 0 if price is unknown or invalid (e.g. 0 or >=1)."""
        if not self.cur_price or self.cur_price <= 0 or self.cur_price >= 1:
            return 0.0
        return round(((1 - self.cur_price) / self.cur_price) * 100, 1)

    def to_dict(self, total_tracked: int) -> dict:
        return {
            "market_id": self.market_id,
            "market_question": self.market_question,
            "outcome": self.outcome,
            "agree_count": self.count,
            "total_tracked": total_tracked,
            "total_size_usd": round(self.total_size_usd, 2),
            "category": self.category,
        }


def _is_market_still_open(p: dict) -> bool:
    """Positions in resolved/expired markets should never count as active
    conviction, no matter what size the raw data reports. endDate is the
    most direct signal for that."""
    end_date = p.get("endDate")
    if not end_date:
        return True  # no date info — don't exclude on a guess
    try:
        end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        return end_dt > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def parse_positions(wallet: str, raw: list[dict], min_size: float = MIN_POSITION_SIZE_USD) -> list[Position]:
    out = []
    for p in raw:
        if not _is_market_still_open(p):
            continue

        # currentValue can be legitimately 0 (position resolved/worthless or
        # already redeemed) — that must NOT fall back to the stale original
        # "size" figure. Only fall back when the field is genuinely absent.
        current_value = p.get("currentValue")
        raw_value = current_value if current_value is not None else p.get("size", 0)
        try:
            size_usd = float(raw_value)
        except (TypeError, ValueError):
            size_usd = 0.0

        if size_usd < min_size:
            continue
        try:
            cur_price = float(p.get("curPrice") or p.get("avgPrice") or 0)
        except (TypeError, ValueError):
            cur_price = 0.0

        out.append(Position(
            wallet=wallet,
            market_id=p.get("conditionId") or p.get("marketId", "unknown"),
            market_question=p.get("title") or p.get("question", "unknown market"),
            outcome=p.get("outcome", "unknown"),
            size_usd=size_usd,
            event_id=str(p.get("eventId") or ""),
            token_id=str(p.get("asset") or ""),
            end_date=str(p.get("endDate") or ""),
            cur_price=cur_price,
        ))
    return out


def compute_consensus(all_positions: list[Position], threshold: int = CONSENSUS_THRESHOLD) -> list[ConsensusSignal]:
    grouped: dict[tuple, ConsensusSignal] = {}
    for pos in all_positions:
        key = (pos.market_id, pos.outcome)
        if key not in grouped:
            grouped[key] = ConsensusSignal(
                pos.market_id, pos.market_question, pos.outcome,
                event_id=pos.event_id, token_id=pos.token_id,
                end_date=pos.end_date, cur_price=pos.cur_price
            )
        signal = grouped[key]
        if pos.wallet not in signal.agreeing_wallets:
            signal.agreeing_wallets.append(pos.wallet)
            signal.total_size_usd += pos.size_usd
    return [s for s in grouped.values() if s.count >= threshold]


def attach_categories(signals: list[ConsensusSignal], category_map: dict[str, str]) -> None:
    """Label each signal in-place using a {event_id: category} map,
    e.g. the output of polymarket_api.fetch_event_categories()."""
    for s in signals:
        s.category = category_map.get(s.event_id, "Uncategorized")
