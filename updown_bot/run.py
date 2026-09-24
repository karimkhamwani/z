"""Run the Polymarket Up/Down momentum bot.

    .venv/bin/python run.py                  # paper trading with config.toml (resumes saved paper portfolio)
    .venv/bin/python run.py --fresh          # start over from starting_equity
    .venv/bin/python run.py --minutes 30     # stop automatically after 30 minutes
"""
import argparse
import asyncio
import logging
import sys

from bot.config import load_config
from bot.engine import Engine
from bot.net import ssl_context


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--fresh", action="store_true", help="discard the saved paper portfolio and start from starting_equity")
    ap.add_argument("--minutes", type=float, default=None, help="stop after this many minutes")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252; never crash on "→" or "¢"
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg = load_config(args.config)
    if cfg.mode != "paper":
        sys.exit("Only mode = \"paper\" is supported. Live execution is not implemented (see bot/live.py).")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s | %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    ssl_context(cfg.feeds.ca_bundle, cfg.feeds.relax_x509_strict)
    engine = Engine(cfg, fresh=args.fresh, run_seconds=args.minutes * 60 if args.minutes else None)
    try:
        asyncio.run(engine.run())
    except (KeyboardInterrupt, SystemExit):
        logging.info("stopped — portfolio saved to %s", engine.state_path)


if __name__ == "__main__":
    main()
