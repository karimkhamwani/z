"""Networking helpers: one SSL context for every connection, and a small JSON GET."""
from __future__ import annotations

import asyncio
import json
import ssl
import urllib.request
from pathlib import Path

import certifi

UA = {"User-Agent": "Mozilla/5.0"}  # the CLOB API rejects Python's default User-Agent
_CTX: ssl.SSLContext | None = None


def ssl_context(ca_bundle: str = "", relax_x509_strict: bool = True) -> ssl.SSLContext:
    """Verified TLS context trusting: the OS certificate store (on Windows this includes corporate/proxy roots),
    certifi's Mozilla roots, and optionally an extra PEM bundle (e.g. roots exported from the macOS keychain).
    `relax_x509_strict` only drops Python 3.13's strict-X.509 flag; the certificate chain is still verified."""
    global _CTX
    ctx = ssl.create_default_context()          # loads the OS store (Windows: ROOT + CA system stores)
    ctx.load_verify_locations(cafile=certifi.where())
    if ca_bundle and Path(ca_bundle).exists():
        ctx.load_verify_locations(cafile=ca_bundle)
    if relax_x509_strict and hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    _CTX = ctx
    return ctx


def atomic_write_text(path: Path, text: str, retries: int = 20) -> None:
    """Write via temp file + replace. On Windows, replace fails while another process (the dashboard) has the
    target open, so retry briefly instead of crashing the bot."""
    import time
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(retries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.02 * (i + 1))
    # give up quietly for this write; the next one (≤1 s later) will succeed


def get_ctx() -> ssl.SSLContext:
    return _CTX or ssl_context()


def _get_json_sync(url: str, timeout: float) -> object:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=get_ctx()) as r:
        return json.load(r)


async def get_json(url: str, timeout: float = 10.0, retries: int = 3) -> object:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await asyncio.to_thread(_get_json_sync, url, timeout)
        except Exception as e:  # network hiccups are expected; caller decides what to do on None
            last = e
            await asyncio.sleep(0.5 * (attempt + 1))
    raise ConnectionError(f"GET failed after {retries} tries: {url}: {last}")
