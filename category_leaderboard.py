"""
category_leaderboard.py — per-category top-trader leaderboards.

Polymarket's own leaderboard is global (top by overall PnL/volume) and has
no concept of category. To get "top 10 Sports traders" as its own list,
separate from "top 10 Weather traders," we build it ourselves:

1. Pull a wide candidate pool (CANDIDATE_POOL_SIZE) from the global
   leaderboard — wide enough that someone who's #1 in Weather but outside
   the global top 20 still gets found.
2. Pull every candidate's positions.
3. Tag each position's event with its category.
4. Within each category, rank candidates by how much they have sized into
   that category specifically, and keep the top TOP_N_PER_CATEGORY.
5. Run consensus checks separately per category, using only that
   category's own top-N wallets — not the same global list for everything.

Cost note: this fetches positions for CANDIDATE_POOL_SIZE wallets instead of
just the top 20, so a full pass is meaningfully slower. Tune the constants
below, and make sure your polling interval (POLL_INTERVAL_SECONDS in
consensus_bot.py / REFRESH_SECONDS in webapp/main.py) leaves enough room
for a pass to finish before the next one starts.
"""

from collections import defaultdict
import logging

from polymarket_api import fetch_leaderboard, fetch_positions, fetch_event_categories, polite_sleep
from consensus_logic import parse_positions, compute_consensus
import wallet_tracker

log = logging.getLogger("category_leaderboard")

CANDIDATE_POOL_SIZE = 100   # how many wallets to pull from the global leaderboard first
TOP_N_PER_CATEGORY = 10     # how many top wallets count as "the leaderboard" within a category

# Categories to skip entirely — no leaderboard is built for these, no
# signals will ever fire for them, and their positions don't count toward
# any other category either.
EXCLUDED_CATEGORIES = ["Crypto"]

# Threshold for a category-level consensus signal (out of TOP_N_PER_CATEGORY).
CATEGORY_CONSENSUS_THRESHOLD = 4


def build_category_data(period: str = "30d") -> dict:
    """Do the heavy lifting once per cycle: pull the candidate pool, their
    positions, categorize everything, and rank wallets within each category.

    NOTE: this touches CANDIDATE_POOL_SIZE wallets one at a time (plus one
    request per unique event afterward) — with the default of 100, that's
    on the order of 150+ sequential network calls. Progress lines below
    are there so a long pass doesn't look like a hang.

    Returns a dict with:
      - wallets: the full candidate pool (list of addresses)
      - all_positions: every candidate's positions (list of Position)
      - category_map: {event_id: category_label}
      - category_top_wallets: {category: [wallet, ...]} (top N per category,
        excluding anything in EXCLUDED_CATEGORIES)
    """
    wallets = fetch_leaderboard(period=period, limit=CANDIDATE_POOL_SIZE)
    log.info("Got %d candidate wallets. Fetching positions (this is the slow part)...", len(wallets))

    all_positions = []
    for i, wallet in enumerate(wallets, start=1):
        try:
            raw = fetch_positions(wallet)
            all_positions.extend(parse_positions(wallet, raw))
        except Exception:
            pass  # one bad wallet shouldn't kill the whole pass
        if i % 10 == 0 or i == len(wallets):
            log.info("  ...positions: %d/%d wallets done", i, len(wallets))
        polite_sleep(0.15)

    event_ids = list(dict.fromkeys(p.event_id for p in all_positions if p.event_id))
    log.info("Found %d unique events across all positions. Looking up categories...", len(event_ids))
    category_map = fetch_event_categories(event_ids)
    log.info("Category lookup done.")

    # Track every candidate wallet's position, in every category — including
    # excluded ones (EXCLUDED_CATEGORIES only controls which categories can
    # produce trading SIGNALS, not which wallets/positions get tracked for
    # accuracy purposes).
    newly_observed_positions = wallet_tracker.record_observed_positions(all_positions, category_map)

    # Sum each wallet's position size within each category — skipping
    # excluded categories entirely, so they never form a leaderboard and
    # never produce a signal.
    wallet_category_size: dict = defaultdict(lambda: defaultdict(float))
    for p in all_positions:
        cat = category_map.get(p.event_id, "Uncategorized")
        if cat in EXCLUDED_CATEGORIES:
            continue
        wallet_category_size[p.wallet][cat] += p.size_usd

    # Rank wallets within each category, keep the top N.
    category_candidates: dict = defaultdict(list)
    for wallet, cats in wallet_category_size.items():
        for cat, size in cats.items():
            category_candidates[cat].append((wallet, size))

    category_top_wallets = {}
    for cat, candidates in category_candidates.items():
        candidates.sort(key=lambda x: x[1], reverse=True)
        category_top_wallets[cat] = [w for w, _ in candidates[:TOP_N_PER_CATEGORY]]

    # Polymarket's leaderboard is already ranked best-to-worst, so a
    # wallet's position in this list IS its overall rank (1 = best).
    # This lets alerts show not just "4 of this category's top 10 agree"
    # but where those specific 4 traders actually sit in the full pool —
    # e.g. #3 and #91 agreeing is a very different signal than #3 and #7.
    wallet_overall_rank = {w: i + 1 for i, w in enumerate(wallets)}

    return {
        "wallets": wallets,
        "all_positions": all_positions,
        "category_map": category_map,
        "category_top_wallets": category_top_wallets,
        "wallet_overall_rank": wallet_overall_rank,
        "newly_observed_positions": newly_observed_positions,
    }


def compute_category_consensus(data: dict, threshold: int = CATEGORY_CONSENSUS_THRESHOLD) -> list:
    """Run consensus separately within each category's own top-N wallets.

    Returns a flat list of ConsensusSignal, each already labeled with its
    category and counted against that category's own wallet list (not the
    global pool).
    """
    signals = []
    for cat, top_wallets in data["category_top_wallets"].items():
        top_set = set(top_wallets)
        cat_positions = [
            p for p in data["all_positions"]
            if p.wallet in top_set and data["category_map"].get(p.event_id, "Uncategorized") == cat
        ]
        cat_signals = compute_consensus(cat_positions, threshold=threshold)
        for s in cat_signals:
            s.category = cat  # already implied by construction, but explicit beats implicit
        signals.extend(cat_signals)
    return signals
