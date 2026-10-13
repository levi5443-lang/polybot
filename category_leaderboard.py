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
4. Within each category, keep whoever has sized into that category at all,
   then rank THOSE candidates by their proven category-specific realized
   ROI% (see rank_wallets_by_category_roi below), keeping the top
   TOP_N_PER_CATEGORY.
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

CANDIDATE_POOL_SIZE = 100   # target size of the candidate pool (see _pull_merged_candidate_pool —
                            # a single fetch_leaderboard() call can't actually reach this alone)
TOP_N_PER_CATEGORY = 10     # how many top wallets count as "the leaderboard" within a category

# Categories to skip entirely — no leaderboard is built for these, no
# signals will ever fire for them, and their positions don't count toward
# any other category either.
EXCLUDED_CATEGORIES = ["Crypto"]

# Threshold for a category-level consensus signal (out of TOP_N_PER_CATEGORY).
CATEGORY_CONSENSUS_THRESHOLD = 4

# CONSENSUS_PRICE_BAND (added 2026-10-13, Levi's request): a Consensus
# signal only counts if the current price is still inside this band.
# Same reasoning as early_movers.py's own price band — a market already
# sitting near 0 or 1 has already converged toward that outcome, so even
# a correct call pays little; there's no real room left to profit from
# entering now, no matter how many top wallets agree. Matches Early
# Movers' band for consistency; adjust independently if the data later
# suggests Consensus should use a different range.
CONSENSUS_PRICE_BAND = (0.15, 0.85)

# MERGE_PERIODS (added 2026-10-12, Levi's request): confirmed that
# fetch_leaderboard() only ever returns ~50 wallets per call no matter what
# limit is passed — Polymarket's /v1/leaderboard endpoint caps a single
# request's response at 50, regardless of the requested limit. Asking for
# CANDIDATE_POOL_SIZE=100 in one call was therefore silently capped at 50
# the whole time. Rather than rely on an undocumented offset/pagination
# param (unconfirmed against current Polymarket API docs), this instead
# merges several DIFFERENT leaderboard periods and de-duplicates by wallet
# address — "30d" is the stable, established ranking; "7d" and "1d" surface
# wallets who are hot right now but haven't accumulated enough 30-day
# volume to show up there yet. Order matters: earlier periods in this list
# win ties when building the merged, order-preserving candidate list.
MERGE_PERIODS = ["30d", "7d", "1d"]


def _pull_merged_candidate_pool(primary_period: str) -> list[str]:
    """Builds the candidate pool by pulling every period in MERGE_PERIODS
    (plus primary_period, if it's some other value a caller passed in) and
    merging unique wallets, preserving first-seen order. This is the
    workaround for the 50-wallet-per-call cap described above — one period
    alone tops out at 50 wallets; merging a few distinct periods gets us a
    meaningfully larger, still-legitimate pool, capped at
    CANDIDATE_POOL_SIZE total.
    """
    periods = list(dict.fromkeys([primary_period] + MERGE_PERIODS))

    merged: list[str] = []
    seen: set = set()
    for period in periods:
        try:
            batch = fetch_leaderboard(period=period, limit=CANDIDATE_POOL_SIZE)
        except Exception as e:
            log.warning("Failed to fetch '%s' leaderboard: %s", period, e)
            continue
        new_this_period = 0
        for wallet in batch:
            if wallet not in seen:
                seen.add(wallet)
                merged.append(wallet)
                new_this_period += 1
        log.info("  ...leaderboard period '%s': %d wallet(s), %d new to the pool",
                  period, len(batch), new_this_period)
        if len(merged) >= CANDIDATE_POOL_SIZE:
            break

    return merged[:CANDIDATE_POOL_SIZE]


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
    wallets = _pull_merged_candidate_pool(period)
    log.info("Got %d candidate wallets (merged across %s). Fetching positions (this is the slow part)...",
              len(wallets), list(dict.fromkeys([period] + MERGE_PERIODS)))

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
    # never produce a signal. This $ size is now ONLY used to decide who's
    # even a candidate for a category's leaderboard (i.e. has real skin in
    # it) — see category_top_wallets below for how the actual ranking works.
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

    # RANKING CHANGE (2026-10-13, Levi's request): a category's top N used
    # to be whoever had the most $ sized into that category — a whale with
    # a mediocre track record counted the same as a smaller but genuinely
    # good trader, as long as they'd put enough money in. Now: $ size still
    # decides who's even a CANDIDATE (you need real skin in the category to
    # count at all), but the actual top-N ranking is by each wallet's own
    # category-specific realized ROI% (wallet_tracker.rank_wallets_by_category_roi),
    # once they have enough resolved history in that category to trust the
    # number (same MIN_SAMPLE_SIZE gate used everywhere else in this
    # codebase). Wallets without enough category history yet fall back to
    # size order (the order they're passed in) and are appended after
    # everyone with a proven record — never penalized, just not yet
    # promoted ahead of someone proven.
    category_top_wallets = {}
    for cat, candidates in category_candidates.items():
        candidates.sort(key=lambda x: x[1], reverse=True)  # size order = the fallback ordering
        candidate_wallets = [w for w, _ in candidates]
        ranked = wallet_tracker.rank_wallets_by_category_roi(candidate_wallets, cat)
        category_top_wallets[cat] = ranked[:TOP_N_PER_CATEGORY]

    # "wallets" (Polymarket's own profit-ranked list) is still how we
    # DISCOVER candidates in the first place — there's no public API to
    # ask Polymarket for "sort by ROI%" directly. But the actual rank we
    # show and act on is our OWN historical realized ROI% ordering, once
    # a wallet has enough resolved history to trust — see
    # wallet_tracker.rank_wallets_by_realized_roi(). Wallets without
    # enough history yet fall back to their original relative order, so
    # the pool stays fully populated while gradually reflecting proven
    # performance instead of raw Polymarket profit rank.
    roi_ranked_wallets = wallet_tracker.rank_wallets_by_realized_roi(wallets)
    wallet_overall_rank = {w: i + 1 for i, w in enumerate(roi_ranked_wallets)}

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
    global pool). Signals whose current price has already moved outside
    CONSENSUS_PRICE_BAND are dropped (2026-10-13, Levi's request) — see
    that constant's own comment for why.
    """
    import signal_momentum

    signals = []
    for cat, top_wallets in data["category_top_wallets"].items():
        top_set = set(top_wallets)
        cat_positions = [
            p for p in data["all_positions"]
            if p.wallet in top_set and data["category_map"].get(p.event_id, "Uncategorized") == cat
        ]

        # Track momentum at EVERY agreement level (even just 1 wallet) —
        # not only signals that already meet the alert threshold. This
        # way, by the time something first crosses the real threshold and
        # triggers its one-time alert, real multi-cycle history (e.g.
        # "1→2→3→4") already exists to show, instead of every signal's
        # first alert always reading "new" with nothing to compare to.
        for s in compute_consensus(cat_positions, threshold=1):
            signal_momentum.record_and_get_momentum(s.market_id, s.outcome, s.count)

        cat_signals = compute_consensus(cat_positions, threshold=threshold)

        before_band = len(cat_signals)
        cat_signals = [
            s for s in cat_signals
            if CONSENSUS_PRICE_BAND[0] <= s.cur_price <= CONSENSUS_PRICE_BAND[1]
        ]
        if before_band != len(cat_signals):
            log.info("  [%s] price-band filter: %d/%d signal(s) kept (%.0f-%.0f%% band)",
                      cat, len(cat_signals), before_band,
                      CONSENSUS_PRICE_BAND[0] * 100, CONSENSUS_PRICE_BAND[1] * 100)

        for s in cat_signals:
            s.category = cat  # already implied by construction, but explicit beats implicit
        signals.extend(cat_signals)
    return signals
