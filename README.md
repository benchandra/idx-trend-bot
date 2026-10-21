# IDX Trend Bot (automatic version)

Runs by itself on GitHub's free servers. You don't type anything in: you get
Telegram messages, and a dashboard page that updates itself.

## What it does on its own (Jakarta time, weekdays)

- **09:00-15:30, every 30 minutes:** checks prices and news for the bot's stocks.
  A Telegram alert arrives only when something needs attention: a holding below its
  stop level or near its daily price limit, the IHSG down 3%, 5% or 8%, or strongly
  positive or negative news on a holding.
- **17:30 (retry at 19:00):** fills the model account's orders at the day's real
  prices, reruns the model, sets the next session's orders, scores the day's news,
  and sends a summary.
- **1st of each month:** reruns the 10-year backtest.

The model account is paper money (Rp100,000,000 to start). The bot never touches
your real brokerage account.

## One-time setup (about 15 minutes)

### 1. Make a Telegram bot for the alerts
1. In Telegram, search for **@BotFather**, open it, tap **Start**, send `/newbot`.
2. Give it any name, then a username ending in `bot` (for example `ben_idx_alerts_bot`).
3. BotFather replies with a **token** (looks like `123456789:AAH...`). Copy it.
4. Search for your new bot, open it and tap **Start** (it can only message you after this).
5. Search for **@userinfobot** and tap **Start**. It replies with your **Id** (a number). Copy it.

### 2. Put the bot on GitHub
1. Sign in or sign up at github.com.
2. Top right **+**, then **New repository**. Name it `idx-trend-bot`, choose **Public**
   (needed for the free dashboard page; nothing personal is stored in it), then
   **Create repository**.
3. On the new page click **uploading an existing file**. Unzip the file from Claude,
   open the `idx-trend-bot` folder, select everything inside it (including the
   `.github` folder) and drag it into the browser. Click **Commit changes**.
   - On a Mac, if `.github` is invisible, press **Cmd + Shift + .** in Finder.
   - Check: the repository now shows `.github`, `bot`, `docs`, `README.md`, `requirements.txt`.

### 3. Add your Telegram details (kept secret, never visible to others)
1. In the repository: **Settings**, **Secrets and variables**, **Actions**, **New repository secret**.
2. Name `TELEGRAM_TOKEN`, value: the token from step 1. Save.
3. Name `TELEGRAM_CHAT_ID`, value: your Id from step 1. Save.
4. Optional, for better news scoring: `ANTHROPIC_API_KEY`, a key from
   console.anthropic.com (billed separately from a Claude subscription). Without it
   the bot uses a simpler keyword score.
5. Optional: on the **Variables** tab add `START_CASH` (for example `100000000`)
   before the first run, to change the model account's size.

### 4. Switch on the dashboard page
**Settings**, **Pages**. Under *Build and deployment* choose **Deploy from a branch**,
branch **main**, folder **/docs**, then **Save**. A minute or two later it is live at
`https://YOUR-USERNAME.github.io/idx-trend-bot/`. Bookmark it on your phone.

### 5. First run
Open the **Actions** tab (enable workflows if GitHub asks), click **End of day**,
then **Run workflow**. About 3 minutes later a Telegram message should arrive.
From then on everything runs on schedule.

## Version 2 (20 October 2026)

**What changed**
- **Hysteresis:** new positions still come from the top 8, but a holding is kept while it stays inside the top 16 and in an uptrend. This removes most of the weekly in-and-out churn.
- **Coarser resizing:** a position is only resized when it is off its target by more than half (and at least 2% of the account).
- **Market brake on the bot's own basket:** the 200-day brake now watches the equal-weighted basket of the 30 stocks instead of the IHSG, whose composition changes in October 2026 (BEI removes the HSC stocks). The IHSG's own trend is still shown.
- **Censored days:** while IDX's lower price limit is 10% or tighter (7% from 19 Oct to 31 Dec 2026), daily moves are cut short, so the volatility used for sizing and stops is floored at the 2-day realised volatility. The price-limit table was updated for the 7% rule.

**Backtest, 4 Jan 2016 to 19 Oct 2026, after costs (prices only):**

| | Per year | Volatility | Sharpe | Worst fall | Costs a year | Trades a year |
|---|---|---|---|---|---|---|
| v1 rules, same data fixes | −0.8% | 12.5% | 0.00 | −35% | 5.2% | 204 |
| **v2** | **+2.7%** | 11.6% | 0.29 | −27% | 2.5% | 93 |
| IHSG | +0.9% | 16.6% | 0.14 | −45% | – | – |

Two rule versions have now been tried on the same data. The deflated Sharpe ratio for v2 (the chance its
Sharpe is really above zero after allowing for two tries) is **0.73**: encouraging, not proof. It stays paper
money. The monthly backtest reruns both versions and keeps the trial count in `docs/data/trials.json`.

## Trial 3: the stress gauge (20 October 2026) — shown, not applied

The dashboard now carries an **IDX stress gauge (IDX-SVI)**: seven readings (basket realised volatility, the
IHSG GARCH forecast, Cboe's emerging-market implied volatility VXEEM, the big banks lagging the basket as a
foreign-flow proxy, the share of stocks down more than 3% or limit-locked, USD/IDR volatility, and a Williams
VIX Fix) turned into z-scores over the past year and averaged with fixed weights. Below 0.5 is normal, 0.5 to
1.5 elevated, above 1.5 risk-off. It is on the Today tab with a chart and a "what is driving it" table, and in
the Telegram summary.

It was also tested as a third tier of the brake (at most 70% invested and no new positions when elevated, at
most 30% when risk-off, 50% around MSCI announcement dates). **That version did not beat v2**: +1.9% a year
against +2.7%, a worse worst fall (−30% against −27%) and a lower deflated Sharpe (0.63 with three tries).
The gauge is reactive: it jumped from −0.1 to 2.0 only on the day of the 16 October 2026 crash, then kept
the bot out of the rebounds. So v2 stays the rulebook, the gauge is information, and the thresholds were
deliberately not tuned (tuning them on the same test would just be more tries). `backtest_v3.json` keeps the
result; `python -m bot.engine --rules v3` reruns it.

## Live dashboard (near-live, not tick data)

True real-time prices need a licensed feed or a broker API, and no Indonesian broker offers one to retail.
What is possible for free: Yahoo Finance 5-minute bars, usually a few minutes behind the exchange.

- **Level 1 (already on):** the Intraday workflow now runs every 15 minutes during the session and writes
  `live.json`; the Today tab shows a Live card and refreshes it every minute. GitHub's timers can start late,
  so expect 5–20 minutes of delay.
- **Level 2 (about 1–5 minutes behind):** run `python -m bot.live --every 60` on any machine that stays on
  (a free always-on VM such as Oracle Cloud's free tier, a Raspberry Pi, or a laptop). Set two environment
  variables so it can push: `GITHUB_REPO` (for example `ben/idx-trend-bot`) and `GITHUB_TOKEN` (GitHub →
  Settings → Developer settings → Fine-grained tokens → this repository only → Contents: read and write).
  It writes to a separate `live` branch so the dashboard page is not rebuilt on every push.
- What is not possible: tick-by-tick, order-book depth, or placing orders. Your broker app stays the place
  for that; the dashboard is the place to see what the bot thinks.

## Mirror mode: run the bot on what you actually own (20 October 2026)

The model account can be seeded from your real holdings, so every evening's orders apply to your real book
(you still place them yourself in your broker app).

1. Actions → **Seed account** → Run workflow. Positions as `SYMBOL LOTS AVERAGE_PRICE`, comma-separated,
   for example `BBCA 43 6562, BMRI 66 4956`; cash = free cash in the account.
2. Only names in the bot's 30-stock universe are taken. Anything else (GOTO, AADI, …) is left alone and is
   not in the bot's equity figure.
3. The bot then runs its end-of-day job on that book and sends the orders to Telegram. "Since start" on the
   dashboard measures against your cost basis, so it will show your real loss.
4. Re-seed whenever the real account and the model account drift apart (orders you skipped, new deposits).

## Kronos layer (added October 2026)

[Kronos](https://github.com/shiyu-coder/Kronos) is an open-source foundation model for candlestick
charts (MIT licence, AAAI 2026). The bot now runs it after every close, as a second opinion that
**never places orders**:

- For the IHSG, the bot's holdings and targets, and the three big banks, Kronos reads the last
  400 daily bars and samples 16 possible paths for the next 5 sessions.
- You get: the share of paths that end higher, the median move, the model's own 10th-90th
  percentile range, and whether its risk view is wider or tighter than the bot's GARCH forecast.
- Where to see it: a short "Kronos view" block in the Telegram summary, and the **Kronos** tab on
  the dashboard (chart of the IHSG with the sampled paths, a table per stock).
- Every reading is appended to `docs/data/kronos_log.csv` so it can be judged later, the same way
  news scores are logged.

**Is it any good?** Nobody knows yet for IDX. Kronos was trained on data up to 2024/25, so only
forecasts made after that count. Two ways to find out:
1. **Actions → Kronos check → Run workflow.** It re-forecasts each of the last 30 sessions using
   only data available at the time and scores them (direction hit rate, range coverage, probability
   score) against simple baselines. The result appears in the Kronos tab.
2. Wait a few months and let the daily log build up.

Do not switch the rulebook to Kronos until those checks say it beats the baselines after costs.

**Settings (Settings → Secrets and variables → Actions → Variables, all optional):**
`KRONOS` = `off` to skip it; `KRONOS_PATHS` (default 16, more is slower and steadier);
`KRONOS_NAMES` = `all` to score all 30 stocks (about 10 minutes on GitHub's runners).

**Files:** `bot/kronos_signal.py` (the layer), `bot/kronos/` (the model code, MIT), the
`End of day` and `Kronos check` workflows. PyTorch (CPU build, about 200 MB) installs inside the
workflows; nothing changes on your phone or laptop.

## Day to day
Nothing to do. Read the Telegram messages; open the dashboard for detail.

## Good to know
- **Near-live, not tick-by-tick.** Free prices from Yahoo Finance can lag the
  exchange by several minutes, and GitHub's timers sometimes start late. Fine for a
  daily strategy, not for split-second trading.
- **Version 1 lost to the IHSG in its backtest** (1.4% against 3.2% a year,
  2016-2026). Treat it as paper trading; see the dashboard's Backtest tab.
- **News scores don't trigger trades.** They are saved daily in
  `docs/data/sentiment_log.csv` so they can be tested later.
- **To pause:** Actions, pick a workflow, the ••• menu, Disable workflow.
- **If a run fails:** Actions shows a red mark. Open it for the error, or ask Claude.
- Research tool, not financial advice.
