"""
backtest.py — did the CURRENT category-based consensus system actually
predict outcomes historically?

This mirrors the live system's real logic as closely as possible:
- Same candidate pool discovery (Polymarket's own leaderboard)
- Same category segmentation (event tags)
- Same per-category top-10-by-position-size ranking
- Same 4/10 (CATEGORY_CONSENSUS_THRESHOLD) agreement rule

Known, unavoidable limitations — read before trusting the output:

1. SURVIVORSHIP BIAS: we can only pull TODAY's leaderboard, not a
   historical snapshot of who was "top" back when each test market
   resolved. Today's winners weren't necessarily on our radar then.
   Polymarket's public API doesn't expose historical leaderboards, so
   this bias is structural and can't be fully removed.

2. ROI-based reranking (which the LIVE system switches a wallet to once
   it has enough resolved history) CANNOT be backtested here — it
   depends on OUR OWN accumulated tracking data, which didn't exist in
   the past. This backtest ranks wallets by category position size only,
   the same fallback the live system itself uses for any wallet without
   enough history yet.

3. Category assignment uses CURRENT event tags applied retroactively — a
   reasonable assumption (categories rarely change), but not guaranteed
   accurate for every historical event.

4. Only tests the REGULAR CONSENSUS signal type. Early Movers depend on
   knowing exactly when OUR system first discovered each historical
   event, which isn't reconstructable after the fact. Elite Movers
   depend on the (also non-reconstructable) ROI ranking. Neither is
   backtested here.

5. ⚠️ UNVERIFIED FIELD NAMES: this fetches each wallet's FULL trade
   history once (not filtered to one market via the API, unlike the
   original version of this script) and then needs to match each trade
   back to a specific market/event locally. The original script never
   needed to read a market/event ID off a trade record directly — it
   filtered via the API's own query parameters instead. I could not
   verify the exact field name Polymarket's /trades endpoint uses for
   this (candidates tried below: 'conditionId', 'market', 'eventId') in
   this environment. If results look empty/wrong, check
   TRADE_MARKET_ID_FIELDS and TRADE_EVENT_ID_FIELDS below against a
   real response first.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from polymarket_api import (
    fetch_leaderboard, fetch_trades, fetch_resolved_markets,
    fetch_event_categories, polite_sleep,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

LOOKBACK_HOURS = 24                 # how long before resolution to snapshot positions
CATEGORY_CONSENSUS_THRESHOLD = 4    # matches the live system
TOP_N_PER_CATEGORY = 10             # matches the live system
CANDIDATE_POOL_SIZE = 50            # matches what the live leaderboard actually returns
MARKETS_TO_TEST = 40
EXCLUDED_CATEGORIES = ["Crypto"]    # matches the live system

# See limitation #5 above — try each candidate field name in order until
# one produces a non-empty value.
TRADE_MARKET_ID_FIELDS = ["conditionId", "market", "asset"]
TRADE_EVENT_ID_FIELDS = ["eventId", "event_id"]


@dataclass
class BacktestResult:
    market_question: str
    category: str
    predicted_outcome: str
    actual_outcome: str
    consensus_count: int
    correct: bool


def _get_first(d: dict, keys: list[str]):
    for k in keys:
        if d.get(k):
            return d[k]
    return None


def fetch_all_wallet_trades(wallets: list[str]) -> dict[str, list[dict]]:
    """Fetch each wallet's FULL trade history ONCE, up front — reused
    across every market's cutoff reconstruction, instead of re-fetching
    per market like the original version did (which doesn't scale once
    you're testing many markets against many wallets)."""
    all_trades = {}
    for i, w in enumerate(wallets, 1):
        try:
            all_trades[w] = fetch_trades(wallet=w, limit=500)
        except Exception as e:
            log.warning("Could not fetch trades for %s: %s", w, e)
            all_trades[w] = []
        polite_sleep(0.15)
        if i % 10 == 0:
            log.info("  ...fetched trade history for %d/%d wallets", i, len(wallets))
    return all_trades


def reconstruct_net_position(trades: list[dict], market_id: str, cutoff_ts: float) -> dict:
    """Sum signed trade size by outcome for ONE market, using only fills
    before cutoff_ts. Returns {outcome: net_size_usd}."""
    net = {}
    for t in trades:
        if _get_first(t, TRADE_MARKET_ID_FIELDS) != market_id:
            continue
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


def reconstruct_category_exposure(trades: list[dict], category_map: dict, target_category: str,
                                    cutoff_ts: float) -> float:
    """Total net-long exposure a wallet had across ALL markets in a given
    category, at a cutoff time — same concept the live category
    leaderboard uses to rank wallets within a category."""
    markets_in_category = set()
    for t in trades:
        market_id = _get_first(t, TRADE_MARKET_ID_FIELDS)
        event_id = str(_get_first(t, TRADE_EVENT_ID_FIELDS) or "")
        if market_id and category_map.get(event_id, "Uncategorized") == target_category:
            markets_in_category.add(market_id)

    total = 0.0
    for market_id in markets_in_category:
        net = reconstruct_net_position(trades, market_id, cutoff_ts)
        total += sum(v for v in net.values() if v > 0)
    return total


def run_backtest():
    log.info("Fetching current top-%d candidate pool...", CANDIDATE_POOL_SIZE)
    wallets = fetch_leaderboard(period="30d", limit=CANDIDATE_POOL_SIZE)
    log.info("Tracking %d wallets. NOTE: survivorship bias — see module docstring.", len(wallets))

    log.info("Fetching full trade history for all %d wallets (slow, one-time cost)...", len(wallets))
    wallet_trades = fetch_all_wallet_trades(wallets)

    log.info("Fetching %d resolved markets to test against...", MARKETS_TO_TEST)
    markets = fetch_resolved_markets(limit=MARKETS_TO_TEST)

    results: list[BacktestResult] = []

    for market in markets:
        condition_id = market.get("conditionId") or market.get("id")
        question = market.get("question", "unknown market")
        actual_outcome = market.get("resolvedOutcome") or market.get("outcome")
        resolved_at_raw = market.get("closedTime") or market.get("endDate")
        event_id = str(market.get("eventId") or "")

        if not condition_id or not actual_outcome or not resolved_at_raw:
            continue

        try:
            resolved_dt = datetime.fromisoformat(str(resolved_at_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        cutoff_dt = resolved_dt - timedelta(hours=LOOKBACK_HOURS)
        cutoff_ts = cutoff_dt.replace(tzinfo=timezone.utc).timestamp()

        category_map = fetch_event_categories([event_id]) if event_id else {}
        category = category_map.get(event_id, "Uncategorized")
        if category in EXCLUDED_CATEGORIES:
            continue
        polite_sleep(0.1)

        # Rank wallets WITHIN this category by their exposure at cutoff —
        # same concept as the live category leaderboard.
        category_exposure = []
        for w in wallets:
            exposure = reconstruct_category_exposure(wallet_trades[w], category_map, category, cutoff_ts)
            if exposure > 0:
                category_exposure.append((w, exposure))
        category_exposure.sort(key=lambda x: x[1], reverse=True)
        top_wallets_in_category = {w for w, _ in category_exposure[:TOP_N_PER_CATEGORY]}

        if len(top_wallets_in_category) < CATEGORY_CONSENSUS_THRESHOLD:
            continue  # not even enough wallets active in this category yet

        outcome_votes: dict[str, int] = {}
        for w in top_wallets_in_category:
            net = reconstruct_net_position(wallet_trades[w], condition_id, cutoff_ts)
            if not net:
                continue
            top_outcome = max(net, key=lambda o: abs(net[o]))
            if net[top_outcome] > 0:
                outcome_votes[top_outcome] = outcome_votes.get(top_outcome, 0) + 1

        if not outcome_votes:
            continue

        predicted_outcome = max(outcome_votes, key=outcome_votes.get)
        consensus_count = outcome_votes[predicted_outcome]

        if consensus_count < CATEGORY_CONSENSUS_THRESHOLD:
            continue  # no signal would have fired here — skip, don't count as a miss

        results.append(BacktestResult(
            market_question=question, category=category,
            predicted_outcome=predicted_outcome, actual_outcome=str(actual_outcome),
            consensus_count=consensus_count,
            correct=(predicted_outcome == str(actual_outcome)),
        ))

    report(results)


def report(results: list[BacktestResult]):
    if not results:
        log.info("No markets crossed the category consensus threshold — nothing to evaluate. "
                  "If this seems wrong, check the field-name assumptions in TRADE_MARKET_ID_FIELDS "
                  "and TRADE_EVENT_ID_FIELDS against a real /trades response first (see module "
                  "docstring, limitation #5).")
        return

    correct = sum(1 for r in results if r.correct)
    total = len(results)
    log.info("=" * 70)
    log.info("BACKTEST RESULTS (category-based, lookback=%dh, threshold=%d/category)",
              LOOKBACK_HOURS, CATEGORY_CONSENSUS_THRESHOLD)
    log.info("Signal fired on %d/%d tested markets. Overall accuracy: %.1f%% (%d/%d correct)",
              total, MARKETS_TO_TEST, 100 * correct / total, correct, total)
    log.info("=" * 70)

    by_category = defaultdict(list)
    for r in results:
        by_category[r.category].append(r)
    for cat, cat_results in sorted(by_category.items()):
        cat_correct = sum(1 for r in cat_results if r.correct)
        log.info("  %s: %d/%d correct (%.1f%%)", cat, cat_correct, len(cat_results),
                  100 * cat_correct / len(cat_results))

    log.info("-" * 70)
    for r in results:
        mark = "✓" if r.correct else "✗"
        log.info("%s [%s, %d/%d agree] predicted '%s' actual '%s' — %s",
                  mark, r.category, r.consensus_count, TOP_N_PER_CATEGORY,
                  r.predicted_outcome, r.actual_outcome, r.market_question)


if __name__ == "__main__":
    run_backtest()
