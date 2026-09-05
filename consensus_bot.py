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
from elite_movers import find_elite_moves, TOP_N_ELITE
from telegram_alert import (
    send_telegram_alert, format_consensus_message, format_early_mover_message,
    format_elite_mover_message,
)

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
    """PAPER MODE ONLY. Live signals never call this — see
    request_live_trade() below, which routes them through
    trade_approval.py's Telegram approve/reject flow instead, since real
    money should never move without your explicit tap."""
    import risk_manager

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
        mode="paper", signal_type="consensus", end_date=signal.end_date
    )


def request_live_trade(signal):
    """LIVE MODE ONLY. Asks for approval via Telegram instead of trading
    immediately — see trade_approval.py for the actual approve/reject
    handling, which happens on a later cycle once you respond."""
    import risk_manager
    import trade_approval

    if risk_manager.has_open_trade(signal.market_id, signal.outcome, mode="live"):
        log.info("Already have an open live position on '%s' [%s] — skipping.",
                  signal.market_question, signal.outcome)
        return

    if not signal.token_id:
        log.error("No token_id available for '%s' [%s] — cannot request approval.",
                  signal.market_question, signal.outcome)
        return

    if trade_approval.has_pending_approval(signal.market_id, signal.outcome):
        log.info("Already have a pending approval request for '%s' [%s] — not asking again.",
                  signal.market_question, signal.outcome)
        return

    trade_approval.request_trade_approval(signal)


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

    if not PAPER_MODE:
        import trade_approval
        trade_approval.process_pending_approvals()

    run_regular_consensus(data)
    run_early_movers(data)
    run_elite_movers(data)
    run_accuracy_check()


def run_accuracy_check():
    import trade_tracker
    import wallet_tracker

    resolved_count = trade_tracker.sync_resolved_trades()
    if resolved_count:
        log.info("Accuracy sync: %d trade(s) resolved this pass.", resolved_count)
    log.info(trade_tracker.format_accuracy_summary(mode="paper"))
    log.info(trade_tracker.format_accuracy_summary(mode="live"))

    wallet_resolved_count = wallet_tracker.sync_wallet_resolutions()
    if wallet_resolved_count:
        log.info("Wallet tracker: %d tracked wallet position(s) resolved this pass.", wallet_resolved_count)

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
            import wallet_tracker
            wallet_records = {
                w: wallet_tracker.format_wallet_record(w, signal.category)
                for w in signal.agreeing_wallets
            }
            wallet_rois = {
                w: wallet_tracker.format_wallet_roi(w)
                for w in signal.agreeing_wallets
            }
            send_telegram_alert(format_consensus_message(
                signal, len(top_wallets_in_cat),
                wallet_ranks=data["wallet_overall_rank"], pool_size=len(data["wallets"]),
                wallet_records=wallet_records, wallet_rois=wallet_rois
            ))
            _alerted_keys.add(key)
        else:
            log.info("(already alerted — skipping duplicate ping)")

        if PAPER_MODE:
            execute_trade(signal)
        else:
            request_live_trade(signal)


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
            import wallet_tracker
            wallet_records = {
                w: wallet_tracker.format_wallet_record(w, signal.category)
                for w in signal.agreeing_wallets
            }
            wallet_rois = {
                w: wallet_tracker.format_wallet_roi(w)
                for w in signal.agreeing_wallets
            }
            send_telegram_alert(format_early_mover_message(
                signal, wallet_ranks=data["wallet_overall_rank"], pool_size=len(data["wallets"]),
                wallet_records=wallet_records, wallet_rois=wallet_rois
            ))
            _alerted_early_mover_keys.add(key)

            if PAPER_MODE:
                execute_trade(signal)
            else:
                request_live_trade(signal)
        else:
            log.info("(already alerted — skipping duplicate ping)")


def run_elite_movers(data):
    """No threshold, no agreement needed — a SINGLE top-5 overall-ranked
    trader taking any new position is worth an alert on its own. Since
    this only ever looks at data["newly_observed_positions"] (positions
    that were, this exact cycle, confirmed new to wallet_tracker's
    ledger), there's no need for a separate dedup set here — a given
    position can only ever appear as "newly observed" once, ever."""
    moves = find_elite_moves(data)
    if not moves:
        log.info("No elite-trader moves this pass (top %d).", TOP_N_ELITE)
        return

    if CATEGORY_FILTER:
        before = len(moves)
        moves = [m for m in moves if m["category"] in CATEGORY_FILTER]
        log.info("Elite movers — category filter %s: %d/%d kept.", CATEGORY_FILTER, len(moves), before)
        if not moves:
            return

    import wallet_tracker
    from consensus_logic import ConsensusSignal

    for move in moves:
        log.info("ELITE MOVE [%s]: #%d trader took '%s' on '%s' ($%.0f)",
                  move["category"], move["rank"], move["outcome"], move["market_question"],
                  move["size_usd"])
        record_str = wallet_tracker.format_wallet_record(move["wallet"], move["category"])
        roi_str = wallet_tracker.format_wallet_roi(move["wallet"])
        send_telegram_alert(format_elite_mover_message(
            move, pool_size=len(data["wallets"]), record_str=record_str, roi_str=roi_str
        ))

        # Elite moves are single-wallet events (no "agreement" needed by
        # definition), but execute_trade/request_live_trade both work off
        # a ConsensusSignal shape — build one so this reuses the exact
        # same tested logic as regular consensus and early movers, rather
        # than duplicating it.
        signal = ConsensusSignal(
            market_id=move["market_id"], market_question=move["market_question"],
            outcome=move["outcome"], agreeing_wallets=[move["wallet"]],
            total_size_usd=move["size_usd"], category=move["category"],
            event_id=move.get("event_id", ""), token_id=move.get("token_id", ""),
            end_date=move.get("end_date", ""), cur_price=move.get("cur_price", 0.0),
        )
        if PAPER_MODE:
            execute_trade(signal)
        else:
            request_live_trade(signal)


def run_forever():
    import shared_storage
    shared_storage.migrate_legacy_local_data()

    log.info("Starting consensus bot. PAPER_MODE=%s, category_threshold=%d, poll=%ds, period=%s",
              PAPER_MODE, CATEGORY_CONSENSUS_THRESHOLD, POLL_INTERVAL_SECONDS, LEADERBOARD_PERIOD)
    while True:
        run_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
