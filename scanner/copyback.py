"""Backtest: what would copying each trader with $COPY_SIZE_USD per trade have made over the
last COPY_BACKTEST_DAYS days?

    python -m scanner.copyback [handle,handle,...] [days]   (GitHub: Actions -> "Copy-trade backtest")

Uses each trader's own FOMO trade history (FOMO API /v2/users/{handle}/positions, 250 credits a
page). For every trade they opened in the window:
  * you buy $20 at their average entry, and sell when they did at their average exit
    (still-open trades are valued at today's price; partly sold ones are mixed accordingly)
  * FOMO fees ($0.95 min / 0.5% each way) and 1% slippage each way are included
  * "with copy lag" also assumes you buy COPY_LAG_PCT% higher and sell COPY_LAG_PCT% lower than
    they did, because you only find out after they trade. That's the realistic number.
Also reports how much money you'd have needed: the most $20 positions open at the same time.

Limits, said plainly: this is their FOMO-app history only, at average prices (not each fill);
coins they received or sent instead of buying are skipped; one month is a short sample, and
the people you pick to copy are usually the ones who just had a good month.
"""
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config, report, state as st
from .fomo import _get
from .tracker import trade_pnl

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("copyback")
e = report.e
MAX_PAGES = int(os.getenv("COPYBACK_PAGES") or 6)


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


def history(s, handle, since):
    """Their positions opened since `since` (pages until older than that, max MAX_PAGES)."""
    out, cursor = [], "start"
    for _ in range(MAX_PAGES):
        d = _get(s, f"/v2/users/{handle}/positions", 250, {"cursor": cursor, "limit": 100}, required=True)
        if not isinstance(d, dict) or d.get("available") is False:
            break
        page = d.get("positions") or []
        out += page
        cursor = d.get("nextCursor")
        closed = [_ts(p.get("createdAt")) for p in page if p.get("status") == "closed"]
        if not cursor or (closed and min(x for x in closed if x) < since):
            break
    seen, uniq = set(), []
    for p in out:
        k = p.get("tradeId") or ((p.get("token") or {}).get("address"), p.get("createdAt"))
        if k not in seen and (_ts(p.get("createdAt")) or 0) >= since:
            seen.add(k)
            uniq.append(p)
    return uniq


def copy_trade(p, size, lag):
    """One of their trades copied with $size. Returns a row, or None if it can't be priced."""
    entry = p.get("avgEntryPrice")
    if not entry or entry <= 0 or (p.get("costBasisUsd") or 0) < 5:
        return None                                   # received (not bought), or dust
    now_px, out_px = p.get("priceUsd"), p.get("avgExitPrice")
    got = (p.get("boughtAmount") or 0) + (p.get("transferredInAmount") or 0)
    sold = (p.get("soldAmount") or 0) / got if got else 0
    if p.get("status") == "closed":
        if not out_px:
            return None                               # sent away rather than sold: can't tell
        exit_px, how = out_px, "closed"
    else:
        if not now_px:
            return None
        sold = min(max(sold, 0), 1)
        exit_px = (sold * out_px + (1 - sold) * now_px) if (out_px and sold) else now_px
        how = "still open (valued now)"
    t_in = _ts(p.get("createdAt"))
    t_out = _ts(p.get("closedAt")) if p.get("status") == "closed" else None
    their = (p.get("realizedPnlUsd") or 0) + (p.get("unrealizedPnlUsd") or 0)
    return {"symbol": (p.get("token") or {}).get("symbol") or "?",
            "address": (p.get("token") or {}).get("address"), "status": how,
            "opened": datetime.fromtimestamp(t_in, timezone.utc).strftime("%Y-%m-%d %H:%M") if t_in else "",
            "t_in": t_in, "t_out": t_out,
            "their_pct": round(100 * their / p["costBasisUsd"], 1) if p.get("costBasisUsd") else None,
            "move_pct": round((exit_px / entry - 1) * 100, 1),
            "pnl": trade_pnl(size, entry, exit_px, slip_pct=1),
            "pnl_lag": trade_pnl(size, entry * (1 + lag / 100), exit_px * (1 - lag / 100), slip_pct=1)}


def peak_open(rows, size):
    """Most positions open at once x size = the money you'd actually have needed."""
    pts = []
    for r in rows:
        if r["t_in"]:
            pts.append((r["t_in"], 1))
            pts.append((r["t_out"] or time.time(), -1))
    cur = best = 0
    for _, d in sorted(pts, key=lambda x: (x[0], x[1])):
        cur += d
        best = max(best, cur)
    return best, best * size


def tally(rows, size):
    n = len(rows)
    pk, cap = peak_open(rows, size)
    return {"n": n, "wins": sum(1 for r in rows if r["pnl"] > 0), "wins_lag": sum(1 for r in rows if r["pnl_lag"] > 0),
            "pnl": round(sum(r["pnl"] for r in rows), 2), "pnl_lag": round(sum(r["pnl_lag"] for r in rows), 2),
            "open": sum(1 for r in rows if r["t_out"] is None), "peak": pk, "capital": cap,
            "best": max(rows, key=lambda r: r["pnl"]) if rows else None,
            "worst": min(rows, key=lambda r: r["pnl"]) if rows else None,
            "big": sum(1 for r in rows if r["move_pct"] >= 100), "fees": round(n * 2 * config.FEE_MIN_USD, 2)}


def build_email(results, size, days, lag):
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    usd = lambda v: f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    col = lambda v: "#0a7d38" if v > 0 else "#b00020" if v < 0 else "#111"
    body_rows, all_rows = "", []
    for h, rows in sorted(results.items(), key=lambda x: -tally(x[1], size)["pnl_lag"] if x[1] else 0):
        if rows is None:
            body_rows += f"<tr><td>@{e(h)}</td><td colspan='7'>no trade history from the FOMO API</td></tr>"
            continue
        t = tally(rows, size)
        all_rows += rows
        wr = f"{round(100 * t['wins_lag'] / t['n'])}%" if t["n"] else "-"
        best = f"${e(t['best']['symbol'])} {usd(t['best']['pnl'])}" if t["best"] else "-"
        worst = f"${e(t['worst']['symbol'])} {usd(t['worst']['pnl'])}" if t["worst"] else "-"
        body_rows += (f"<tr><td>@{e(h)}</td><td>{t['n']}</td><td>{wr}</td>"
                      f"<td style='color:{col(t['pnl'])}'>{usd(t['pnl'])}</td>"
                      f"<td style='color:{col(t['pnl_lag'])}'><b>{usd(t['pnl_lag'])}</b></td>"
                      f"<td>${t['capital']:,.0f}<br><span style='font-size:11px;color:#888'>{t['peak']} at once</span></td>"
                      f"<td style='font-size:12px'>{best}</td><td style='font-size:12px'>{worst}</td></tr>")
    T = tally(all_rows, size)
    trows = "".join(
        f"<tr><td>${e(r['symbol'])}</td><td>{e(r['trader'])}</td><td>{r['opened']}</td><td>{r['move_pct']:+.0f}%</td>"
        f"<td style='color:{col(r['pnl_lag'])}'>{usd(r['pnl_lag'])}</td><td>{e(r['status'])}</td></tr>"
        for r in sorted(all_rows, key=lambda r: -r["pnl_lag"])[:15])
    body = f"""<html><body style="font-family:Arial,sans-serif;max-width:720px">
<h2 style="margin:0 0 4px">Copy-trade backtest: ${size:.0f} per trade, last {days} days</h2>
<p style="font-size:13px;color:#555">{now} · If you had copied every FOMO trade these traders opened in the last {days} days
with ${size:.0f} each, buying and selling when they did. FOMO fees and 1% slippage each way included.
<b>"With copy lag"</b> also assumes you buy {lag:.0f}% higher and sell {lag:.0f}% lower than they did (you only see their
trade after it happens) - that's the realistic column. Open trades are valued at today's price.</p>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Trader</th><th>Trades</th><th>Winners</th><th>Exact copy</th><th>With copy lag</th>
<th>Money needed</th><th>Best</th><th>Worst</th></tr>{body_rows}
<tr style="background:#f9f9f9"><td><b>All of them</b></td><td>{T['n']}</td><td>-</td>
<td style='color:{col(T['pnl'])}'>{usd(T['pnl'])}</td><td style='color:{col(T['pnl_lag'])}'><b>{usd(T['pnl_lag'])}</b></td>
<td>${T['capital']:,.0f}</td><td colspan="2">{T['big']} trades doubled or better</td></tr></table>
<p style="font-size:13px">Fees alone on {T['n']} copied trades: at least ${T['fees']:,.2f} (FOMO's $0.95 minimum each way on
${size:.0f} trades). "Money needed" is the most ${size:.0f} positions you'd have had open at the same time - if you don't have
that much, you'd have missed some of their trades.</p>
<h3 style="margin:14px 0 6px">Biggest copied trades (with copy lag)</h3>
<table style="border-collapse:collapse;font-size:12px;width:100%" border="1" cellpadding="3">
<tr style="background:#f3f3f3"><th>Coin</th><th>Trader</th><th>Opened (UTC)</th><th>Their move</th><th>Your ${size:.0f}</th><th></th></tr>{trows}</table>
<p style="font-size:11px;color:#888">Their FOMO-app trades only, at average prices. Coins they received or sent instead of
buying/selling are skipped. One month is a short sample - the live paper copy books in your digest are the real test.
Full data: out/copyback.csv in this run's Artifacts. Not financial advice.</p></body></html>"""
    subject = f"Copy backtest ({days}d, ${size:.0f}/trade): {usd(T['pnl_lag'])} with copy lag across {T['n']} trades"
    return subject, body


def run(handles=None, days=None):
    handles = [h.strip().lstrip("@") for h in (handles or ",".join(config.COPY_TRADERS)).split(",") if h.strip()]
    days = int(days or config.COPY_BACKTEST_DAYS)
    size, lag = config.COPY_SIZE_USD, config.COPY_LAG_PCT
    since = time.time() - days * 86400
    s = st.load()
    results = {}
    for h in handles:
        try:
            ps = history(s, h, since)
        except Exception:
            log.exception("history failed for %s", h)
            ps = None
        if ps is None:
            results[h] = None
            continue
        rows = []
        for p in ps:
            r = copy_trade(p, size, lag)
            if r:
                r["trader"] = h
                rows.append(r)
        results[h] = rows
        log.info("%s: %d positions, %d copyable", h, len(ps), len(rows))
    os.makedirs("out", exist_ok=True)
    flat = [r for rows in results.values() if rows for r in rows]
    if flat:
        keys = ["trader", "symbol", "address", "opened", "status", "their_pct", "move_pct", "pnl", "pnl_lag"]
        with open("out/copyback.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(flat)
    subject, body = build_email(results, size, days, lag)
    with open("out/copyback.html", "w") as f:
        f.write(body)
    report.send(subject, body)
    print(subject)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None, sys.argv[2] if len(sys.argv) > 2 else None)
