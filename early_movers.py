"""
early_movers.py — flag positions where a QUALIFYING wallet got in before
a meaningful price move, with room still left to capture.

REDEFINED 2026-10-13 (Levi's request). The original version of this
signal meant "early" literally — a market the event's own `creationDate`
said had just appeared on Polymarket, within NEW_MARKET_MAX_AGE_MINUTES.
In practice that definition produced zero qualifying signals, cycle
after cycle: tracked wallets essentially never happened to be the very
first ones into a market that had *just* been created. Investigating why
led to the real question — the objective isn't "detect new markets," it's
to earn as much as possible — which makes "early" the wrong axis to
measure in the first place. A market's calendar age tells you nothing
about whether there's still money to be made in it.

So this now measures "early" relative to PRICE, not time: a wallet is an
early mover if (a) a wallet with a real track record opened this specific
position recently, (b) the price has since moved meaningfully in their
favor — i.e. the market later caught onto something they were already
positioned for — and (c) the price hasn't already converged near 0 or 1,
so there's still real room left to profit from entering now ourselves.

Why bound position age at all, if this is about price and not time? A
position opened months ago that only just started moving could mean a
slow thesis is finally paying off with a long runway left — or it could
mean the easy money was made ages ago and this is just the tail of it.
Price alone can't tell those apart. A position opened recently where the
price has already moved meaningfully is a cleaner read: fast money went
in, the market reacted fast, and that kind of fresh momentum tends to
keep running in the short term rather than reverse. The age bound is
what keeps this signal pointed at genuinely early, still-developing
moves instead of late-stage stories we can no longer tell apart from
stale ones.

Similarly, why gate on a price band? A move from 30c to 50c is a real
read being confirmed with plenty of room left. A move from 88c to 95c
usually isn't new information anymore — that's the market converging on
an outcome everyone already sees coming as resolution nears. Chasing
that is worse risk/reward for no real edge, so positions already priced
near certainty don't qualify no matter how big the underlying wallet's
edge might otherwise be.

WALLET QUALITY GATE: a wallet's position only counts if it has at least
EARLY_MOVER_MIN_RESOLVED resolved positions on record — using OUR OWN
observed history (wallet_tracker), not Polymarket's leaderboard rank.
The old version also required a top-EARLY_MOVER_MAX_RANK overall rank,
but that rank only exists for wallets currently in category_leaderboard's
candidate-pool snapshot (confirmed capped at roughly 50-100 wallets per
cycle) — a wallet with a genuinely strong, proven track record that
simply isn't on today's leaderboard pull was being wrongly excluded.
Resolved-count (and, downstream, the paper-trade ROI/streak gate in
consensus_bot.py's run_early_movers) is enough on its own and doesn't
have that blind spot.

This is separate from — and in addition to — the PAPER-trade-only gate
in consensus_bot.py's run_early_movers (2 consecutive wins + positive
average ROI across the agreeing wallets), which controls whether a
paper trade gets LOGGED, not whether the alert fires.
"""

import logging

import wallet_tracker
from consensus_logic import compute_consensus
from category_leaderboard import EXCLUDED_CATEGORIES

log = logging.getLogger("early_movers")

MIN_EARLY_MOVERS = 1  # a single qualifying wallet is enough to fire the signal on its own

EARLY_MOVER_MIN_RESOLVED = 5  # minimum resolved positions (overall, our own history) before a
                               # wallet counts at all

EARLY_MOVER_MAX_POSITION_AGE_MINUTES = 10080  # 7 days — how recently the wallet must have
                                                # opened THIS position for a later price move to
                                                # count as "early" rather than a stale thesis
                                                # finally paying off (see module docstring)

EARLY_MOVER_MIN_PRICE_MOVE_PTS = 10  # percentage points the price must have moved in the
                                      # wallet's favor since they entered, before it counts as
                                      # "the market caught on to something they were early to"

EARLY_MOVER_PRICE_BAND = (0.15, 0.85)  # current price must stay inside this band — outside it,
                                         # the market's already converged toward certainty and
                                         # there's little room left to profit from entering now


def find_early_movers(all_positions: list, category_map: dict) -> list:
    """Returns a list of ConsensusSignal for positions where at least
    MIN_EARLY_MOVERS QUALIFYING wallets (see module docstring — resolved-
    count gate, recent entry, meaningful favorable price move, price
    still inside EARLY_MOVER_PRICE_BAND) are on the same side. Each
    signal's .category is the real category (Sports, Politics, etc.) —
    callers should present these as a distinct alert type, not mix them
    into regular per-category consensus output.
    """
    qualifying_positions = []
    skipped_category = 0
    skipped_resolved = 0
    skipped_age = 0
    skipped_move = 0
    skipped_band = 0

    for p in all_positions:
        # EXCLUDED_CATEGORIES filter (added 2026-10-15 — real bug fix,
        # Levi reported Crypto signals showing up here): this was the one
        # signal-generating path in the codebase NOT applying it.
        # category_leaderboard.py's EXCLUDED_CATEGORIES is documented as
        # controlling which categories can produce trading SIGNALS (not
        # which positions get tracked for accuracy) — Consensus already
        # respected this because Crypto never gets a category leaderboard
        # entry at all, so no Consensus signal could ever form for it.
        # Early Movers works off the full all_positions list directly and
        # had no equivalent check, so Crypto positions were passing
        # straight through to real alerts.
        category = category_map.get(p.event_id, "Uncategorized")
        if category in EXCLUDED_CATEGORIES:
            skipped_category += 1
            continue

        rec = wallet_tracker.get_wallet_overall_record(p.wallet)
        if rec["total_resolved"] < EARLY_MOVER_MIN_RESOLVED:
            skipped_resolved += 1
            continue

        age_minutes = wallet_tracker.get_position_age_minutes(p.wallet, p.market_id, p.outcome)
        if age_minutes is None or age_minutes > EARLY_MOVER_MAX_POSITION_AGE_MINUTES:
            skipped_age += 1
            continue

        price_move_pts = wallet_tracker.get_price_change_pts(p.wallet, p.market_id, p.outcome, p.cur_price)
        if price_move_pts is None or price_move_pts < EARLY_MOVER_MIN_PRICE_MOVE_PTS:
            skipped_move += 1
            continue

        if not (EARLY_MOVER_PRICE_BAND[0] <= p.cur_price <= EARLY_MOVER_PRICE_BAND[1]):
            skipped_band += 1
            continue

        qualifying_positions.append(p)

    log.info(
        "Early movers: %d/%d position(s) pass the price-based quality gate "
        "(excluded category [%d dropped], %d+ resolved [%d dropped], opened <= %dmin ago [%d dropped], "
        "moved %d+pts in their favor [%d dropped], price in %d-%d%% band [%d dropped]).",
        len(qualifying_positions), len(all_positions),
        skipped_category,
        EARLY_MOVER_MIN_RESOLVED, skipped_resolved,
        EARLY_MOVER_MAX_POSITION_AGE_MINUTES, skipped_age,
        EARLY_MOVER_MIN_PRICE_MOVE_PTS, skipped_move,
        round(EARLY_MOVER_PRICE_BAND[0] * 100), round(EARLY_MOVER_PRICE_BAND[1] * 100), skipped_band,
    )

    signals = compute_consensus(qualifying_positions, threshold=MIN_EARLY_MOVERS)

    for s in signals:
        s.category = category_map.get(s.event_id, "Uncategorized")

    return signals
