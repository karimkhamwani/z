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
    """Verified TLS context. `ca_bundle` adds extra roots (e.g. a corporate proxy CA exported from the OS keychain).
    `relax_x509_strict` only drops Python 3.13's strict-X.509 flag; the certificate chain is still verified."""
    global _CTX
    cafile = ca_bundle if ca_bundle and Path(ca_bundle).exists() else certifi.where()
    ctx = ssl.create_default_context(cafile=cafile)
    if relax_x509_strict and hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    _CTX = ctx
    return ctx


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
