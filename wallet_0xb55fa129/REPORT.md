# Wallet 0xb55fa129… — Polymarket Bot Analysis

**Profile:** https://polymarket.com/@0xb55fa1296e6ec55d0ce53d93b9237389f11764d4-1777575277609
**Proxy wallet:** `0xb55fa1296e6ec55d0ce53d93b9237389f11764d4` (pseudonym "Lively-Authenticity")
**Data window:** Sep 16 23:00 → Sep 23 23:00 UTC, 2026 (7 days). The clean ledger uses the six full UTC days **Sep 17–22**.
All raw data and every computed table are in this folder (see §10). General analysis only, not financial advice.

---

## 1. TL;DR

- A **multi-market crypto Up/Down bot**. It trades 7 coins (BTC, ETH, SOL, XRP, BNB, DOGE, HYPE) across **5m, 15m, 1h and 4h** windows, plus a few "ETH/BTC above $X" strike markets. It covers **10,410 markets in 7 days**.
- **100% BUY, 0 sells**, holds everything to settlement. 91% of notional is taker orders.
- **Trading alone is slightly negative.** On $2.88M notional it earned +2.2% before fees and paid 2.25% in fees, so net before rebates was **−$1.7K**.
- **Rebates are the profit.** Taker rebates are paid at the **Diamond tier (exactly 44% of the previous day's fees)**, plus maker rebates. Six full days net **+$31.8K cash (~$5.3K/day)**, with **77% from rebates**. 5 of 6 days were positive.
- The **same 3-second momentum edge** as the first bot (x-MoneyForWhiskas). Taker fills after the coin moved ≥0.5 bp toward the side earn **+6.7% to +9.3% net**. Fills after an adverse move lose 10–15%.
- **Where it leaks:** BTC and ETH **5-minute** markets lose **−$19.8K** before rebates. The 15m, 1h and 4h markets and the small alt markets make it back.
- **Polymarket's numbers are misleading for this wallet.** The weekly leaderboard shows **−$298K on $5.98M volume**. Real cash volume was $2.95M, and on-chain flows match my cash ledger to the cent. The profile chart (+$1.216M lifetime) is pre-fee.

---

## 2. Account snapshot

| Field | Value |
|---|---|
| Lifetime volume (leaderboard) | $125.8M |
| Lifetime leaderboard P&L | $100.8K (rank ~1852). Methodology unclear. |
| Profile P&L chart | $1,216,048 lifetime since May 1 2026 (**pre-fee**) |
| Markets traded (lifetime) | 149,941 |
| Open position value (Sep 23) | ~$9.3K (mostly ETH/BTC 4h markets) |
| Weekly leaderboard | vol $5.98M, P&L −$298K (**unreliable**, see §8) |

Lifetime chart, weekly (pre-fee): May 8 $100K → Jun 5 $408K → Jul 3 $568K → Aug 7 $798K → Sep 4 $1.098M → Sep 18 $1.149M → Sep 23 $1.216M. Growth slowed in early September (+$51K from Sep 4 to Sep 18), then picked up again.

---

## 3. What it trades (7 days)

| Timeframe | Markets | Fills | Notional | Taker % | Buys both sides | Median pair cost | Median imbalance | Net before rebates |
|---|---|---|---|---|---|---|---|---|
| 5m | 6,084 | 70,662 | $1,290,613 | 98% | 77.5% | 1.007 | 29% | **−$19,776** |
| 15m | 2,938 | 41,079 | $800,192 | 98% | 72.8% | 1.058 | 36% | +$5,741 |
| 1h | 602 | 24,764 | $538,762 | 66% | 89.7% | 1.007 | 20% | +$5,731 |
| 4h | 168 | 8,002 | $211,894 | 92% | 93.5% | 0.969 | 25% | +$5,302 |
| strike ("above $X") | 618 | 3,872 | $43,228 | 76% | 27.7% | 1.16 | 100% | +$1,297 |

Top market families by notional (net % after fees, before rebates):

| Family | Notional | Pre-fee | Net |
|---|---|---|---|
| BTC 5m | $760,612 | +0.49% | **−1.91% (−$14,538)** |
| BTC 15m | $495,785 | +3.33% | +1.03% (+$5,092) |
| BTC 1h | $367,153 | +3.02% | +1.50% (+$5,523) |
| ETH 5m | $242,355 | +0.44% | **−2.03% (−$4,921)** |
| ETH 15m | $151,839 | +1.62% | −0.74% |
| BTC 4h | $141,778 | +3.61% | +1.29% |
| ETH 1h | $133,439 | +0.48% | −1.58% |
| SOL 5m | $101,846 | +1.73% | −0.81% |
| ETH 4h | $40,003 | +7.98% | +5.53% |
| SOL 1h | $25,705 | +10.0% | +8.05% |

Full table: `analysis/by_family.csv` (23 families).

**Fingerprint**
- Median fill $8.44 (p90 $43); 84% of fills are taker.
- Average price paid is 0.535. It leans toward buying the favourite more than bot #1 does (big notional at 60–90¢).
- Market-level P&L: mean −$0.16, median +$1.30, sd $123, win rate 53.3%.
- Less hedged than bot #1: it buys both sides in 74% of markets (bot #1: 99.6%), the median pair cost is **$1.02** (bot #1: $0.966), and the median imbalance is 33% (bot #1: 10%). **This bot takes much more directional risk.**

---

## 4. Economics

### 4.1 Six-day cash ledger (rebates matched to the day that earned them)
| Day (UTC) | Fills | Notional | Taker edge | Fees | Maker edge | Taker rebate | Maker rebate | **Net** |
|---|---|---|---|---|---|---|---|---|
| Thu Sep 17 | 19,143 | $263,395 | +825 | −5,010 | −3,782 | +2,205 | +333 | **−5,429** |
| Fri Sep 18 | 21,506 | $383,473 | +13,189 | −8,498 | +3,156 | +3,739 | +235 | **+11,821** |
| Sat Sep 19 | 10,728 | $227,408 | +6,232 | −4,636 | +999 | +2,040 | +147 | **+4,782** |
| Sun Sep 20 | 16,275 | $305,601 | +5,766 | −6,835 | +797 | +3,008 | +183 | **+2,918** |
| Mon Sep 21 | 26,963 | $578,023 | +21,520 | −13,745 | +1,221 | +6,048 | +82 | **+15,127** |
| Tue Sep 22 | 27,911 | $614,674 | +9,137 | −14,585 | +1,494 | +6,417 | +82 | **+2,545** |
| **Total** | 122,526 | **$2,372,574** | **+56,669** | **−53,309** | **+3,885** | **+23,457** | **+1,062** | **+31,764** |

Sep 23 (partial, to 23:00): before rebates −$9,387. The expected rebate of ~$5,079 would leave the day at about **−$4.3K**.

### 4.2 Attribution (six days)
| Source | $ | Share of net |
|---|---|---|
| Taker trading, net of fees | +$3,360 | 11% |
| Maker trading | +$3,885 | 12% |
| Taker rebate (44%) | +$23,457 | 74% |
| Maker rebate | +$1,062 | 3% |
| **Net** | **+$31,764** | ≈ $5,294/day, 1.34% of notional |

### 4.3 Seven-day taker vs maker
| | Fills | Notional | Pre-fee | Fees | Net before rebates |
|---|---|---|---|---|---|
| Taker | 124,658 | $2,629,399 | +$56,347 (2.14%) | $65,070 (2.47%) | −$8,723 |
| Maker | 23,721 | $255,290 | +$7,018 (2.75%) | $0 | +$7,018 |
| All | 148,379 | $2,884,690 | +$63,365 (2.20%) | $65,070 | −$1,705 |

Total rebates received in the window: taker $26,190, maker $1,357.

### 4.4 Rebate tier
- The taker rebate is **exactly 0.44 × the previous day's fees** on every day. That is **Diamond**.
- Projected 30-day weighted volume = **$9.19M** (wV = taker notional × (1 − p) × 2.3), just under the $10M Obsidian threshold. The documented formula matches the observed tier.
- ~$0.8M more wV per month would reach Obsidian (50%), worth about +$3.9K/week at current fees (6% of ~$65K weekly fees).

---

## 5. The signal: 3-second momentum (taker fills, 6 coins with Binance 1 s data)

| Coin's move in the 3 s before the fill (toward the side bought) | Fills | Notional | Share | Pre-fee | Net before rebates | ≈ Net after 44% rebate |
|---|---|---|---|---|---|---|
| ≤ −2 bp | 8,554 | $158,792 | 6% | −12.5% | **−$23,745 (−15.0%)** | ≈ −$22.0K |
| −2 to −0.5 | 14,882 | $284,547 | 11% | −7.5% | **−$28,214 (−9.9%)** | ≈ −$25.1K |
| flat ±0.5 | 50,241 | $1,298,312 | 51% | +1.1% | **−$18,898 (−1.5%)** | ≈ −$4.5K |
| +0.5 to +2 | 26,719 | $543,106 | 21% | +9.2% | **+$36,370 (+6.7%)** | ≈ +$42.3K |
| ≥ +2 bp | 14,936 | $271,950 | 11% | +11.7% | **+$25,320 (+9.3%)** | ≈ +$28.2K |

- **Momentum fills (32% of taker $)** earn +$61.7K before rebates. That is the whole edge.
- **Adverse fills (17%)** lose −$52K. This bot has proportionally more adverse flow than bot #1, and that is its main leak.
- **Flat flow (51%)** is still slightly negative even after the 44% rebate. At Obsidian (50%) it would be about −$2.5K.
- Mean prior-3 s move on taker fills: +0.33 bp. Mean next-5 s drift: +0.10 bp, so it is slightly ahead of price.
- Maker fills: flat +1.8%, momentum +6.5–8.9%, adverse (≤ −2 bp) −5.0%.

Other cuts (net before rebates):
- **By price:** 10–30¢ is the worst (−8% to −12%: cheap long-shots lose). 80–100¢ is the best (+1.9% to +2.2%, $12.6K). 0–10¢ is +4.7% on small size. → `analysis/by_price.csv`
- **By time elapsed in the window:** 50–60% elapsed is best (+3.5%); 60–70% is worst (−4.4%). → `analysis/by_time_in_window.csv`
- **By UTC hour:** 17:00–19:00 UTC (US afternoon) lose about −$17K combined; 03:00, 07:00–08:00 and 20:00 UTC are best. → `analysis/by_utc_hour.csv`

---

## 6. How this bot works (inferred)

1. **Universe:** every live crypto Up/Down market Polymarket lists: BTC, ETH, SOL, XRP, BNB, DOGE and HYPE; 5m, 15m, 1h and 4h. Plus opportunistic "above $X" strike markets.
2. **Pricing:** a fair-value model driven by live spot price per coin. σ per second this week: BTC 0.54 bp, BNB 0.65 bp, ETH 0.82 bp, SOL 1.12 bp, XRP 1.41 bp, DOGE 1.65 bp.
3. **Execution:** mostly taker. It lifts offers after spot moves (momentum) and also sends a lot of flat-momentum flow. It rests some maker bids, especially on 1h markets (34% maker).
4. **Positions:** it usually ends up two-sided but often directionally tilted (median imbalance 33%). The 4h markets are the most hedged (pair cost 0.969).
5. **Settlement:** redeems winners every few minutes (9,367 redemptions in 7 days).
6. **Economics:** trading ≈ break-even after fees; the Diamond taker rebate (44%) is the profit.

---

## 7. Comparison with bot #1 (x-MoneyForWhiskas)

| | Bot #1 x-MoneyForWhiskas | Bot #2 0xb55fa129 |
|---|---|---|
| Markets | BTC 5m only | 7 coins × 5m/15m/1h/4h + strikes |
| Markets / week | 1,832 | 10,410 |
| Notional / week | $1.54M | $2.88M |
| Taker share of $ | 81% | 91% |
| Buys both sides | 99.6% | 74% |
| Median pair cost | $0.966 | $1.02 |
| Median imbalance | 10% | 33% |
| Rebate tier | Obsidian 50% | Diamond 44% |
| Net / day (6 full days) | ~$3,760 | ~$5,290 |
| Net % of notional | 1.74% | 1.34% |
| Profitable days | 6 / 6 | 5 / 6 |
| Share of profit from rebates | 79% | 77% |
| Momentum share of taker $ | 19% | 32% |
| Adverse share of taker $ | 11% | 17% |
| Biggest leak | flat flow (fixed by 50% rebate) | BTC/ETH 5m + adverse fills |

**Takeaways:**
- Both bots run the **same business model**: a fast momentum-taker plus rebate farm.
- Bot #1 is **more efficient and lower-risk**: hedged pairs and a higher tier.
- Bot #2 is **bigger but noisier**. It makes more dollars on more volume, with more directional risk and a losing day.
- If bot #2 dropped BTC/ETH 5m or fixed its adverse fills, it would have made roughly +$20K more this week before rebates. Some of that volume, though, is what keeps it at Diamond.

---

## 8. Verification

- **On-chain reconciliation** (`analysis/onchain_reconciliation.json`), Sep 23 11:31 → 22:58 UTC (11.45 h):
  - pUSD out = **$303,849.68**, exactly equal to the activity TRADE `usdcSize` total.
  - pUSD in = **$294,538.51**, exactly equal to the REDEEM total. All of it is mints from `0x0`; no other income.
- **Winners:** official CLOB resolution for 10,410 of 10,423 markets (13 were still open).
- **Fees** are embedded in the cash paid (`usdcSize − size × price`); zero means a maker fill.
- **P&L chart ≈ pre-fee P&L:** chart change Sep 17–22 = +$62.4K, versus my pre-fee trading edge of +$60.6K.
- **Weekly leaderboard (−$298K P&L, $5.98M volume) does not match** the chain or the chart. Its volume is about 2× real cash volume. Do not use it.

---

## 9. Caveats
- One week of data. Only fills are visible (no quotes, cancels or latency).
- Binance USDT spot was used as the price proxy; Polymarket settles on Chainlink. HYPE is not on Binance spot, so it is excluded from the signal analysis (1.2% of volume).
- 1h market start times are parsed from slugs as US Eastern (EDT, UTC−4).
- Any capital outside this proxy wallet is unknown.

---

## 10. Files in this folder

```
REPORT.md                         ← this file
raw/
  activity_7d.json / .csv         every activity event (158,045: trades, redeems, rebates)
  winners.json                    official winner + metadata for 10,423 markets
  prices_1s_{BTC,ETH,SOL,XRP,BNB,DOGE}USDT.json   Binance 1-second closes, 7 days
  positions_open_top500.json      current positions (top 500 by value)
  pnl_all_daily.json              Polymarket P&L chart, lifetime daily
  pnl_1w_hourly.json              Polymarket P&L chart, last week hourly
  profile_stats.json              value / markets traded / leaderboard (all + week)
  sample_activity.json            first 500-event sample
analysis/
  fills.csv                       148,379 fills: price, shares, cash, fee, taker flag, winner, won,
                                  seconds into/left in window, 3s/10s momentum, next-5s drift, model fair value
  markets.csv                     10,410 markets: shares per side, avg prices, pair cost, imbalance, cash, payout, net P&L
  daily_ledger.csv                per-day taker edge / fees / maker edge / rebates / net
  by_family.csv  by_timeframe.csv  by_asset.csv  by_price.csv  by_time_in_window.csv  by_utc_hour.csv
  by_momentum_taker.csv  by_momentum_maker.csv
  structure_by_timeframe.csv      two-sided %, pair cost, imbalance per timeframe
  summary.json  extras.json  onchain_reconciliation.json
scripts/
  01_fetch_activity.py   02_fetch_prices.py   03_fetch_winners.py
  04_analyze.py          05_onchain_reconcile.py   06_extras.py
```

**To refresh:** run the scripts in order from this folder (`python3 scripts/01_fetch_activity.py` … `06_extras.py`). To analyse a different wallet, change `W` in scripts 01 and 05.
