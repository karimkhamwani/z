# Up/Down Momentum Bot (paper trading)

A paper-trading bot for Polymarket's crypto **Up or Down** markets (default: BTC 5-minute), starting at **$200** and sizing as a percentage of equity so it scales as it grows. It includes a live **dashboard**.

It's built from what the two wallet teardowns showed (see `../POLYMARKET_BTC5M_BOT.md` and `../wallet_0xb55fa129/REPORT.md`):

- The only trades that made money **before rebates** for both bots were **momentum taker fills**: buying a side right after the coin moved toward it, before the order book repriced. Flat and adverse fills lost money. Rebates don't help at $200, so this bot trades **only** that slice.
- Markets settle on **Chainlink 60-second TWAP at the end ≥ TWAP at the start**. This rule matched 97.8% of 1,832 official results; spot-vs-spot matched only 87%. The fair-value model prices this exactly, including how the outcome locks in during the last minute.
- The volatility input was calibrated on those 1,832 results. Chainlink prints are smoothed, so the bot measures volatility from **30-second changes**; 1-second changes understate it by about 2×.

> General software, not financial advice. Paper results can be better than live results (see *Paper vs. live*). Live mode trades real money. Read **Live trading** below before turning it on, and make sure you're allowed to use Polymarket where you live: the international exchange blocks some jurisdictions, including the US.

---

## Quick start (Windows, macOS, Linux)

You need **Python 3.11+** (on Windows, install it from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"**) and **git**. Then:

```bash
git clone https://github.com/karimkhamwani/z.git
```

```bash
cd z/updown_bot
```

```bash
python manage.py setup
```

`setup` creates `.venv`, updates pip, installs the dependencies and runs the tests. There are only two packages, `websockets` and `certifi` (pinned in `requirements.txt`); everything else is Python's standard library. Both ship prebuilt Windows wheels (x64 and ARM64), so no compiler or Visual Studio is needed, and nothing is installed outside `.venv`. If `pip` fails (proxy, corporate certificates, offline machine), `setup` prints the fix for each case. On Windows, type `py -3` instead of `python` if `python` isn't found. On macOS/Linux it may be `python3`.

| What | Command | Windows double-click |
|---|---|---|
| Start paper trading (fresh $200) | `python manage.py run --fresh` | `start_bot.bat` (resumes; add `--fresh` from a terminal) |
| Resume the saved paper portfolio | `python manage.py run` | `start_bot.bat` |
| Dashboard (opens http://127.0.0.1:8766) | `python manage.py dashboard` | `start_dashboard.bat` |
| Text report | `python manage.py report` (`--csv` to export) | `report.bat` |
| Tests | `python manage.py test` | — |

- Run the bot and the dashboard in two terminals. Stop either with **Ctrl+C**; the paper portfolio is saved and resumes next time.
- `python manage.py run --minutes 30` stops automatically after 30 minutes.
- Paths (`config.toml`, `data/`, `certs/`) are resolved from the project folder, so the commands work from any working directory. Nothing machine-specific is committed; `.venv/`, `data/` and `certs/` are git-ignored and created locally.
- **After startup the bot sits out the current window.** It needs Chainlink prints from the 60 seconds before a window opens to know the start price, so it starts trading at the next window boundary (at most 5 minutes). The dashboard says so.

### Platform notes
- **Certificates / corporate networks.** The bot trusts the OS certificate store plus certifi. On Windows that already includes corporate proxy roots, so nothing to do. On macOS behind a TLS-inspecting proxy, if connections fail with `CERTIFICATE_VERIFY_FAILED`, run `python manage.py certs` once. It exports the keychain roots to `certs/system_ca.pem`, which `config.toml` picks up.
- **Clock.** Keep automatic time sync on (Windows: *Settings → Time & language → Set time automatically*). The bot compares Chainlink timestamps with your clock, and a drift of a second or more degrades its price estimate.
- **Sleep.** Stop the machine sleeping while the bot runs. A sleeping machine drops the websocket feeds; the watchdogs reconnect after wake-up, but those windows are missed.
- **Line endings** are pinned by `.gitattributes` (`.bat` = CRLF, everything else LF), so a clone on any OS runs as-is.

---

## Live trading

The bot has three modes, set by `mode` in `config.toml`:

| Mode | Account | Orders | Ledger |
|---|---|---|---|
| `paper` | simulated ($`starting_equity`) | simulated fills | `data/` |
| `shadow` | **your real account** (balance, positions) | **not sent**; logged as "would send" | `data_live/` |
| `live` | **your real account** | **real FAK orders** | `data_live/` |

Live uses the same strategy, signals and limits as paper. Only execution and accounting change:
- **Equity = your portfolio balance:** pUSD cash plus the current value of all positions, read from Polymarket every `sync_every_s` (15 s). Sizing, the $30-per-market cap and both stops use it. The daily stop's "starting capital" is the balance at the first live start (`--fresh` re-reads it).
- **Orders:** fill-and-kill market BUYs through the official SDK (`polymarket-client`): `amount` = 5 shares × price cap, `max_price` = the cap, `max_spend` = amount + taker fee. Nothing rests on the book. A fill below the cap buys slightly more than 5 shares for the same dollars, never more money.
- **Winnings** are claimed automatically once a market resolves (`redeem_positions`). Proxy/Safe/deposit wallets need a Relayer API key for this (gasless); EOAs claim on-chain and need a little POL for gas.

### 1. Credentials
Copy `.env.example` to `.env` in `updown_bot/` (git-ignored; on macOS/Linux run `chmod 600 .env`) and fill in:

```
POLY_PRIVATE_KEY=0x…            # signer key (the wallet that signs orders)
POLY_FUNDER_ADDRESS=0x…         # Polymarket wallet that holds the pUSD (profile menu). Optional for type 0
POLY_SIGNATURE_TYPE=1           # 0 = EOA, 1 = proxy wallet, 2 = Safe, 3 = deposit wallet
POLY_RELAYER_API_KEY=…          # optional: automatic, gasless claiming (Settings → API Keys → Relayer)
POLY_RELAYER_API_KEY_ADDRESS=0x…
```

The SDK detects the wallet type itself. The bot refuses to trade if it doesn't match `POLY_SIGNATURE_TYPE`, or if the resolved wallet isn't `POLY_FUNDER_ADDRESS`. Keys are read only from `.env` or environment variables and are never logged. Prefer a dedicated signer holding nothing else. Accounts created since May 2026 (deposit wallets) can use a scoped, revocable **Session Key**, so your owner key never touches the bot.

### 2. Check the account (no orders)

```bash
python manage.py preflight
```

It prints the wallet and type, pUSD cash (with the raw on-chain value), positions, the equity the bot will use, whether claiming is automatic, and your limits.

### 3. Shadow run (real account, no orders)
Set `mode = "shadow"`, then run `python manage.py run` and `python manage.py dashboard --live`. The amber **SHADOW** badge shows; the Orders panel lists what it would have sent. Watch a few hours.

### 4. Go live
Set `mode = "live"`, then:

```bash
python manage.py run --live
```

It prints the wallet, balance and limits and starts trading. (Set `require_confirmation = true` under `[live]` to also require typing **LIVE**.) The dashboard (`python manage.py dashboard --live`) shows a red **LIVE · REAL MONEY** badge, the account strip (cash, positions, last sync, claiming, kill switch), and the **Orders** panel with every order and the exchange's answer.

### Safety controls
| Control | What it does |
|---|---|
| Kill switch | create a file named `STOP` in `data_live/` → no new orders at once; delete it to resume |
| Two-key start | `mode = "live"` **and** the `--live` flag; either one alone refuses to start. Optional typed LIVE prompt: `require_confirmation = true` |
| Stale balance | no orders if the balance hasn't synced for 3 × `sync_every_s` |
| Low cash | no orders while pUSD cash < `min_cash_usd` ($5) |
| Rejections | "not enough balance/allowance" halts at once; `max_consecutive_errors` (5) other errors in a row halts |
| Unknown outcome | if an order request times out or errors, the bot checks the account's recent trades before assuming no fill |
| Matching delay | markets with `seconds_delay > 0` are never traded |
| Rate limit | at most `max_orders_per_minute` (20) |
| Resting orders | `cancel_all_on_start` cancels open orders at start and shutdown, **including ones you placed by hand** (set it to false if you trade manually on the same account) |
| Existing limits | 5 shares/order, $30/market incl. fees, daily stop 50% of starting capital, 35% drawdown halt, no buys under 20¢ (`min_price`) |

Live halts are sticky until you restart. The reason is shown on the dashboard and in `data_live/bot.log` (rotated, 5 × 5 MB).

### Run it unattended
Use `python manage.py run --live` under a service manager that restarts it: Task Scheduler or NSSM on Windows, `launchd` on macOS, `systemd` on Linux. On restart it resumes its state and cancels stray orders.

### Lessons from the first live session (Sep 24)
- **Don't restart often.** Every restart skips the current window and re-measures volatility (about 5.5 min without trading). The first session restarted 6 times in 30 min, and **8 of its 12 fills were placed on the default volatility**, which lost $14.49 of the $21.47. The bot now refuses to trade on the default.
- **"No orders found to match" is normal:** a fill-and-kill order found nothing at your price. It now counts as a no-fill, not an error.
- **The first order in a market used to take ~2 s** (the SDK loading market details); the bot now loads them as soon as a market appears. Other orders take ~0.6 s.

### Latency: where it goes and how to cut it
Every live order's time is shown in the Orders panel as **sign + send**. From a US machine the path looks like this:

| Part | Typical | What the bot does |
|---|---|---|
| Reacting to a price move | ~0 ms | evaluates on every Coinbase tick (it used to check every 100 ms, ~50 ms average delay) |
| Signing the order | 0.2 ms | uses the `coincurve` C library (pure Python took 3.5 ms) |
| Opening a connection | ~100 ms if cold | avoided: the 15 s balance sync keeps the order connection warm (`sync_every_s` must stay under 30) |
| Network round trip to Polymarket | ~130 ms from the US East Coast | **only improved by running the bot closer to Polymarket's servers** |
| Polymarket's matching | the rest | fixed |

Measure your own path with `python manage.py latency`. It reports the Cloudflare edge, new vs warm connection times and signing speed. Polymarket's servers are commonly reported to be in AWS London (eu-west-2): run the tool on a small server there and compare the warm round trip before moving the bot.

### Before real money: know the gap
Paper assumed 300 ms latency and 50% of displayed size. Live competes with faster bots, so start with the small caps you've set and compare the Orders panel (fill rate, latency) and fills with your paper results before raising any limit. The bot's per-fill fee is an estimate from the published formula; the balance sync always reflects the real cash.

---

## How it trades

Every 100 ms, for each live market:

1. **Start price** R = mean of the Chainlink prints in the 60 s before the window opened.
2. **Chainlink now** is estimated as the last Chainlink print plus the Coinbase move since that print. Chainlink lags about 1–2 s; Coinbase leads.
3. **Fair value** P(Up) = Φ((E[final TWAP] − R) / sd). Seconds of the final minute that have already printed are locked in. The rest follow a random walk with σ from 30-second changes (EWMA, 10-minute half-life). See `bot/model.py`.
4. **Signal:** the coin moved **≥ 0.5 bp in the last 3 s** toward a side, **and** that side's ask is below fair value by **≥ 2¢ after the taker fee** (fee = 0.07 × p × (1 − p) per share). Guards added after the first live session:
   - **No trading until volatility is measured.** Every start and restart measures it fresh from Chainlink (about 5.5 min) before trading. Set `vol_restore_max_age_s` (e.g. 900) to reuse a recent saved estimate instead.
   - **Edges above 12¢ are skipped** (`max_edge`): a disagreement that large usually means the model is wrong, not the market.
   - **3-second moves above 5 bp are skipped** (`max_momentum_bp`).
   - **Coinbase can shift the Chainlink estimate by at most 3 σ·√lag** (`max_spot_adjust_sigma`), so a one-exchange spike can't flip the model.
5. **Order:** a FAK limit buy at up to ask + 2¢, never above the price that keeps 2¢ of edge after fees.
6. **Paper fill:** wait `latency_ms` (700 ms, matching measured live latency), re-read the live book, and take at most `liquidity_haircut` (50%) of each ask level up to the limit. Fees are charged per level; fills under 5 shares or $1 are skipped.
7. **Settlement:** Polymarket's official result, read on-chain from the Conditional Tokens contract. Markets resolve there about 60–100 s after they end; the CLOB API's `winner` flag lags by many minutes and is only a fallback. The bot's own TWAP estimate is logged next to the official result as a check.
8. **Rebates (estimate):** yesterday's taker fees × your tier, credited after 00:00 UTC. Tiers use the documented 30-day weighted volume.

### Sizing and limits
Each order's size is the **smallest** of these, all set in `[risk]` in `config.toml`:

| Setting | Default | Meaning |
|---|---|---|
| `max_shares_per_order` | **5** | hard cap per order in shares (0 = no cap). Polymarket's minimum order is also 5, so by default every order is exactly 5 shares |
| `max_market_usd` | **$30** | max total spent in one market, **including fees and orders still in flight** (0 = no cap) |
| `max_market_exposure_pct` | 25% | also caps each market at this share of equity; the lower of the two caps wins |
| `clip_pct_equity` | 5% | order size as a share of equity (what makes size grow once you lift the share cap) |
| `max_clip_usd` | $250 | hard cap per order in USD |
| `daily_loss_stop_pct` + `daily_loss_stop_basis` | 50% of `"initial"` | no new positions for the rest of the UTC day once today's loss reaches 50% of starting capital ($100 on $200). `"day_start"` measures it against equity at 00:00 UTC instead |
| `max_drawdown_kill_pct` | 35% | halt the bot if equity falls this far below its peak |

With the defaults, an order costs at most about $4.80 (5 shares at 95¢ plus fee), so a market takes several orders to reach $30. The bot stops adding to a market once the next 5-share order would push total spend past $30.

**Scaling up later:** raise `max_market_usd` and `max_shares_per_order` (or set them to 0) and let `clip_pct_equity` and `max_market_exposure_pct` size positions from equity. Past a few thousand dollars, order-book depth limits you before these settings do (median fill in the analysed bots: about $8), so add assets (`assets = ["btc","eth","sol"]`) rather than making orders bigger.

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
manage.py         cross-platform launcher: setup | run | dashboard | report | test | preflight | latency | certs
latency.py        measures network latency to Polymarket's order server from this machine
*.bat             Windows double-click shortcuts for manage.py
run.py            start the bot          report.py      terminal report (+ --csv)
dashboard.py      local dashboard        config.toml    every tunable parameter
bot/model.py      TWAP fair value, vol   bot/strategy.py momentum + edge signal
bot/feeds.py      Coinbase + Chainlink   bot/book.py     CLOB order books
bot/markets.py    discovery, resolution  bot/paper.py    fills, portfolio, settlement, rebates
bot/risk.py       sizing, stops          bot/engine.py   orchestration, status.json
bot/ledger.py     SQLite ledger          bot/analytics.py shared performance maths
bot/live.py       live + shadow execution, account sync, claiming (official polymarket-client SDK)
bot/secrets.py    .env / environment credentials, validation, masking
.env.example      template for live credentials (copy to .env, never commit)
tests/            41 tests: model (incl. Monte-Carlo TWAP check), paper engine, risk, live execution vs a fake SDK
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
