"""Chart check from GeckoTerminal hourly candles (free, keyless, ~10 calls/min).

Looks at the last 7 days the way a trader eyeballs a chart:
trend (EMA20/EMA50), higher lows day over day, where price sits vs the 7-day high,
volume trend, buy vs sell candle volume, rejection wicks, and whether it's overextended.
"""
import logging
import time

from . import chains, config
from .http import request

log = logging.getLogger("chart")
_last_call = [0.0]


def _gt(path, params=None):
    wait = config.GT_SLEEP_SEC - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    r = request("GET", config.GT_BASE + path, params=params,
                headers={"accept": "application/json;version=20230302"})
    return r.json() if r is not None else None


def candles(chain, pool):
    d = _gt(f"/networks/{chains.CHAIN[chain]['gt']}/pools/{pool}/ohlcv/hour",
            {"aggregate": 1, "limit": 168, "currency": "usd"})
    try:
        rows = d["data"]["attributes"]["ohlcv_list"]
    except (TypeError, KeyError):
        return None
    rows = sorted([[float(x) for x in r[:6]] for r in rows], key=lambda r: r[0])
    return rows or None


def token_info(chain, addr):
    d = _gt(f"/networks/{chains.CHAIN[chain]['gt']}/tokens/{addr}/info")
    try:
        a = d["data"]["attributes"]
    except (TypeError, KeyError):
        return {}
    h = a.get("holders") or {}
    dist = h.get("distribution_percentage") or {}
    try:
        top10 = float(dist.get("top_10")) if dist.get("top_10") is not None else None
    except (TypeError, ValueError):
        top10 = None
    return {"holders": h.get("count"), "top10_pct": top10, "gt_score": a.get("gt_score"),
            "telegram": a.get("telegram_handle"), "twitter": a.get("twitter_handle")}


def ema(vals, n):
    k, out = 2 / (n + 1), []
    for v in vals:
        out.append(v if not out else v * k + out[-1] * (1 - k))
    return out


def analyze(rows):
    """Return {'points': -12..20, 'reasons': [...], 'flags': [...], 'verdict': str}."""
    if not rows or len(rows) < 12:
        return None
    o, h, l, c, v = ([r[i] for r in rows] for i in (1, 2, 3, 4, 5))
    price = c[-1]
    e20, e50 = ema(c, 20), ema(c, min(50, len(c)))
    pts, reasons, flags, verdict = 0, [], [], []

    up = price > e20[-1] > e50[-1] and e20[-1] > e20[-7 if len(e20) >= 7 else 0]
    if up:
        pts += 6
        verdict.append("uptrend")
        reasons.append("chart: price above rising 20h and 50h averages")
    elif price > e20[-1]:
        pts += 3
        verdict.append("recovering")
    elif price < e50[-1] and e20[-1] < e50[-1]:
        pts -= 4
        verdict.append("downtrend")
        flags.append("chart in downtrend (below 20h and 50h averages)")

    # higher lows over the last 3 days
    if len(l) >= 72:
        d1, d2, d3 = min(l[-72:-48]), min(l[-48:-24]), min(l[-24:])
        if d1 < d2 < d3:
            pts += 4
            verdict.append("higher lows 3 days running")
            reasons.append("chart: higher lows 3 days in a row (buyers stepping in higher)")
        elif d2 < d3:
            pts += 2
            verdict.append("higher low today")
    if len(rows) >= 72:
        pts += 2
        reasons.append(f"survived {len(rows) // 24}+ days of trading (not a fresh launch)")

    hi7 = max(h)
    dd = 1 - price / hi7 if hi7 else 0
    vol24, vol_prev = sum(v[-24:]), sum(v[-96:-24]) / 3 if len(v) >= 96 else None
    vol_up = vol_prev and vol24 >= 1.2 * vol_prev
    if vol_up:
        pts += 3
        reasons.append(f"chart: 24h volume {vol24 / vol_prev:.1f}x the prior 3-day average")
    if up and 0.12 <= dd <= 0.45:
        pts += 3
        reasons.append(f"chart: healthy pullback, {dd * 100:.0f}% off 7-day high while trend holds")
    elif dd < 0.1 and vol_up:
        pts += 2
        reasons.append("chart: pushing 7-day highs on rising volume")
    if dd > 0.6:
        pts -= 8
        flags.append(f"down {dd * 100:.0f}% from its 7-day high")

    upv = sum(v[i] for i in range(len(c) - 24, len(c)) if i >= 0 and c[i] >= o[i])
    dnv = sum(v[i] for i in range(len(c) - 24, len(c)) if i >= 0 and c[i] < o[i])
    if dnv and upv / dnv >= 1.3:
        pts += 2
        reasons.append(f"chart: green-candle volume {upv / dnv:.1f}x red over 24h")
    elif upv and dnv / upv >= 1.5:
        pts -= 2
        flags.append("more volume on red candles than green (distribution)")

    wicks = [(h[i] - max(o[i], c[i])) / (h[i] - l[i]) for i in range(-6, 0) if h[i] > l[i]]
    if wicks and sum(wicks) / len(wicks) > 0.5:
        pts -= 2
        flags.append("long upper wicks - sellers hitting every pump")

    gain24 = price / c[-25] - 1 if len(c) >= 25 and c[-25] else 0
    if price / e50[-1] > 2.5 or gain24 > 2:
        pts -= 4
        flags.append(f"overextended (+{gain24 * 100:.0f}% in 24h) - likely to pull back first")

    return {"points": max(-12, min(20, pts)), "reasons": reasons, "flags": flags,
            "verdict": ", ".join(verdict) or "sideways", "dd": dd, "days": len(rows) / 24,
            "support": min(l[-24:]), "high7": hi7,
            "closes": [[r[0], r[4]] for r in rows[-48:]]}
