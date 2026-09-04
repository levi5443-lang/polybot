"""
shared_storage.py — shared key-value storage for state that both the
worker and dashboard services need to see (trade history, wallet records,
category cache, etc).

Backed by Render's managed Key-Value store (Redis-compatible) when
REDIS_URL is set — this is what makes the worker's trades visible to the
dashboard's /history and /accuracy Telegram commands, and vice versa,
since they run as two completely separate machines with no shared disk
between them (a local file written by one is invisible to the other).

Falls back to local JSON files (the old per-service-only behavior) when
REDIS_URL isn't set, so this still works fine for local testing on a
laptop without needing a real Redis instance running.
"""

import json
import os
import logging

log = logging.getLogger("shared_storage")

REDIS_URL = os.environ.get("REDIS_URL")

_redis_client = None
if REDIS_URL:
    try:
        import redis
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    except ImportError:
        log.error("REDIS_URL is set but the 'redis' package isn't installed — "
                   "falling back to local files (add redis to requirements.txt).")
    except Exception as e:
        log.error("Could not set up shared storage client (%s) — falling back to local files.", e)


def _local_path(key: str) -> str:
    from storage_paths import persistent_path
    return persistent_path(key)


def get_json(key: str, default):
    """Load a JSON value by key. Returns `default` if missing or on any
    failure — a storage problem should never crash a poll cycle."""
    if _redis_client:
        try:
            raw = _redis_client.get(key)
            return json.loads(raw) if raw is not None else default
        except Exception as e:
            log.error("Shared storage read failed for '%s': %s", key, e)
            return default

    path = _local_path(key)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def set_json(key: str, value) -> None:
    """Save a JSON value by key. Failures are logged, never raised."""
    if _redis_client:
        try:
            _redis_client.set(key, json.dumps(value))
            return
        except Exception as e:
            log.error("Shared storage write failed for '%s': %s", key, e)
            return

    path = _local_path(key)
    try:
        with open(path, "w") as f:
            json.dump(value, f)
    except OSError as e:
        log.error("Could not save '%s' locally: %s", key, e)


# Keys that previously lived only on the worker's local disk (/var/data),
# before this service switched to shared Redis storage. The old files are
# still physically sitting there, unread by the app now — this recovers
# them once, automatically, on first startup after the switch.
_LEGACY_KEYS_AND_DEFAULTS = [
    ("trade_log.json", []),
    ("wallet_records.json", {}),
    ("category_cache.json", {}),
    ("seen_events_cache.json", []),
    ("digest_state.json", {}),
]


def migrate_legacy_local_data() -> None:
    """One-time, automatic, safe-to-call-every-startup recovery: for each
    known key, if Redis is empty/default AND an old local file exists with
    real data, pull that local data into Redis. No-ops harmlessly once the
    migration has already happened (Redis will no longer be empty)."""
    if not _redis_client:
        return  # nothing to migrate INTO — we're already just using local files

    for key, empty_default in _LEGACY_KEYS_AND_DEFAULTS:
        current = get_json(key, empty_default)
        already_has_data = bool(current) and current != empty_default
        if already_has_data:
            continue  # Redis already has real data for this key — don't overwrite it

        path = _local_path(key)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r") as f:
                legacy_value = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if not legacy_value or legacy_value == empty_default:
            continue  # old file exists but has nothing worth recovering

        set_json(key, legacy_value)
        log.info("Recovered legacy local data for '%s' (%d item(s)) into shared storage.",
                  key, len(legacy_value))
