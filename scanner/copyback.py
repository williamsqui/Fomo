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
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import chains, config, http, report, state as st
from .fomo import _get
from .tracker import trade_pnl

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("copyback")
e = report.e
MAX_PAGES = int(os.getenv("COPYBACK_PAGES") or 200)           # 250 FOMO credits each
MAX_SECONDS = int(os.getenv("COPYBACK_MAX_SECONDS") or 420)   # the workflow sets ~25 min   # stop paging in time to send the email
_DEADLINE = [0.0]


DIAG = {}   # handle -> what the API actually returned (shown in the email, so a blank result explains itself)
OPEN_KEYS = ("createdAt", "openedAt", "created_at", "opened_at", "firstBuyAt", "firstTradeAt", "timestamp", "time")
CLOSE_KEYS = ("closedAt", "closed_at", "lastSellAt", "exitAt")


def _ts(v):
    """ISO text, epoch seconds or epoch milliseconds -> epoch seconds (None if unreadable)."""
    if v in (None, ""):
        return None
    try:
        x = float(v)
        return x / 1000 if x > 1e11 else x
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


MEANING = {"timeout/network:": "the FOMO API took too long to answer (busy, or a very large history) - run it again",
           "500": "the FOMO API had an internal error - run it again later",
           "502": "the FOMO API was temporarily unreachable - run it again later",
           "503": "the FOMO API was temporarily unavailable - run it again later",
           "504": "the FOMO API timed out putting this history together - run it again",
           "401": "your FOMO_API_KEY secret is missing or no longer valid",
           "402": "your FOMO API credits for this month are used up",
           "404": "FOMO doesn't know this handle - check the spelling",
           "429": "too many requests at once (free keys: 20 a minute) - try again in a few minutes"}


def explain(err):
    """Turn the API's raw error into plain words (and keep the raw text for me to read)."""
    code = (err or "").split(" ", 1)[0]
    if not err:
        return "no reply at all from the FOMO API (network problem or the API is down)"
    return f"{MEANING.get(code, 'FOMO API error')} [{err[:160]}]"


def opened(p):
    return next((_ts(p[k]) for k in OPEN_KEYS if p.get(k) not in (None, "")), None)


def closed_at(p):
    return next((_ts(p[k]) for k in CLOSE_KEYS if p.get(k) not in (None, "")), None)


def _positions(d):
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    for k in ("positions", "items", "trades", "results"):
        if isinstance(d.get(k), list):
            return d[k]
    inner = d.get("data")
    return _positions(inner) if inner is not None else []


RETRYABLE = ("429", "500", "502", "503", "504", "timeout/network:")


def _retry_after(err):
    """Seconds the API asked us to wait ("retryAfterSeconds": 25), else 25. Capped at 60."""
    try:
        body = json.loads(err.split(" ", 1)[1])
        return min(60, max(5, int(body.get("retryAfterSeconds") or 25)))
    except (IndexError, ValueError, TypeError, AttributeError):
        return 25


def snapshot(d):
    """The API's saved fallback copy (sent when FOMO itself is too busy): ~50 trades, no paging."""
    return (isinstance(d, dict) and not d.get("nextCursor")
            and bool(d.get("stale") or d.get("capturedAt") or "ageSeconds" in d))


def fetch_page(s, handle, cursor, diag, tries=5):
    """One page of their history. When FOMO is busy the API says "did not answer in time, retry in
    25s" or hands back a saved 50-trade snapshot - so wait as asked and try again a few times."""
    path, last = f"/v2/users/{handle}/positions", None
    for attempt in range(tries):
        if _DEADLINE[0] and time.time() > _DEADLINE[0]:
            break
        # no page-size option: asking for 100 at a time made FOMO's side slower and time out more
        params = {"cursor": cursor} if attempt < tries - 1 or cursor != "start" else None
        d = _get(s, path, 250, params, required=True, timeout=90)
        if d is None:
            err = http.LAST_ERROR[0]
            diag["retries"] = diag.get("retries", 0) + 1
            if not err.startswith(RETRYABLE) or attempt == tries - 1:
                return None
            log.info("%s: FOMO busy (%s) - waiting %ss", handle, err[:40], _retry_after(err))
            time.sleep(_retry_after(err))
            continue
        if snapshot(d) and attempt < tries - 1:
            last = d                                  # keep it in case FOMO never answers live
            diag["retries"] = diag.get("retries", 0) + 1
            time.sleep(20)
            continue
        return d
    if last is not None:
        return last
    return None


CACHE = os.getenv("COPYBACK_CACHE") or "cbcache/history.json"


def load_cache():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(c):
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    with open(CACHE + ".tmp", "w") as f:
        json.dump(c, f)
    os.replace(CACHE + ".tmp", CACHE)


def _key(p):
    return p.get("tradeId") or f"{(p.get('token') or {}).get('address')}|{opened(p)}"


def _closed(page):
    return [p for p in page if str(p.get("status") or "").lower() == "closed" and opened(p)]


def history(s, handle, since, cache=None):
    """Every position they opened since `since`.

    Very active traders (Mickey: ~45 trades a day, 25 per page) need 50+ pages for 30 days and FOMO
    often stalls part-way, so progress is SAVED between runs: each run first fetches their newest
    pages until it meets trades it already has, then carries on further back from where the last
    run stopped. Run it again until the email says "complete"."""
    cache = cache if cache is not None else {}
    diag = {"pages": 0, "fetched": 0, "too_old": 0, "no_date": 0}
    DIAG[handle] = diag
    if chains.wallet_kind(handle):                # a bare wallet address: rebuild it from the chain
        from . import walletback
        ps = walletback.history(s, handle.strip().lstrip("@"), since, cache, diag, _DEADLINE[0])
        save_cache(cache)
        return ps
    ent = cache.get(handle.lower())
    if (not ent or ent.get("since", 0) > since + 3600 or time.time() - ent.get("updated", 0) > 3 * 86400):
        ent = {"positions": {}, "cursor": None, "done": False, "since": since, "updated": time.time()}
    cache[handle.lower()] = ent
    pos = ent["positions"]
    had = bool(pos)
    resume = ent.get("cursor") if not ent.get("done") else None
    cursor, catching_up = "start", had          # catching up = re-reading their newest pages
    for _ in range(MAX_PAGES):
        if _DEADLINE[0] and time.time() > _DEADLINE[0]:
            diag["partial"] = "ran out of time this run"
            break
        d = fetch_page(s, handle, cursor, diag)
        diag["pages"] += 1
        if d is None:
            err = http.LAST_ERROR[0]
            diag["error"] = explain(err)
            if catching_up and resume:
                # their newest page won't load - skip it and keep digging further back, which is what
                # the run is for (the newest trades get picked up next time)
                catching_up, cursor = False, resume
                diag["skipped_newest"] = True
                continue
            if err.startswith(RETRYABLE):
                fails = diag["fails"] = diag.get("fails", 0) + 1
                if fails < 6:                         # FOMO says the bookmark stays valid: rest, retry
                    time.sleep(60)
                    continue
            elif cursor not in ("start", None) and not catching_up:
                ent["cursor"] = None                  # a stale bookmark: start over next run
            break
        diag["fails"] = 0
        if diag["pages"] == 1:
            diag["top_keys"] = sorted(d)[:12] if isinstance(d, dict) else f"list of {len(d)}"
        if isinstance(d, dict) and d.get("available") is False:
            diag["error"] = "FOMO API says this trader's history isn't available"
            break
        if snapshot(d):
            diag["partial"] = ("FOMO was too busy to send their live history, so this is only its saved "
                               "snapshot of their latest ~50 trades")
        page = _positions(d)
        if page and "fields" not in diag:
            diag["fields"] = sorted(page[0])[:25]
        closed = _closed(page)
        overlap = bool(closed) and all(_key(p) in pos for p in closed)
        for p in page:
            pos[_key(p)] = p                          # newest data wins (prices, status)
        nxt = d.get("nextCursor") if isinstance(d, dict) else None
        dates = [opened(p) for p in closed]
        reached_end = not nxt or nxt == cursor or not page or (dates and min(dates) < since)
        if catching_up and (overlap or reached_end):
            catching_up = False
            if ent.get("done") or not resume:         # older part already complete / nothing to resume
                if ent.get("done") or reached_end:
                    break
            else:
                cursor = resume                       # carry on further back from last run
                ent["updated"] = time.time()
                save_cache(cache) if cache is not None else None
                continue
        if reached_end:
            ent["done"], ent["cursor"] = True, None
            break
        cursor = nxt
        if not catching_up:
            ent["cursor"] = nxt                       # bookmark: where the next run continues
        ent["updated"] = time.time()
        if cache is not None:
            save_cache(cache)
    ent["updated"] = time.time()
    save_cache(cache)
    alls = list(pos.values())
    closed_all = [opened(p) for p in _closed(alls)]
    if closed_all:
        diag["back_to"] = min(closed_all)
    diag["complete"] = bool(ent.get("done"))
    if not ent.get("done") and "partial" not in diag:
        diag["partial"] = "not all of the period fetched yet"
    if not ent.get("done"):
        diag["partial"] += " - progress is saved: run the backtest again to continue further back"
    diag["fetched"] = len(alls)
    uniq = []
    for p in alls:
        t = opened(p)
        if t is None:
            diag["no_date"] += 1
        elif t < since:
            diag["too_old"] += 1
        else:
            uniq.append(p)
    return uniq


SKIP = {}   # handle -> {reason: count}
OWN = {}    # handle -> their own real-money result on positions opened in the window


def own_pnl(ps):
    """What THEY made with their own money and their own sizes: realized + unrealized P&L on every
    position they opened in the window (as FOMO reports it), and how much they put in."""
    pnl = inv = 0.0
    n = wins = 0
    for p in ps:
        r, u = _num(p.get("realizedPnlUsd")) or 0.0, _num(p.get("unrealizedPnlUsd")) or 0.0
        pnl += r + u
        inv += _num(p.get("costBasisUsd")) or 0.0
        n += 1
        wins += 1 if r + u > 0 else 0
    return {"pnl": round(pnl, 2), "invested": round(inv, 2), "n": n, "wins": wins}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def copy_trade(p, size, lag, why=None):
    """One of their trades copied with $size. Returns a row, or None if it can't be priced."""
    why = why if why is not None else {}
    p = {k: (_num(v) if k in ("avgEntryPrice", "avgExitPrice", "priceUsd", "costBasisUsd", "boughtAmount",
                                "soldAmount", "transferredInAmount", "realizedPnlUsd", "unrealizedPnlUsd") else v)
         for k, v in p.items()}
    entry = p.get("avgEntryPrice")
    if not entry or entry <= 0:
        why["received, not bought (no entry price)"] = why.get("received, not bought (no entry price)", 0) + 1
        return None
    if (p.get("costBasisUsd") or 0) < 5:
        why["under $5 (dust)"] = why.get("under $5 (dust)", 0) + 1
        return None
    now_px, out_px = p.get("priceUsd"), p.get("avgExitPrice")
    got = (p.get("boughtAmount") or 0) + (p.get("transferredInAmount") or 0)
    sold = (p.get("soldAmount") or 0) / got if got else 0
    status = str(p.get("status") or "").lower()
    is_closed = status == "closed" or (not status and closed_at(p) is not None)
    if is_closed:
        if not out_px:
            why["sent away, not sold (no exit price)"] = why.get("sent away, not sold (no exit price)", 0) + 1
            return None
        exit_px, how = out_px, "closed"
    else:
        if not now_px:
            why["open, no current price"] = why.get("open, no current price", 0) + 1
            return None
        sold = min(max(sold, 0), 1)
        exit_px = (sold * out_px + (1 - sold) * now_px) if (out_px and sold) else now_px
        how = "still open (valued now)"
    t_in = opened(p)
    t_out = closed_at(p) if is_closed else None
    their = (p.get("realizedPnlUsd") or 0) + (p.get("unrealizedPnlUsd") or 0)
    tok = p.get("token") or {}
    return {"symbol": tok.get("symbol") or "?",
            "address": tok.get("address") or tok.get("mint"), "status": how,
            "network": tok.get("network") or tok.get("chain") or p.get("network") or p.get("chain") or "sol",
            "entry": entry, "exit_px": exit_px,
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


def diag_html():
    """What the FOMO API actually gave us per trader - so an empty table explains itself."""
    rows = ""
    for h, d in DIAG.items():
        sk = "; ".join(f"{k}: {v}" for k, v in (SKIP.get(h) or {}).items()) or "-"
        back = (datetime.fromtimestamp(d["back_to"], timezone.utc).strftime("%b %d") if d.get("back_to") else "-")
        state = "<b style='color:#0a7d38'>complete</b>" if d.get("complete") else "in progress"
        rows += (f"<tr><td>{e(chains.who(h))}<br>{state} · back to {back} · {d.get('pages', 0)} pages this run"
                 f"{'<br><b>' + e(d['partial']) + '</b>' if d.get('partial') else ''}</td>"
                 f"<td>{d.get('fetched', 0)}</td><td>{d.get('too_old', 0)}</td>"
                 f"<td>{d.get('no_date', 0)}</td><td>{e(sk)}</td>"
                 f"<td style='font-size:10px'>{e(d.get('error') or '')} {e(str(d.get('top_keys', '')))}<br>"
                 f"{e(', '.join(d.get('fields') or []))}</td></tr>")
    if not rows:
        return ""
    return f"""<h3 style="margin:14px 0 6px;font-size:13px">What the FOMO API returned</h3>
<table style="border-collapse:collapse;font-size:11px;width:100%;color:#555" border="1" cellpadding="3">
<tr><th>Trader</th><th>Trades fetched</th><th>Older than window</th><th>No date</th><th>Skipped</th><th>Details</th></tr>{rows}</table>"""


def build_email(results, size, days, lag):
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    usd = lambda v: f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    col = lambda v: "#0a7d38" if v > 0 else "#b00020" if v < 0 else "#111"
    body_rows, all_rows = "", []
    for h, rows in sorted(results.items(), key=lambda x: -tally(x[1], size)["pnl_lag"] if x[1] else 0):
        if rows is None:
            body_rows += f"<tr><td>{e(chains.who(h))}</td><td colspan='8'>no trade history from the FOMO API</td></tr>"
            continue
        if not rows and DIAG.get(h, {}).get("error"):
            body_rows += (f"<tr><td>{e(chains.who(h))}</td><td colspan='8' style='color:#b36b00'>couldn't get their history: "
                          f"{e(DIAG[h]['error'].split(' [')[0])}</td></tr>")
            continue
        t = tally(rows, size)
        all_rows += rows
        wr = f"{round(100 * t['wins_lag'] / t['n'])}%" if t["n"] else "-"
        best = f"${e(t['best']['symbol'])} {usd(t['best']['pnl'])}" if t["best"] else "-"
        worst = f"${e(t['worst']['symbol'])} {usd(t['worst']['pnl'])}" if t["worst"] else "-"
        part = DIAG.get(h, {}).get("partial")
        flag = "<br><span style='color:#b36b00;font-size:11px'>partial data - see below</span>" if part else ""
        o = OWN.get(h) or {}
        own = (f"<b style='color:{col(o['pnl'])}'>{usd(o['pnl'])}</b><br><span style='font-size:11px;color:#888'>"
               f"on ${o['invested']:,.0f} put in · {o['n']} positions</span>") if o else "-"
        body_rows += (f"<tr><td>{e(chains.who(h))}{flag}</td><td>{own}</td><td>{t['n']}</td><td>{wr}</td>"
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
<tr style="background:#f3f3f3"><th>Trader</th><th>Their own P&amp;L<br><span style="font-weight:normal;font-size:11px">(their money, their sizes)</span></th><th>Trades</th><th>Winners</th><th>Exact copy</th><th>With copy lag</th>
<th>Money needed</th><th>Best</th><th>Worst</th></tr>{body_rows}
<tr style="background:#f9f9f9"><td><b>All of them</b></td><td>{usd(sum(o['pnl'] for o in OWN.values()))}</td><td>{T['n']}</td><td>-</td>
<td style='color:{col(T['pnl'])}'>{usd(T['pnl'])}</td><td style='color:{col(T['pnl_lag'])}'><b>{usd(T['pnl_lag'])}</b></td>
<td>${T['capital']:,.0f}</td><td colspan="2">{T['big']} trades doubled or better</td></tr></table>
<p style="font-size:13px">Fees alone on {T['n']} copied trades: at least ${T['fees']:,.2f} (FOMO's $0.95 minimum each way on
${size:.0f} trades). "Money needed" is the most ${size:.0f} positions you'd have had open at the same time - if you don't have
that much, you'd have missed some of their trades.</p>
<h3 style="margin:14px 0 6px">Biggest copied trades (with copy lag)</h3>
<table style="border-collapse:collapse;font-size:12px;width:100%" border="1" cellpadding="3">
<tr style="background:#f3f3f3"><th>Coin</th><th>Trader</th><th>Opened (UTC)</th><th>Their move</th><th>Your ${size:.0f}</th><th></th></tr>{trows}</table>
{diag_html()}
<p style="font-size:11px;color:#888">Their FOMO-app trades only, at average prices. Coins they received or sent instead of
buying/selling are skipped. One month is a short sample - the live paper copy books in your digest are the real test.
Full data: out/copyback.csv in this run's Artifacts. Not financial advice.</p></body></html>"""
    subject = f"Copy backtest ({days}d, ${size:.0f}/trade): {usd(T['pnl_lag'])} with copy lag across {T['n']} trades"
    if len(OWN) == 1:
        (h1, o1), = OWN.items()
        subject += f" · {chains.who(h1)} made {usd(o1['pnl'])} themselves"
    return subject, body


def run(handles=None, days=None):
    _DEADLINE[0] = time.time() + MAX_SECONDS
    handles = [h.strip().lstrip("@") for h in (handles or ",".join(config.COPY_TRADERS)).split(",") if h.strip()]
    days = int(days or config.COPY_BACKTEST_DAYS)
    size, lag = config.COPY_SIZE_USD, config.COPY_LAG_PCT
    since = time.time() - days * 86400
    s = st.load()
    cache = load_cache()
    results = {}
    for h in handles:
        try:
            ps = history(s, h, since, cache)
        except Exception:
            log.exception("history failed for %s", h)
            ps = None
        if ps is None:
            results[h] = None
            continue
        rows, why = [], SKIP.setdefault(h, {})
        for p in ps:
            r = copy_trade(p, size, lag, why)
            if r:
                r["trader"] = h
                rows.append(r)
        results[h] = rows
        OWN[h] = own_pnl(ps)
        log.info("%s: %d positions, %d copyable, own P&L %s", h, len(ps), len(rows), OWN[h])
        time.sleep(3)                                     # stay under 20 requests a minute
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
