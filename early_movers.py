"""
early_movers.py — flag brand-new Polymarket events where 2+ of our tracked
top-100 traders are already positioned, on the same side, within moments
of the market appearing.

This is a genuinely different signal from category consensus: it doesn't
require a market to have accumulated enough activity to rank into any
category's top-10 leaderboard — it only cares whether our wallets moved
fast on something that just showed up at all.

"New" means: an event ID we haven't seen in a previous cycle. The set of
seen event IDs is persisted to disk (same pattern as the category cache),
so this works across separate process restarts, not just within one
long-lived run.

Known limitation: a market is only ever checked for early movers during
the single cycle it's first discovered. If fewer than MIN_EARLY_MOVERS
wallets are in it at that moment but more join later, that's a real
opportunity this won't catch — it deliberately only measures "who got in
fast," not ongoing activity (that's what the regular category consensus
signals are for).

⚠️ DIAGNOSTIC MODE (as of this version): MIN_EARLY_MOVERS is temporarily
set to 1 (not the intended 2), and extra diagnostic logging was added,
because zero early-mover signals fired across 25+ consecutive real
cycles at threshold=2 — worth confirming whether that's genuine rarity
or a deeper bug in how "new" is being determined before trusting the
feature. Revert to MIN_EARLY_MOVERS = 2 once that's understood.
"""

import json
import os
import logging

from polymarket_api import fetch_newest_events
from consensus_logic import compute_consensus

log = logging.getLogger("early_movers")

MIN_EARLY_MOVERS = 1  # TEMPORARY DIAGNOSTIC — was 2. See note below.
NEWEST_EVENTS_TO_CHECK = 100  # how many of Polymarket's newest events to look at each cycle

from storage_paths import persistent_path

SEEN_EVENTS_FILE = persistent_path("seen_events_cache.json")


def _load_seen_events() -> set:
    if os.path.exists(SEEN_EVENTS_FILE):
        try:
            with open(SEEN_EVENTS_FILE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def _save_seen_events(seen: set) -> None:
    try:
        with open(SEEN_EVENTS_FILE, "w") as f:
            json.dump(list(seen), f)
    except OSError:
        pass


def find_early_movers(all_positions: list, category_map: dict) -> list:
    """Returns a list of ConsensusSignal for brand-new markets where at
    least MIN_EARLY_MOVERS of our tracked wallets are already on the same
    side. Each signal's .category is the real category (Sports, Politics,
    etc.) — callers should present these as a distinct alert type, not
    mix them into regular per-category consensus output.
    """
    try:
        newest_events = fetch_newest_events(limit=NEWEST_EVENTS_TO_CHECK)
    except Exception as e:
        log.warning("Failed to fetch newest events: %s", e)
        return []

    seen = _load_seen_events()

    new_market_ids = set()
    for event in newest_events:
        eid = str(event.get("id") or "")
        if eid and eid not in seen:
            for market in (event.get("markets") or []):
                cid = market.get("conditionId")
                if cid:
                    new_market_ids.add(cid)

    # Mark everything just looked at as seen (whether or not it was new),
    # so nothing gets reported twice across cycles.
    all_ids = {str(e.get("id")) for e in newest_events if e.get("id")}
    _save_seen_events(seen | all_ids)

    if not new_market_ids:
        log.info("Early movers: no brand-new markets found this cycle.")
        return []

    log.info("Early movers: %d brand-new market(s) discovered this cycle.", len(new_market_ids))

    new_market_positions = [p for p in all_positions if p.market_id in new_market_ids]

    # DIAGNOSTIC: how many of the "new" markets have ANY tracked wallet in
    # them at all, regardless of threshold? This tells us whether the zero
    # results are "genuinely rare" (some overlap exists, just below 2) or
    # "structurally broken" (essentially zero overlap ever) in one cycle,
    # rather than needing to wait and guess across many cycles.
    touched_market_ids = {p.market_id for p in new_market_positions}
    log.info("Early movers DIAGNOSTIC: %d/%d new markets have ANY tracked wallet in them "
              "(%d total matching positions from %d wallets).",
              len(touched_market_ids), len(new_market_ids), len(new_market_positions),
              len({p.wallet for p in new_market_positions}))

    signals = compute_consensus(new_market_positions, threshold=MIN_EARLY_MOVERS)

    for s in signals:
        s.category = category_map.get(s.event_id, "Uncategorized")

    return signals
