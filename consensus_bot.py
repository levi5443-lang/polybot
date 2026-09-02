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
from telegram_alert import send_telegram_alert, format_consensus_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("consensus_bot")

LEADERBOARD_PERIOD = "30d"    # "1d" | "7d" | "30d" | "all"
POLL_INTERVAL_SECONDS = 300   # a full pass over CANDIDATE_POOL_SIZE wallets takes longer
                               # than the old 20-wallet version — leave headroom here
PAPER_MODE = True

# Only alert on these categories. Empty list = no filter (alert on everything).
CATEGORY_FILTER: list[str] = []

_alerted_keys: set[tuple] = set()


def execute_trade(signal):
    if PAPER_MODE:
        log.info("[PAPER TRADE] Would take '%s' on '%s' (%d agree, $%.0f)",
                  signal.outcome, signal.market_question, signal.count, signal.total_size_usd)
    else:
        raise NotImplementedError(
            "Live execution not implemented — wire up py-clob-client + risk logic first."
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
            send_telegram_alert(format_consensus_message(signal, len(top_wallets_in_cat)))
            _alerted_keys.add(key)
        else:
            log.info("(already alerted — skipping duplicate ping)")

        execute_trade(signal)


def run_forever():
    log.info("Starting consensus bot. PAPER_MODE=%s, category_threshold=%d, poll=%ds, period=%s",
              PAPER_MODE, CATEGORY_CONSENSUS_THRESHOLD, POLL_INTERVAL_SECONDS, LEADERBOARD_PERIOD)
    while True:
        run_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
