"""
risk_manager.py — position sizing for live execution.

This is the safety layer that sits BETWEEN a consensus signal and an actual
order. It answers: how much should this trade be sized at? (POSITION_SIZE_PCT
of current wallet balance, never pushing total open live exposure past
TOTAL_EXPOSURE_CAP_PCT).

Trades this bot places are recorded to a local JSON ledger (TRADE_LOG_FILE)
so exposure can be computed from real open positions rather than guessed.

There used to also be a daily realized-loss cap (DAILY_LOSS_CAP_USD) that
blocked new live trades once today's realized losses hit $100 — removed
at Levi's request (2026-09-08). The only remaining per-trade risk controls
are position sizing and the total exposure cap below.

BASIS CONFIRMED (2026-10-13, Levi's explicit answer): both
POSITION_SIZE_PCT and TOTAL_EXPOSURE_CAP_PCT are measured against
TRADABLE CASH ONLY, not total account equity including open positions.
The 2% figure is the full dollar amount at risk on a trade, not sizing
built around a planned stop-loss.

POLYBOT EXPOSURE NOTE (2026-10-13): get_current_live_exposure_usd() below
sums size_usd across OUR OWN local ledger of trades THIS bot has placed.
That's correct for execution.py's self-custodied path, where this ledger
is the only record there is. For the Polybot-backed path
(polybot_execution.py), this is NOT enough on its own — the Polybot
account may hold positions/reservations this bot never placed (the
account had ~$62.82 in positions before this integration existed), so
compute_trade_size_usd() below now accepts an optional
current_exposure_usd override; trade_approval.py passes Polybot's own
live committed figure (positionsValue + reservedUsdTotal) when using
that backend, rather than relying on this local ledger alone.

IMPORTANT: this module's math is fully unit-testable and IS tested. What is
NOT tested (because it can't be, without a funded live wallet) is whether
the actual order placement in execution.py behaves as documented against
Polymarket's real CLOB. Treat that part as an unverified first draft.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("risk_manager")

POSITION_SIZE_PCT = 0.02      # 2% of tradable-cash balance per trade — full dollar risk, not stop-loss sizing
TOTAL_EXPOSURE_CAP_PCT = 0.26 # never have more than this fraction of tradable cash across ALL open live positions combined

# Shared across both services (worker + dashboard) — this is what makes
# /history and /accuracy on Telegram (served by the dashboard) able to see
# trades the worker actually placed. A local-only file would be invisible
# across that service boundary.
from shared_storage import get_json, set_json

TRADE_LOG_KEY = "trade_log.json"


def _load_trade_log() -> list[dict]:
    return get_json(TRADE_LOG_KEY, [])


def _save_trade_log(trades: list[dict]) -> None:
    set_json(TRADE_LOG_KEY, trades)


def reset_paper_trades() -> int:
    """Wipes every paper-mode entry from the trade log — open or closed —
    while leaving any live (real-money) trades completely untouched.
    Returns how many paper entries were removed. This is intentionally
    NOT automatic; it only runs when explicitly triggered (e.g. the
    /resetpaper Telegram command), since wiping trade history is
    something you should control precisely, not something that happens
    as a side effect of a deploy."""
    trades = _load_trade_log()
    remaining = [t for t in trades if t.get("mode") != "paper"]
    removed_count = len(trades) - len(remaining)
    _save_trade_log(remaining)
    log.info("Reset: removed %d paper trade(s) from the ledger, %d live trade(s) preserved.",
              removed_count, len(remaining))
    return removed_count


def get_position_size_usd(wallet_balance_usd: float) -> float:
    """2% of current wallet balance. Recomputed fresh each trade — NOT 2%
    of some fixed starting amount — so it naturally shrinks if the bankroll
    is down and grows if it's up."""
    return round(wallet_balance_usd * POSITION_SIZE_PCT, 2)


def get_current_live_exposure_usd() -> float:
    """Sum of size_usd across every OPEN, live-mode trade in OUR OWN local
    ledger — how much THIS BOT has committed across all live positions it
    has placed. See the POLYBOT EXPOSURE NOTE in the module docstring for
    why this alone isn't the right number for the Polybot-backed execution
    path."""
    trades = _load_trade_log()
    return sum(t["size_usd"] for t in trades if t.get("mode") == "live" and t["status"] == "open")


def compute_trade_size_usd(wallet_balance_usd: float, current_exposure_usd: float = None) -> float:
    """The size a new LIVE trade should use: 2% of current balance, but
    never allowed to push total open live exposure past
    TOTAL_EXPOSURE_CAP_PCT of the wallet.

    current_exposure_usd: pass this explicitly to use a real, authoritative
    exposure figure (e.g. polybot_execution.get_live_committed_usd()) instead
    of this module's own local ledger — added 2026-10-13 for the Polybot
    execution path, additive and backward-compatible: omitted, this behaves
    exactly as before (falls back to get_current_live_exposure_usd()).

    Returns 0.0 if the 2% trade wouldn't fit under the cap at all — trades
    are SKIPPED entirely in that case, never silently downsized, so every
    live position stays a consistent, predictable 2% risk rather than some
    smaller leftover amount that's harder to reason about.
    """
    proposed = get_position_size_usd(wallet_balance_usd)
    current_exposure = (
        current_exposure_usd if current_exposure_usd is not None
        else get_current_live_exposure_usd()
    )
    cap_usd = TOTAL_EXPOSURE_CAP_PCT * wallet_balance_usd

    if current_exposure + proposed > cap_usd:
        log.info("Trade size blocked by exposure cap: current $%.2f + proposed $%.2f "
                  "would exceed %.0f%% cap ($%.2f of $%.2f balance).",
                  current_exposure, proposed, TOTAL_EXPOSURE_CAP_PCT * 100, cap_usd, wallet_balance_usd)
        return 0.0

    return proposed


def has_open_trade(market_id: str, outcome: str, mode: str = "live") -> bool:
    """True if there's already an open ledger entry for this exact
    market+outcome+mode. Prevents re-trading (or re-logging a paper trade
    for) the same ongoing signal every single poll cycle."""
    trades = _load_trade_log()
    return any(
        t["market_id"] == market_id and t["outcome"] == outcome
        and t.get("mode", "live") == mode and t["status"] == "open"
        for t in trades
    )


def has_ever_traded(market_id: str, outcome: str, mode: str = "live") -> bool:
    """True if this exact market+outcome+mode has EVER been logged,
    regardless of current status (open OR closed). This is the right
    check before opening a brand-new trade — has_open_trade alone isn't
    enough, because the moment a trade resolves it becomes 'closed', and
    a signal that keeps re-firing on an already-DECIDED market would slip
    right past an open-only check and get re-traded every single cycle
    forever (a real bug this fixed: a concluded match kept getting
    re-opened and instantly re-resolved, over and over, because nothing
    remembered it had already been traded once it closed). Markets never
    un-resolve on Polymarket, so once traded, a market+outcome is done —
    permanently — regardless of status."""
    trades = _load_trade_log()
    return any(
        t["market_id"] == market_id and t["outcome"] == outcome
        and t.get("mode", "live") == mode
        for t in trades
    )


def record_trade_open(market_id: str, market_question: str, outcome: str,
                       size_usd: float, entry_price: float, category: str,
                       mode: str = "live", signal_type: str = "consensus",
                       end_date: str = "", agreement_count: int = None,
                       avg_wallet_roi_pct: float = None) -> None:
    """mode is 'live' (real money) or 'paper' (tracked for accuracy only,
    no money moved). signal_type is 'consensus' or 'early_mover' — lets
    accuracy be broken down by which kind of signal produced the trade.
    end_date is Polymarket's own expected-resolution date for the market,
    carried through so /positions can show when a decision is expected.

    agreement_count/avg_wallet_roi_pct (added 2026-10-13, Levi's request):
    pure instrumentation, optional, default None — captures how strong the
    signal looked AT THE MOMENT it was traded (how many wallets agreed,
    what their average realized ROI was). Doesn't change any trading
    behavior by itself; it's what makes it possible to later ask, from
    real resolved data, whether e.g. 6-of-10 agreement actually
    outperforms the 4-of-10 floor, instead of guessing."""
    trades = _load_trade_log()
    trades.append({
        "market_id": market_id,
        "market_question": market_question,
        "outcome": outcome,
        "category": category,
        "size_usd": size_usd,
        "entry_price": entry_price,
        "mode": mode,
        "signal_type": signal_type,
        "end_date": end_date,
        "agreement_count": agreement_count,
        "avg_wallet_roi_pct": avg_wallet_roi_pct,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "realized_pnl": None,
        "closed_at": None,
    })
    _save_trade_log(trades)
    log.info("Recorded %s trade open: %s [%s] $%.2f @ %.3f",
              mode, market_question, outcome, size_usd, entry_price)


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


# todays_realized_loss_usd() / daily_loss_cap_reached() were removed here
# along with DAILY_LOSS_CAP_USD (2026-09-08, Levi's request) — there is no
# longer a daily realized-loss cap blocking new live trades. Position
# sizing and the total exposure cap above are the only remaining controls.
