# 🏏 Polymarket Cricket Trading Bot

Automated trading bot for cricket match winner markets on Polymarket.

**Strategy:** Buy YES tokens ≤ 0.90 USDC using cricket live data signals → Hold to market resolution (+10%) or sell at 0.85 stop loss (−5%).

**Edge:** ESPN Cricinfo data updates every ~8-15 seconds. Polymarket price oracle lags 30-120 seconds. Bot detects win probability shift BEFORE the market reflects it.

---

## Architecture

```
ESPN Cricinfo (15s refresh)
    ↓
Signal Engine (RRR + wickets + form + h2h → 0-100 score)
    ↓
Entry Logic (price ≤ 0.90 AND signal ≥ 70 AND risk check)
    ↓
Polymarket CLOB → Market Buy
    ↓
Exit Monitor (every 20s) → Stop Loss @ 0.85 OR Wait for resolution
    ↓
Telegram Bot (alerts + commands)
```

---

## Quick Start (5 Steps)

### Step 1: Clone & Install

```bash
git clone <your-repo-url>
cd polymarket-cricket-bot
pip install -r requirements.txt
```

### Step 2: Configure Environment

```bash
cp .env.example .env
# Edit .env with your values (see below)
```

**Required .env values:**

| Variable | Where to get it |
|---|---|
| `PRIVATE_KEY` | Your Polygon wallet private key |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram → /newbot |
| `TELEGRAM_CHAT_ID` | Run `python get_chat_id.py` (see Step 3) |
| `CRICKET_DATA_API_KEY` | https://cricketdata.org → Sign up → API Key (free) |

### Step 3: Get Your Telegram Chat ID

```bash
# First, set TELEGRAM_BOT_TOKEN in .env
# Then open Telegram, message your new bot (/start)
python get_chat_id.py
# Copy the Chat ID printed to terminal → paste into .env
```

### Step 4: Generate Polymarket API Credentials

```bash
python setup_credentials.py
# This signs a message with your wallet to derive API credentials
# Saved to polymarket_creds.json automatically
```

### Step 5: Run the Bot

```bash
python main.py
```

You should receive a Telegram message: **"🤖 Polymarket Cricket Bot ONLINE"**

---

## Telegram Commands

| Command | Description |
|---|---|
| `/status` | Bot status, circuit breaker, open positions, P&L |
| `/positions` | All open positions with live P&L |
| `/pnl` | Full P&L report (wins, losses, win rate) |
| `/history` | Last 10 closed trades |
| `/balance` | Wallet USDC balance |
| `/close <id>` | Force close a position (triggers stop loss sell) |
| `/pause` | Pause new entries (existing positions still monitored) |
| `/resume` | Resume trading + reset circuit breaker |
| `/signal <team>` | Check signal for a team |
| `/help` | Full command list |

---

## Deploy to Render.com (Free 24/7)

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Initial bot"
git remote add origin https://github.com/YOUR_USERNAME/polymarket-cricket-bot.git
git push -u origin main
```

**IMPORTANT:** Add `.gitignore` before pushing:
```
.env
polymarket_creds.json
*.db
logs/
__pycache__/
*.pyc
```

### 2. Connect to Render.com

1. Go to [render.com](https://render.com) → New → **Background Worker**
2. Connect your GitHub repo
3. Render auto-detects `render.yaml`
4. Set environment variables in **Render Dashboard → Environment tab:**
   - `PRIVATE_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `CRICKET_DATA_API_KEY`
   - `CLOB_API_KEY` / `CLOB_API_SECRET` / `CLOB_API_PASSPHRASE` (from setup_credentials.py)

5. Click **Deploy**

### 3. Keep Alive with UptimeRobot (Prevents free tier sleep)

1. Go to [uptimerobot.com](https://uptimerobot.com) → Free account
2. Add Monitor → HTTP(S)
3. URL: `https://your-render-app.onrender.com/health`
4. Interval: **every 5 minutes**

This pings your health endpoint, keeping Render's free worker awake.

---

## Strategy Details

### Entry Conditions (ALL must be true)
1. Polymarket YES token price ≤ 0.90 USDC
2. Cricket signal score ≥ 70/100 (or price ≤ 0.88 for price-only entry)
3. Risk checks pass (balance, exposure, no duplicate position)
4. Market volume ≥ $100 (liquidity guard)

### Signal Engine Components

| Component | Weight | What it measures |
|---|---|---|
| Win probability estimate | 35 pts | Heuristic based on match state |
| RRR vs CRR ratio | 25 pts | Required vs current run rate |
| Wickets in hand | 15 pts | Batting team's resources |
| Momentum (last 3 overs) | 15 pts | Recent scoring rate |
| Team recent form | 5 pts | Last 5 match win rate |
| Head-to-head history | 5 pts | Historical matchup advantage |

### Exit Conditions
- **Win:** Market resolves → USDC auto-claimable. P&L ≈ +10%
- **Stop loss:** Bid price drops to ≤ 0.85 → Bot places limit sell at 0.85
  - If not filled in 90s → retry at 0.83, then 0.81, floor at 0.70

### Risk Controls
- Max concurrent positions: 5 (configurable)
- Max total exposure: $5 USDC (configurable)
- Circuit breaker: auto-pause after 3 consecutive stop losses
- No duplicate positions in same market

---

## Approved Teams

**International:** Afghanistan, Australia, Bangladesh, England, India, Ireland, New Zealand, Pakistan, South Africa, Sri Lanka, West Indies, Zimbabwe (+ Women's versions of each)

**IPL:** All 10 franchise teams

**Excluded:** County cricket, Ranji Trophy, BBL, The Hundred, U-19, practice matches

---

## Files

```
main.py                      # Entry point — starts all loops
config.py                    # All config + environment vars
database.py                  # SQLite persistence
logger.py                    # Colored logging
setup_credentials.py         # One-time Polymarket auth setup
get_chat_id.py               # Find your Telegram chat ID
requirements.txt
render.yaml                  # Render.com deploy config

cricket/
  api_client.py              # ESPN scraper + cricketdata.org adapter
  signal_engine.py           # 0-100 signal score calculator
  match_filter.py            # Team/tournament whitelist

polymarket/
  client.py                  # CLOB API wrapper (buy/sell/price)
  market_scanner.py          # Gamma API market discovery

strategy/
  entry_logic.py             # Buy decision logic
  exit_logic.py              # Stop loss monitor + execution
  risk_manager.py            # Pre-trade gates + circuit breaker

telegram_bot/
  bot.py                     # Commands + proactive alerts
```

---

## Testing with $5

1. Start with `TRADE_AMOUNT_USDC=1.0` and `MAX_TOTAL_EXPOSURE_USDC=5.0`
2. Watch the Telegram bot for signals and trades
3. Check `/status` every few hours
4. After 5-10 trades, review `/pnl` to validate strategy

---

## Upgrading Cricket Data (Optional)

The bot works with free ESPN scraping. For better data quality:

1. **cricketdata.org paid plan** — More API calls, better coverage
2. **RapidAPI Cricbuzz** — Most reliable, ~$10/month

To use a paid API, update `cricket/api_client.py` — the adapter pattern makes this easy.
