"""Rug / honeypot checks. A coin that fails a hard check is never recommended.

Solana: RugCheck (free, no key).  Base/BNB/Robinhood: GoPlus (free, no key).
"""
import logging

from . import chains, config
from .http import request

log = logging.getLogger("safety")


def check(chain, addr):
    """Return {'ok': bool, 'verified': bool, 'hard': [...], 'flags': [...], 'good': [...], 'top10': float|None}."""
    try:
        return _rugcheck(addr) if chain == "solana" else _goplus(chain, addr)
    except Exception as e:  # never let a bad response crash the run
        log.warning("safety check failed for %s:%s: %s", chain, addr, e)
        return {"ok": True, "verified": False, "hard": [], "flags": ["safety check unavailable"],
                "good": [], "top10": None}


def _rugcheck(mint):
    r = request("GET", f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
    out = {"ok": True, "verified": False, "hard": [], "flags": [], "good": [], "top10": None}
    if r is None:
        out["flags"].append("RugCheck unavailable")
        return out
    d = r.json()
    out["verified"] = True
    for risk in d.get("risks") or []:
        name, level = risk.get("name", ""), risk.get("level", "")
        low = name.lower()
        if level == "danger" or any(w in low for w in ("mint authority", "freeze authority", "copycat")):
            out["hard"].append(name)
        elif level == "warn":
            out["flags"].append(name)
    lp = d.get("lpLockedPct")
    if lp is not None and float(lp) >= 90:
        out["good"].append(f"LP {float(lp):.0f}% locked/burned")
    elif lp is not None and float(lp) < 50 and "pump" not in mint.lower():
        out["flags"].append(f"only {float(lp):.0f}% of liquidity locked")
    if not out["hard"]:
        out["good"].append("RugCheck: no danger-level risks")
    out["ok"] = not out["hard"]
    return out


def _flag(d, k):
    return str(d.get(k, "0")) == "1"


def _goplus(chain, addr):
    cid = chains.CHAIN[chain]["goplus"]
    out = {"ok": True, "verified": False, "hard": [], "flags": [], "good": [], "top10": None}
    r = request("GET", f"https://api.gopluslabs.io/api/v1/token_security/{cid}",
                params={"contract_addresses": addr})
    d = ((r.json() if r is not None else {}) or {}).get("result") or {}
    d = d.get(addr.lower()) or next(iter(d.values()), None) if d else None
    if not d:
        if chain == "robinhood":
            return _blockscout(addr, out)
        out["flags"].append("GoPlus has no data for this token - honeypot/tax not verified")
        return out
    out["verified"] = True
    if _flag(d, "is_honeypot") or _flag(d, "cannot_sell_all"):
        out["hard"].append("honeypot / can't sell")
    for k, label in (("owner_change_balance", "owner can change balances"),
                     ("hidden_owner", "hidden owner"), ("selfdestruct", "self-destruct"),
                     ("is_blacklisted", "blacklist function"), ("transfer_pausable", "trading can be paused"),
                     ("slippage_modifiable", "owner can change tax")):
        if _flag(d, k):
            (out["hard"] if k in ("owner_change_balance", "hidden_owner", "selfdestruct") else out["flags"]).append(label)
    if _flag(d, "is_mintable"):
        out["flags"].append("owner can mint more tokens")
    try:
        tax = max(float(d.get("buy_tax") or 0), float(d.get("sell_tax") or 0)) * 100
    except ValueError:
        tax = 0
    if tax > config.MAX_TAX_PCT:
        out["hard"].append(f"{tax:.0f}% buy/sell tax")
    if str(d.get("is_open_source", "1")) == "0":
        out["hard"].append("contract not verified/open source")
    holders = [h for h in d.get("holders") or []
               if str(h.get("is_contract", "0")) != "1" and str(h.get("is_locked", "0")) != "1"]
    try:
        out["top10"] = round(sum(float(h.get("percent", 0)) for h in holders[:10]) * 100, 1)
    except ValueError:
        pass
    lp_locked = sum(float(h.get("percent", 0) or 0) for h in d.get("lp_holders") or []
                    if str(h.get("is_locked", "0")) == "1" or (h.get("address") or "").startswith("0x000000000000000000000000000000000000dead"))
    if lp_locked >= 0.9:
        out["good"].append(f"LP {lp_locked * 100:.0f}% locked/burned")
    if not out["hard"]:
        out["good"].append("GoPlus: no honeypot, tax OK")
    out["ok"] = not out["hard"]
    return out


def _blockscout(addr, out):
    """Robinhood Chain fallback: at least confirm the contract source is verified."""
    r = request("GET", f"https://robinhoodchain.blockscout.com/api/v2/smart-contracts/{addr}", retries=1)
    d = r.json() if r is not None else {}
    if d.get("is_verified"):
        out["flags"].append("only basic check available (verified contract, honeypot/tax not tested)")
    else:
        out["hard"].append("contract source not verified and no honeypot check available")
        out["ok"] = False
    return out
