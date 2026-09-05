"""
early_movers.py — flag brand-new Polymarket events where 2+ of our tracked
top-100 traders are already positioned, on the same side, within moments
of the market appearing.

This is a genuinely different signal from category consensus: it doesn't
require a market to have accumulated enough activity to rank into any
category's top-10 leaderboard — it only cares whether our wallets moved
fast on something that just showed up at all.

"New" means: the event's own `creationDate` (a real timestamp Polymarket
reports) is within NEW_MARKET_MAX_AGE_MINUTES. This replaced an earlier,
flawed definition ("an event ID not seen in a previous cycle's top-100-
by-ID window") that produced huge, wildly fluctuating "new" counts (16 to
473 per cycle) with zero real overlap ever found — strong evidence that
window-membership doesn't reliably mean "just created." A dedicated seen-
events cache still prevents re-alerting on the same genuinely-new event
across the several cycles it stays within the age window.

Known limitation: a market is only ever checked for early movers during
the single cycle it's first discovered. If fewer than MIN_EARLY_MOVERS
wallets are in it at that moment but more join later, that's a real
opportunity this won't catch — it deliberately only measures "who got in
fast," not ongoing activity (that's what the regular category consensus
signals are for).

⚠️ DIAGNOSTIC MODE (as of this version): MIN_EARLY_MOVERS is still 1 (not
the intended 2) for one more round, specifically to confirm the
creationDate fix actually produces real overlap now, before reverting to
the intended threshold of 2.
"""

import logging
from datetime import datetime, timezone

from polymarket_api import fetch_newest_events
from consensus_logic import compute_consensus

log = logging.getLogger("early_movers")

MIN_EARLY_MOVERS = 1  # TEMPORARY DIAGNOSTIC — was 2. See note above.
NEWEST_EVENTS_TO_CHECK = 100  # how many of Polymarket's newest events to look at each cycle
NEW_MARKET_MAX_AGE_MINUTES = 10  # a bit over 2x the poll interval, to tolerate cycle timing drift

from shared_storage import get_json, set_json

SEEN_EVENTS_KEY = "seen_events_cache.json"


def _load_seen_events() -> set:
    return set(get_json(SEEN_EVENTS_KEY, []))


def _save_seen_events(seen: set) -> None:
    set_json(SEEN_EVENTS_KEY, list(seen))


def _event_age_minutes(event: dict) -> float:
    """Minutes since Polymarket says this event was actually created.
    Returns None if creationDate is missing or unparseable — we never
    guess an age, we just skip anything we can't confirm."""
    creation_date = event.get("creationDate")
    if not creation_date:
        return None
    try:
        dt = datetime.fromisoformat(str(creation_date).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except (ValueError, TypeError):
        return None


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
    genuinely_new_count = 0
    stale_but_unseen_count = 0
    for event in newest_events:
        eid = str(event.get("id") or "")
        if not eid or eid in seen:
            continue

        age_minutes = _event_age_minutes(event)
        if age_minutes is None:
            continue  # can't confirm real age — skip rather than guess
        if age_minutes > NEW_MARKET_MAX_AGE_MINUTES:
            # Never seen before, but NOT actually recently created — this
            # is exactly the noise the old ID-window approach couldn't
            # tell apart from genuine freshness.
            stale_but_unseen_count += 1
            continue

        genuinely_new_count += 1
        for market in (event.get("markets") or []):
            cid = market.get("conditionId")
            if cid:
                new_market_ids.add(cid)

    # Mark everything just looked at as seen (whether or not it was new),
    # so nothing gets reprocessed on future cycles.
    all_ids = {str(e.get("id")) for e in newest_events if e.get("id")}
    _save_seen_events(seen | all_ids)

    log.info("Early movers: %d event(s) newly observed this cycle — %d genuinely new "
              "(created within %d min), %d excluded as window-shift noise (unseen but older).",
              genuinely_new_count + stale_but_unseen_count, genuinely_new_count,
              NEW_MARKET_MAX_AGE_MINUTES, stale_but_unseen_count)

    if not new_market_ids:
        log.info("Early movers: no genuinely brand-new markets found this cycle.")
        return []

    log.info("Early movers: %d brand-new market(s) (by conditionId) this cycle.", len(new_market_ids))

    new_market_positions = [p for p in all_positions if p.market_id in new_market_ids]

    # DIAGNOSTIC: how many of the "new" markets have ANY tracked wallet in
    # them at all, regardless of threshold?
    touched_market_ids = {p.market_id for p in new_market_positions}
    log.info("Early movers DIAGNOSTIC: %d/%d new markets have ANY tracked wallet in them "
              "(%d total matching positions from %d wallets).",
              len(touched_market_ids), len(new_market_ids), len(new_market_positions),
              len({p.wallet for p in new_market_positions}))

    signals = compute_consensus(new_market_positions, threshold=MIN_EARLY_MOVERS)

    for s in signals:
        s.category = category_map.get(s.event_id, "Uncategorized")

    return signals
