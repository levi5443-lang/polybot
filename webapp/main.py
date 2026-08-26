"""
webapp/main.py — dashboard backend.

Runs a background refresh loop building separate top-10 leaderboards per
category (see category_leaderboard.py), and serves the resulting signals
over a small JSON API + a static frontend.

Run locally:
    pip install -r ../requirements.txt fastapi uvicorn
    uvicorn main:app --reload --port 8000

Deploy on Render exactly like your other services:
    Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import sys
import os
import time
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from category_leaderboard import (  # noqa: E402
    build_category_data,
    compute_category_consensus,
    CANDIDATE_POOL_SIZE,
    TOP_N_PER_CATEGORY,
    CATEGORY_CONSENSUS_THRESHOLD,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")

LEADERBOARD_PERIOD = "30d"    # "1d" | "7d" | "30d" | "all"
# A full pass now covers CANDIDATE_POOL_SIZE wallets (not just 20), so this
# needs more headroom than before — bump if you see overlapping refreshes.
REFRESH_SECONDS = 240

_cache = {
    "updated_at": None,
    "tracked_wallets": 0,
    "signals": [],
    "categories": [],
    "error": None,
}


async def refresh_loop():
    while True:
        try:
            data = await asyncio.to_thread(build_category_data, LEADERBOARD_PERIOD)
            signals = await asyncio.to_thread(
                compute_category_consensus, data, CATEGORY_CONSENSUS_THRESHOLD
            )
            signals.sort(key=lambda s: s.count, reverse=True)

            # total_tracked for display purposes: how many wallets were in
            # THAT signal's own category leaderboard, not the whole pool.
            signal_dicts = []
            for s in signals:
                cat_wallet_count = len(data["category_top_wallets"].get(s.category, []))
                signal_dicts.append(s.to_dict(cat_wallet_count))

            _cache["updated_at"] = time.time()
            _cache["tracked_wallets"] = len(data["wallets"])
            _cache["signals"] = signal_dicts
            _cache["categories"] = sorted(data["category_top_wallets"].keys())
            _cache["error"] = None
            log.info("Refreshed: %d candidate wallets, %d categories, %d signals",
                      len(data["wallets"]), len(data["category_top_wallets"]), len(signals))
        except Exception as e:
            log.error("Refresh failed: %s", e)
            _cache["error"] = str(e)

        await asyncio.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(refresh_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/api/consensus")
async def get_consensus():
    return _cache


@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
