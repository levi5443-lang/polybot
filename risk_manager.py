"""
risk_manager.py — position sizing and daily loss cap for live execution.

This is the safety layer that sits BETWEEN a consensus signal and an actual
order. It answers two questions every time execute_trade() considers
placing a real order:

  1. How much should this trade be sized at? (POSITION_SIZE_PCT of current
     wallet balance)
  2. Are we allowed to trade at all right now? (has today's realized loss
     already hit DAILY_LOSS_CAP_USD?)

Trades this bot places are recorded to a local JSON ledger (TRADE_LOG_FILE)
so the daily loss check has something to work from — Polymarket's own API
doesn't give a clean "today's realized P&L" figure, so we track our own.

IMPORTANT: this module's math is fully unit-testable and IS tested. What is
NOT tested (because it can't be, without a funded live wallet) is whether
the actual order placement in execution.py behaves as documented against
Polymarket's real CLOB. Treat that part as an unverified first draft.
"""

import json
import os
import logging
from datetime import datetime, timezone

log = logging.getLogger("risk_manager")

POSITION_SIZE_PCT = 0.02      # 2% of wallet balance per trade
DAILY_LOSS_CAP_USD = 100.0    # stop opening new trades once today's realized losses hit this

TRADE_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_log.json")


def _load_trade_log() -> list[dict]:
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_trade_log(trades: list[dict]) -> None:
    try:
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except OSError as e:
        log.error("Could not save trade log: %s", e)


def get_position_size_usd(wallet_balance_usd: float) -> float:
    """2% of current wallet balance. Recomputed fresh each trade — NOT 2%
    of some fixed starting amount — so it naturally shrinks if the bankroll
    is down and grows if it's up."""
    return round(wallet_balance_usd * POSITION_SIZE_PCT, 2)


def record_trade_open(market_id: str, market_question: str, outcome: str,
                       size_usd: float, entry_price: float, category: str) -> None:
    trades = _load_trade_log()
    trades.append({
        "market_id": market_id,
        "market_question": market_question,
        "outcome": outcome,
        "category": category,
        "size_usd": size_usd,
        "entry_price": entry_price,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "realized_pnl": None,
        "closed_at": None,
    })
    _save_trade_log(trades)
    log.info("Recorded trade open: %s [%s] $%.2f @ %.3f", market_question, outcome, size_usd, entry_price)


def record_trade_closed(market_id: str, outcome: str, realized_pnl: float) -> None:
    """Call this once a position resolves (win or loss) to close out its
    ledger entry so the daily loss cap can account for it."""
    trades = _load_trade_log()
    for t in trades:
        if t["market_id"] == market_id and t["outcome"] == outcome and t["status"] == "open":
            t["status"] = "closed"
            t["realized_pnl"] = realized_pnl
            t["closed_at"] = datetime.now(timezone.utc).isoformat()
    _save_trade_log(trades)


def todays_realized_loss_usd() -> float:
    """Sum of realized losses (negative P&L only) on trades closed today
    (UTC calendar day). Wins don't offset this — the cap is specifically
    about limiting how much can be LOST in a day, not net P&L."""
    trades = _load_trade_log()
    today = datetime.now(timezone.utc).date()
    total_loss = 0.0
    for t in trades:
        if t["status"] != "closed" or t["closed_at"] is None:
            continue
        closed_date = datetime.fromisoformat(t["closed_at"]).date()
        if closed_date != today:
            continue
        pnl = t.get("realized_pnl") or 0
        if pnl < 0:
            total_loss += abs(pnl)
    return total_loss


def daily_loss_cap_reached() -> bool:
    loss = todays_realized_loss_usd()
    reached = loss >= DAILY_LOSS_CAP_USD
    if reached:
        log.warning("Daily loss cap reached: $%.2f realized loss today (cap=$%.2f). Blocking new trades.",
                     loss, DAILY_LOSS_CAP_USD)
    return reached
