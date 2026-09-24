"""Run the Polymarket Up/Down momentum bot.

    python manage.py run                   # mode from config.toml (paper by default); resumes saved state
    python manage.py run --fresh           # start over (paper: starting_equity; live: current portfolio balance)
    python manage.py run --minutes 30      # stop automatically after 30 minutes
    python manage.py preflight             # live/shadow: check keys, wallet, balance and positions — no orders
    python manage.py run --live            # required in addition to mode = "live" to send real orders
"""
import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from bot.config import load_config
from bot.engine import Engine
from bot.net import ssl_context

HERE = Path(__file__).resolve().parent


def banner(lines: list[str]) -> None:
    w = max(len(l) for l in lines) + 4
    print("\n" + "=" * w)
    for l in lines:
        print(f"  {l}")
    print("=" * w)


async def connect_account(cfg):
    from bot.live import LiveAccount
    from bot.secrets import CredentialError, get_live_creds, mask
    try:
        creds, warnings = get_live_creds(HERE)
    except CredentialError as e:
        sys.exit(f"Credentials: {e}")
    for w in warnings:
        logging.warning(w)
    acct = LiveAccount(creds)
    try:
        await acct.connect()
    except ImportError:
        sys.exit("The Polymarket SDK isn't installed. Run: python manage.py setup")
    return acct, creds, mask


async def preflight(cfg) -> None:
    acct, creds, mask = await connect_account(cfg)
    try:
        snap = await acct.snapshot()
        banner([f"Wallet        {mask(acct.wallet)}  ({acct.wallet_type}; POLY_SIGNATURE_TYPE={creds.signature_type} ✓)",
                f"pUSD cash     ${snap.cash:,.2f}   (raw balance {snap.raw_balance})",
                f"Positions     {snap.open_positions} open, value ${snap.positions_value:,.2f}, "
                f"{len(snap.redeemable)} claimable",
                f"Equity        ${snap.cash + snap.positions_value:,.2f}  = portfolio balance used for sizing and stops",
                f"Claiming      {'automatic' if creds.can_redeem else 'manual on polymarket.com (no Relayer API key)'}",
                f"Limits        {cfg.risk.max_shares_per_order:g} shares/order, ${cfg.risk.max_market_usd:g}/market, "
                f"daily stop {cfg.risk.daily_loss_stop_pct:.0%} of starting capital",
                "Preflight OK — no orders were placed."])
        if snap.cash < cfg.live.min_cash_usd:
            logging.warning("cash $%.2f is below live.min_cash_usd ($%.2f): the bot won't open positions",
                            snap.cash, cfg.live.min_cash_usd)
    finally:
        await acct.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--fresh", action="store_true", help="discard saved state (live: re-read starting capital)")
    ap.add_argument("--minutes", type=float, default=None, help="stop after this many minutes")
    ap.add_argument("--live", action="store_true", help='required with mode = "live": confirms real-money trading')
    ap.add_argument("--yes", action="store_true", help="skip the typed LIVE confirmation (for service managers)")
    ap.add_argument("--preflight", action="store_true", help="check the live account and exit without trading")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252; never crash on "→" or "¢"
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg = load_config(args.config)
    if args.preflight and cfg.mode == "paper":
        cfg.mode = "shadow"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if cfg.mode != "paper":
        Path(cfg.logging.data_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(Path(cfg.logging.data_dir) / "bot.log",
                                                             maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname).1s %(name)s | %(message)s", datefmt="%H:%M:%S")
    for noisy in ("websockets", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ssl_context(cfg.feeds.ca_bundle, cfg.feeds.relax_x509_strict)

    if args.preflight:
        asyncio.run(preflight(cfg))
        return
    if cfg.mode == "live" and not args.live:
        sys.exit('config.toml has mode = "live". Real orders need the --live flag too:  python manage.py run --live\n'
                 'To test with your real account without sending orders, use mode = "shadow".')
    if args.live and cfg.mode != "live":
        sys.exit(f'--live was given but config.toml has mode = "{cfg.mode}". Set mode = "live" to trade real money.')

    async def start() -> None:
        account = None
        if cfg.mode in ("shadow", "live"):
            account, creds, mask = await connect_account(cfg)
            snap = await account.snapshot()
            lines = [f"MODE: {cfg.mode.upper()}  {'— REAL MONEY' if cfg.mode == 'live' else '— orders are NOT sent'}",
                     f"Wallet {mask(account.wallet)} ({account.wallet_type})   cash ${snap.cash:,.2f}   "
                     f"positions ${snap.positions_value:,.2f}",
                     f"{cfg.markets.assets} {cfg.markets.timeframe}   max {cfg.risk.max_shares_per_order:g} shares/order, "
                     f"${cfg.risk.max_market_usd:g}/market, daily stop {cfg.risk.daily_loss_stop_pct:.0%} of starting capital",
                     f"Kill switch: create the file {Path(cfg.logging.data_dir) / 'STOP'}   ·   Ctrl+C stops the bot"]
            banner(lines)
            if cfg.mode == "live" and cfg.live.require_confirmation and not args.yes:
                if not sys.stdin.isatty():
                    await account.close()
                    sys.exit("Live mode needs a typed confirmation; pass --yes when running without a terminal.")
                if input('Type LIVE to start trading real money: ').strip() != "LIVE":
                    await account.close()
                    sys.exit("Not confirmed — nothing was traded.")
        engine = Engine(cfg, fresh=args.fresh, run_seconds=args.minutes * 60 if args.minutes else None, account=account)
        main.engine = engine
        await engine.run()

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        pass
    eng = getattr(main, "engine", None)
    if eng is not None:
        logging.info("stopped — state saved to %s", eng.state_path)


if __name__ == "__main__":
    main()
