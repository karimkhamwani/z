"""Live execution — intentionally NOT implemented.

The paper engine calls `executor.buy(...)`. A live executor must provide the same method and:
  1. Sign and post a FAK (fill-and-kill) limit BUY at `limit` for up to `max_shares` on `market.token(outcome)`
     via Polymarket's CLOB API (official client: py-clob-client; check it supports the post-April-2026 exchange
     and pUSD collateral).
  2. Read the actual fills back (shares, avg price, fee) and update the Portfolio exactly like PaperExecutor.
  3. Redeem winning tokens after resolution.
  4. Load keys only from environment variables or a secrets manager — never from config.toml or the repo.

Do not switch this on until paper trading has been positive after fees for weeks, and compare paper vs. live
fills on tiny size first: paper assumes `liquidity_haircut` of the displayed book and `latency_ms` delay,
which real competition may make worse.
"""


class LiveExecutor:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "Live trading is not implemented. Run in paper mode; see bot/live.py for what a live executor needs.")
