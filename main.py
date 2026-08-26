"""
webapp/main.py — dashboard backend.

Runs a background refresh loop (same data as consensus_bot.py) and serves it
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

# allow importing the sibling modules (polymarket_api.py, consensus_logic.py)
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from polymarket_api import fetch_leaderboard, fetch_positions, polite_sleep  # noqa: E402
from consensus_logic import parse_positions, compute_consensus, CONSENSUS_THRESHOLD  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")

NUM_TOP_TRADERS = 20
LEADERBOARD_WINDOW = "month"
REFRESH_SECONDS = 120

# In-memory cache the frontend polls — good enough for a single-instance
# prototype; move to Supabase/Redis if you scale this to multiple instances.
_cache = {
    "updated_at": None,
    "tracked_wallets": 0,
    "signals": [],
    "error": None,
}


async def refresh_loop():
    while True:
        try:
            wallets = await asyncio.to_thread(fetch_leaderboard, LEADERBOARD_WINDOW, NUM_TOP_TRADERS)
            all_positions = []
            for wallet in wallets:
                raw = await asyncio.to_thread(fetch_positions, wallet)
                all_positions.extend(parse_positions(wallet, raw))
                await asyncio.sleep(0.2)

            signals = compute_consensus(all_positions, threshold=CONSENSUS_THRESHOLD)
            signals.sort(key=lambda s: s.count, reverse=True)

            _cache["updated_at"] = time.time()
            _cache["tracked_wallets"] = len(wallets)
            _cache["signals"] = [s.to_dict(len(wallets)) for s in signals]
            _cache["error"] = None
            log.info("Refreshed: %d wallets, %d signals", len(wallets), len(signals))
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
