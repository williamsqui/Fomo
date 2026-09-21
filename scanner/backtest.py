"""Rough backtest: what would copying today's top FOMO traders have done over the last ~90 days?

    python -m scanner.backtest            (run from the "Backtest" button in GitHub Actions)

Read this before trusting the number it produces:
  * Solana only. The free Base/BNB connections refuse to look up old trades.
  * It uses TODAY's top 100. They're on the board because they did well recently, so
    their past buys look better than a random trader's would (survivorship bias). The
    most recent BT_SKIP_DAYS are skipped because those trades are what put them there.
  * Only the parts of the score with history are used: top-trader buying, the real chart
    check, momentum and market cap. There's no history for X, Telegram, safety checks
    or liquidity, so those are left out.
  * Exits follow your rules on 15-minute candles: +50% target, -30% stop, otherwise sell
    after 48h. If a candle touches both, we assume the stop (worst case). A gap below the
    stop fills at the real price, not the stop.
  * Entry is BT_LAG_MIN after the signal (scan + email + you opening FOMO), with FOMO fees
    and slippage both ways.
So treat the result as a BEST case. If even this loses money, the strategy doesn't work.
If it makes money, the paper-trading table in your digests is what confirms it for real.
"""
import csv
import html
import logging
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone

from . import chart, config, fomo, report, solana, state as st, tracker
from .http import request

log = logging.getLogger("backtest")
e = html.escape

DAYS = int(os.getenv("BT_DAYS") or 90)
SKIP_DAYS = int(os.getenv("BT_SKIP_DAYS") or 7)
WALLETS = int(os.getenv("BT_WALLETS") or 50)
TX_PER_WALLET = int(os.getenv("BT_TX_PER_WALLET") or 800)
CREDIT_CAP = int(os.getenv("BT_HELIUS_CREDITS") or 60_000)
MAX_SIGNALS = int(os.getenv("BT_MAX_SIGNALS") or 300)
TIME_LIMIT_MIN = float(os.getenv("BT_TIME_LIMIT_MIN") or 300)
LAG_MIN = float(os.getenv("BT_LAG_MIN") or 20)
SIZE = float(os.getenv("BT_SIZE_USD") or 30)
MIN_BUY_SOL = float(os.getenv("BT_MIN_BUY_SOL") or 0.5)
HELIUS_RPS = float(os.getenv("BT_HELIUS_RPS") or 8)
WINDOW_H = config.TRACK_WINDOW_HOURS
TIERS = (1, 2, 3)


class Budget:
    def __init__(self, cap, deadline):
        self.cap, self.used, self.deadline = cap, 0, deadline

    def left(self, cost=1):
        return self.used + cost <= self.cap and time.time() < self.deadline


def _rpc(calls, budget):
    """One Helius JSON-RPC batch, throttled to the free plan's request rate."""
    if not budget.left(len(calls)):
        return None
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    r = request("POST", f"https://mainnet.helius-rpc.com/?api-key={config.HELIUS_API_KEY}",
                json=body, timeout=60, retries=4)
    budget.used += len(calls)
    time.sleep(len(calls) / HELIUS_RPS)
    if r is None:
        return None
    out = r.json()
    out = [out] if isinstance(out, dict) else out
    out.sort(key=lambda x: x.get("id", 0))
    return [o.get("result") for o in out]


# ---------------------------------------------------------------- 1. trades
def collect_buys(traders, t0, t1, budget):
    """Every swap-buy by these wallets between t0 and t1 (unix seconds)."""
    buys, stats = [], {"wallets": 0, "tx": 0, "reach": []}
    for t in traders:
        w = t.get("wallet")
        if not w or not budget.left(2):
            continue
        sigs, before = [], None
        while len(sigs) < TX_PER_WALLET and budget.left():
            opts = {"limit": 1000, **({"before": before} if before else {})}
            res = _rpc([("getSignaturesForAddress", [w, opts])], budget)
            page = (res or [None])[0] or []
            if not page:
                break
            sigs += [x for x in page if not x.get("err") and t0 <= (x.get("blockTime") or 0) <= t1]
            before = page[-1]["signature"]
            if (page[-1].get("blockTime") or 0) < t0 or len(page) < 1000:
                break
        sigs = sigs[:TX_PER_WALLET]
        stats["wallets"] += 1
        if sigs:  # how far back this wallet's history actually goes (newest-first, capped)
            stats["reach"].append(sigs[-1].get("blockTime") or t1)
        for i in range(0, len(sigs), 10):
            chunk = sigs[i:i + 10]
            txs = _rpc([("getTransaction", [x["signature"], {"encoding": "jsonParsed",
                                                             "maxSupportedTransactionVersion": 0}])
                        for x in chunk], budget)
            if txs is None:
                break
            stats["tx"] += len(chunk)
            for sig, tx in zip(chunk, txs):
                for mint, side, amt, sol, usd in solana.parse_swap(tx, w):
                    if side == "buy" and mint not in solana.QUOTES and (sol >= MIN_BUY_SOL or usd >= MIN_BUY_SOL * 150):
                        buys.append({"ts": sig["blockTime"], "mint": mint, "handle": t["handle"],
                                     "rank": t["rank"], "sol": round(sol, 3)})
        log.info("wallet %s (#%s): %d sigs, total buys %d, credits %d",
                 t["handle"], t["rank"], len(sigs), len(buys), budget.used)
    return buys, stats


# ---------------------------------------------------------------- 2. signals
def find_signals(buys):
    """The moment a coin reaches 1, 2 and 3+ distinct top-trader buyers within the lookback.

    Each tier is its own strategy ("buy when N top traders are in"). One signal per coin
    per tier per 48h, so a coin being bought all week doesn't count as 20 trades.
    """
    by_mint = {}
    for b in sorted(buys, key=lambda b: b["ts"]):
        by_mint.setdefault(b["mint"], []).append(b)
    look = config.LOOKBACK_HOURS * 3600
    out = []
    for mint, bs in by_mint.items():
        last = {}
        for i, b in enumerate(bs):
            win = [x for x in bs[:i + 1] if x["ts"] >= b["ts"] - look]
            who = {}
            for x in win:
                who.setdefault(x["handle"], x)
            n = len(who)
            for tier in TIERS:
                if n >= tier and b["ts"] - last.get(tier, -1e18) > WINDOW_H * 3600:
                    last[tier] = b["ts"]
                    w = sum(1 + (101 - x["rank"]) / 100 for x in who.values())
                    out.append({"mint": mint, "ts": b["ts"], "tier": tier, "n": n,
                                "first_ts": min(x["ts"] for x in win),
                                "who": ", ".join(sorted(who)[:4]),
                                "smart_pts": round(30 * (1 - math.exp(-0.5 * w)), 1)})
    # most informative first (3+ traders, then 2), newest first within a tier
    out.sort(key=lambda s: (-s["tier"], -s["ts"]))
    return out


# ---------------------------------------------------------------- 3. prices
def pool_info(mint):
    d = chart._gt(f"/networks/solana/tokens/{mint}/pools", {"page": 1})
    pools = (d or {}).get("data") or []
    if not pools:
        return None
    a = pools[0].get("attributes") or {}
    try:
        price = float(a.get("base_token_price_usd") or 0)
        fdv = float(a.get("fdv_usd") or 0)
    except (TypeError, ValueError):
        return None
    created = a.get("pool_created_at")
    try:
        created = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None
    except ValueError:
        created = None
    return {"pool": pools[0]["id"].split("_", 1)[-1], "supply": fdv / price if price > 0 else None,
            "created": created, "symbol": (a.get("name") or "?").split(" / ")[0]}


def candles_15m(pool, until):
    d = chart._gt(f"/networks/solana/pools/{pool}/ohlcv/minute",
                  {"aggregate": 15, "limit": 1000, "before_timestamp": int(until), "currency": "usd"})
    try:
        rows = d["data"]["attributes"]["ohlcv_list"]
    except (TypeError, KeyError):
        return None
    return sorted([[float(x) for x in r[:6]] for r in rows], key=lambda r: r[0]) or None


def hourly(rows):
    out = {}
    for t, o, h, l, c, v in rows:
        k = int(t // 3600 * 3600)
        if k not in out:
            out[k] = [k, o, h, l, c, v]
        else:
            r = out[k]
            r[2], r[3], r[4], r[5] = max(r[2], h), min(r[3], l), c, r[5] + v
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------- 4. trade
def simulate(rows, signal_ts):
    """Enter LAG_MIN after the signal at the next candle's open; exit by your rules."""
    start = signal_ts + LAG_MIN * 60
    after = [r for r in rows if r[0] >= start]
    if not after:
        return None
    entry = after[0][1]
    if entry <= 0:
        return None
    stop, target = entry * (1 - config.STOP_LOSS_PCT / 100), entry * (1 + config.TARGET_GAIN_PCT / 100)
    end = after[0][0] + WINDOW_H * 3600
    last = after[0]
    for t, o, h, l, c, v in after:       # includes the entry candle: it can hit the stop too
        if t > end:
            break
        last = [t, o, h, l, c, v]
        if l <= stop:
            # gapped below the stop -> you get the open. Crashed through it and stayed down ->
            # you get the close (meme crashes don't wait for your order). Just a wick -> the stop.
            px = o if o < stop else min(stop, c)
            return {"how": "stop", "entry": entry, "exit": px, "hours": (t - after[0][0]) / 3600}
        if h >= target:
            px = max(o, target)
            return {"how": "target", "entry": entry, "exit": px, "hours": (t - after[0][0]) / 3600}
    if last[0] < end - 3 * 3600 and time.time() < end:
        return None                      # not enough candles yet to judge this one
    return {"how": "time", "entry": entry, "exit": last[4], "hours": (last[0] - after[0][0]) / 3600}


def features(hrows, sig, info):
    """What the scanner could have known at the signal moment."""
    past = [r for r in hrows if r[0] + 3600 <= sig["ts"]][-168:]
    if len(past) < 12:
        return None
    ch = chart.analyze(past)
    price = past[-1][4]
    h1 = (price / past[-2][4] - 1) * 100 if len(past) >= 2 and past[-2][4] else 0
    v6 = sum(r[5] for r in past[-7:-1]) / 6 or 1
    first = [r for r in hrows if r[0] <= sig["first_ts"]]
    runup = (price / first[-1][4] - 1) * 100 if first and first[-1][4] else 0
    return {"chart_pts": ch["points"] if ch else 0, "chart": ch["verdict"] if ch else "n/a",
            "h1": round(h1, 1), "vol_accel": round(past[-1][5] / v6, 2),
            "mcap": price * info["supply"] if info.get("supply") else None,
            "age_h": (sig["ts"] - info["created"]) / 3600 if info.get("created") else None,
            "runup": round(runup, 1)}


def passes_filters(f):
    reasons = []
    if f["mcap"] is None or not (config.MIN_MCAP_USD <= f["mcap"] <= config.MAX_MCAP_USD):
        reasons.append("mcap")
    if f["age_h"] is not None and f["age_h"] < config.MIN_PAIR_AGE_HOURS:
        reasons.append("age")
    return reasons


def scanner_like(r):
    """Closest approximation of 'the scanner would have sent this' with the data we have."""
    return (r["tier"] >= 2 and not r["filtered"] and r["chart_pts"] >= 6
            and r["h1"] <= 40 and r["runup"] <= config.MAX_RUNUP_PCT)


# ---------------------------------------------------------------- 5. report
def stats(rows):
    if not rows:
        return None
    pnl = [r["pnl"] for r in rows]
    return {"n": len(rows), "win": sum(r["how"] == "target" for r in rows),
            "stop": sum(r["how"] == "stop" for r in rows), "total": round(sum(pnl), 2),
            "avg": round(sum(pnl) / len(pnl), 2), "median": round(statistics.median(pnl), 2)}


def build_report(results, meta):
    groups = [
        ("Your scanner's rules (approx.): 2+ traders, filters, chart uptrend, not chasing", [r for r in results if r["scanner_like"]]),
        ("3+ top traders bought (filters applied)", [r for r in results if r["tier"] == 3 and not r["filtered"]]),
        ("2 top traders bought (filters applied)", [r for r in results if r["tier"] == 2 and not r["filtered"]]),
        ("1 top trader bought (filters applied)", [r for r in results if r["tier"] == 1 and not r["filtered"]]),
        ("2+ traders but chart NOT in uptrend", [r for r in results if r["tier"] >= 2 and not r["filtered"] and r["chart_pts"] < 6]),
        ("Filtered out by your market cap / age rules", [r for r in results if r["filtered"] and r["tier"] >= 2]),
    ]
    row = lambda name, s: (
        f"<tr><td>{e(name)}</td><td>{s['n']}</td><td>{round(100 * s['win'] / s['n'])}%</td>"
        f"<td>{round(100 * s['stop'] / s['n'])}%</td><td>{tracker_usd(s['avg'])}</td><td>{tracker_usd(s['median'])}</td>"
        f"<td style='color:{'#0a7d38' if s['total'] > 0 else '#b00020'}'><b>{tracker_usd(s['total'])}</b></td></tr>"
        if s else f"<tr><td>{e(name)}</td><td>0</td><td colspan=5>no trades</td></tr>")
    table = "".join(row(n, stats(g)) for n, g in groups)
    main = stats(groups[0][1])
    verdict = ("Not enough scanner-like trades to judge." if not main or main["n"] < 15 else
               f"Even in this best-case test, following the rules LOST money: {tracker_usd(main['avg'])} per ${SIZE:.0f} trade."
               if main["total"] < 0 else
               f"In this best-case test, following the rules made {tracker_usd(main['avg'])} per ${SIZE:.0f} trade "
               f"({round(100 * main['win'] / main['n'])}% hit +{config.TARGET_GAIN_PCT:.0f}%). Real results will be lower - "
               "watch the paper-trading table to confirm.")
    sl = sorted((r for r in results if r["scanner_like"]), key=lambda r: r["pnl"])
    ex = lambda rs: "".join(
        f"<tr><td>{datetime.fromtimestamp(r['ts'], timezone.utc):%b %d}</td><td>${e(r['symbol'])}</td><td>{r['n']}</td>"
        f"<td>{e(r['how'])}</td><td>{tracker_usd(r['pnl'])}</td></tr>" for r in rs)
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:680px;margin:auto;padding:8px;color:#111">
<h2 style="margin:4px 0">Backtest: copying today's top FOMO traders (Solana)</h2>
<p style="color:#555;font-size:13px">{e(meta['range'])} · ${SIZE:.0f} per trade · entry {LAG_MIN:.0f} min after the signal ·
+{config.TARGET_GAIN_PCT:.0f}% target / -{config.STOP_LOSS_PCT:.0f}% stop / {WINDOW_H}h time exit · FOMO fees + {config.PAPER_SLIPPAGE_PCT:.0f}% slippage each way</p>
<div style="background:#fff6e5;border-left:5px solid #b36b00;border-radius:6px;padding:10px;font-size:13px;margin:8px 0">
<b>This is a best case, not a forecast.</b> It uses today's top traders (who are there because they did well), skips X/Telegram/safety/liquidity
(no history exists for them) and only covers Solana. If a line below loses money here, it will lose more in real life.</div>
<div style="background:#f3f6ff;border-radius:6px;padding:10px;font-size:14px;margin:8px 0"><b>Bottom line:</b> {e(verdict)}</div>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Strategy</th><th>Trades</th><th>Hit +50%</th><th>Stopped</th><th>Avg</th><th>Median</th><th>Total</th></tr>{table}</table>
<h3 style="margin:16px 0 6px">Worst and best scanner-like trades</h3>
<table style="border-collapse:collapse;font-size:12px;width:100%" border="1" cellpadding="3">
<tr style="background:#f3f3f3"><th>Date</th><th>Coin</th><th>Traders</th><th>Exit</th><th>P&amp;L</th></tr>{ex(sl[:5])}{ex(sl[-5:][::-1]) if len(sl) > 5 else ''}</table>
<p style="font-size:12px;color:#555">Coverage: {meta['wallets']} trader wallets, {meta['tx']:,} transactions, {meta['buys']:,} buys,
{meta['signals']} signals, {len(results)} simulated. Skipped: {meta['no_data']} with no price history (often dead or rugged coins -
another reason this is a best case), {meta['too_new']} too recent to finish. Helius credits used: {meta['credits']:,}.
{'Stopped early at the time limit - results cover part of the period.' if meta.get('truncated') else ''}</p>
</body></html>"""
    return "Backtest: " + verdict[:90], body


def tracker_usd(v):
    return f"{'+' if v > 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


# ---------------------------------------------------------------- run
def run():
    t_start = time.time()
    deadline = t_start + TIME_LIMIT_MIN * 60
    s = st.load()                                    # read-only: reuse the scanner's leaderboard
    traders = [t for t in fomo.leaderboard(s) if t.get("wallet")][:WALLETS]
    room = config.HELIUS_MONTHLY_CREDITS - s.get("helius_credits_used", 0) - 150_000   # leave the live scanner room
    cap = max(0, min(CREDIT_CAP, room))
    log.info("backtest: %d wallets, %d days, Helius cap %d credits", len(traders), DAYS, cap)
    now = time.time()
    t0, t1 = now - DAYS * 86400, now - SKIP_DAYS * 86400
    # spend at most ~half the time on wallets, keep the rest for price data
    budget = Budget(cap, t_start + TIME_LIMIT_MIN * 30)
    buys, cstats = collect_buys(traders, t0, t1, budget)
    signals = find_signals(buys)
    log.info("%d buys -> %d signals", len(buys), len(signals))

    pools, results, no_data, too_new, truncated = {}, [], 0, 0, False
    for sig in signals[:MAX_SIGNALS]:
        if time.time() > deadline:
            truncated = True
            break
        if sig["mint"] not in pools:
            pools[sig["mint"]] = pool_info(sig["mint"])
        info = pools[sig["mint"]]
        if not info:
            no_data += 1
            continue
        rows = candles_15m(info["pool"], sig["ts"] + (WINDOW_H + 1) * 3600 + LAG_MIN * 60)
        if not rows:
            no_data += 1
            continue
        f = features(hourly(rows), sig, info)
        trade = simulate(rows, sig["ts"])
        if not f or not trade:
            if f and trade is None:
                too_new += 1
            else:
                no_data += 1
            continue
        r = {**sig, **f, **trade, "symbol": info["symbol"],
             "pnl": tracker.trade_pnl(SIZE, trade["entry"], trade["exit"])}
        r["filtered"] = bool(passes_filters(f))
        r["scanner_like"] = scanner_like(r)
        results.append(r)

    reach = statistics.median(cstats["reach"]) if cstats["reach"] else t0
    fmt_d = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%b %d")
    meta = {"range": (f"{fmt_d(t0)} - {fmt_d(t1)} {datetime.fromtimestamp(t1, timezone.utc):%Y}"
                      + (f" (each trader capped at {TX_PER_WALLET} transactions, so a typical trader's history "
                         f"reaches back to {fmt_d(reach)})" if reach > t0 + 3 * 86400 else "")),
            "wallets": cstats["wallets"], "tx": cstats["tx"], "buys": len(buys), "signals": len(signals),
            "no_data": no_data, "too_new": too_new, "credits": budget.used, "truncated": truncated}
    subject, body = build_report(results, meta)
    os.makedirs("out", exist_ok=True)
    with open("out/backtest.html", "w") as fh:
        fh.write(body)
    with open("out/backtest.csv", "w", newline="") as fh:
        cols = ["ts", "symbol", "mint", "tier", "n", "who", "smart_pts", "chart_pts", "chart", "h1", "vol_accel",
                "mcap", "age_h", "runup", "filtered", "scanner_like", "how", "entry", "exit", "hours", "pnl"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    report.send(subject, body)
    print(subject)
    return results, meta


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
    if not config.HELIUS_API_KEY:
        sys.exit("HELIUS_API_KEY is required")
    run()
