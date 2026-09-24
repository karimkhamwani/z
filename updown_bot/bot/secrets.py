"""Live-trading credentials: read from environment variables or a local, git-ignored `.env` file.

    POLY_PRIVATE_KEY      signer key (the wallet that signs orders)
    POLY_FUNDER_ADDRESS   Polymarket account wallet that holds the funds (proxy wallet address).
                          Optional for POLY_SIGNATURE_TYPE=0: defaults to the signer's own address.
    POLY_SIGNATURE_TYPE   0 = EOA, 1 = proxy wallet (Magic/email), 2 = Safe (browser wallet), 3 = deposit wallet.
                          The official SDK detects the wallet type itself; this value is checked against it.
    POLY_RELAYER_API_KEY / POLY_RELAYER_API_KEY_ADDRESS   optional: gasless claiming of winnings (proxy/safe/deposit)

POLYMARKET_* names (as in Polymarket's docs) are accepted as aliases. Never put keys in config.toml or commit
them. Values are never logged; addresses are shown masked.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

SIGNATURE_TYPES = {0: "EOA", 1: "POLY_PROXY", 2: "GNOSIS_SAFE", 3: "DEPOSIT_WALLET"}
ALIASES = {
    "POLY_PRIVATE_KEY": ("POLY_PRIVATE_KEY", "POLYMARKET_PRIVATE_KEY"),
    "POLY_FUNDER_ADDRESS": ("POLY_FUNDER_ADDRESS", "POLYMARKET_WALLET_ADDRESS"),
    "POLY_SIGNATURE_TYPE": ("POLY_SIGNATURE_TYPE",),
    "POLY_RELAYER_API_KEY": ("POLY_RELAYER_API_KEY", "POLYMARKET_RELAYER_API_KEY"),
    "POLY_RELAYER_API_KEY_ADDRESS": ("POLY_RELAYER_API_KEY_ADDRESS", "POLYMARKET_RELAYER_API_KEY_ADDRESS"),
}
HEX_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveCreds:
    private_key: str = ""
    wallet: str = ""                 # funder / account wallet
    signature_type: int = 1
    relayer_key: str = ""
    relayer_address: str = ""

    @property
    def expected_wallet_type(self) -> str:
        return SIGNATURE_TYPES[self.signature_type]

    @property
    def can_redeem(self) -> bool:
        """EOAs redeem on-chain themselves (needs POL for gas); smart wallets need a Relayer API key."""
        return self.signature_type == 0 or bool(self.relayer_key and self.relayer_address)

    @property
    def relayer(self) -> bool:
        return bool(self.relayer_key and self.relayer_address)

    def __repr__(self) -> str:  # never print secrets, even by accident
        return (f"LiveCreds(wallet={mask(self.wallet)}, type={self.expected_wallet_type}, private_key=***, "
                f"relayer={'set' if self.relayer else 'not set'})")


def mask(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else ("(not set)" if not addr else "***")


def load_dotenv(path: Path) -> list[str]:
    """Minimal .env loader (KEY=VALUE per line, # comments, optional quotes, inline # comments after a space).
    Existing environment variables win. Returns warnings (e.g. file readable by other users)."""
    warnings: list[str] = []
    if not path.exists():
        return warnings
    if os.name == "posix" and path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
        warnings.append(f"{path} is readable by other users; run: chmod 600 {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            q = value[0]
            value = value[1:value.find(q, 1)] if value.find(q, 1) > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value
    return warnings


def _get(name: str) -> str:
    for k in ALIASES[name]:
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


def _address_of(private_key: str) -> str:
    from eth_account import Account   # installed with polymarket-client
    return Account.from_key(private_key).address


def get_live_creds(project_dir: Path) -> tuple[LiveCreds, list[str]]:
    warnings = load_dotenv(project_dir / ".env")
    pk = _get("POLY_PRIVATE_KEY")
    funder = _get("POLY_FUNDER_ADDRESS")
    st_raw = _get("POLY_SIGNATURE_TYPE") or "1"
    rk, ra = _get("POLY_RELAYER_API_KEY"), _get("POLY_RELAYER_API_KEY_ADDRESS")
    if not pk:
        raise CredentialError("Missing POLY_PRIVATE_KEY. Copy .env.example to .env and fill it in.")
    if not HEX_KEY.match(pk):
        raise CredentialError("POLY_PRIVATE_KEY must be 64 hex characters (optionally prefixed with 0x)")
    pk = pk if pk.startswith("0x") else "0x" + pk
    try:
        sig_type = int(st_raw)
        assert sig_type in SIGNATURE_TYPES
    except (ValueError, AssertionError):
        raise CredentialError("POLY_SIGNATURE_TYPE must be 0 (EOA), 1 (proxy), 2 (Safe) or 3 (deposit wallet)")
    if not funder:
        if sig_type != 0:
            raise CredentialError("Missing POLY_FUNDER_ADDRESS (your Polymarket wallet address, from the profile menu)")
        funder = _address_of(pk)
    if not ADDRESS.match(funder):
        raise CredentialError("POLY_FUNDER_ADDRESS must look like 0x followed by 40 hex characters")
    if sig_type == 0 and funder.lower() != _address_of(pk).lower():
        raise CredentialError("POLY_SIGNATURE_TYPE=0 (EOA) but POLY_FUNDER_ADDRESS is not the signer's own address")
    if bool(rk) != bool(ra):
        raise CredentialError("Set both POLY_RELAYER_API_KEY and POLY_RELAYER_API_KEY_ADDRESS, or neither")
    if ra and not ADDRESS.match(ra):
        raise CredentialError("POLY_RELAYER_API_KEY_ADDRESS must look like 0x followed by 40 hex characters")
    creds = LiveCreds(pk, funder, sig_type, rk, ra)
    if not creds.can_redeem:
        warnings.append("No Relayer API key: winnings won't be claimed automatically. Claim them on polymarket.com, "
                        "or add POLY_RELAYER_API_KEY / POLY_RELAYER_API_KEY_ADDRESS (Settings → API Keys → Relayer).")
    elif sig_type == 0:
        warnings.append("EOA wallet: claiming winnings is an on-chain transaction; keep a little POL for gas.")
    return creds, warnings
