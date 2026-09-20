"""FOMO data via fomoapi.io (unofficial third-party API for fomo.family).

Free tier = 250k credits/month, so we cache aggressively:
  leaderboard (250cr)  every LEADERBOARD_REFRESH_HOURS
  trending    (250cr)  every TRENDING_REFRESH_HOURS
  thesis      (1250cr) for the top few candidates, THESIS_TOKENS_PER_DAY
"""
import logging
import time
from datetime import datetime, timezone

from . import chains, config, state as st
from .http import request

log = logging.getLogger("fomo")

COST = {"leaderboard": 250, "trending": 250, "thesis": 1250}


def _get(s, path, cost, params=None, required=False):
    if not config.FOMO_API_KEY:
        return None
    if not required and not st.within_budget(
            s["fomo_credits_used"], config.FOMO_MONTHLY_CREDITS, cost):
        log.info("FOMO budget pacing: skipping %s", path)
        return None
    r = request("GET", config.FOMO_BASE + path, params=params,
                headers={"authorization": f"Bearer {config.FOMO_API_KEY}"})
    if r is None:
        return None
    s["fomo_credits_used"] += int(r.headers.get("x-credits-cost", cost) or cost)
    rem = r.headers.get("x-credits-remaining")
    if rem is not None:
        try:  # trust the server's own counter when present
            s["fomo_credits_used"] = max(s["fomo_credits_used"],
                                         config.FOMO_MONTHLY_CREDITS - int(rem))
        except ValueError:
            pass
    return r.json()


def leaderboard(s):
    lb = s["leaderboard"]
    stale = time.time() - lb["ts"] > config.LEADERBOARD_REFRESH_HOURS * 3600
    if stale or not lb["traders"]:
        data = _get(s, f"/v2/leaderboard/{config.LEADERBOARD_WINDOW}", COST["leaderboard"],
                    {"limit": config.LEADERBOARD_SIZE}, required=not lb["traders"])
        traders = (data or {}).get("traders") if isinstance(data, dict) else data
        if traders:
            s["leaderboard"] = {"ts": time.time(), "traders": [
                {"rank": t.get("rank", i + 1),
                 "handle": t.get("handle", ""),
                 "pnlUsd": t.get("pnlUsd", 0),
                 "wallet": (t.get("wallets") or {}).get("solana"),
                 "evm": ((t.get("wallets") or {}).get("evm") or "").lower() or None}
                for i, t in enumerate(traders[:config.LEADERBOARD_SIZE])]}
            log.info("Leaderboard refreshed: %d traders", len(traders))
    return s["leaderboard"]["traders"]


def trending(s):
    tr = s["trending"]
    if time.time() - tr["ts"] > config.TRENDING_REFRESH_HOURS * 3600:
        data = _get(s, "/v2/leaderboard/tokens/trending", COST["trending"], {"limit": 100})
        toks = (data or {}).get("tokens") if isinstance(data, dict) else data
        if toks:
            out = []
            for t in toks:
                net = t.get("network") or t.get("chain") or t.get("chainId") or (t.get("token") or {}).get("network") or "solana"
                chain = chains.FOMO_NET.get(str(net).lower().strip())
                addr = (t.get("token") or {}).get("address")
                if chain in config.CHAINS and addr:
                    out.append({"key": chains.key(chain, addr), "rank": t.get("rank"),
                                "holders": t.get("holders")})
            s["trending"] = {"ts": time.time(), "tokens": out}
    return s["trending"]["tokens"]


def thesis(s, key):
    """Community posts ('theses') about a coin on FOMO. Cached 12h, daily cap."""
    chain, mint = chains.split(key)
    c = s["thesis_cache"].get(key)
    if c and time.time() - c["ts"] < 12 * 3600:
        return c["items"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if s["thesis_day"]["day"] != today:
        s["thesis_day"] = {"day": today, "count": 0}
    if s["thesis_day"]["count"] >= config.THESIS_TOKENS_PER_DAY:
        return c["items"] if c else None
    data = _get(s, f"/v2/thesis/token/{mint}", COST["thesis"],
                {"limit": 50, "network": chains.CHAIN[chain]["fomo"], "sort": "recent"})
    if data is None:
        return c["items"] if c else None
    items = data if isinstance(data, list) else (data.get("theses") or data.get("items") or [])
    items = [{"handle": i.get("handle"), "text": (i.get("text") or "")[:280],
              "likes": i.get("likes", 0), "tradeUsd": i.get("tradeUsd", 0),
              "isDev": i.get("isDev", False), "ts": i.get("ts")} for i in items]
    s["thesis_day"]["count"] += 1
    s["thesis_cache"][key] = {"ts": time.time(), "items": items}
    return items
