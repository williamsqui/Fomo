"""Early lane (HIGH RISK): coins fresh off a launchpad, caught before the big move.

Why a separate lane: the main scanner looks for $500k+ coins that top traders are buying, and
it refuses to chase. That's exactly why it never saw $NPC - a community pump that graduated from
pump.fun at ~$70k and went 10x+ with one top trader in it. Catching those means looking at tiny,
new coins, where most go to zero. So this lane has its own rules, its own small fixed size, its
own exits, and its own paper-trading record that says whether it actually makes money.

Every scan:
  1. discover  - new coins from FOMO's Graduated board, DexScreener's newest paid profiles and
                 community takeovers, GeckoTerminal new + trending pools, FOMO trending, and any
                 launchpad coin a tracked trader just bought. Only pump.fun / Bonk.fun / Bags mints.
  2. watch     - every discovered coin is re-priced each scan for up to 48h (the NPC pump started
                 ~12h after it graduated), dead ones are dropped.
  3. filter    - $40k-$1.5M market cap, $15k+ liquidity, 20 min - 72h old, not already +250%/1h.
  4. score     - momentum 35 / traction 20 / community 20 / tracked traders holding 25.
  5. safety    - RugCheck on the best few: mint/freeze authority or any danger risk = reject,
                 liquidity must be burned/locked (pump.fun burns it automatically).
  6. act       - 60+ -> HIGH RISK alert email (max 6/day; run Check my position if you buy);
                 50+ -> paper trade, so the record shows which score bar really pays.
Exits (paper and advice): sell half at +100%, the rest when it falls 30% from its high,
stop -35%, done after 24h. Fees ($0.95 min each way) and 2% slippage each way included.
"""
import html
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import chains, chart, config, dex, fomo, safety
from .sizing import fmt

log = logging.getLogger("early")
e = html.escape
NAMES = {"pump": "pump.fun", "bonk": "Bonk.fun", "BAGS": "Bags"}


def launchpad(key):
    chain, addr = chains.split(key)
    if chain != "solana":
        return None
    for suf in config.EARLY_LAUNCHPADS:
        if addr.endswith(suf):
            return NAMES.get(suf, suf)
    return None


def _book(s):
    b = s.setdefault("early", {})
    for k, v in (("seen", {}), ("alerts", {}), ("trades", []), ("deep", {}), ("day", ""), ("sent", 0)):
        b.setdefault(k, v)
    return b


# ------------------------------------------------------------------ 1. discover
def _dex_list(path):
    r = dex.request("GET", f"{config.DEX_BASE}{path}")
    data = r.json() if r is not None else None
    return [x for x in (data if isinstance(data, list) else [])
            if x.get("chainId") == "solana" and x.get("tokenAddress")]


def _gt_new_pools():
    d = chart._gt("/networks/solana/new_pools", {"page": 1})
    out = []
    for pool in (d or {}).get("data") or []:
        tid = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        if "_" in tid:
            out.append(tid.split("_", 1)[1])
    return out


def discover(s, ctx=None, now=None):
    """Add newly found launchpad coins to the watch list. Returns {key: [sources]} found this scan."""
    now = now or time.time()
    b = _book(s)
    found = {}

    def add(addr_or_key, src):
        k = addr_or_key if ":" in addr_or_key else chains.key("solana", addr_or_key)
        if launchpad(k):
            found.setdefault(k, []).append(src)

    profiles = {x["tokenAddress"] for x in _dex_list("/token-profiles/latest/v1")}
    for a in profiles:
        add(a, "DexScreener profile")
    for x in _dex_list("/community-takeovers/latest/v1"):
        add(x["tokenAddress"], "community takeover")
    for a in _gt_new_pools():
        add(a, "new pool")
    for k in chart.trending_tokens(s, "solana"):
        add(k, "GeckoTerminal trending")
    for t in fomo.graduated(s):
        add(t["key"], "FOMO graduated")
    for t in (s.get("trending") or {}).get("tokens") or []:
        if "key" in t:
            add(t["key"], "FOMO trending")
    day = now - 86400                                  # launchpad coins a tracked trader just bought
    for ev in s.get("trader_buys") or []:
        if ev["side"] == "buy" and ev["ts"] >= day and not ev.get("held"):
            add(ev["key"], "tracked trader bought")

    for k, srcs in found.items():
        it = b["seen"].setdefault(k, {"first": now, "src": [], "lp": launchpad(k)})
        it["src"] = sorted(set(it["src"]) | set(srcs))
        if k.split(":", 1)[1] in profiles:
            it["profile"] = True
    # forget old / excess entries (oldest first), but never ones with an open paper trade
    keep_open = {t["key"] for t in b["trades"] if not t.get("done")}
    cutoff = now - config.EARLY_WATCH_HOURS * 3600
    b["seen"] = {k: v for k, v in b["seen"].items() if v["first"] >= cutoff or k in keep_open}
    if len(b["seen"]) > config.EARLY_MAX_WATCH:
        order = sorted(b["seen"], key=lambda k: (k not in keep_open, -b["seen"][k]["first"]))
        b["seen"] = {k: b["seen"][k] for k in order[:config.EARLY_MAX_WATCH]}
    return found


# ------------------------------------------------------------------ 3-4. filter + score
def age_min(m):
    return (time.time() * 1000 - m["created_ms"]) / 60000 if m.get("created_ms") else None


def in_range(m):
    """Early-lane pre-filter (cheap, DexScreener data only)."""
    if not m or m["price"] <= 0 or not m["mcap"]:
        return False
    a = age_min(m)
    return (config.EARLY_MIN_MCAP <= m["mcap"] <= config.EARLY_MAX_MCAP
            and m["liquidity"] >= config.EARLY_MIN_LIQ
            and m["liquidity"] / m["mcap"] >= 0.05
            and a is not None and config.EARLY_MIN_AGE_MIN <= a <= config.EARLY_MAX_AGE_H * 60)


def score(m, seen=None, holders=None, info=None, sf=None, grad_rank=None, fomo_trending=False):
    """0-100 for a small new coin. holders = [(handle, trust)] of tracked traders holding it."""
    seen, holders, info = seen or {}, holders or [], info or {}
    pts, why, flags, hard = {}, [], [], []
    ch = m.get("change") or {}
    h1, m5 = ch.get("h1", 0) or 0, ch.get("m5", 0) or 0
    ratio = m["buys_h1"] / (m["sells_h1"] + 1)
    vol = m.get("volume") or {}

    # momentum (35)
    mo = 0
    if 15 <= h1 <= 150:
        mo += 14
    elif 5 <= h1 < 15:
        mo += 7
    elif 150 < h1 <= config.EARLY_MAX_H1:
        mo += 6
        flags.append(f"already +{h1:.0f}% this hour - late entry, expect a pullback")
    elif h1 > config.EARLY_MAX_H1:
        mo -= 10
        flags.append(f"already +{h1:.0f}% this hour (chasing)")
    elif h1 < -20:
        mo -= 10
        flags.append(f"dumping: {h1:.0f}% in 1h")
    mo += 4 if m5 > 0 else 0
    mo += max(0.0, min(9.0, (ratio - 1) * 10))
    accel = vol.get("h1", 0) / (vol.get("h6", 0) / 6 + 1)
    mo += max(0.0, min(5.0, (accel - 1) * 3))
    if m["liquidity"] and vol.get("h1", 0) / m["liquidity"] >= 1:
        mo += 3
    if m["sells_h1"] > m["buys_h1"] * 1.3:
        mo -= 6
        flags.append(f"more sellers than buyers: {m['buys_h1']} buys vs {m['sells_h1']} sells in 1h")
    elif ratio >= 1.2:
        why.append(f"buyers in control: {m['buys_h1']} buys vs {m['sells_h1']} sells in 1h ({h1:+.0f}%)")
    pts["momentum"] = max(-10.0, min(35.0, mo))

    # traction (20)
    tr = 0
    mc0 = seen.get("mcap0")
    if mc0:
        g = m["mcap"] / mc0 - 1
        if g >= 0.5:
            tr += 6
            why.append(f"market cap up {g * 100:.0f}% since first spotted")
        elif g >= 0.2:
            tr += 3
        elif g < -0.3:
            tr -= 5
    hc = info.get("holders")
    if isinstance(hc, int):
        tr += 8 if hc >= 2000 else 6 if hc >= 800 else 3 if hc >= 300 else 0
        h0 = seen.get("holders0")
        if h0 and hc >= h0 * 1.2:
            tr += 6
            why.append(f"holders growing: {h0:,} -> {hc:,}")
        elif hc >= 300:
            why.append(f"{hc:,} holders")
    top10 = info.get("top10_pct")
    if top10 is not None and top10 > 40:
        hard.append(f"top 10 wallets hold {top10:.0f}%")
    pts["traction"] = max(-5.0, min(20.0, tr))

    # community (20)
    co = 0
    if m.get("twitter"):
        co += 4
    if m.get("telegram"):
        co += 2
    if m.get("website"):
        co += 2
    if not (m.get("twitter") or m.get("telegram") or m.get("website")):
        flags.append("no socials at all")
    if seen.get("profile"):
        co += 4
        why.append("dev paid for a DexScreener profile")
    if grad_rank:
        co += 6 if grad_rank <= 10 else 4 if grad_rank <= 30 else 2
        why.append(f"on FOMO's Graduated board (#{grad_rank})")
    if fomo_trending:
        co += 2
        why.append("trending on FOMO")
    pts["community"] = min(20.0, co)

    # tracked traders holding it (25)
    w = sum(t for _, t in holders)
    pts["smart"] = 0.0 if not holders else 12.0 if w < 1.5 else 20.0 if w < 2.5 else 25.0
    if holders:
        why.insert(0, f"{len(holders)} top-100/followed trader(s) hold it: " + ", ".join(h for h, _ in holders[:4]))

    # structure
    if m["mcap"] and m["liquidity"] / m["mcap"] < 0.06:
        pts["momentum"] -= 4
        flags.append("thin liquidity for its size")
    a = age_min(m)
    if a is not None and a < 30:
        flags.append(f"only {a:.0f} min since graduating - snipers still selling")

    # safety
    if sf:
        hard += sf.get("hard") or []
        flags += [f for f in sf.get("flags") or [] if "liquidity locked" not in f]
        lp = sf.get("lp_locked")
        if not sf.get("verified"):
            hard.append("RugCheck unavailable - never trade a fresh coin without a safety check")
        elif lp is not None and lp < 80 and m["key"].endswith("pump"):
            # pump.fun burns the pool's LP at graduation, so anything less is abnormal
            hard.append(f"only {lp:.0f}% of liquidity burned/locked - the dev could pull it")
        elif lp is not None and lp < 50:
            # (Bonk.fun / Bags pools lock liquidity differently; RugCheck may under-read them)
            flags.append(f"RugCheck sees only {lp:.0f}% of liquidity locked - check before buying")

    total = int(max(0, min(100, round(sum(pts.values())))))
    chasing = h1 > config.EARLY_MAX_H1 or m5 > 60
    return {"key": m["key"], "score": total, "points": {k: round(v, 1) for k, v in pts.items()},
            "reasons": why, "flags": flags, "hard": hard, "market": m, "chasing": chasing,
            "launchpad": launchpad(m["key"]), "holders": [h for h, _ in holders],
            "checked": sf is not None}


# ------------------------------------------------------------------ the scan
def scan(s, ctx, trust, now=None):
    """Discover, re-price, score. Returns (alert-worthy results, paper-worthy results, markets)."""
    now = now or time.time()
    b = _book(s)
    discover(s, ctx, now)
    open_keys = [t["key"] for t in b["trades"] if not t.get("done")]
    keys = list(dict.fromkeys(list(b["seen"]) + open_keys))
    markets = dex.tokens(keys) if keys else {}
    failed = set(dex.FAILED)
    grad = {t["key"]: t["rank"] for t in (s.get("graduated") or {}).get("tokens") or []}
    trending = {t["key"] for t in (s.get("trending") or {}).get("tokens") or [] if "key" in t}

    quick = []
    for k in list(b["seen"]):
        it, m = b["seen"][k], markets.get(k)
        if not m:
            if k not in failed and now - it["first"] > 3600:
                b["seen"].pop(k)                          # never listed / delisted
            continue
        it.setdefault("mcap0", m["mcap"])
        if (m["liquidity"] < 5_000 or m["mcap"] < 20_000) and now - it["first"] > 3600:
            b["seen"].pop(k)                              # dead
            continue
        if not in_range(m):
            continue
        hold = _holders(s, ctx, k, m["price"], trust)
        r = score(m, it, hold, (b["deep"].get(k) or {}).get("info"), None, grad.get(k), k in trending)
        quick.append(r)

    # deep checks (RugCheck + holder count) for the best few, cached 60 / 30 min
    quick.sort(key=lambda r: -r["score"])
    out = []
    for i, r in enumerate(quick):
        k, m = r["key"], r["market"]
        d = b["deep"].setdefault(k, {})
        if i < config.EARLY_FINALISTS and r["score"] >= config.EARLY_PAPER_SCORE - 15 and not r["chasing"]:
            if now - d.get("sf_ts", 0) > 3600:
                sf = safety.check("solana", chains.split(k)[1])
                if sf.get("verified"):             # RugCheck down = not checked (retry next scan)
                    d["sf"], d["sf_ts"] = sf, now
            if now - d.get("info_ts", 0) > 1800:
                info = chart.token_info("solana", chains.split(k)[1])
                if info:
                    d["info"], d["info_ts"] = info, now
                    if info.get("holders") is not None and not b["seen"][k].get("holders0"):
                        b["seen"][k]["holders0"] = info["holders"]
        if "sf" not in d:
            continue                                      # never judged without a safety check
        hold = _holders(s, ctx, k, m["price"], trust)
        full = score(m, b["seen"][k], hold, d.get("info"), d["sf"], grad.get(k), k in trending)
        out.append(full)
    b["deep"] = {k: v for k, v in b["deep"].items() if k in b["seen"]}
    good = [r for r in out if not r["hard"] and not r["chasing"]]
    alerts = [r for r in good if r["score"] >= config.EARLY_ALERT_SCORE]
    papers = [r for r in good if r["score"] >= config.EARLY_PAPER_SCORE]
    log.info("early lane: watching %d coins, %d in range, %d fully checked, %d paper, %d alert",
             len(b["seen"]), len(quick), len(out), len(papers), len(alerts))
    for r in sorted(out, key=lambda r: -r["score"])[:5]:
        log.info("  early %3d %-10s %s", r["score"], r["market"]["symbol"],
                 "; ".join(r["hard"] or r["flags"][:2]) or "ok")
    return alerts, papers, markets


def _holders(s, ctx, k, price, trust):
    """[(handle, weight)] of tracked traders holding k now; an old bag counts half, like the main lane."""
    if not ctx:
        return []
    from .holders import held_events
    return [(x["handle"], trust(x["handle"]) * (0.5 if x.get("stale") else 1.0))
            for x in held_events(s, ctx, k, price)]


# ------------------------------------------------------------------ paper trades
def _fee(x):
    return max(config.FEE_MIN_USD, x * config.FEE_PCT / 100)


def pnl(size, entry, parts):
    """$ result: one buy, then partial sells [(fraction, price)], fees + slippage each way."""
    slip = config.EARLY_SLIPPAGE_PCT / 100
    tokens = (size - _fee(size)) / (entry * (1 + slip))
    got = 0.0
    for frac, px in parts:
        gross = tokens * frac * px * (1 - slip)
        got += gross - _fee(gross) if gross > 0 else 0.0     # nothing to sell after a rug
    return round(got - size, 2)


def open_paper(s, r, now=None):
    b = _book(s)
    k = r["key"]
    if any(t["key"] == k and not t.get("done") for t in b["trades"]):
        return None
    if any(t["key"] == k and (now or time.time()) - t["ts"] < 86400 for t in b["trades"]):
        return None                                       # one paper trade per coin per day
    p = r["market"]["price"]
    t = {"key": k, "symbol": r["market"]["symbol"], "ts": now or time.time(), "entry": p, "high": p,
         "score": r["score"], "alerted": False, "parts": [],
         "size": config.EARLY_SIZE_USD, "lp": r.get("launchpad"), "done": False}
    b["trades"].append(t)
    return t


def step_paper(s, markets, now=None):
    """Advance open paper trades with this scan's prices."""
    now = now or time.time()
    b = _book(s)
    b["trades"] = [t for t in b["trades"] if not t.get("done") or now - t["ts"] < 30 * 86400]
    for t in b["trades"]:
        if t.get("done"):
            continue
        m = markets.get(t["key"])
        px = m["price"] if m and m["price"] > 0 else None
        if not px:
            if now - t["ts"] > config.EARLY_MAX_HOURS * 3600 * 2:   # vanished: count it as a total loss
                _close(t, 0.0, "no price (delisted/rugged)", now, 1.0 - sum(f for f, _ in t["parts"]))
            continue
        t["high"] = max(t["high"], px)
        left = 1.0 - sum(f for f, _ in t["parts"])
        if px <= t["entry"] * (1 - config.EARLY_STOP_PCT / 100) and not t["parts"]:
            _close(t, px, "stop", now)
        elif not t["parts"] and px >= t["entry"] * (1 + config.EARLY_TP_PCT / 100):
            t["parts"].append([0.5, px])                   # sold half at +100%
        elif t["parts"] and px <= t["high"] * (1 - config.EARLY_TRAIL_PCT / 100):
            _close(t, px, "trailing stop after +100%", now, left)
        elif now - t["ts"] > config.EARLY_MAX_HOURS * 3600:
            _close(t, px, "24h time limit", now, left)


def _close(t, px, how, now, left=1.0):
    if left > 0:
        t["parts"].append([left, px])
    t.update(done=True, how=how, exit_ts=now, pnl=pnl(t["size"], t["entry"], t["parts"]),
             hours=round((now - t["ts"]) / 3600, 1))


def summary(s, days=14):
    b = s.get("early") or {}
    ts = [t for t in b.get("trades") or [] if t["ts"] >= time.time() - days * 86400]
    done = [t for t in ts if t.get("done")]

    def tally(xs):
        return {"n": len(xs), "wins": sum(1 for t in xs if t["pnl"] > 0),
                "pnl": round(sum(t["pnl"] for t in xs), 2),
                "big": sum(1 for t in xs if t["parts"] and t["parts"][0][1] >= t["entry"] * 2)}
    return {"all": tally(done), "alerted": tally([t for t in done if t.get("alerted")]),
            "paper_only": tally([t for t in done if not t.get("alerted")]),
            "open": sum(1 for t in ts if not t.get("done")),
            "watching": len(b.get("seen") or {}),
            "recent": sorted(done, key=lambda t: -t["exit_ts"])[:5]}


# ------------------------------------------------------------------ alerts
def due_alerts(s, alerts, now=None):
    """Alert-worthy results we haven't emailed in 24h, within the daily cap."""
    now = now or time.time()
    b = _book(s)
    today = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
    if b["day"] != today:
        b["day"], b["sent"] = today, 0
    b["alerts"] = {k: v for k, v in b["alerts"].items() if now - v < 86400}
    room = max(0, config.EARLY_MAX_ALERTS_PER_DAY - b["sent"])
    fresh = [r for r in sorted(alerts, key=lambda r: -r["score"]) if r["key"] not in b["alerts"]]
    return fresh[:min(room, 2)]


def verify(rs, scan_prices):
    """Re-price right before emailing: drop anything dumping or suddenly overextended."""
    live = dex.tokens([r["key"] for r in rs]) if rs else {}
    ok = []
    for r in rs:
        m = live.get(r["key"])
        if not m:
            continue
        drop = (1 - m["price"] / scan_prices[r["key"]]) * 100 if scan_prices.get(r["key"]) else 0
        h1 = (m.get("change") or {}).get("h1", 0) or 0
        if drop > 15 or h1 > config.EARLY_MAX_H1:
            log.info("early alert dropped %s: %s", m["symbol"], f"fell {drop:.0f}%" if drop > 15 else f"+{h1:.0f}% 1h")
            continue
        r["market"] = m
        ok.append(r)
    return ok


def mark_sent(s, rs, now=None):
    b = _book(s)
    for r in rs:
        b["alerts"][r["key"]] = now or time.time()
        b["sent"] += 1
        for t in b["trades"]:                    # "alerted" in the record = really emailed
            if t["key"] == r["key"] and not t.get("done"):
                t["alerted"] = True


def build_email(rs):
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    size = config.EARLY_SIZE_USD
    cards = []
    for r in rs:
        m = r["market"]
        p = m["price"]
        a = age_min(m) or 0
        age = f"{a / 60:.1f}h" if a >= 90 else f"{a:.0f} min"
        tp, stop = p * (1 + config.EARLY_TP_PCT / 100), p * (1 - config.EARLY_STOP_PCT / 100)
        be = pnl(size, 1.0, [(1.0, 1.0)])
        cards.append(f"""
<div style="border:2px solid #b36b00;border-radius:10px;padding:12px;margin:0 0 14px">
<div style="font-size:18px"><b>${e(m['symbol'])}</b> · {e(r['launchpad'] or 'launchpad')} · {age} old ·
<span style="background:#b36b00;color:#fff;border-radius:4px;padding:1px 6px">EARLY {r['score']}/100</span></div>
<div style="font-size:13px;color:#555;margin:4px 0">Market cap ${m['mcap']:,.0f} · liquidity ${m['liquidity']:,.0f} ·
price {fmt(p)} · {(m.get('change') or {}).get('h1', 0):+.0f}% in 1h</div>
<div style="font-size:13px"><b>Why:</b><br>{'<br>'.join('+ ' + e(x) for x in r['reasons'][:6]) or '+ momentum'}</div>
<div style="font-size:13px;color:#b00020;margin-top:4px">{'<br>'.join('− ' + e(x) for x in r['flags'][:5])}</div>
<table style="font-size:13px;margin-top:8px" cellpadding="3">
<tr><td>Buy</td><td><b>${size:.0f} max</b> (fees + slippage ≈ ${-be:.2f} of it on a flat round trip)</td></tr>
<tr><td>Sell half</td><td>{fmt(tp)} (+{config.EARLY_TP_PCT:.0f}%) - gets your money back</td></tr>
<tr><td>Rest</td><td>sell if it falls {config.EARLY_TRAIL_PCT:.0f}% from its high</td></tr>
<tr><td>Stop-loss</td><td>{fmt(stop)} (-{config.EARLY_STOP_PCT:.0f}%) - FOMO won't do this for you. If you buy, run <b>Check my position</b> and I'll email you when it's hit</td></tr>
<tr><td>Time limit</td><td>{config.EARLY_MAX_HOURS:.0f}h. Don't hold it overnight at full size.</td></tr></table>
<div style="font-size:12px;margin-top:6px"><a href="{e(m.get('url', ''))}">Chart</a> ·
<code>{e(chains.split(r['key'])[1])}</code></div></div>""")
    body = f"""<html><body style="font-family:Arial,sans-serif;max-width:640px">
<h2 style="margin:0 0 4px">Early coin alert - HIGH RISK</h2>
<p style="font-size:13px;color:#555;margin:0 0 12px">{now} · Fresh launchpad coins with momentum, community and a clean
safety check. Most coins this small fail - size every trade so losing it all is fine, and follow the exits.
This lane's own paper-trading record is in your digest; trust it over any single win.</p>
{''.join(cards)}
<p style="font-size:11px;color:#888">Not financial advice. The early lane is paper-tracked separately from your main picks.</p>
</body></html>"""
    subject = "EARLY (high risk): " + ", ".join(f"${r['market']['symbol']} {r['score']}" for r in rs)
    return subject, body


def table(s):
    """Digest section: is the early lane making money?"""
    c = summary(s)
    if not c["watching"] and not c["all"]["n"] and not c["open"]:
        return ""
    usd = lambda v: f"{'+' if v > 0 else ''}${v:,.2f}"

    def row(name, v):
        wr = f"{round(100 * v['wins'] / v['n'])}%" if v["n"] else "-"
        col = "#0a7d38" if v["pnl"] > 0 else "#b00020" if v["pnl"] < 0 else "#111"
        return (f"<tr><td>{name}</td><td>{v['n']}</td><td>{wr}</td><td>{v['big']}</td>"
                f"<td style='color:{col}'><b>{usd(v['pnl'])}</b></td></tr>")
    recent = ", ".join(f"${e(t['symbol'])} {usd(t['pnl'])} ({e(t['how'])})" for t in c["recent"]) or "none closed yet"
    return f"""<h3 style="margin:18px 0 6px">Early lane (high risk, paper, ${config.EARLY_SIZE_USD:.0f} per trade, 14 days)</h3>
<p style="font-size:12px;color:#555;margin:0 0 6px">Watching {c['watching']} fresh launchpad coins · {c['open']} paper trades open.
Exits: half at +{config.EARLY_TP_PCT:.0f}%, rest on a {config.EARLY_TRAIL_PCT:.0f}% drop from the high, stop -{config.EARLY_STOP_PCT:.0f}%, {config.EARLY_MAX_HOURS:.0f}h max.
"Alerted" = scored {config.EARLY_ALERT_SCORE}+ and was emailed; "paper only" = {config.EARLY_PAPER_SCORE}-{config.EARLY_ALERT_SCORE - 1}, tracked to see where the bar should be.</p>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Group</th><th>Closed</th><th>Winners</th><th>Hit +100%</th><th>Total P&amp;L</th></tr>
{row("Alerted", c["alerted"])}{row("Paper only", c["paper_only"])}{row("All", c["all"])}</table>
<p style="font-size:12px;color:#555">Last closed: {recent}</p>"""
