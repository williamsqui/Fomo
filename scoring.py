"""Confidence score (0-100): how likely a coin is to reach +50% (the target).

Rule-based and transparent, written as "would I put my own money in this?".
The track record measures how often each score band actually hits +50%, so
you can tune the weights below using real results.

Components (max points):
  smart money   30  top-100 FOMO traders buying (rank-weighted), minus sellers
  chart         20  trend, higher lows, pullback vs 7d high, volume, wicks
  momentum      10  1h buy/sell pressure, volume acceleration
  setup/safety  15  market cap, liquidity depth, rug checks, holder spread
  social        20  X (14) + Telegram, website, holder growth (6)
  community      5  FOMO trending, FOMO community posts
Severe red flags multiply the score down. Hard fails are never recommended.
"""
import math
import time

from . import config

MAX = {"smart": 30, "chart": 20, "momentum": 10, "setup": 15, "social": 20, "community": 5}
SEVERE = ("chasing", "botted", "sold:", "downtrend", "from its 7-day high", "overextended", "distribution")


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def smart_money(events, key):
    since = time.time() - config.LOOKBACK_HOURS * 3600
    ev = [e for e in events if e["key"] == key and e["ts"] >= since]
    buyers, sellers = {}, {}
    for e in ev:
        (buyers if e["side"] == "buy" else sellers).setdefault(e["handle"], []).append(e)
    usd = lambda xs: sum(x.get("usd") or 0 for x in xs)
    amt = lambda xs: sum(x.get("amount") or 0 for x in xs)
    # sold >= 80% of what they bought in the window (or sold with no buy) = seller
    net_sellers = {h for h in sellers if amt(sellers[h]) >= 0.8 * amt(buyers.get(h, []))}
    active = {h: v for h, v in buyers.items() if h not in net_sellers}
    return {
        "buyers": sorted(active, key=lambda h: active[h][0]["rank"]),
        "buyer_ranks": {h: v[0]["rank"] for h, v in active.items()},
        "sellers": sorted(net_sellers),
        "buy_usd": round(sum(usd(v) for v in active.values())),
        # "held when first seen" snapshots have no real buy time, so they don't set these
        "first_buy": min((x["ts"] for v in active.values() for x in v if not x.get("held")), default=None),
        "last_buy": max((x["ts"] for v in active.values() for x in v if not x.get("held")), default=None),
        "weight": sum(1 + (101 - v[0]["rank"]) / 100 for v in active.values()),
    }


def eligible(m):
    """Cheap pre-filter on DexScreener data: 'is this even investable?'"""
    if not m or m["price"] <= 0:
        return False
    age_h = (time.time() * 1000 - m["created_ms"]) / 3.6e6 if m["created_ms"] else 999
    return (config.MIN_MCAP_USD <= m["mcap"] <= config.MAX_MCAP_USD
            and m["liquidity"] >= config.MIN_LIQUIDITY_USD
            and m["liquidity"] / m["mcap"] >= config.MIN_LIQ_TO_MCAP
            and age_h >= config.MIN_PAIR_AGE_HOURS)


def score(m, sm, *, x=None, thesis=None, trending_rank=None, chart=None, safety=None,
          info=None, tg=None, holder_growth=None, deep=False):
    pts, reasons, flags, hard = {}, [], [], []

    # ---- smart money -------------------------------------------------------
    sp = MAX["smart"] * (1 - math.exp(-0.5 * sm["weight"]))
    sp += 2 if sm["buy_usd"] >= 5_000 else 0
    sp -= 7 * len(sm["sellers"])
    pts["smart"] = clamp(sp, -10, MAX["smart"])
    if sm["buyers"]:
        who = ", ".join(f"{h} (#{sm['buyer_ranks'][h]})" for h in sm["buyers"][:4])
        mins = int((time.time() - sm["first_buy"]) / 60) if sm["first_buy"] else 0
        reasons.append(f"{len(sm['buyers'])} top-100 FOMO trader(s) bought or hold it: {who}"
                       + (f" (~${sm['buy_usd']:,}" + (f", first buy {mins} min ago)" if sm["first_buy"] else ")")
                          if sm["buy_usd"] else ""))
    if sm["sellers"]:
        flags.append(f"leaderboard trader(s) sold: {', '.join(sm['sellers'][:3])}")

    # ---- chart -------------------------------------------------------------
    if chart:
        pts["chart"] = chart["points"]
        reasons += chart["reasons"]
        flags += chart["flags"]
    else:
        pts["chart"] = 0
        if deep and (safety is None or safety["ok"]):
            hard.append("couldn't load the chart - not buying blind")

    # ---- momentum ----------------------------------------------------------
    ch1 = m["change"].get("h1", 0)
    ratio = m["buys_h1"] / (m["sells_h1"] + 1)
    accel = m["volume"].get("h1", 0) / (m["volume"].get("h6", 0) / 6 + 1)
    mp = clamp((ratio - 1) * 5, 0, 4) + clamp((accel - 1) * 2.5, 0, 3)
    if 5 <= ch1 <= 40:
        mp += 3
    elif ch1 > 100:
        mp -= 3
        flags.append(f"already +{ch1:.0f}% this hour (chasing risk)")
    elif ch1 < -15:
        mp -= 3
    pts["momentum"] = clamp(mp, -5, MAX["momentum"])
    if ratio >= 1.3:
        reasons.append(f"buy pressure: {m['buys_h1']} buys vs {m['sells_h1']} sells in the last hour")

    # ---- setup / safety ----------------------------------------------------
    stp = 5 if m["mcap"] <= 20_000_000 else 3
    liq_ratio = m["liquidity"] / m["mcap"] if m["mcap"] else 0
    stp += 3 if liq_ratio >= 0.08 else 2 if liq_ratio >= 0.05 else 0
    stp += 2 if m["liquidity"] >= 150_000 else 0
    if safety:
        hard += safety["hard"]
        flags += safety["flags"]
        if safety["verified"] and safety["ok"]:
            stp += 3 if any("locked" in g for g in safety["good"]) else 2
            reasons += safety["good"][:2]
    top10 = (info or {}).get("top10_pct") or (safety or {}).get("top10")
    if top10 is not None:
        if top10 > config.MAX_TOP10_HOLDERS_PCT:
            hard.append(f"top 10 wallets hold {top10:.0f}% (dump risk)")
        elif top10 <= 25:
            stp += 2
            reasons.append(f"well spread: top 10 wallets hold {top10:.0f}%")
    pts["setup"] = clamp(stp, -5, MAX["setup"])

    # ---- social ------------------------------------------------------------
    have_x = x is not None
    so = 0
    if have_x:
        so += min(5, 2 * math.log2(1 + x["unique_authors"]))
        so += min(4, 1.5 * len(x["kol_authors"]))
        so += min(3, 3 * len(x["leaderboard_authors"]))
        so += min(2, 0.7 * math.log10(1 + x["engagement"]))
        if x["bot_ratio"] > 0.5 and x["tweets_6h"] >= 5:
            so -= 5
            flags.append(f"X chatter looks botted ({int(x['bot_ratio'] * 100)}% spam/tiny accounts)")
        if x["tweets_6h"]:
            reasons.append(f"X: {x['tweets_6h']} posts from {x['unique_authors']} accounts in 6h")
        if x["kol_authors"]:
            reasons.append("X accounts with 10k+ followers posting: " + ", ".join("@" + k for k in x["kol_authors"][:3]))
        if x["leaderboard_authors"]:
            reasons.append("leaderboard traders posting on X: " + ", ".join(x["leaderboard_authors"]))
    if tg:
        so += 3 if tg >= 5000 else 2 if tg >= 1000 else 0
        if tg >= 1000:
            reasons.append(f"Telegram community: {tg:,} members")
    if m.get("website"):
        so += 1
    if holder_growth is not None:
        if holder_growth >= 5:
            so += 2
            hc = (info or {}).get("holders")
            reasons.append(f"holders up {holder_growth:.0f}% in 24h" + (f" ({hc:,} total)" if isinstance(hc, int) else ""))
        elif holder_growth < -3:
            flags.append(f"holders shrinking ({holder_growth:.0f}% in 24h)")
    if not m.get("twitter") and not m.get("telegram"):
        flags.append("no X or Telegram linked")
    if m.get("boosts", 0) >= 500:
        flags.append("heavy paid DexScreener promotion")
    pts["social"] = clamp(so, -5, MAX["social"])

    # ---- FOMO community ----------------------------------------------------
    cp = 0
    if trending_rank:
        cp += 2 if trending_rank <= 20 else 1
        reasons.append(f"trending on FOMO (#{trending_rank})")
    if thesis:
        likes = sum(t.get("likes", 0) for t in thesis)
        cp += min(3, len(thesis) / 5 + likes / 100)
        reasons.append(f"{len(thesis)} FOMO community posts ({likes} likes)")
        if any(t.get("isDev") for t in thesis):
            flags.append("the dev is shilling on FOMO")
    pts["community"] = clamp(cp, 0, MAX["community"])

    avail = sum(MAX.values()) - (0 if have_x else 14)
    raw = 100 * sum(pts.values()) / avail
    severe = sum(any(k in f for k in SEVERE) for f in flags)
    unverified = 0.93 if safety and not safety["verified"] else 1.0  # I don't trust what I can't check
    total = int(clamp(round(raw * 0.88 ** severe * unverified), 0, 100))

    # ---- would I actually buy it? -----------------------------------------
    real_demand = bool(sm["buyers"]) or (have_x and x["kol_authors"] and chart and "uptrend" in chart["verdict"])
    need = config.MIN_SEND_SCORE + (8 if safety and not safety["verified"] else 0)
    why_not = []
    if hard:
        why_not.append("failed safety: " + "; ".join(hard))
    if deep and not real_demand:
        why_not.append("no smart-money buying or credible buzz")
    if total < need:
        why_not.append(f"score {total} below bar {need}")
    tier = "HIGH" if total >= 75 else "MEDIUM" if total >= 62 else "LOW"
    return {"key": m["key"], "score": total, "tier": tier, "points": {k: round(v, 1) for k, v in pts.items()},
            "reasons": reasons, "flags": flags, "hard": hard, "market": m, "smart": sm, "x": x,
            "chart": chart, "safety": safety, "qualified": deep and not why_not, "why_not": why_not,
            "x_checked": have_x}
