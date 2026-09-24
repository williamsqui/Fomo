"""Which top-100 traders hold a coin right now - the one source of truth for scoring.

Solana: every leaderboard wallet's full holdings are read each scan (solana.scan_wallets),
so for any coin we know exactly who holds it, however long ago they bought.
Base/BNB/Robinhood: balances of every leaderboard wallet are read for candidate coins
(evm.holdings_scan) and kept as a per-coin snapshot.

Why this exists: the 10-minute scan used to count only buys from the last 6 hours and only
knew about holders in a coin's top-20 list. A whale who bought yesterday and trimmed 10% today
scored as a SELLER, while "Check my position" (which reads every wallet live) correctly saw a
holder - the same coin at the same moment got opposite readings.
"""
import time

from . import chains, config, solana
from .evm import MIN_EVENT_USD, holding_events

EVM_MAX_AGE_MIN = 120


def context(s, traders):
    by_sol = {t["wallet"]: t for t in traders if t.get("wallet")}
    by_evm = {t["evm"]: t for t in traders if t.get("evm")}
    return {"by_sol": by_sol, "by_evm": by_evm, "sol": solana.holder_index(s, by_sol)}


def held_events(s, ctx, k, price):
    """'held' events (not stored) for every top trader holding k with a real position now."""
    if not price or price <= 0:
        return []
    if not k.startswith("solana:"):
        out = holding_events(s, k, price, ctx["by_evm"], max_age_min=EVM_MAX_AGE_MIN)
    else:
        now, out = time.time(), []
        for w, amt in (ctx["sol"].get(k) or {}).items():
            t = ctx["by_sol"].get(w)
            if t and amt * price >= MIN_EVENT_USD:
                out.append({"ts": now, "wallet": w, "handle": t["handle"], "rank": t["rank"], "key": k,
                            "side": "buy", "amount": amt, "usd": round(amt * price, 2), "held": True})
    # a bag bought long ago (or before we started watching) is weaker evidence than a position
    # opened in the last 48h - it may be a forgotten bag, not conviction
    fresh = fresh_buyers(s, k)
    for e in out:
        e["stale"] = e["handle"] not in fresh
    return out


def fresh_buyers(s, k):
    """Handles with a logged buy of k in the last 48h (the state keeps 48h of trades)."""
    return {e["handle"] for e in s.get("trader_buys") or [] if e["key"] == k and e["side"] == "buy"
            and not e.get("held")}


def wallets(s, ctx, k, price):
    return [e["wallet"] for e in held_events(s, ctx, k, price)]


def still_holding(s, k, wallet, after=None):
    """Token amount this wallet holds now, 0.0 if it holds none, or None if we can't tell.

    after: only trust a read taken at/after this time (a snapshot older than a sell can't
    tell us whether that sell emptied the wallet)."""
    now = time.time()
    if k.startswith("solana:"):
        sn = (s.get("wallet_snap") or {}).get(wallet)
        if not sn or now - sn["ts"] > config.HOLDINGS_MAX_AGE_MIN * 60 or (after and sn["ts"] < after):
            return None
        return sn["bal"].get(chains.split(k)[1], 0.0)
    h = (s.get("holdings") or {}).get(k)
    if not h or now - h["ts"] > EVM_MAX_AGE_MIN * 60 or (after and h["ts"] < after):
        return None
    return h["bal"].get(wallet, 0.0)


def drop_exited(s, k, events):
    """Remove logged buys of traders a fresh read (taken after the buy) shows hold nothing now."""
    out = []
    for e in events:
        if e["side"] == "buy" and not e.get("held") and e.get("wallet"):
            if still_holding(s, k, e["wallet"], after=e["ts"]) == 0.0:
                continue
        out.append(e)
    return out


def sold_out(s, e):
    """Did this sell event take the trader (nearly) all the way out? Trims are not exits."""
    if "out" in e:                               # Solana holdings diff knows exactly
        if not e["out"]:
            return False
    now_amt = still_holding(s, e["key"], e["wallet"], after=e["ts"])
    if now_amt is None:
        return e.get("out", True)                # can't check: trust the event
    return now_amt <= (e.get("amount") or 0) * 0.25   # what's left is small next to what they sold


def candidate_keys(s, ctx, now=None):
    """Coins to keep scoring because top traders are IN them - not only while a buy is <6h old.

    * any coin 3+ top traders hold (accumulation, even if nobody bought in the last 6h)
    * any coin a current holder bought in the last 48h
    Coins DexScreener can't list (spam airdrops) are skipped for 6-24h.
    """
    now = now or time.time()
    skip = s.setdefault("dex_skip", {})          # key -> skip until
    for k in [k for k, until in skip.items() if until <= now]:
        skip.pop(k)
    bought = {(e["key"], e["wallet"]) for e in s["trader_buys"] if e["side"] == "buy"}
    out = {}
    for k, ws in ctx["sol"].items():
        if k in skip:
            continue
        if len(ws) >= config.HELD_CANDIDATE_MIN or any((k, w) in bought for w in ws):
            out[k] = len(ws)
    for k, h in (s.get("holdings") or {}).items():          # EVM coins seen before
        if h.get("bal") and k not in skip and now - h["ts"] <= 48 * 3600:
            out[k] = max(out.get(k, 0), len(h["bal"]))
    return [k for k, _ in sorted(out.items(), key=lambda x: -x[1])][:config.HELD_CANDIDATES_MAX]
