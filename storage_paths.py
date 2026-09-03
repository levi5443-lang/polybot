"""
storage_paths.py — resolves where locally-persisted state files should live.

The bug this fixes: category_cache.json, trade_log.json, etc. were being
written to os.path.dirname(__file__) — i.e., wherever the source code
itself lives. On Render, that directory gets rebuilt fresh from GitHub on
every single deploy, so anything written there is wiped out immediately.

Render's actual persistent disk for this service is mounted at /var/data
(configured in the service's disk settings) — that's the one place that
survives across deploys and restarts. This module points every local
state file at that mount when it exists, and falls back to sitting next
to the source code otherwise (so local testing on a laptop still works
fine, just without persistence across separate runs — which was already
the reality before this fix, so it's not a regression for local use).
"""

import os

RENDER_DISK_PATH = "/var/data"


def persistent_dir() -> str:
    if os.path.isdir(RENDER_DISK_PATH):
        return RENDER_DISK_PATH
    return os.path.dirname(os.path.abspath(__file__))


def persistent_path(filename: str) -> str:
    return os.path.join(persistent_dir(), filename)
