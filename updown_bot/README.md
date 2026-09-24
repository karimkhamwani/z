# Up/Down Momentum Bot (paper trading)

A paper-trading bot for Polymarket's crypto **Up or Down** markets (default: BTC 5-minute), starting at **$200** and sizing as a percentage of equity so it scales as it grows. It includes a live **dashboard**.

It's built from what the two wallet teardowns showed (see `../POLYMARKET_BTC5M_BOT.md` and `../wallet_0xb55fa129/REPORT.md`):

- The only trades that made money **before rebates** for both bots were **momentum taker fills**: buying a side right after the coin moved toward it, before the order book repriced. Flat and adverse fills lost money. Rebates don't help at $200, so this bot trades **only** that slice.
- Markets settle on **Chainlink 60-second TWAP at the end ≥ TWAP at the start**. This rule matched 97.8% of 1,832 official results; spot-vs-spot matched only 87%. The fair-value model prices this exactly, including how the outcome locks in during the last minute.
- The volatility input was calibrated on those 1,832 results. Chainlink prints are smoothed, so the bot measures volatility from **30-second changes**; 1-second changes understate it by about 2×.

> General software, not financial advice. Paper results can be better than live results (see *Paper vs. live*). Only mode `paper` exists; live execution is intentionally not implemented (`bot/live.py`).

---

## Quick start

```bash
cd updown_bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Start the bot (terminal 1):

```bash
.venv/bin/python run.py --fresh
```

Start the dashboard (terminal 2), then open http://127.0.0.1:8766:

```bash
.venv/bin/python dashboard.py --port 8766
```

Terminal report at any time:

```bash
.venv/bin/python report.py
```

Run the tests:

```bash
.venv/bin/python -m unittest -v tests.test_core
```

- `run.py` resumes the saved paper portfolio (`data/portfolio.json`). `--fresh` resets it to `starting_equity`.
- `--minutes N` stops the bot automatically after N minutes.
- **After startup the bot sits out the current window.** It needs Chainlink prints from the 60 seconds before a window opens to know the start price, so it starts trading at the next window boundary (at most 5 minutes). The dashboard says so.

**Corporate networks:** if Coinbase or Polymarket connections fail with `CERTIFICATE_VERIFY_FAILED`, your network is inspecting TLS. Export the Mac's trusted roots once (the bot then verifies against them):

```bash
security find-certificate -a -p /Library/Keychains/System.keychain /System/Library/Keychains/SystemRootCertificates.keychain > certs/system_ca.pem
```

---

## How it trades

Every 100 ms, for each live market:

1. **Start price** R = mean of the Chainlink prints in the 60 s before the window opened.
2. **Chainlink now** is estimated as the last Chainlink print plus the Coinbase move since that print. Chainlink lags about 1–2 s; Coinbase leads.
3. **Fair value** P(Up) = Φ((E[final TWAP] − R) / sd). Seconds of the final minute that have already printed are locked in. The rest follow a random walk with σ from 30-second changes (EWMA, 10-minute half-life). See `bot/model.py`.
4. **Signal:** the coin moved **≥ 0.5 bp in the last 3 s** toward a side, **and** that side's ask is below fair value by **≥ 2¢ after the taker fee** (fee = 0.07 × p × (1 − p) per share).
5. **Order:** a FAK limit buy at up to ask + 2¢, never above the price that keeps 2¢ of edge after fees.
6. **Paper fill:** wait `latency_ms` (300 ms), re-read the live book, and take at most `liquidity_haircut` (50%) of each ask level up to the limit. Fees are charged per level; fills under 5 shares or $1 are skipped.
7. **Settlement:** Polymarket's official result, read on-chain from the Conditional Tokens contract. Markets resolve there about 60–100 s after they end; the CLOB API's `winner` flag lags by many minutes and is only a fallback. The bot's own TWAP estimate is logged next to the official result as a check.
8. **Rebates (estimate):** yesterday's taker fees × your tier, credited after 00:00 UTC. Tiers use the documented 30-day weighted volume.

### Sizing and scaling ($200 → more)
| Setting | Default | At $200 | At $2,000 |
|---|---|---|---|
| `clip_pct_equity` (per order) | 5% | $10 | $100 |
| `max_market_exposure_pct` | 25% | $50 | $500 |
| `max_clip_usd` | $250 | — | — |
| `daily_loss_stop_pct` | 15% | stop at −$30/day | −$300/day |
| `max_drawdown_kill_pct` | 35% | halt at −$70 from peak | — |

Sizes grow automatically with equity. Past a few thousand dollars, the order book's depth (median fill in the analysed bots: ~$8) caps you before these percentages do. Add more assets (`assets = ["btc","eth","sol"]`) rather than bigger orders.

---

## Dashboard

`dashboard.py` is a zero-dependency local web server. It reads `data/status.json` (rewritten every second), `data/portfolio.json` and `data/paper.db`.

- **Top row:** equity and return, net trading P&L (after fees), fees, estimated rebates and tier, markets and win rate, max drawdown, and feed health.
- **Live market:** countdown, start price vs current Chainlink estimate, 3 s momentum, σ, Chainlink lag, and model P(Up) against the market's bid/ask. Edge after fee is shown for each side, highlighted when the bot's signal is live.
- **This window:** model P(Up) vs the market's Up mid and bid/ask band, with your fills marked.
- **Equity curve**, and **where the P&L comes from** (tabs by momentum, edge, time left, price, and model calibration).
- Recent fills, settled markets, skipped signals with the reason for each skip, and a daily table.

---

## Files
```
run.py            start the bot          report.py      terminal report (+ --csv)
dashboard.py      local dashboard        config.toml    every tunable parameter
bot/model.py      TWAP fair value, vol   bot/strategy.py momentum + edge signal
bot/feeds.py      Coinbase + Chainlink   bot/book.py     CLOB order books
bot/markets.py    discovery, resolution  bot/paper.py    fills, portfolio, settlement, rebates
bot/risk.py       sizing, stops          bot/engine.py   orchestration, status.json
bot/ledger.py     SQLite ledger          bot/analytics.py shared performance maths
bot/live.py       (not implemented)      tests/          16 unit tests incl. Monte-Carlo check of the TWAP model
data/             paper.db, portfolio.json, status.json (created at runtime)
```

`data/paper.db` tables: `fills`, `settlements` (official vs model winner), `rejections` (every skipped signal and why), `snapshots` (1 Hz model and book state, for tuning or replay), `equity`, `rebates`, `events`.

---

## Paper vs. live: why paper can look better

- **Latency:** the analysed bots are co-located and fast. From a home connection you may lose the 3-second window to them more often than a 300 ms simulated delay suggests. Try `latency_ms = 600–1000` to stress-test.
- **Queue competition:** paper takes 50% of displayed size. In live trading, faster takers may empty the level first.
- **Chainlink basis:** the bot's settlement estimate uses the public Chainlink feed; the `model` column in *Settled markets* shows how often it agrees with the official result.
- **Before any live money:** run paper for at least 2 weeks and several hundred fills. Look at `By momentum` and `Calibration`. Momentum buckets should be positive after fees, and calibration should track the diagonal. Then compare paper against live on tiny size.

## Tuning checklist
- Too few trades: lower `min_edge` to 0.015 or `min_momentum_bp` to 0.4, and check *Skipped signals* for the main reason.
- Losing in a bucket: raise its threshold. For example, if fills with under 30 s left lose, raise `min_seconds_left`.
- Calibration off (model says 0.8, wins 0.7): raise `vol_prior_bp` or `basis_sigma_usd`.
- Add assets: ETH, SOL and XRP markets exist on 5m/15m/1h/4h. The second wallet made its money on 15m/1h/4h, not 5m.
