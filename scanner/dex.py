"""Free market data from DexScreener (no key; ~300 req/min on /tokens/v1)."""
import logging
import time

from . import chains, config
from .http import request

log = logging.getLogger("dex")
SOL_MINT = "So11111111111111111111111111111111111111112"


def tokens(keys):
    """{token key: best-liquidity pair summary}. Batches 30 addresses per call per chain."""
    by_chain = {}
    for k in dict.fromkeys(keys):
        c, a = chains.split(k)
        by_chain.setdefault(c, []).append(a)
    out = {}
    for chain, addrs in by_chain.items():
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            r = request("GET", f"{config.DEX_BASE}/tokens/v1/{chains.CHAIN[chain]['dex']}/{','.join(chunk)}")
            if r is None:
                continue
            pairs = r.json()
            if isinstance(pairs, dict):
                pairs = pairs.get("pairs") or []
            wanted = {chains.norm(chain, a) for a in chunk}
            for p in pairs:
                addr = chains.norm(chain, (p.get("baseToken") or {}).get("address") or "")
                if addr not in wanted:
                    continue
                k = chains.key(chain, addr)
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                if k in out and out[k]["liquidity"] >= liq:
                    continue
                out[k] = summarize(p, chain, k)
            time.sleep(0.25)
    return out


def summarize(p, chain, k):
    info = p.get("info") or {}
    socials = {s.get("type"): s.get("url") for s in info.get("socials") or []}
    tx = p.get("txns") or {}
    return {
        "key": k, "chain": chain,
        "address": chains.split(k)[1],
        "pair": p.get("pairAddress", ""),
        "dex": p.get("dexId", ""),
        "symbol": p["baseToken"].get("symbol", "?"),
        "name": p["baseToken"].get("name", ""),
        "price": float(p.get("priceUsd") or 0),
        "liquidity": float((p.get("liquidity") or {}).get("usd") or 0),
        "mcap": float(p.get("marketCap") or p.get("fdv") or 0),
        "change": {k2: float(v or 0) for k2, v in (p.get("priceChange") or {}).items()},
        "volume": {k2: float(v or 0) for k2, v in (p.get("volume") or {}).items()},
        "buys_h1": (tx.get("h1") or {}).get("buys", 0),
        "sells_h1": (tx.get("h1") or {}).get("sells", 0),
        "buys_h24": (tx.get("h24") or {}).get("buys", 0),
        "sells_h24": (tx.get("h24") or {}).get("sells", 0),
        "created_ms": p.get("pairCreatedAt") or 0,
        "url": p.get("url", ""),
        "boosts": (p.get("boosts") or {}).get("active", 0),
        "twitter": socials.get("twitter") or socials.get("x"),
        "telegram": socials.get("telegram"),
        "website": ((info.get("websites") or [{}])[0] or {}).get("url"),
    }


def sol_price():
    d = tokens([chains.key("solana", SOL_MINT)])
    v = next(iter(d.values()), None)
    return v["price"] if v else 150.0
