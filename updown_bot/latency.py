"""Measure this machine's latency to Polymarket's order server (no keys, no orders).

    python manage.py latency

Run it on every machine or server you consider for the bot and pick the lowest "warm round trip":
that's roughly what each order pays on the network, on top of Polymarket's own processing time.
"""
import asyncio
import socket
import ssl
import statistics as st
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bot.config import load_config  # noqa: E402
from bot.net import ssl_context  # noqa: E402

HOST = "clob.polymarket.com"
URL = f"https://{HOST}/time"


def cold_request(ctx: ssl.SSLContext) -> dict:
    """One request on a brand-new connection, timing each phase."""
    t0 = time.perf_counter()
    addr = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)[0][4]
    t_dns = time.perf_counter()
    sock = socket.create_connection(addr[:2], timeout=10)
    t_tcp = time.perf_counter()
    tls = ctx.wrap_socket(sock, server_hostname=HOST)
    t_tls = time.perf_counter()
    tls.sendall(f"GET /time HTTP/1.1\r\nHost: {HOST}\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n".encode())
    tls.recv(1)
    t_first = time.perf_counter()
    tls.close()
    return {"dns": t_dns - t0, "tcp": t_tcp - t_dns, "tls": t_tls - t_tcp, "first_byte": t_first - t_tls,
            "total": t_first - t0}


async def warm_requests(ctx: ssl.SSLContext, n: int = 15) -> list[float]:
    import httpx
    out = []
    async with httpx.AsyncClient(verify=ctx, headers={"User-Agent": "Mozilla/5.0"}) as c:
        await c.get(URL)
        for _ in range(n):
            t = time.perf_counter()
            await c.get(URL)
            out.append(time.perf_counter() - t)
            await asyncio.sleep(0.2)
    return out


def edge_location(ctx: ssl.SSLContext) -> str:
    import urllib.request
    try:
        req = urllib.request.Request(f"https://{HOST}/cdn-cgi/trace", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            kv = dict(line.split("=", 1) for line in r.read().decode().splitlines() if "=" in line)
        return f"{kv.get('colo', '?')} (Cloudflare edge), your country {kv.get('loc', '?')}"
    except Exception as e:
        return f"unknown ({e})"


def signing_speed() -> str:
    try:
        import eth_keys
        from eth_account import Account
    except ImportError:
        return "eth-account not installed (run: python manage.py setup)"
    typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                        {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                       "Order": [{"name": "salt", "type": "uint256"}, {"name": "maker", "type": "address"},
                                 {"name": "tokenId", "type": "uint256"}, {"name": "makerAmount", "type": "uint256"}]},
             "primaryType": "Order", "domain": {"name": "x", "version": "1", "chainId": 137, "verifyingContract": "0x" + "11" * 20},
             "message": {"salt": 1, "maker": "0x" + "22" * 20, "tokenId": 1, "makerAmount": 1}}
    acct = Account.create()      # throwaway key, never used for anything
    ts = []
    for _ in range(20):
        t = time.perf_counter()
        acct.sign_typed_data(full_message=typed)
        ts.append(time.perf_counter() - t)
    backend = eth_keys.KeyAPI().backend.__class__.__name__
    hint = "" if "CoinCurve" in backend else "  ← slow: run `python manage.py setup` to install coincurve"
    return f"{st.median(ts) * 1000:.1f} ms ({backend}){hint}"


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg = load_config(HERE / "config.toml")
    ctx = ssl_context(cfg.feeds.ca_bundle, cfg.feeds.relax_x509_strict)
    print(f"Measuring latency to {HOST} from this machine …\n")
    print(f"  Network edge        {edge_location(ctx)}")
    colds = [cold_request(ctx) for _ in range(3)]
    med = {k: st.median(c[k] for c in colds) * 1000 for k in colds[0]}
    print(f"  New connection      {med['total']:.0f} ms  (DNS {med['dns']:.0f} + TCP {med['tcp']:.0f} + TLS {med['tls']:.0f} "
          f"+ first byte {med['first_byte']:.0f})")
    warm = asyncio.run(warm_requests(ctx))
    w = st.median(warm) * 1000
    print(f"  Warm round trip     {w:.0f} ms  (median of {len(warm)}; min {min(warm) * 1000:.0f}, max {max(warm) * 1000:.0f})")
    print(f"  Order signing       {signing_speed()}")
    print("\nWhat it means")
    print(f"  • Each order pays about the warm round trip ({w:.0f} ms) plus Polymarket's processing. The bot keeps")
    print("    the order connection warm through its 15 s balance sync, so the new-connection cost is avoided.")
    if med["tcp"] < 15 and w > 80:
        print(f"  • TCP to the Cloudflare edge takes {med['tcp']:.0f} ms, but a full round trip takes {w:.0f} ms: most of the time")
        print("    is the path from Cloudflare to Polymarket's servers. A server closer to them cuts it directly.")
    print("  • Polymarket's servers are commonly reported to run in AWS London (eu-west-2). Run this tool on a")
    print("    small server there (and anywhere else you're considering) and compare the warm round trip.")


if __name__ == "__main__":
    main()
