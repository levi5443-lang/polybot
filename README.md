# Polymarket Top-Trader Consensus Tracker

Tracks Polymarket's public leaderboard, pulls the current positions of the
top traders, and flags markets where enough of them are on the same side.
Alerts go out over Telegram; a small dashboard shows live signals. Trade
execution is stubbed out (paper mode only) until the signal has been
backtested and you've deliberately wired up real order placement.

## Layout

```
.
├── requirements.txt
├── polymarket_api.py     # shared fetchers: leaderboard, positions, trades, resolved markets
├── consensus_logic.py    # shared data models + agreement-detection logic
├── consensus_bot.py      # polling loop -> Telegram alerts (paper-trade only)
├── telegram_alert.py     # one-way Telegram push notifications
├── backtest.py           # tests the consensus signal against past resolved markets
└── webapp/
    ├── main.py            # FastAPI backend, background refresh loop, JSON API
    └── static/index.html  # live dashboard (polls /api/consensus)
```

`consensus_bot.py` and `webapp/main.py` both import `polymarket_api.py` and
`consensus_logic.py` so alerting and the dashboard never compute consensus
differently.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt fastapi uvicorn
```

Set Telegram credentials (only needed for `consensus_bot.py`):

```bash
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
```

Use a bot token created specifically for this project (via @BotFather),
separate from any existing signal bot, so a bug here can't touch that
channel.

## Running it

**Alert bot** (single pass):
```bash
python3 consensus_bot.py
```

**Dashboard**:
```bash
cd webapp
uvicorn main:app --reload --port 8000
```
Then open `http://localhost:8000`.

**Backtest** (run this before trusting the signal with anything):
```bash
python3 backtest.py
```
Read the docstring at the top of `backtest.py` first — it has a known
survivorship-bias limitation (it uses today's leaderboard to judge past
markets) that affects how much weight to put on the accuracy number.

## Deploying (Render)

Push this repo to GitHub, connect it in Render, and set the start command to:
```
uvicorn main:app --host 0.0.0.0 --port $PORT
```
(with the working directory set to `webapp/`). Set `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` as environment variables in Render's dashboard — never
commit them.

## Status / next steps

- Not yet tested against live data end-to-end (verify field names in
  `polymarket_api.py` against current Polymarket API docs — they drift).
- Execution is intentionally not implemented. Going live needs
  `py-clob-client`, a funded Polygon wallet, and real position-sizing logic —
  don't copy top traders' raw position sizes without knowing their bankroll.
