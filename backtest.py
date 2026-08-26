"""
backtest.py — did top-trader consensus actually predict outcomes?

Approach
--------
/positions only shows CURRENT state, which is useless for the past. So for
each resolved market, we reconstruct each top trader's position from their
raw trade history (/trades), summed up to a cutoff time before the market
closed (e.g. 24h before resolution) — then check whether the consensus side
at that cutoff matched the actual resolved outcome.

This tells you whether the signal has any predictive value BEFORE you ever
risk capital on it. Treat the output as a starting point, not proof — sample
size, market selection, and lookback window all bias the result. Run this
with a few different LOOKBACK_HOURS and market samples before trusting it.

Limitations to know about:
- Trade history endpoints are often paginated/rate-limited; large backtests
  will need retry/backoff logic added.
- "Top traders" here uses the CURRENT leaderboard to look at PAST markets —
  that's survivorship bias (today's winners weren't necessarily in your
  tracked set back then). A more rigorous backtest would use a leaderboard
  snapshot from before each market resolved, which Polymarket's public API
  may not expose historically.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from polymarket_api import fetch_leaderboard, fetch_trades, fetch_resolved_markets, polite_sleep

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

LOOKBACK_HOURS = 24          # how long before resolution to snapshot positions
CONSENSUS_THRESHOLD = 5      # same semantics as the live bot
NUM_TOP_TRADERS = 20
MARKETS_TO_TEST = 50


@dataclass
class BacktestResult:
    market_question: str
    predicted_outcome: str
    actual_outcome: str
    consensus_count: int
    correct: bool


def reconstruct_position_at(wallet_trades: list[dict], cutoff_ts: float) -> dict:
    """Sum signed trade size by outcome, using only fills before cutoff_ts.
    Returns {outcome: net_size_usd}. Positive = net long that outcome.
    """
    net = {}
    for t in wallet_trades:
        ts = t.get("timestamp") or t.get("createdAt")
        try:
            trade_ts = float(ts)
        except (TypeError, ValueError):
            continue
        if trade_ts > cutoff_ts:
            continue

        outcome = t.get("outcome", "unknown")
        side = t.get("side", "BUY")
        size = float(t.get("size", 0) or 0)
        price = float(t.get("price", 0) or 0)
        usd = size * price
        signed = usd if side.upper() == "BUY" else -usd
        net[outcome] = net.get(outcome, 0) + signed
    return net


def run_backtest():
    log.info("Fetching current top-trader leaderboard as our tracked set...")
    wallets = fetch_leaderboard(period="all", limit=NUM_TOP_TRADERS)
    log.info("Tracking %d wallets (NOTE: survivorship bias — see module docstring).", len(wallets))

    log.info("Fetching %d resolved markets to test against...", MARKETS_TO_TEST)
    markets = fetch_resolved_markets(limit=MARKETS_TO_TEST)

    results: list[BacktestResult] = []

    for market in markets:
        condition_id = market.get("conditionId") or market.get("id")
        question = market.get("question", "unknown market")
        actual_outcome = market.get("resolvedOutcome") or market.get("outcome")
        resolved_at_raw = market.get("closedTime") or market.get("endDate")

        if not condition_id or not actual_outcome or not resolved_at_raw:
            continue  # skip incomplete records

        try:
            resolved_dt = datetime.fromisoformat(str(resolved_at_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        cutoff_dt = resolved_dt - timedelta(hours=LOOKBACK_HOURS)
        cutoff_ts = cutoff_dt.replace(tzinfo=timezone.utc).timestamp()

        outcome_votes: dict[str, int] = {}
        for wallet in wallets:
            try:
                trades = fetch_trades(wallet=wallet, market=condition_id)
            except Exception as e:
                log.warning("Skipping wallet %s for market %s: %s", wallet, question, e)
                continue
            polite_sleep(0.1)

            net_by_outcome = reconstruct_position_at(trades, cutoff_ts)
            if not net_by_outcome:
                continue
            # this wallet's dominant side at the cutoff
            top_outcome = max(net_by_outcome, key=lambda o: abs(net_by_outcome[o]))
            if net_by_outcome[top_outcome] > 0:  # only count net-long positions as "agreement"
                outcome_votes[top_outcome] = outcome_votes.get(top_outcome, 0) + 1

        if not outcome_votes:
            continue

        predicted_outcome = max(outcome_votes, key=outcome_votes.get)
        consensus_count = outcome_votes[predicted_outcome]

        if consensus_count < CONSENSUS_THRESHOLD:
            continue  # no signal would have fired here — skip, don't count as a miss

        results.append(
            BacktestResult(
                market_question=question,
                predicted_outcome=predicted_outcome,
                actual_outcome=str(actual_outcome),
                consensus_count=consensus_count,
                correct=(predicted_outcome == str(actual_outcome)),
            )
        )

    report(results)


def report(results: list[BacktestResult]):
    if not results:
        log.info("No markets crossed the consensus threshold — nothing to evaluate. "
                  "Try lowering CONSENSUS_THRESHOLD or increasing MARKETS_TO_TEST.")
        return

    correct = sum(1 for r in results if r.correct)
    total = len(results)
    log.info("=" * 60)
    log.info("BACKTEST RESULTS  (lookback=%dh, threshold=%d, sample=%d markets)",
              LOOKBACK_HOURS, CONSENSUS_THRESHOLD, total)
    log.info("Signal fired on %d/%d tested markets. Accuracy: %.1f%% (%d/%d correct)",
              total, MARKETS_TO_TEST, 100 * correct / total, correct, total)
    log.info("=" * 60)
    for r in results:
        mark = "✓" if r.correct else "✗"
        log.info("%s [%d agree] predicted '%s' actual '%s' — %s",
                  mark, r.consensus_count, r.predicted_outcome, r.actual_outcome, r.market_question)


if __name__ == "__main__":
    run_backtest()
