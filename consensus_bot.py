"""
Polymarket Top-Trader Consensus Bot — alerting loop.

Tracks a SEPARATE top-10 leaderboard per category (Sports, Politics,
Weather, Crypto, etc.), each drawn from within a wide overall candidate
pool — see category_leaderboard.py for how that ranking is built.
"""

import time
import logging

import requests

from category_leaderboard import (
    build_category_data,
    compute_category_consensus,
    CANDIDATE_POOL_SIZE,
    TOP_N_PER_CATEGORY,
    CATEGORY_CONSENSUS_THRESHOLD,
)
from early_movers import find_early_movers, MIN_EARLY_MOVERS
from telegram_alert import send_telegram_alert, format_consensus_message, format_early_mover_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("consensus_bot")

LEADERBOARD_PERIOD = "30d"    # "1d" | "7d" | "30d" | "all"
POLL_INTERVAL_SECONDS = 300   # a full pass over CANDIDATE_POOL_SIZE wallets takes longer
                               # than the old 20-wallet version — leave headroom here
PAPER_MODE = True

# Only alert on these categories. Empty list = no filter (alert on everything).
CATEGORY_FILTER: list[str] = []

_alerted_keys: set[tuple] = set()
_alerted_early_mover_keys: set[tuple] = set()


def execute_trade(signal):
    import risk_manager

    if PAPER_MODE:
        if risk_manager.has_open_trade(signal.market_id, signal.outcome, mode="paper"):
            log.info("[PAPER TRADE] Already tracking '%s' [%s] — not re-logging.",
                      signal.market_question, signal.outcome)
            return
        log.info("[PAPER TRADE] Would take '%s' on '%s' (%d agree, $%.0f)",
                  signal.outcome, signal.market_question, signal.count, signal.total_size_usd)
        # Paper trades are tracked at a fixed notional $1 "unit" — the
        # accuracy math only cares whether the prediction was right, not
        # a dollar amount, since no money actually moves in paper mode.
        risk_manager.record_trade_open(
            signal.market_id, signal.market_question, signal.outcome,
            size_usd=1.0, entry_price=0, category=signal.category,
            mode="paper", signal_type="consensus"
        )
        return

    # Real execution — imported lazily so paper mode never requires
    # py-clob-client to be installed at all.
    import execution

    if risk_manager.has_open_trade(signal.market_id, signal.outcome, mode="live"):
        log.info("Already have an open live position on '%s' [%s] — not placing another order.",
                  signal.market_question, signal.outcome)
        return

    if not signal.token_id:
        log.error("No token_id available for '%s' [%s] — cannot place a real order, skipping.",
                  signal.market_question, signal.outcome)
        return

    if risk_manager.daily_loss_cap_reached():
        log.warning("Skipping trade on '%s' — daily loss cap already reached.", signal.market_question)
        return

    try:
        balance = execution.get_wallet_balance_usd()
    except Exception as e:
        log.error("Could not fetch wallet balance, skipping trade: %s", e)
        return

    size_usd = risk_manager.get_position_size_usd(balance)
    if size_usd < 1.0:
        log.warning("Computed position size ($%.2f) is too small to trade, skipping.", size_usd)
        return

    log.info("LIVE TRADE: '%s' [%s] — sizing $%.2f (2%% of $%.2f balance)",
              signal.market_question, signal.outcome, size_usd, balance)

    try:
        resp = execution.place_market_buy(signal.token_id, size_usd)
    except Exception as e:
        log.error("Order placement failed for '%s': %s", signal.market_question, e)
        return

    entry_price = resp.get("price", 0) if isinstance(resp, dict) else 0
    risk_manager.record_trade_open(
        signal.market_id, signal.market_question, signal.outcome,
        size_usd, entry_price, signal.category
    )


def run_once():
    log.info("Building per-category leaderboards (candidate pool=%d, top %d/category)...",
              CANDIDATE_POOL_SIZE, TOP_N_PER_CATEGORY)
    try:
        data = build_category_data(period=LEADERBOARD_PERIOD)
    except requests.RequestException as e:
        log.error("Failed to build category data: %s", e)
        return

    num_categories = len(data["category_top_wallets"])
    log.info("Pulled %d candidate wallets, found %d categories.",
              len(data["wallets"]), num_categories)

    run_regular_consensus(data)
    run_early_movers(data)
    run_accuracy_check()


def run_accuracy_check():
    import trade_tracker
    resolved_count = trade_tracker.sync_resolved_trades()
    if resolved_count:
        log.info("Accuracy sync: %d trade(s) resolved this pass.", resolved_count)
    log.info(trade_tracker.format_accuracy_summary(mode="paper"))
    log.info(trade_tracker.format_accuracy_summary(mode="live"))

    sent = trade_tracker.maybe_send_daily_digest(send_telegram_alert)
    if sent:
        log.info("Sent daily accuracy digest to Telegram.")


def run_regular_consensus(data):
    signals = compute_category_consensus(data, threshold=CATEGORY_CONSENSUS_THRESHOLD)
    if not signals:
        log.info("No consensus signals this pass (threshold=%d per category).",
                  CATEGORY_CONSENSUS_THRESHOLD)
        return

    if CATEGORY_FILTER:
        before = len(signals)
        signals = [s for s in signals if s.category in CATEGORY_FILTER]
        log.info("Category filter %s: %d/%d signals kept.", CATEGORY_FILTER, len(signals), before)
        if not signals:
            return

    for signal in sorted(signals, key=lambda s: s.count, reverse=True):
        key = (signal.market_id, signal.outcome, signal.category)
        top_wallets_in_cat = data["category_top_wallets"].get(signal.category, [])
        log.info("CONSENSUS [%s]: %d/%d on '%s' for '%s' ($%.0f)",
                  signal.category, signal.count, len(top_wallets_in_cat),
                  signal.outcome, signal.market_question, signal.total_size_usd)

        if key not in _alerted_keys:
            send_telegram_alert(format_consensus_message(
                signal, len(top_wallets_in_cat),
                wallet_ranks=data["wallet_overall_rank"], pool_size=len(data["wallets"])
            ))
            _alerted_keys.add(key)
        else:
            log.info("(already alerted — skipping duplicate ping)")

        execute_trade(signal)


def run_early_movers(data):
    signals = find_early_movers(data["all_positions"], data["category_map"])
    if not signals:
        log.info("No early-mover signals this pass (threshold=%d).", MIN_EARLY_MOVERS)
        return

    if CATEGORY_FILTER:
        before = len(signals)
        signals = [s for s in signals if s.category in CATEGORY_FILTER]
        log.info("Early movers — category filter %s: %d/%d kept.", CATEGORY_FILTER, len(signals), before)
        if not signals:
            return

    for signal in sorted(signals, key=lambda s: s.count, reverse=True):
        key = (signal.market_id, signal.outcome, signal.category)
        log.info("EARLY MOVER [%s]: %d tracked traders already in '%s' for '%s' ($%.0f)",
                  signal.category, signal.count, signal.outcome, signal.market_question,
                  signal.total_size_usd)

        if key not in _alerted_early_mover_keys:
            send_telegram_alert(format_early_mover_message(
                signal, wallet_ranks=data["wallet_overall_rank"], pool_size=len(data["wallets"])
            ))
            _alerted_early_mover_keys.add(key)
        else:
            log.info("(already alerted — skipping duplicate ping)")


def run_forever():
    log.info("Starting consensus bot. PAPER_MODE=%s, category_threshold=%d, poll=%ds, period=%s",
              PAPER_MODE, CATEGORY_CONSENSUS_THRESHOLD, POLL_INTERVAL_SECONDS, LEADERBOARD_PERIOD)
    while True:
        run_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
