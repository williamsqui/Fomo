"""Review your own FOMO trades: what did the winners look like BEFORE you bought, and what
would the early lane's rules have done with them?

    python -m scanner.review [handle]        (GitHub: Actions -> "Review my trades")

For every trade on your FOMO profile (FOMO API /v2/users/{handle}/positions, 250 credits a page):
  * rebuilds the chart around your first buy from 15-minute candles (GeckoTerminal, free)
  * BEFORE the buy: coin age, market cap, move in the last 1h / 6h / 24h, volume speed-up,
    how far below its 24h high, which launchpad
  * AFTER the buy: the best price in the next 48h (what was possible), the worst dip first,
    and what the early lane's exits (half at +100%, rest on a 30% drop from the high, stop
    -35%, 24h) would have made on your actual stake - next to what you really made
  * whether the early lane's filters would even have looked at it, and a rough early score
Then compares winners with losers, so we tune the scanner on YOUR trades, not on stories.
Emails the report and saves out/review.csv (download it from the run's "Artifacts").

It's rough on purpose-limited data: FOMO gives average entry/exit, not every fill; candles have
no buyer counts or holder numbers, so the early score here is an approximation (momentum,
size, age, socials only). A handful of trades is a hint, not proof.
"""
import csv
import logging
import os
import statistics as stats
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import chains, chart, config, dex, early, report, state as st
from .fomo import _get

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("review")
e = report.e
PAGES = int(os.getenv("REVIEW_PAGES") or 4)
MAX_TRADES = int(os.getenv("REVIEW_MAX_TRADES") or 60)


def _ts(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def positions(s, handle):
    out, cursor = [], "start"
    for _ in range(PAGES):
        d = _get(s, f"/v2/users/{handle}/positions", 250, {"cursor": cursor, "limit": 100}, required=True)
        if not isinstance(d, dict):
            break
        out += d.get("positions") or []
        cursor = d.get("nextCursor")
        if not cursor:
            break
    seen, uniq = set(), []
    for p in out:
        k = p.get("tradeId") or (p.get("token") or {}).get("address")
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def market_for(addr):
    opts = ["base", "bsc", "robinhood"] if addr.lower().startswith("0x") else ["solana"]
    found = dex.tokens([chains.key(c, addr) for c in opts])
    return max(found.values(), key=lambda m: m["liquidity"]) if found else None


def candles(m, entry_ts):
    """15-minute candles from ~52h before the buy to 48h after it: [[ts, o, h, l, c, v], ...]."""
    if not m or not m.get("pair"):
        return []
    d = chart._gt(f"/networks/{chains.CHAIN[m['chain']]['gt']}/pools/{m['pair']}/ohlcv/minute",
                  {"aggregate": 15, "before_timestamp": int(entry_ts + 48 * 3600), "limit": 400, "currency": "usd"})
    try:
        rows = d["data"]["attributes"]["ohlcv_list"]
    except (TypeError, KeyError):
        return []
    return sorted(([float(x) for x in r[:6]] for r in rows), key=lambda r: r[0])


def _chg(before, hours, px):
    old = [r for r in before if r[0] <= before[-1][0] - hours * 3600]
    return (px / old[-1][4] - 1) * 100 if old and old[-1][4] else None


def simulate_early(rows, entry, size):
    """The early lane's exits on 15-min candles after the buy (stop checked first = conservative)."""
    parts, high, t0 = [], entry, rows[0][0] if rows else 0
    for ts, o, h, lo, c, v in rows:
        if not parts:
            if lo <= entry * (1 - config.EARLY_STOP_PCT / 100):
                parts.append([1.0, entry * (1 - config.EARLY_STOP_PCT / 100)])
                return early.pnl(size, entry, parts), "stop"
            if h >= entry * (1 + config.EARLY_TP_PCT / 100):
                parts.append([0.5, entry * (1 + config.EARLY_TP_PCT / 100)])
                high = max(high, h)
                continue
        else:
            high = max(high, h)
            trail = high * (1 - config.EARLY_TRAIL_PCT / 100)
            if lo <= trail:
                parts.append([0.5, trail])
                return early.pnl(size, entry, parts), f"+100% then trailing ({high / entry:.1f}x high)"
        if ts - t0 > config.EARLY_MAX_HOURS * 3600:
            parts.append([1.0 - sum(f for f, _ in parts), c])
            return early.pnl(size, entry, parts), "24h limit"
    if rows:
        parts.append([1.0 - sum(f for f, _ in parts), rows[-1][4]])
        return early.pnl(size, entry, parts), "still open"
    return None, "no candles"


def analyze(p):
    tok = p.get("token") or {}
    addr, sym = tok.get("address"), tok.get("symbol") or "?"
    entry, cost = p.get("avgEntryPrice"), p.get("costBasisUsd") or 0
    t_in = _ts(p.get("createdAt"))
    if not addr or not entry or not t_in or cost < 3:
        return None
    pnl_usd = (p.get("realizedPnlUsd") or 0) + (p.get("unrealizedPnlUsd") or 0)
    row = {"symbol": sym, "address": addr, "status": p.get("status"), "cost": round(cost, 2),
           "pnl_usd": round(pnl_usd, 2), "pnl_pct": round(100 * pnl_usd / cost, 1) if cost else None,
           "bought": datetime.fromtimestamp(t_in, timezone.utc).strftime("%Y-%m-%d %H:%M"),
           "held_h": round(((_ts(p.get("closedAt")) or time.time()) - t_in) / 3600, 1)}
    m = market_for(addr)
    if not m:
        row["note"] = "not on DexScreener any more (dead or delisted)"
        return row
    row.update(chain=m["chain"], launchpad=early.launchpad(m["key"]) or ("launchpad-less" if m["chain"] == "solana" else m["chain"]))
    supply = m["mcap"] / m["price"] if m["price"] else None
    row["mcap_entry"] = round(entry * supply) if supply else None
    row["age_h"] = round((t_in * 1000 - m["created_ms"]) / 3.6e6, 1) if m.get("created_ms") else None
    rows = candles(m, t_in)
    before = [r for r in rows if r[0] <= t_in]
    after = [r for r in rows if r[0] > t_in - 900]
    if len(before) >= 5:
        row["chg_1h"] = _r(_chg(before, 1, entry))
        row["chg_6h"] = _r(_chg(before, 6, entry))
        row["chg_24h"] = _r(_chg(before, 24, entry))
        v1 = sum(r[5] for r in before[-4:])
        v6 = sum(r[5] for r in before[-24:]) / 6
        row["vol_accel"] = round(v1 / v6, 2) if v6 else None
        hi24 = max(r[2] for r in before[-96:])
        row["from_24h_high"] = _r((entry / hi24 - 1) * 100) if hi24 else None
    if after:
        peak = max(r[2] for r in after)
        i_peak = max(range(len(after)), key=lambda i: after[i][2])
        row["peak_48h_pct"] = _r((peak / entry - 1) * 100)
        row["hours_to_peak"] = round((after[i_peak][0] - t_in) / 3600, 1)
        row["dip_before_peak_pct"] = _r((min(r[3] for r in after[:i_peak + 1]) / entry - 1) * 100)
        row["could_have_made"] = round(cost * peak / entry - cost, 2)
        sim, how = simulate_early(after, entry, cost)
        row["early_rules_pnl"], row["early_rules_how"] = sim, how
    # would the early lane have looked at it at the moment you bought?
    a_min = row["age_h"] * 60 if row.get("age_h") is not None else None
    row["early_filter"] = bool(row.get("mcap_entry") and config.EARLY_MIN_MCAP <= row["mcap_entry"] <= config.EARLY_MAX_MCAP
                               and a_min is not None and config.EARLY_MIN_AGE_MIN <= a_min <= config.EARLY_MAX_AGE_H * 60
                               and (row.get("chg_1h") or 0) <= config.EARLY_MAX_H1)
    if row.get("chg_1h") is not None and row.get("mcap_entry"):
        liq = m["liquidity"] * (entry / m["price"]) ** 0.5 if m["price"] else m["liquidity"]
        snap = dict(m, price=entry, mcap=row["mcap_entry"], liquidity=liq, buys_h1=1, sells_h1=1,
                    created_ms=m["created_ms"] - (time.time() - t_in) * 1000,
                    change={"h1": row["chg_1h"], "h6": row.get("chg_6h") or 0, "m5": 0},
                    volume={"h1": sum(r[5] for r in before[-4:]), "h6": sum(r[5] for r in before[-24:])})
        row["early_score_approx"] = early.score(snap)["score"]
    return row


def _r(v):
    return round(v, 1) if v is not None else None


def compare(rows):
    win = [r for r in rows if (r.get("pnl_usd") or 0) > 0]
    lose = [r for r in rows if (r.get("pnl_usd") or 0) <= 0]
    feats = [("mcap_entry", "Market cap when you bought", "$"), ("age_h", "Coin age when you bought (h)", ""),
             ("chg_1h", "Move in the 1h before (%)", ""), ("chg_6h", "Move in the 6h before (%)", ""),
             ("chg_24h", "Move in the 24h before (%)", ""), ("vol_accel", "Volume speed-up (last 1h vs 6h avg)", "x"),
             ("from_24h_high", "Below its 24h high (%)", ""), ("peak_48h_pct", "Best price within 48h (%)", ""),
             ("dip_before_peak_pct", "Worst dip before that peak (%)", ""), ("hours_to_peak", "Hours to the peak", ""),
             ("early_score_approx", "Rough early score", "")]

    def med(xs, k):
        v = [x[k] for x in xs if x.get(k) is not None]
        return stats.median(v) if v else None
    table = []
    for k, label, unit in feats:
        a, b = med(win, k), med(lose, k)
        f = (lambda v: "-" if v is None else f"${v:,.0f}" if unit == "$" else f"{v:,.1f}{unit}")
        table.append((label, f(a), f(b)))
    lp = {}
    for r in rows:
        x = lp.setdefault(r.get("launchpad") or "?", [0, 0])
        x[0 if (r.get("pnl_usd") or 0) > 0 else 1] += 1
    return win, lose, table, lp


def build_email(handle, rows):
    win, lose, table, lp = compare(rows)
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    real = sum(r.get("pnl_usd") or 0 for r in rows)
    sim_rows = [r for r in rows if r.get("early_rules_pnl") is not None]
    sim = sum(r["early_rules_pnl"] for r in sim_rows)
    real_same = sum(r.get("pnl_usd") or 0 for r in sim_rows)
    left = sum(max(0, (r.get("could_have_made") or 0) - (r.get("pnl_usd") or 0)) for r in win)
    caught = [r for r in win if r.get("early_filter")]
    usd = lambda v: f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    def cell(v, f):
        return "-" if v is None else f(v)

    def trow(r):
        return (f"<tr><td>${e(r['symbol'])}</td><td>{e(r.get('launchpad') or '')}</td>"
                f"<td>{usd(r['pnl_usd'])} ({r.get('pnl_pct') or 0:+.0f}%)</td>"
                f"<td>{cell(r.get('mcap_entry'), lambda v: f'${v:,.0f}')}</td>"
                f"<td>{cell(r.get('age_h'), lambda v: f'{v:g}')}</td>"
                f"<td>{cell(r.get('chg_1h'), lambda v: f'{v:+.0f}%')}</td>"
                f"<td>{cell(r.get('peak_48h_pct'), lambda v: f'{v:+.0f}%')}</td>"
                f"<td>{cell(r.get('early_rules_pnl'), usd)}</td>"
                f"<td>{'yes' if r.get('early_filter') else 'no'}</td></tr>")
    trs = "".join(trow(r) for r in sorted(rows, key=lambda r: -(r.get("pnl_usd") or 0)))
    cmp_rows = "".join(f"<tr><td>{e(a)}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in table)
    lp_rows = ", ".join(f"{e(k)}: {v[0]} won / {v[1]} lost" for k, v in sorted(lp.items(), key=lambda x: -sum(x[1])))
    body = f"""<html><body style="font-family:Arial,sans-serif;max-width:720px">
<h2 style="margin:0 0 4px">Your trades, reviewed - @{e(handle)}</h2>
<p style="font-size:13px;color:#555">{now} · {len(rows)} trades · {len(win)} winners, {len(lose)} losers ·
your total {usd(real)}</p>
<h3 style="margin:14px 0 6px">What your winners looked like before you bought vs your losers (medians)</h3>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th></th><th>Winners</th><th>Losers</th></tr>{cmp_rows}</table>
<p style="font-size:13px">By launchpad: {lp_rows or '-'}</p>
<h3 style="margin:14px 0 6px">Exits</h3>
<p style="font-size:13px">On the {len(sim_rows)} trades with chart data, the early lane's exit rules (half at +100%, rest on a
30% drop from the high, stop -35%) would have made <b>{usd(sim)}</b> vs your actual <b>{usd(real_same)}</b>.
On your winners, selling at the best price within 48h would have made {usd(left)} more than you did - nobody sells
the exact top, but that's the size of the gap a trailing stop is meant to close.
The early lane's filters would have picked up {len(caught)} of your {len(win)} winners at the moment you bought.</p>
<h3 style="margin:14px 0 6px">Every trade</h3>
<table style="border-collapse:collapse;font-size:12px;width:100%" border="1" cellpadding="3">
<tr style="background:#f3f3f3"><th>Coin</th><th>Launchpad</th><th>You made</th><th>Mcap at buy</th><th>Age (h)</th>
<th>1h before</th><th>Best in 48h</th><th>Early rules</th><th>Early filter?</th></tr>{trs}</table>
<p style="font-size:11px;color:#888">FOMO gives average entry/exit prices, not each fill, and the candles have no buyer or
holder counts - so the early score here is rough. Full data: out/review.csv in this run's Artifacts. A few dozen trades is a
hint, not proof; the early lane's paper record is the real test.</p></body></html>"""
    return f"Trade review: {len(win)} winners / {len(lose)} losers, {usd(real)}", body


def run(handle=None):
    handle = (handle or os.getenv("FOMO_HANDLE") or "williamsqui").lstrip("@")
    s = st.load()
    ps = positions(s, handle)
    log.info("%d positions for @%s", len(ps), handle)
    ps = sorted(ps, key=lambda p: _ts(p.get("createdAt")) or 0, reverse=True)[:MAX_TRADES]
    rows = []
    for p in ps:
        try:
            r = analyze(p)
        except Exception:
            log.exception("couldn't analyze %s", (p.get("token") or {}).get("symbol"))
            continue
        if r:
            rows.append(r)
            log.info("%-10s %s", r["symbol"], {k: r.get(k) for k in ("pnl_usd", "mcap_entry", "age_h", "chg_1h",
                                                                      "peak_48h_pct", "early_rules_pnl")})
    os.makedirs("out", exist_ok=True)
    keys = sorted({k for r in rows for k in r})
    with open("out/review.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    if not rows:
        report.send(f"Trade review: no trades found for @{handle}",
                    f"<p>The FOMO API returned no positions for @{e(handle)}. Check the handle.</p>")
        return
    subject, body = build_email(handle, rows)
    with open("out/review.html", "w") as f:
        f.write(body)
    report.send(subject, body)
    print(subject)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
