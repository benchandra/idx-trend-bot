# IDX Trend Bot v2 (automatic, approval-ready)

Runs by itself on GitHub's free servers. You type nothing in: you get Telegram messages,
and a dashboard page that updates itself. New in v2: every order is a **proposal** that
passes deterministic checks and an AI review committee before you (or a policy) approve it,
with a full audit trail, a risk gate you can halt from Telegram, and support for a real
IPOT / Stockbit account alongside the paper account.

## What it does on its own (Jakarta time, weekdays)

- **17:30 (retry 19:00) — End of day.** Fills the paper account's orders at the day's real
  prices, reruns the model (v2 rules), builds the next session's proposals, runs the checks
  (IDX rules, cash, risk limits), asks the committee (if you gave it a key), decides what the
  approval mode allows, and sends a summary. Proposals that need your tap arrive as separate
  messages with **Approve / Halve / Reject** buttons.
- **08:30 — Pre-open.** Collects your taps, applies the mode's default to anything you did
  not answer, and sends the final order list (and writes the slip for your real account).
- **09:00–15:30, every 30 minutes — Intraday.** Prices and news for the bot's stocks; alerts on
  stops, price limits, IHSG falls (−3/−5/−8%) and strong news; moves the risk gate to
  REDUCING / HALTED on big market falls; picks up `/halt`, `/resume`, `/status`, `/fill`.
- **1st of each month.** Reruns the 10-year backtest.

The paper account starts with Rp100,000,000. The bot never touches your real brokerage
account: with `BROKER=slip` it writes an order slip for you and tracks the fills you confirm.

## One-time setup (about 15 minutes)

### 1. Telegram bot (same as v1; skip if done)
1. In Telegram, open **@BotFather**, tap **Start**, send `/newbot`, give it a name and a
   username ending in `bot`. Copy the **token**.
2. Open your new bot and tap **Start** (it can only message you after this).
3. Open **@userinfobot**, tap **Start**, copy your **Id**.

### 2. Put the bot on GitHub
1. Sign in at github.com. Top right **+**, **New repository**, name `idx-trend-bot`,
   choose **Public** (needed for the free dashboard page; no personal data is stored),
   **Create repository**.
2. Click **uploading an existing file**. Unzip the file from Claude, open the
   `idx-trend-bot` folder, select everything inside it (including the hidden `.github`
   folder) and drag it into the browser. **Commit changes.**
   - On a Mac, if `.github` is invisible: **Cmd + Shift + .** in Finder.
   - Check: the repository shows `.github`, `bot`, `deploy`, `docs`, `README.md`, `requirements.txt`.
   - If you already have the v1 repository: upload the new files over it (same names) and
     add the new ones. Your `docs/data/portfolio.json` keeps its history.

### 3. Secrets (Settings → Secrets and variables → Actions → New repository secret)
| Name | Value | Needed |
|---|---|---|
| `TELEGRAM_TOKEN` | the token from step 1 | yes |
| `TELEGRAM_CHAT_ID` | your Id from step 1 | yes |
| `ANTHROPIC_API_KEY` | a key from console.anthropic.com (billed per use) | optional: switches on the committee and better news scores |

### 4. Variables (same page, **Variables** tab → New repository variable)
| Name | Values | Default | What it does |
|---|---|---|---|
| `APPROVAL_MODE` | `auto`, `policy`, `manual` | `auto` | see "Approval modes" below |
| `BROKER` | `paper`, `slip` | `paper` | `slip` also writes an order slip for your real account and tracks `/fill` confirmations |
| `BROKER_FEES` | `ipot`, `stockbit`, `ajaib`, `mirae`, `bni` | `ipot` | fee profile used by the model and the backtest |
| `RULES` | `v2`, `v1` | `v2` | v2 = keep while in the top 16, resize only when >50% off target |
| `COMMITTEE` | `lite`, `tradingagents`, `off` | `lite` | which AI review runs (needs the Anthropic key) |
| `START_CASH` | e.g. `100000000` | 100000000 | paper account size (set before the first run) |
| `RUNNER` | `vm` | (empty) | set to `vm` once the always-on box is running, so the scheduled workflows stand down |

Ben's starting settings: `APPROVAL_MODE=manual`, `BROKER_FEES=stockbit`, `START_CASH=100000000`, `BROKER=paper`.

### 5. Dashboard page
**Settings → Pages**: *Deploy from a branch*, branch **main**, folder **/docs**, **Save**.
A minute later it is live at `https://YOUR-USERNAME.github.io/idx-trend-bot/`.

### 6. First run
**Actions** tab (enable workflows if asked) → **End of day** → **Run workflow**. About 3
minutes later the summary arrives in Telegram. Then everything runs on schedule.

## Approval modes

| Mode | Buys | Sells (risk-reducing) | If you don't tap by 08:30 |
|---|---|---|---|
| `auto` (paper default) | approved when all checks pass; committee veto is applied | approved | — |
| `policy` (recommended for real money after paper) | approved when all checks pass; a committee veto is sent to you with buttons (you can override the AI, never a hard block) | approved | rejected |
| `manual` (first weeks of real money) | every one waits for your tap | wait for your tap too | rejected |

Deterministic blocks (odd lots, bad tick, outside ARA/ARB, suspended, no cash, risk-gate
limits) can't be overridden by anyone. The risk gate goes **REDUCING** (sells only) on a
−4% day, after 5 losing sessions in a row (cooldown), or IHSG −5% intraday, and **HALTED** (nothing) at −15% drawdown or IHSG −8%.
Automatic states lapse at the next end of day; `/halt` from Telegram stays until `/resume`.

**Manual mode and the shadow account.** In manual mode the paper account only trades what you
approve, so its record reflects your taps. A second *shadow* account (`docs/data/portfolio_shadow.json`)
takes every proposal automatically and is the pure-strategy record; both are shown in the daily
message and on the Proposals tab. Missed taps cost nothing in paper mode, but they do change the
paper account's history — the shadow account is the one to judge the rules by.

## Telegram commands
- `/status` — risk gate state, mode, proposals waiting for you.
- `/halt` — stop all new orders immediately. `/resume` — back to normal.
- `/fill BBCA 6250 5` — record a real fill (stock, price, lots) when `BROKER=slip`.

## Approving on the dashboard (no Telegram needed)
Proposals tab → each proposal waiting for you has **Approve / Halve / Reject** buttons. A tap opens a
pre-filled GitHub issue page (you must be signed in to GitHub on that phone or laptop); press **Submit new
issue**. Within a minute the *Dashboard approvals* workflow records it and closes the issue with a
confirmation comment. The decision is applied at the next pre-open (08:30 WIB) or within 5 minutes on
the always-on box. Only issues opened by the repository owner count. Telegram buttons keep working in
parallel; the first decision wins.

## Your IPOT / Stockbit account
Nothing to connect while paper trading: `BROKER=paper` needs no account. When you go live, this is the
setup (both apps work; the bot only needs one fee profile and one place to enter orders).

**Which one**
- **Stockbit** for the fee profile and entering orders: cheaper (0.15% buy / 0.25% sell vs IPOT's 0.19% /
  0.29%, ≈0.3% of the account per year at v2's turnover) and its web app works on a laptop for the
  browser-assisted flow. Set `BROKER_FEES=stockbit`.
- **IPOT** if you want exchange-side stop orders: IPOT's Smart Order (auto buy/sell at a trigger price)
  lets you park the bot's stop levels in the app so exits fire even when the bot is down. If you use IPOT
  for orders, set `BROKER_FEES=ipot`.
- Don't run the same strategy in both accounts: pick one for real orders, keep the other for its features.

**Setup for real orders (later, after paper)**
1. Variables: `BROKER=slip`, `APPROVAL_MODE=manual` (first weeks) then `policy`, `BROKER_FEES=stockbit` or `ipot`,
   `START_CASH` = the cash you actually deposit.
2. Each evening the proposals arrive; approve on the dashboard or in Telegram.
3. 08:30 message + `docs/data/slip.json` = the order list. Open Stockbit (or IPOT), enter each order as a
   **limit order at the listed price**, lots as listed, before 09:00. Your PIN and the final tap stay yours.
4. Confirm each fill in Telegram: `/fill AKRA 1480 53` (stock, price, lots). Unconfirmed orders expire at the
   close; the Audit tab and `docs/data/real_account.json` keep the chain proposal → checks → approval → fill.
5. Stops: copy the stop levels from the Today tab into IPOT Smart Order (or Stockbit's automatic order if
   your app version offers one) once a week; the bot's own alerts stay on as a second layer.
6. No Indonesian broker offers a retail order API. Reverse-engineered apps breach the terms of service and
   can get an account frozen, so the bot never logs in for you.

**IBKR?** Not easier: a full brokerage application, USD funding by international transfer, and its API
needs a gateway program running somewhere. It also trades US/global stocks, not IDX, so the strategy
would need its own validation. Park it.

## Always-on box (free VM) — click-by-click
GitHub's timers start late and run at best every 30 minutes. A small always-on VM runs the whole
schedule itself (end of day 17:30/19:00, pre-open 08:30, intraday every 5 minutes, taps and `/halt`
picked up within 5 minutes, heartbeats 08:45 and 16:15, monthly backtest) and pushes the results to
GitHub so the dashboard keeps working. Do this after the first GitHub run has succeeded.

1. **GitHub token for the box.** GitHub → your photo → **Settings → Developer settings → Personal access
   tokens → Fine-grained tokens → Generate new token.** Name `idx-vm`, expiration 1 year, *Repository
   access: Only select repositories → idx-trend-bot*, *Permissions → Repository → Contents: Read and
   write*. Generate, copy the token (starts with `github_pat_`).
2. **Fill the template.** Open `deploy/cloud-init.yaml` from the zip in a text editor; replace every
   `<...>` (Telegram token and id, your GitHub username, the token). Keep `APPROVAL_MODE=manual`,
   `BROKER_FEES=stockbit`. Select all, copy.
3. **Oracle Cloud (Always Free).** oracle.com/cloud/free → Start for free → sign up (a card is required for
   identity; the always-free shapes are never billed). Home region: Singapore. Then in the console:
   ☰ → **Compute → Instances → Create instance**.
   - Name `idx-bot`. *Image and shape → Edit*: image **Ubuntu 24.04**; shape **Ampere → VM.Standard.A1.Flex,
     1 OCPU, 6 GB** (if "Out of capacity", choose **AMD → VM.Standard.E2.1.Micro**, also always free).
   - *Networking*: leave defaults (a public IP is assigned).
   - *Add SSH keys*: "Generate a key pair for me" and download it (kept for emergencies only).
   - **Show advanced options → Management → Cloud-init script**: paste the filled template.
   - **Create.** Within ~5 minutes the VM installs itself and sends a Telegram heartbeat.
   - Fallback if Oracle signup fails: Google Cloud free tier (**e2-micro** in a US region, Ubuntu 24.04);
     paste the template under *Advanced options → Management → Automation (Startup script)* as user-data.
4. **Tell GitHub to stand down.** Repository → Settings → Secrets and variables → Actions → Variables →
   **New repository variable** `RUNNER` = `vm`. The scheduled workflows now skip; the *Dashboard approvals*
   workflow keeps running. Delete the variable to fall back to GitHub any time.
5. **Check.** Two heartbeats a day (08:45, 16:15 WIB). If they stop: Oracle console → the instance →
   *Reboot*; if that doesn't fix it, ask Claude with a screenshot of `journalctl -u idx-monitor`.
6. **Updates.** New bot versions: upload the files to GitHub as usual; the box pulls them before every job.

## Backtest on real IDX data, 2016 → 25 Sep 2026 (walk-forward, after costs)

| | Yearly return | Volatility | Sharpe | Worst fall | PSR | Trades/yr | Costs/yr |
|---|---|---|---|---|---|---|---|
| v1 rules (IPOT fees) | +1.4% | 11.9% | 0.17 | −33% | 0.72 | 198 | 4.9% |
| **v2 rules (IPOT fees)** | **+3.8%** | **11.3%** | **0.39** | **−27%** | **0.90** | **91** | **2.4%** |
| v2 rules (Stockbit fees) | +3.9% | 11.2% | 0.40 | −27% | 0.91 | 91 | 2.1% |
| IHSG (price index) | +3.0% | 16.1% | 0.27 | −42% | 0.81 | — | — |

Against the IHSG the v2 line has a beta of about 0.4 and an alpha of about +3% a year, but the alpha's
t-statistic is only ~1.0 (Newey-West): consistent with a small edge, also consistent with none.

v2 is trial #2. Deflated for two trials the probability that its true Sharpe is above zero
is 0.85 (needs 0.95 to count as evidence), and the minimum track record for that confidence
is about 18 years versus the 10.7 available. So: better than v1, better than the index on
risk-adjusted terms, **still paper money only**.

## Good to know
- **Near-live, not tick-by-tick.** Yahoo prices can lag by minutes; GitHub timers start late.
- **The committee is advisory.** It can only agree, halve or veto a buy; every veto is logged
  with what the model alone would have done, so it can be scored after a few months.
- **To pause everything:** Actions, pick each workflow, ••• menu, Disable workflow — or send `/halt`.
- **If a run fails:** Actions shows a red mark. Open it for the error, or ask Claude.
- Research tool, not financial advice.
