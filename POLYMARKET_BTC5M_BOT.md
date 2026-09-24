# Polymarket BTC 5-Minute Bot — Knowledge Base

A teardown of the Polymarket account **x-MoneyForWhiskas**, covering how it makes money and a plan for building a similar bot, including a $200 starter version.

> Data window: **Sep 16 20:00 → Sep 23 20:00 UTC, 2026** (7 days). Clean ledger figures use the six full UTC days **Sep 17–22**.
> This is general analysis, not financial advice. Past profit does not mean the strategy will keep working.

---

## 1. TL;DR

- The bot trades **only** Polymarket's *Bitcoin Up or Down – 5 minute* markets. It **only buys, never sells**, buys **both Up and Down** in 99.6% of markets, and holds everything to settlement.
- After fees, its **trading breaks even**. Taker orders earn about 3.1% of notional before fees and pay about 3.1% in fees.
- The profit comes from **rebates**. Polymarket's **Taker Rebate Program** refunds **50%** of taker fees every night, and resting maker orders pay no fee and earn a **maker rebate** (20% of the fee they generate).
- Result: about **$3,760/day net cash profit**, and **79% of it is rebates**. All 6 full days were profitable.
- The real trading edge is **latency/momentum**. After BTC spot moves ≥0.5 bp in 3 seconds toward a side, it buys that side's stale offers, and those fills earn **+13% to +27% net**.
- **Polymarket's profile P&L excludes fees.** The chart shows +$483K lifetime and +$45K for this week. True cash profit this week was about **+$29K**.

---

## 2. Account facts

| Field | Value |
|---|---|
| Username | `x-MoneyForWhiskas` (pseudonym "Flashy-Gold") |
| Bio | "Trading to pay for my cat's food." |
| Profile | https://polymarket.com/@x-moneyforwhiskas?tab=positions |
| Proxy wallet | `0x3048d65321be3497164cdfc2996f94f98a2e7537` |
| Active since | ~Jun 11 2026 (first point on the P&L chart) |
| Lifetime volume | $32,264,663 |
| Leaderboard P&L | $132,734 (rank ~1455). Methodology unclear. |
| Profile P&L chart | $483,509 (excludes fees; see §6) |
| Markets traded | 29,511 |
| pUSD cash in wallet (on-chain, Sep 23) | ~$58,944 |
| Open position value | ~$938 |

---

## 3. Behaviour fingerprint (7 days)

| Metric | Value |
|---|---|
| Activity events | 120,933 (119,093 TRADE, 1,826 REDEEM, 7 TAKER_REBATE, 7 MAKER_REBATE) |
| Markets | 1,832 (all `btc-updown-5m-<unix_start>`) |
| Side | 100% BUY, 0 SELL, 0 MERGE, 0 SPLIT |
| Two-sided | Buys both Up and Down in 99.6% of markets |
| Exit | Holds to resolution, then redeems winners (~every 5 min). Never redeems losing tokens. |
| Notional (pre-fee, size×price) | $1,542,537 |
| Cash spent (incl. fees) | $1,581,438 |
| Taker share | 61% of fills, 81% of notional |
| Fill rate while active | ~21 fills/min |
| Median fill | $8.11 (19 shares); p90 $32.81; max ~$92 |
| Per market | median 57 fills; mean ~$860; max ~$4,300 |
| Entry timing | Fairly even over 0–240 s; tapers in the last 60 s; nothing after close |
| Prices bought | 10¢–90¢, centred on 50¢ |
| Avg Up+Down cost per pair | $0.966 pre-fee (median); $0.990 after fees |
| Pairs locked below $1 after fees | 59.5% of markets |
| Net share imbalance (\|Up−Down\| / total) | median 10%, p90 33% |
| Per-market net P&L | mean +$3.77, median −$3.16, sd $111, p5 −$162, p95 +$199, win rate 48.8% |
| Downtime observed | Sep 19 06:00–13:00 UTC (P&L chart flat over the same hours) |

**How to read this:** the bot is a single fair-value engine that quotes and buys both outcomes against a live BTC price. Positions end up nearly balanced, so most of each market's payout is locked in. The ~10% share imbalance is the actual directional bet.

---

## 4. Fees and rebates (how Polymarket charges and pays)

### 4.1 Taker fee (crypto category)
```
fee = 0.07 × shares × p × (1 − p)        # p = fill price
fee as % of notional = 0.07 × (1 − p)
```
| Price | Fee per share | % of notional |
|---|---|---|
| 20¢ | 1.12¢ | 5.6% |
| 50¢ | 1.75¢ | 3.5% |
| 80¢ | 1.12¢ | 1.4% |

- Makers pay **no fee**.
- **The fee is embedded in the cash paid.** In the activity API, `usdcSize − size × price` = the fee (zero on maker fills). Verified on-chain (§7).

### 4.2 Taker Rebate Program (launched May 28 2026)
Tier is set by 30-day **weighted volume (wV)**, counting taker trades only:
```
wV = notional × (1 − entry_price) × category_weight      # crypto weight = 2.3
```
| Tier | 30-day wV | Rebate of taker fees |
|---|---|---|
| Bronze | $2K | 3% |
| Silver | $20K | 8% |
| Gold | $200K | 18% |
| Platinum | $1M | 32% |
| Diamond | $4M | 44% |
| Obsidian | $10M+ | 50% |

- Paid daily just after 00:00 UTC in pUSD, as a `TAKER_REBATE` activity event, covering the **previous** day.
- Category weights: Sports 1.0, Politics/Finance/Tech 1.3, Economics/Culture/Weather 1.7, **Crypto 2.3**, Geopolitics 0.
- Excluded: maker trades, omnibus wallets, and wash trading or self-matching (can lead to rebate removal).
- The docs mention one-time bonuses when you first reach a tier. Amounts not verified.

**Observed for this bot:** each day's taker rebate = **exactly 50%** of the previous day's taker fees (3653/7306, 4007/8015, 938/1875, 1595/3190, 2815/5630, 3285/6570). ⚠️ My wV calculation from its taker flow gives ~$6.3M/30d, which is Diamond, yet it receives 50%. Either its earlier volume was higher or the formula differs slightly. Treat wV estimates as approximate.

### 4.3 Maker Rebate Program
- Crypto pool = **20%** of taker fees. Each maker's share = their fee-equivalent (fee formula applied to their filled maker orders) ÷ the total.
- **Observed:** the bot's `MAKER_REBATE` = **exactly 20%** of the fee-equivalent on its maker fills every day, ≈ **0.6% of maker notional**.

### 4.4 Worked example: 100 shares at 50¢ ($50)
| Mode | Fee | Rebate | Net fee cost |
|---|---|---|---|
| Taker, no tier | $1.75 | 0 | −$1.75 |
| Taker, Gold 18% | $1.75 | $0.32 | −$1.43 |
| Taker, Diamond 44% | $1.75 | $0.77 | −$0.98 |
| Taker, Obsidian 50% | $1.75 | $0.875 | −$0.875 |
| Maker | $0 | $0.35 | **+$0.35** |

### 4.5 Profit equations
```
Taker:  profit = edge − fee × (1 − rebate%)
Maker:  profit = edge + 0.20 × fee_equivalent        (≈ edge + 0.6% of notional)
```
Break-even taker edge per share at 50¢: 1.75¢ (no tier) · 1.44¢ Gold · 1.19¢ Platinum · 0.98¢ Diamond · 0.875¢ Obsidian.

**Rebates never exceed fees.** With zero edge you lose (1 − rebate%) of every fee. Trading against yourself returns at most 50% + 20% = 70% of the fee, so it still loses, and it breaks the terms of service.

---

## 5. Economics: where the money comes from

### 5.1 Six-day cash ledger (Sep 17–22, rebates matched to the day that earned them)
| Day (UTC) | Fills | Notional | Taker edge | Fees | Maker edge | Taker rebate | Maker rebate | **Net** |
|---|---|---|---|---|---|---|---|---|
| Thu Sep 17 | 21,999 | $286,572 | +6,733 | −7,306 | +204 | +3,653 | +285 | **+3,570** |
| Fri Sep 18 | 23,801 | $314,239 | +7,736 | −8,015 | +1,360 | +4,007 | +336 | **+5,424** |
| Sat Sep 19 | 6,076 | $77,867 | +1,477 | −1,875 | −578 | +938 | +109 | **+70** |
| Sun Sep 20 | 10,730 | $134,188 | +3,548 | −3,190 | +2,369 | +1,595 | +197 | **+4,519** |
| Mon Sep 21 | 15,910 | $221,098 | +5,130 | −5,630 | −410 | +2,815 | +263 | **+2,168** |
| Tue Sep 22 | 21,033 | $262,835 | +8,159 | −6,570 | +1,640 | +3,285 | +316 | **+6,830** |
| **Total** | 99,549 | $1,296,799 | **+32,783** | **−32,586** | **+4,585** | **+16,293** | **+1,506** | **+22,581** |

"Edge" means payout minus cost at the pre-fee fill price.

### 5.2 Profit attribution
| Source | 6-day $ | Share of net |
|---|---|---|
| Taker trading, net of fees | +$197 | ~1% |
| Maker trading (no fees) | +$4,585 | 20% |
| Taker rebate (50%) | +$16,293 | 72% |
| Maker rebate (20% pool) | +$1,506 | 7% |
| **Net** | **+$22,581** | ≈ $3,764/day |

Daily sd ≈ $1.6K; 6 of 6 days positive.

### 5.3 7-day split, taker vs maker
| | Fills | Notional | Pre-fee P&L | Fees | Net before rebates |
|---|---|---|---|---|---|
| Taker | 73,071 | $1,243,036 | +$38,577 (3.10%) | $38,902 | −$325 |
| Maker | 46,022 | $299,501 | +$7,234 (2.42%) | $0 | +$7,234 |
| All | 119,093 | $1,542,537 | +$45,811 (2.97%) | $38,902 | +$6,909 |

---

## 6. The signal: 3-second BTC momentum

Every taker fill was compared with Binance BTCUSDT 1-second prices. "Momentum" = BTC's move over the 3 s before the fill, in the direction of the side bought.

| Prior 3 s move | Fills | Notional | % of taker $ | Pre-fee | Fees | Net (no rebate) | Net after 50% rebate |
|---|---|---|---|---|---|---|---|
| ≤ −2 bp | 1,896 | $34,784 | 3% | −$9,411 (−27%) | $1,083 | −$10,494 | **−$9,952** |
| −2 to −0.5 | 5,813 | $102,155 | 8% | −$16,137 (−16%) | $3,269 | −$19,406 | **−$17,771** |
| flat ±0.5 | 51,068 | $867,841 | 70% | +$17,442 (+2.0%) | $27,331 | −$9,888 (−1.1%) | **+$3,777 (+0.4%)** |
| +0.5 to +2 | 10,662 | $178,337 | 14% | +$28,497 (+16%) | $5,383 | +$23,114 (+13%) | **+$25,805** |
| ≥ +2 bp | 3,632 | $59,919 | 5% | +$18,185 (+30%) | $1,835 | +$16,350 (+27%) | **+$17,267** |

What this shows:
- **Momentum fills (+0.5 bp or more, 19% of taker $)** carry almost all the gross edge. These are stale offers bought before market makers reprice.
- **Adverse fills** (BTC just moved against the side) lose heavily. These are likely one leg of a two-sided position that got picked off.
- **Flat flow (70% of taker $)** loses 1.1% on its own, but the 50% rebate turns it into +0.4%. It exists mainly to keep volume at the top rebate tier. At Platinum (32%) or below, this flow loses money.

Other slices (all fills, 7 days):
- By time in window, net before rebates: 0–60 s +0.51% · 60–120 s −0.14% · 120–180 s +1.02% · 180–240 s −0.03% · 240–300 s +1.36%.
- By price, net before rebates: 20–35¢ worst (−2.0%); 50–65¢ best (+1.6%); fees make cheap-side buying expensive.
- Momentum at fill time (taker): mean prior-3s move +0.16 bp; next-5s drift +0.07 bp, so the bot is slightly ahead of price.

---

## 7. Verification and data gotchas

1. **Winners:** CLOB `/markets/{conditionId}` `tokens[].winner` for all 1,832 markets. These match redemption amounts **100%**. **Binance agrees only 87%**, because markets settle on **Chainlink BTC/USD**, not Binance.
2. **Fees are embedded:** on-chain pUSD outflows over a 6 h window = **$86,872.02** = the sum of activity `usdcSize` to the cent. Inflows = **$87,720.24** = redemptions. No separate fee transfers.
3. **No hidden income:** over ~1.5 days of incoming pUSD, the only sources were redemptions (mints from `0x0`) and the two daily rebate payers.
4. **Fees are not deducted in shares:** redeemed amount = winning shares bought, exactly.
5. **Polymarket P&L chart ≈ pre-fee P&L.** Daily chart change ≈ gross + ~0.8 × fees. Use cash-basis accounting.
6. `closed-positions` `realizedPnl` also ignores fees. `positions` shows `entryFeesUsdc` separately.
7. **API quirks:**
   - The data API caps `offset`: activity ~5,000, trades ~10,000. Page by time with `start`/`end` (30-min windows worked).
   - **The CLOB API blocks Python's default User-Agent.** Send `User-Agent: Mozilla/5.0`.
   - `api.binance.com` may be geo-blocked; `data-api.binance.vision` works.
   - Public Polygon RPC (`polygon-bor-rpc.publicnode.com`) has **no archive state** and prunes logs older than ~1.5 days.
   - In the activity API, `price` is pre-fee while `usdcSize` includes the fee.

---

## 8. Data sources and endpoints

```
# Profile → wallet
GET https://gamma-api.polymarket.com/public-search?q=<name>&search_profiles=true
# Account stats
GET https://data-api.polymarket.com/value?user=<wallet>
GET https://data-api.polymarket.com/traded?user=<wallet>
GET https://data-api.polymarket.com/v1/leaderboard?user=<wallet>&timePeriod=ALL|WEEK|MONTH|DAY
GET https://user-pnl-api.polymarket.com/user-pnl?user_address=<wallet>&interval=all|1w&fidelity=1d|1h
# Activity / trades / positions
GET https://data-api.polymarket.com/activity?user=<wallet>&limit=500&offset=N&start=<ts>&end=<ts>&sortDirection=ASC
GET https://data-api.polymarket.com/trades?user=<wallet>&limit=500&offset=N&takerOnly=false
GET https://data-api.polymarket.com/positions?user=<wallet>&limit=500&sizeThreshold=0
GET https://data-api.polymarket.com/closed-positions?user=<wallet>&limit=50&offset=N&sortBy=TIMESTAMP&sortDirection=DESC
# Market metadata / winner
GET https://clob.polymarket.com/markets/<conditionId>        # needs browser User-Agent
# BTC 1-second prices
GET https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1s&limit=1000&startTime=<ms>
# On-chain
pUSD token (Polygon): 0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb
RPC: https://polygon-bor-rpc.publicnode.com  (balanceOf, eth_getLogs Transfer topic 0xddf252ad…)
```

Market slug format: `btc-updown-5m-<unix_start>`, where the window runs from `start` to `start+300`. There are 288 markets per day. Minimum order is 5 shares; tick size is 0.01 (some markets 0.001).

Snippet: taker/maker classification and fee per fill
```python
fee   = x["usdcSize"] - x["size"] * x["price"]   # >0 → taker, ≈0 → maker
taker = fee > 1e-4
```

Snippet: fair value used in the analysis
```python
# sigma ≈ 5.39e-5 per second (1s log-return stdev over the week)
from math import log, sqrt, erf
Phi   = lambda z: 0.5 * (1 + erf(z / sqrt(2)))
p_up  = Phi(log(S_t / S_open) / (sigma * sqrt(max(t_end - t, 1))))
```

---

## 9. Blueprint: rebuilding the full bot

Reconstructed from fills only. The wallet shows no quotes, cancels or latency, so treat every parameter as a hypothesis to test.

1. **Data plane**
   - Multi-venue BTC spot (Binance, Coinbase) plus the **Chainlink BTC/USD stream** used for settlement.
   - Polymarket CLOB WebSocket for both outcome books of the current and next market.
   - A low-latency VPS near Polymarket's matching engine. Measure ping to `clob.polymarket.com` from several regions.
   - Auto-roll to each new `btc-updown-5m-*` market.
2. **Fair-value model**
   - `P(Up) = Φ(ln(S_t/S_open) / (σ·√τ))` with live σ (≈0.54 bp/s this week).
   - Add a short-horizon drift term from the 3 s move.
   - Calibrate against Chainlink settlement prints, not Binance.
3. **Execution**
   - *Taker leg:* buy when `fair − ask > fee(p) × (1 − rebate%)`, mainly right after spot moves ≥0.5 bp/3 s.
   - *Maker leg:* rest bids on both outcomes a few ticks under fair, keeping the combined bid under $1 (the bot pays ~$0.966). Requote on every spot tick.
   - Clip size $5–35; ~20 fills/min; $500–1,000 per market; hard cap ~$4K per market.
   - Keep the Up/Down share imbalance within 10–30%. Stop adding in the last ~45–60 s.
4. **Rebate-tier management**
   - Track trailing 30-day wV against the Diamond ($4M) and Obsidian ($10M) thresholds.
   - Break-even taker flow is only worth trading while it holds the tier.
   - Reconcile nightly `TAKER_REBATE` and `MAKER_REBATE` against expected fees.
5. **Settlement and cash**
   - Redeem winners right after each resolution. Skip losing tokens.
   - About $60K is far more than needed; the working capital actually required is a few markets' worth.
6. **Measurement**
   - Use cash-basis P&L, not Polymarket's chart.
   - Track daily P&L per bucket (momentum / flat / adverse / maker, and by price level). Turn off any bucket that is negative after rebates.

### Capital vs tier (at the bot's average price 0.476, where wV ≈ 1.2 × taker notional)
| Tier | Taker notional/day to hold | $ per market (288/day) | Working capital | Net/day at 0% edge | Net/day at 2.0% edge | Net/day at 3.1% edge |
|---|---|---|---|---|---|---|
| Gold 18% | ~$5.6K | ~$19 | a few hundred $ | −$140 | −$30 | +$31 |
| Platinum 32% | ~$28K | ~$97 | ~$0.5–1K | −$590 | −$31 | +$277 |
| Diamond 44% | ~$111K | ~$385 | ~$2–5K | −$1,930 | +$290 | +$1.5K |
| Obsidian 50% | ~$278K | ~$965 | ~$5–10K | −$4,310 | +$1.25K | +$4.3K |

Capital is not the constraint; **edge and volume are.** The Obsidian/3.1% row roughly matches the bot's real ~$3.8K/day, which supports this model.

---

## 10. The $200 version

### Why it has to be different
- $200 realistically reaches **Gold (18%)** at most, and the first month has little or no tier. The break-even taker edge at 50¢ becomes **1.44–1.75¢**, versus the bot's 0.875¢.
- The bot's flat "volume" flow would **lose money** at this size. Only two parts still work:
  - **Maker orders:** no fee, +0.6% rebate, no tier needed.
  - **Taker momentum trades** where the edge exceeds the *full* fee.
- You would be competing with this bot. It buys the stale orders first and picks off slow maker quotes. For a newcomer, that is the most likely way to lose.

### Phased plan
**Phase 0: record and simulate ($0, 1–2 weeks)**
- Record both order books of every BTC 5-minute market, plus Binance, Coinbase and Chainlink ticks.
- Simulate the rules below with real fees and **no rebate assumed**.
- Pick the VPS region with the lowest latency ($5–20/month).
- Go/no-go: the simulation must be positive after full fees.

**Phase 1: maker orders only (live, $200)**
- Rest bids on Up and Down below fair, combined ≤ ~$0.96.
- 5-share minimum orders (~$2.50 at 50¢); ≤$40 per market.
- Cancel and requote the moment BTC moves.
- No new orders in the last ~45 s.
- Key metric: average fill price versus settlement value. If negative, you are being picked off.

**Phase 2: add taker momentum trades**
- Only buy when BTC has moved ≥0.5 bp in 3 s toward the side **and** `fair − ask ≥ full fee` (≥1.75¢ at 50¢).
- Never trade flat-momentum flow.

**Phase 3: scale only with proof**
- Add capital only after 2+ weeks positive on cash basis.
- Tiers follow volume; don't chase them.

### Risk rules
- Stop the bot at −20% ($40). Daily stop at −$10.
- Per-market Up/Down imbalance ≤30%.
- Redeem after every market to free the capital.
- No self-trading or wash trading.
- Confirm you are allowed to use Polymarket and its API where you live (it is geo-restricted in some countries).

### Expected outcomes (~$6K daily notional)
| Scenario | $/day |
|---|---|
| Picked off (−1% edge) | −$24 (the $200 lasts about a week) |
| Small real edge (+0.5% + 0.6% maker rebate) | +$66 |
| Bot-level edge | $150+ (unlikely without fast servers) |

---

## 11. Risks to the strategy

| Severity | Risk |
|---|---|
| High | **Rebate policy.** 79% of profit depends on one program launched May 2026. A change to thresholds, the 2.3× crypto weight or the 50% rate removes most of the margin. |
| High | **Fee regime.** Fees were introduced specifically to curb latency arbitrage in short crypto markets. Higher or dynamic fees would erase the momentum edge. |
| Med | **Competition.** Faster makers or rival takers shrink the 3-second window. |
| Med | **Scale ceiling.** Books are thin (median fill $8). More size means worse fills. |
| Med | **Oracle basis.** Chainlink and exchange prices disagree near the strike (Binance got 13% of winners wrong). Modelling this badly is a steady loss. |

---

## 12. Method limitations
- One week of data. Only fills are visible: no order book, cancels or latency.
- Binance was used as a stand-in for the Chainlink settlement feed in the signal analysis.
- Any capital outside this wallet is unknown.
- wV/tier math is approximate (see §4.2 caveat).

## 13. Sources
- Polymarket profile: https://polymarket.com/@x-moneyforwhiskas?tab=positions
- Taker Rebate Program: https://docs.polymarket.com/programs/taker-rebates
- Maker Rebates and fee formula: https://docs.polymarket.com/market-makers/maker-rebates
- pUSD: https://docs.polymarket.com/concepts/pusd · contract https://polygonscan.com/address/0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
- Short-term crypto fees: https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/
- Fee overview: https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026

---

## See also
- `wallet_0xb55fa129/REPORT.md`: a second bot (multi-coin, 5m/15m/1h/4h, Diamond tier) running the same momentum + rebate model, with all raw trades and tables in that folder.
