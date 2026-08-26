"""
Polymarket Top-Trader Consensus Bot — alerting loop.
Uses shared consensus_logic.py so the dashboard and this bot never drift apart.
"""

import time
import logging

import requests

from polymarket_api import fetch_leaderboard, fetch_positions, fetch_market_categories, polite_sleep
from consensus_logic import parse_positions, compute_consensus, attach_categories, CONSENSUS_THRESHOLD
from telegram_alert import send_telegram_alert, format_consensus_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("consensus_bot")

NUM_TOP_TRADERS = 20
LEADERBOARD_PERIOD = "30d"    # "1d" | "7d" | "30d" | "all"
POLL_INTERVAL_SECONDS = 300
PAPER_MODE = True

# Only alert on these categories. Empty list = no filter (alert on everything).
# Labels come straight from Polymarket's own tags, e.g.:
# "Politics", "Sports", "Crypto", "Pop Culture", "Business", "Science"
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
    log.info("Fetching leaderboard (top %d, period=%s)...", NUM_TOP_TRADERS, LEADERBOARD_PERIOD)
    try:
        wallets = fetch_leaderboard(period=LEADERBOARD_PERIOD, limit=NUM_TOP_TRADERS)
    except requests.RequestException as e:
        log.error("Failed to fetch leaderboard: %s", e)
        return

    log.info("Tracking %d wallets.", len(wallets))

    all_positions = []
    for wallet in wallets:
        try:
            raw = fetch_positions(wallet)
            all_positions.extend(parse_positions(wallet, raw))
        except requests.RequestException as e:
            log.warning("Failed to fetch positions for %s: %s", wallet, e)
        polite_sleep(0.2)

    signals = compute_consensus(all_positions, threshold=CONSENSUS_THRESHOLD)
    if not signals:
        log.info("No consensus signals this pass (threshold=%d).", CONSENSUS_THRESHOLD)
        return

    try:
        category_map = fetch_market_categories([s.market_id for s in signals])
        attach_categories(signals, category_map)
    except requests.RequestException as e:
        log.warning("Failed to fetch market categories (signals will be Uncategorized): %s", e)

    if CATEGORY_FILTER:
        before = len(signals)
        signals = [s for s in signals if s.category in CATEGORY_FILTER]
        log.info("Category filter %s: %d/%d signals kept.", CATEGORY_FILTER, len(signals), before)
        if not signals:
            return

    for signal in sorted(signals, key=lambda s: s.count, reverse=True):
        key = (signal.market_id, signal.outcome)
        log.info("CONSENSUS [%s]: %d/%d on '%s' for '%s' ($%.0f)",
                  signal.category, signal.count, len(wallets), signal.outcome, signal.market_question,
                  signal.total_size_usd)

        if key not in _alerted_keys:
            send_telegram_alert(format_consensus_message(signal, len(wallets)))
            _alerted_keys.add(key)
        else:
            log.info("(already alerted — skipping duplicate ping)")

        execute_trade(signal)


def run_forever():
    log.info("Starting consensus bot. PAPER_MODE=%s, threshold=%d, poll=%ds, period=%s",
              PAPER_MODE, CONSENSUS_THRESHOLD, POLL_INTERVAL_SECONDS, LEADERBOARD_PERIOD)
    while True:
        run_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_once()
