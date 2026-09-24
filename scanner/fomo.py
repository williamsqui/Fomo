"""FOMO data via fomoapi.io (unofficial third-party API for fomo.family).

Free tier = 250k credits/month, so we cache aggressively:
  leaderboard (250cr)  every LEADERBOARD_REFRESH_HOURS
  trending    (250cr)  every TRENDING_REFRESH_HOURS
  thesis      (1250cr) for the top few candidates, THESIS_TOKENS_PER_DAY
"""
import logging
import time
from datetime import datetime, timezone

from . import chains, config, state as st, traders as reputation
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
            reputation.observe(s, s["leaderboard"]["traders"])
            log.info("Leaderboard refreshed: %d traders (%d days of history)",
                     len(traders), reputation.history_days(s))
    return s["leaderboard"]["traders"]


OFF_BOARD = 101          # rank given to a followed trader who isn't on the top-100 right now


def _norm(h):
    return (h or "").lower().lstrip("@")


def followed(s):
    """Wallets of the traders you follow: {handle: {"wallet", "evm"}}. Looked up once each.

    Free if they're on the leaderboard or are the copy-trader; otherwise one FOMO profile
    lookup (2,500 credits), at most 3 per scan, cached for 30 days."""
    cache = s.setdefault("followed", {})
    board = {_norm(t["handle"]): t for t in (s.get("leaderboard") or {}).get("traders") or []}
    overrides = dict(x.split(":", 1) for x in config.FOLLOW_SOLANA_WALLETS.split(",") if ":" in x)
    looked = 0
    for h in config.FOLLOW_TRADERS:
        n = _norm(h)
        c = cache.get(n)
        if n in board and (board[n].get("wallet") or board[n].get("evm")):
            cache[n] = {"handle": h, "wallet": board[n].get("wallet"), "evm": board[n].get("evm"), "ts": time.time()}
            continue
        if c and time.time() - c["ts"] < 30 * 86400 and (c.get("wallet") or c.get("evm")):
            continue
        cw = ((s.get("copy") or {}).get("wallets") or {}) if n == _norm((s.get("copy") or {}).get("handle")) else {}
        if cw.get("solana") or cw.get("evm"):
            cache[n] = {"handle": h, "wallet": cw.get("solana"), "evm": cw.get("evm"), "ts": time.time()}
            continue
        if looked >= 3 or (c and time.time() - c.get("tried", 0) < 6 * 3600):
            continue
        looked += 1
        d = _get(s, f"/v2/users/{h.lstrip('@')}", 2500)
        u = ((d or {}).get("user") or d or {}) if isinstance(d, dict) else {}
        w = u.get("wallets") or {}
        if w.get("solana") or w.get("evm"):
            cache[n] = {"handle": u.get("handle") or h, "wallet": w.get("solana"),
                        "evm": (w.get("evm") or "").lower() or None, "ts": time.time()}
            log.info("followed trader %s: wallets found", h)
        else:
            cache[n] = {**(c or {}), "handle": h, "tried": time.time(), "ts": (c or {}).get("ts", 0)}
            log.info("followed trader %s: couldn't find their wallets yet", h)
    for h, addr in overrides.items():
        n = _norm(h)
        cache[n] = {**cache.get(n, {}), "handle": cache.get(n, {}).get("handle", h), "wallet": addr.strip(),
                    "ts": time.time()}
    keep = {_norm(h) for h in config.FOLLOW_TRADERS} | {_norm(h) for h in overrides}
    s["followed"] = {n: c for n, c in cache.items() if n in keep}
    return s["followed"]


def tracked(s):
    """Everyone whose wallets we watch: the top 100 plus the traders you follow.

    Followed traders who are also on the board keep their real rank; the rest get rank 101
    (shown as "followed" in emails, weighted like a #FOLLOW_RANK trader)."""
    lb = leaderboard(s)
    fol = followed(s)
    on_board = {_norm(t["handle"]) for t in lb}
    out = [dict(t, followed=_norm(t["handle"]) in fol) for t in lb]
    seen = {t.get("wallet") for t in lb} | {t.get("evm") for t in lb}
    for n, c in fol.items():
        if n not in on_board and (c.get("wallet") or c.get("evm")) and not ({c.get("wallet"), c.get("evm")} & seen - {None}):
            out.append({"rank": OFF_BOARD, "handle": c["handle"], "pnlUsd": 0, "wallet": c.get("wallet"),
                        "evm": c.get("evm"), "followed": True})
    return out


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
