# Research ledger — IDX Trend Bot

Every rule variant that has been run on the data is listed here, because the deflated Sharpe ratio (DSR) has to
count them. Every planned change is written down *before* it is run: what changes, why (which paper), what we
expect. A result that was not pre-registered is a fishing expedition, not a test.

## 1. Trials run so far (same data, 4 Jan 2016 – 19 Oct 2026, hand-picked 30-stock universe)

| Trial | Rules | CAGR | Sharpe | Worst fall | Costs/yr | Verdict |
|---|---|---|---|---|---|---|
| 1 | v1: top-8 rotation, 2% band, IHSG 200-day brake | −0.8% | 0.00 | −35% | 5.2% | baseline |
| 2 | v2: keep holdings inside top 16, resize only if off by half, basket brake, censored-vol floor | **+2.7%** | **0.29** | **−27%** | 2.5% | **production** (DSR 0.72, N=2) |
| 3 | v3: v2 + three-tier stress gauge + MSCI event windows | +1.9% | 0.24 | −30% | 2.3% | failed; gauge kept for display only |

IHSG over the same window: +0.9%/yr, Sharpe 0.14, worst fall −45%.

## 2. The universe problem (21 Oct 2026) — this changes the headline numbers

The 30 names were chosen in 2026 (hindsight). Using BEI's own LQ45 lists, applied date by date, over the window
where we have them (Feb 2022 – Oct 2026, 20 announcements, 77 names, WSKT missing from Yahoo):

| Universe, v2 rules, Feb 2022 – Oct 2026 | CAGR | Sharpe | Worst fall | Exposure |
|---|---|---|---|---|
| Hand-picked 30 (hindsight) | −0.4% | 0.03 | −18.5% | 68% |
| **LQ45 point-in-time (honest)** | **−5.8%** | **−0.43** | **−33.9%** | 58% |
| IHSG | −4.8% | −0.18 | −45.3% | — |
| LQ45 names, equal-weighted, no costs | +6.4% | 0.42 | −37.1% | — |

Read: the hindsight universe was worth about +5 percentage points a year in this window. On the honest universe
the rulebook loses slightly more than the index with lower volatility. Momentum selection on LQ45 names
underperformed holding the same names equally, which is what the literature on Indonesian momentum predicts
(section 3). Year by year, honest universe: 2022 −2.7%, 2023 −6.4%, 2024 +2.3%, 2025 +5.5%, 2026 −23.3%.

Consequences:
- The 10-year numbers in section 1 are upper bounds. The honest baseline before 2022 needs the LQ45 lists for
  2016–2021 (not on the IDX website; to be compiled from BEI announcements and press lists).
- Trials 4 onward are run on the point-in-time universe, from Feb 2022, unless the older lists are found.
- Production stays on the hand-picked 30 for now (switching the live universe to LQ45 would pull in the HSC names
  such as BREN, DSSA, CUAN unless the exclusion list is populated from BEI's October 2026 HSC announcement).

## 3. Paper cards (what each paper says and what it means for this bot)

Format: signal · horizon/rebalance · universe · costs assumed · after-cost result · sample · data needed → implication.

**Chui, Titman & Wei (2010), Individualism and Momentum around the World, J. Finance.** Cross-sectional momentum
in 41 countries; profits are lower in collectivist, less-individualistic markets. Indonesia sits in the low-momentum
group. Monthly, 6-month formation / 6-month holding, no costs. → Prior for the core signal is weak here.

**Chui, Titman & Wei (2000 working paper), Momentum, Legal Systems and Ownership Structure: Asian markets.** Eight
Asian markets: the momentum effect is present in all except **Korea and Indonesia**, and is weak and statistically
significant only for Hong Kong over the full sample. → Same conclusion, Indonesia explicitly.

**Griffin, Ji & Martin (2003), Momentum Investing and Business Cycle Risk, J. Finance.** 40 countries. The Indonesia
row in their country table shows a momentum profit of about **−1.0% per month with t ≈ −3.6** (sample from Dec 1990).
No costs. → In their sample Indonesian momentum was significantly *negative*. The strongest warning in the pile.

**Jegadeesh & Titman (1993), J. Finance.** The original: form on 3–12 month past returns, skip the last week/month,
hold 3–12 months; roughly 1% a month in the US before costs. → The bot's 126-day momentum has no skip-month;
trial 5 adds it.

**Asness, Moskowitz & Pedersen (2013), Value and Momentum Everywhere, J. Finance.** Momentum = past 12-month return
skipping the most recent month; monthly rebalance; eight markets/asset classes (US, UK, Europe, Japan; no Indonesia).
→ Standard definition and cadence; the bot is weekly and 6-month. Trials 5 and 7.

**Barroso & Santa-Clara (2015), Momentum Has Its Moments, JFE.** Scale momentum by its own realized volatility
(6-month daily): Sharpe roughly doubles and crash risk falls. US, monthly. → Trial 8 (which vol to scale by).

**Daniel & Moskowitz (2016), Momentum Crashes, JFE / NBER w20439.** Momentum crashes happen in rebounds after
market declines when past losers rally violently. Dynamic weights using forecast momentum vol and market state.
→ Exactly the regime of Oct 2026 onward; a reason to be slow re-entering momentum after a crash.

**Moreira & Muir (2017), Volatility-Managed Portfolios, J. Finance / NBER w22208.** Scale exposure by 1/σ² of the
previous month's realized variance; raises Sharpe for many factors. → The bot scales by 1/σ (GARCH); trial 8.

**Blitz, Huij & Martens (2011), Residual Momentum, J. Empirical Finance.** Momentum on residuals of a factor
regression: similar returns, about half the volatility, smaller crashes, more robust across time. → Trial 6.

**Maillard, Roncalli & Teïletche (2010), Equally-Weighted Risk Contribution portfolios.** Properties of ERC: sits
between minimum variance and equal weight; uses the correlation matrix. → Trial 4 (replace inverse-vol).

**Kaminski & Lo (2014), When Do Stop-Loss Rules Stop Losses?** Stops add value when returns have momentum/regime
structure and subtract value when they are mean-reverting; framework for measuring it. → Trial 9 (stop width cap).

**Gârleanu & Pedersen (2009/2013), Dynamic Trading with Predictable Returns and Transaction Costs.** With costs,
trade partially toward an "aim" portfolio each period rather than all the way. → Trial 10 (partial adjustment).

**Hurst, Ooi & Pedersen (2014), A Century of Evidence on Trend-Following, AQR.** Time-series momentum on futures
since 1880 is consistently profitable with low correlation to equities. → Supports a trend *brake*; says nothing
about picking stocks within one market.

**Richards (2004/2005), Big Fish in Small Ponds, RBA / JFQA.** Foreign investors' daily flows in six Asian markets
(incl. Indonesia) are positive-feedback and have large price impact. → Foreign flow is a real state variable here;
data is the obstacle.

**Dvořák (2005), Do Domestic Investors Have an Information Advantage? Evidence from Indonesia, J. Finance.** Clients
of local brokers earn higher short-term profits; clients of global brokers do better long term. → Price action in
IDX is shaped by informed locals; short-horizon signals from prices alone are at a disadvantage.

**Bailey & López de Prado (2014), The Deflated Sharpe Ratio, JPM.** The formula this ledger uses. → Keep counting.

**Santosa & Santoso (2019), Does Exchange Rate Volatility cause Overreaction?, IJFA.** LQ45 stocks during the 2018–19
rupiah swing show overreaction and reversal. Small sample. → Supports short-term reversal in IDX, hence skip-month.

**Syamni et al. (2021), The Momentum Strategy of Small Foreign Investors in the IDX, JAFEB.** Three months of 2017
intraday data, 35 LQ45 names. Too short to inform a daily strategy. → Not used.

## 4. Pre-registered trials (run in this order, one at a time, on the point-in-time universe from Feb 2022)

| # | Change | Based on | Expected | Stop rule |
|---|---|---|---|---|
| 4 | Equal Risk Contribution weights instead of inverse-vol (correlation-aware) | Maillard et al. 2010 | lower bank concentration, lower drawdown, similar return | keep if Sharpe ≥ v2 and max DD improves |
| 5 | Momentum 6-1: 126-day return skipping the last 21 days | Jegadeesh-Titman 1993; Santosa 2019 | small Sharpe gain from avoiding reversal | keep if Sharpe improves |
| 6 | Residual momentum (residuals of a 36-month regression on the basket) | Blitz et al. 2011 | similar return, lower vol, smaller crashes | keep if Sharpe improves |
| 7 | Monthly rebalance (stops stay daily) | AMP 2013 | costs down ~40%, Sharpe up | keep if after-cost Sharpe improves |
| 8 | Scale by 1/σ² realized (21-day) instead of 1/σ GARCH | Moreira-Muir 2017; Barroso-Santa-Clara 2015 | lower exposure in turbulence | keep if Sharpe improves |
| 9 | Trailing stop width capped at 15% | Kaminski-Lo 2014 | fewer "stops that never trigger" | keep if max DD improves without Sharpe loss |
| 10 | Trade halfway to target each rebalance | Gârleanu-Pedersen | lower turnover | keep if after-cost Sharpe improves |

Every result, kept or not, is appended to section 1 with its DSR. Nothing is tuned on the three crash days.

## 5. Data still needed

- LQ45 constituent lists Feb 2016 – Jan 2022 (BEI announcements Peng-…/BEI.OPP; press lists in Kontan, Bisnis,
  Pasardana, Ajaib). With these the honest backtest extends to 2016.
- BEI's October 2026 HSC list (80 names) to populate `EXCLUDE` before any switch of the live universe to LQ45.
- Daily foreign net buy/sell per stock (IDX daily statistics) for a foreign-flow factor.
