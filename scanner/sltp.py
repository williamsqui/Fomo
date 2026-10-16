"""Stop-loss / take-profit backtest for a copy-trading bot (CopyFomo-style settings).

    python -m scanner.sltp [handle,handle,...] [days]   (GitHub: Actions -> "Stop-loss / take-profit backtest")

For every trade the trader opened in the window (same history as the copy backtest, shared cache):
  * you buy $COPY_SIZE_USD at their entry
  * each setting below is replayed on 15-minute price candles (GeckoTerminal) between their buy
    and their sell: the stop sells everything when a candle's low touches it, take-profit rungs sell
    part of the position when a candle's high reaches them, and after the first take-profit the stop
    moves to breakeven (CopyFomo's default). Whatever is left is sold when they sold.
  * fees: COPYBOT_FEE_PCT (CopyFomo 2%) on every buy and sell, plus 1% slippage each way.
    A second column also adds FOMO's own fee ($0.95 min / 0.5%) on each transaction, in case the
    bot routes through FOMO and you pay both.
  * conservative: if one candle touches both the stop and a take-profit, the stop wins; a stop that
    gaps fills at the candle's open, not at the stop price.
Trades with no usable candles keep their plain "follow their exits" result and are counted.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import chains, chart, config, copyback, dex, report, state as st

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("sltp")
e = report.e

FEE_PCT = float(os.getenv("COPYBOT_FEE_PCT") or 2)
SLIP = 1.0
CANDLE_MIN = 15
CANDLES = os.getenv("SLTP_CANDLES") or "cbcache/candles.json"
MAX_SECONDS = int(os.getenv("SLTP_MAX_SECONDS") or 1500)

# (name, stop %, [(take-profit %, share of the ORIGINAL position to sell, or "all")], breakeven after TP)
STRATEGIES = [
    ("Follow their exits only (no SL/TP)", None, [], False),
    ("Stop -25%", -25, [], False),
    ("Stop -50%", -50, [], False),
    ("Stop -80%", -80, [], False),
    ("TP +50% sell half", None, [(50, 0.5)], True),
    ("TP +100% sell half", None, [(100, 0.5)], True),
    ("TP +100% sell all", None, [(100, "all")], False),
    ("TP +300% sell half", None, [(300, 0.5)], True),
    ("Ladder +50% 25% / +100% 50% / +300% rest", None, [(50, 0.25), (100, 0.5), (300, "all")], True),
    ("Stop -25% + TP +50% sell half", -25, [(50, 0.5)], True),
    ("Stop -50% + TP +100% sell half", -50, [(100, 0.5)], True),
    ("Stop -50% + TP +100% half (keep -50% stop)", -50, [(100, 0.5)], False),
    ("Stop -50% + ladder 50/100/300", -50, [(50, 0.25), (100, 0.5), (300, "all")], True),
    ("Stop -80% + TP +300% sell half", -80, [(300, 0.5)], True),
]


def fomo_fee(usd):
    return max(config.FEE_MIN_USD, usd * config.FEE_PCT / 100)


def simulate(entry, final_px, candles, size, stop=None, tps=(), be=False, lag=0.0, stack_fomo=False):
    """One copied trade. candles: [[ts, o, h, l, c], ...] after the buy, up to their sell.
    Returns (pnl, what happened)."""
    slip = SLIP / 100
    buy_fee = size * FEE_PCT / 100 + (fomo_fee(size) if stack_fomo else 0)
    fill_in = entry * (1 + lag / 100) * (1 + slip)
    tokens = (size - buy_fee) / fill_in
    left, cash, events = tokens, 0.0, []

    def sell(qty, px):
        nonlocal left, cash
        qty = min(qty, left)
        if qty <= 0:
            return
        gross = qty * px * (1 - slip)
        cash += gross - gross * FEE_PCT / 100 - (fomo_fee(gross) if stack_fomo else 0)
        left -= qty

    stop_px = fill_in * (1 + stop / 100) if stop is not None else None
    rungs = [(fill_in * (1 + p / 100), share, p) for p, share in tps]
    hit = set()
    for ts, o, h, l, c in candles:
        if left <= tokens * 1e-9:
            break
        if stop_px is not None and l <= stop_px:
            sell(left, min(stop_px, o))
            events.append("stop" if not hit else "stop after TP")
            break
        for i, (lvl, share, p) in enumerate(rungs):
            if i not in hit and h >= lvl:
                sell(left if share == "all" else tokens * share, lvl)
                hit.add(i)
                events.append(f"TP+{p}%")
                if be and (stop_px is None or stop_px < fill_in):
                    stop_px = fill_in
    if left > tokens * 1e-9:
        sell(left, final_px * (1 - lag / 100))
    return round(cash - size, 2), events


# ---- price candles ------------------------------------------------------------------------------

def load_candles():
    try:
        with open(CANDLES) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_candles(c):
    os.makedirs(os.path.dirname(CANDLES) or ".", exist_ok=True)
    with open(CANDLES, "w") as f:
        json.dump(c, f)


def chain_of(row):
    return chains.FOMO_NET.get(str(row.get("network") or "sol").lower().strip(), "solana")


def pools(rows):
    """token key -> its biggest pool now (DexScreener, batched)."""
    keys = [chains.key(chain_of(r), r["address"]) for r in rows if r.get("address")]
    try:
        return {k: v["pair"] for k, v in dex.tokens(keys).items() if v.get("pair")}
    except Exception:
        log.exception("pool lookup failed")
        return {}


def fetch_candles(chain, pool, t_from, t_to):
    """15-minute candles covering t_from..t_to (pages back, 1000 candles = ~10 days a call)."""
    out, before = {}, int(t_to) + CANDLE_MIN * 60
    for _ in range(4):
        d = chart._gt(f"/networks/{chains.CHAIN[chain]['gt']}/pools/{pool}/ohlcv/minute",
                      {"aggregate": CANDLE_MIN, "limit": 1000, "currency": "usd", "before_timestamp": before})
        try:
            rows = d["data"]["attributes"]["ohlcv_list"]
        except (TypeError, KeyError):
            return None if not out else sorted(out.values())
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = [float(x) for x in r[:5]]
        first = min(int(r[0]) for r in rows)
        if first <= t_from or len(rows) < 1000:
            break
        before = first
    return sorted(out.values())


def window(cs, t_in, t_out):
    """Candles that START after the buy (the buy candle's low may be from before they bought) and
    before the sell."""
    return [c for c in cs if t_in <= c[0] < t_out]


def usable(entry, cs, t_in):
    """Throw out candles that clearly aren't in the same units as the trade's price."""
    if not cs:
        return False
    near = min(cs, key=lambda c: abs(c[0] - t_in))
    if abs(near[0] - t_in) > 6 * 3600:
        return True                       # e.g. bought on the launch curve before this pool existed
    return 1 / 3 <= near[1] / entry <= 3


# ---- report -------------------------------------------------------------------------------------

def tally(results):
    out = []
    for name, stop, tps, be in STRATEGIES:
        rows = results[name]
        pnl = sum(r["pnl"] for r in rows)
        out.append({"name": name, "pnl": round(pnl, 2), "lag": round(sum(r["lag"] for r in rows), 2),
                    "both": round(sum(r["both"] for r in rows), 2),
                    "wins": sum(1 for r in rows if r["pnl"] > 0), "n": len(rows),
                    "stops": sum(1 for r in rows if any(x.startswith("stop") for x in r["ev"])),
                    "tps": sum(1 for r in rows if any(x.startswith("TP") for x in r["ev"])),
                    "best": max(rows, key=lambda r: r["pnl"]) if rows else None,
                    "worst": min(rows, key=lambda r: r["pnl"]) if rows else None})
    return out


def build_email(handles, days, size, tab, results, nocandle, n):
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    usd = lambda v: f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    col = lambda v: "#0a7d38" if v > 0 else "#b00020" if v < 0 else "#111"
    base = tab[0]
    ranked = sorted(tab, key=lambda t: -t["pnl"])
    rows = ""
    for t in ranked:
        hl = " style='background:#eefaf1'" if t is ranked[0] else (" style='background:#f6f6f6'" if t is base else "")
        diff = t["pnl"] - base["pnl"]
        rows += (f"<tr{hl}><td>{e(t['name'])}</td><td style='color:{col(t['pnl'])}'><b>{usd(t['pnl'])}</b><br>"
                 f"<span style='font-size:11px;color:#888'>{'baseline' if t is base else usd(diff) + ' vs baseline'}</span></td>"
                 f"<td style='color:{col(t['lag'])}'>{usd(t['lag'])}</td><td style='color:{col(t['both'])}'>{usd(t['both'])}</td>"
                 f"<td>{round(100 * t['wins'] / t['n']) if t['n'] else 0}%</td><td>{t['stops']}</td><td>{t['tps']}</td>"
                 f"<td style='font-size:11px'>{('$' + e(t['worst']['symbol']) + ' ' + usd(t['worst']['pnl'])) if t['worst'] else '-'}</td></tr>")
    # what the top setting changed, trade by trade
    top = ranked[0]["name"]
    if top == base["name"] and len(ranked) > 1:
        top = ranked[1]["name"]
    pairs = list(zip(results[base["name"]], results[top]))
    moved = sorted(pairs, key=lambda p: -abs(p[1]["pnl"] - p[0]["pnl"]))[:10]
    mrows = "".join(f"<tr><td>${e(a['symbol'])}</td><td>{e(a['trader'])}</td><td>{usd(a['pnl'])}</td>"
                    f"<td>{usd(b['pnl'])}</td><td style='color:{col(b['pnl'] - a['pnl'])}'>{usd(b['pnl'] - a['pnl'])}</td>"
                    f"<td style='font-size:11px'>{e(', '.join(b['ev']) or '-')}</td></tr>" for a, b in moved)
    who = ", ".join(chains.who(h) for h in handles)
    body = f"""<html><body style="font-family:Arial,sans-serif;max-width:760px">
<h2 style="margin:0 0 4px">Stop-loss / take-profit backtest: {e(who)}, ${size:.0f} per trade, last {days} days</h2>
<p style="font-size:13px;color:#555">{now} · {n} copied trades. Each setting is replayed on {CANDLE_MIN}-minute price candles between
their buy and their sell. After the first take-profit the stop moves to breakeven (CopyFomo's default) unless the name says otherwise.
Fees: <b>{FEE_PCT:g}% bot fee on every buy and sell</b> + 1% slippage each way. Ranked by the main column.</p>
<table style="border-collapse:collapse;font-size:12px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Setting</th><th>P&amp;L<br><span style="font-weight:normal;font-size:11px">bot copies within seconds</span></th>
<th>With 3% copy lag</th><th>If FOMO's fee is<br>charged too</th><th>Winners</th><th>Stops hit</th><th>TPs hit</th><th>Worst trade</th></tr>{rows}</table>
<h3 style="margin:14px 0 6px;font-size:14px">Where "{e(top)}" made or lost money vs following their exits</h3>
<table style="border-collapse:collapse;font-size:12px;width:100%" border="1" cellpadding="3">
<tr style="background:#f3f3f3"><th>Coin</th><th>Trader</th><th>Follow exits</th><th>This setting</th><th>Difference</th><th>What happened</th></tr>{mrows}</table>
<p style="font-size:12px;color:#555">{nocandle} of {n} trades had no usable price candles (coin delisted, or bought on the launch curve
before a pool existed) - those keep their "follow their exits" result in every row. Candles show the highs and lows within each
{CANDLE_MIN} minutes, so a stop or target that was touched for a few seconds counts; real fills on thin coins can be worse.
"Take-profit sells X" means X of the original position. One month is a short sample: a setting that wins here because of one coin
may not win next month. Full data: out/sltp.csv in this run's Artifacts. Not financial advice.</p></body></html>"""
    best = ranked[0]
    subject = (f"SL/TP backtest ({days}d, {who}): best = {best['name']} {usd(best['pnl'])} "
               f"vs {usd(base['pnl'])} following their exits")
    return subject, body


def run(handles=None, days=None):
    t0 = time.time()
    copyback._DEADLINE[0] = t0 + min(copyback.MAX_SECONDS, MAX_SECONDS // 2)
    handles = [h.strip().lstrip("@") for h in (handles or "bystevenr").split(",") if h.strip()]
    days = int(days or config.COPY_BACKTEST_DAYS)
    size = config.COPY_SIZE_USD
    since = time.time() - days * 86400
    s, cache = st.load(), copyback.load_cache()
    trades = []
    for h in handles:
        try:
            ps = copyback.history(s, h, since, cache) or []
        except Exception:
            log.exception("history failed for %s", h)
            ps = []
        for p in ps:
            r = copyback.copy_trade(p, size, config.COPY_LAG_PCT)
            if r and r.get("t_in"):
                r["trader"] = h
                trades.append(r)
        time.sleep(3)
    trades.sort(key=lambda r: r["t_in"])
    ccache = load_candles()
    pool_of = pools([r for r in trades if f"{r['address']}:{r['t_in']}" not in ccache])
    nocandle, deadline = 0, t0 + MAX_SECONDS - 90
    results = {name: [] for name, *_ in STRATEGIES}
    for r in trades:
        ck = f"{r['address']}:{r['t_in']}"
        t_out = r["t_out"] or time.time()
        cs = ccache.get(ck)
        if cs is None and r.get("address") and time.time() < deadline:
            k = chains.key(chain_of(r), r["address"])
            pool = pool_of.get(k)
            cs = (fetch_candles(chain_of(r), pool, r["t_in"], t_out) if pool
                  else None if k in dex.FAILED else [])          # lookup failed: try again next run
            if cs is not None:
                ccache[ck] = cs
        cs = window(cs or [], r["t_in"], t_out)
        ok = usable(r["entry"], cs, r["t_in"])
        nocandle += 0 if ok else 1
        for name, stop, tps, be in STRATEGIES:
            use = cs if ok else []
            a = (stop, tps if ok else (), be)
            pnl, ev = simulate(r["entry"], r["exit_px"], use, size, *a)
            lag, _ = simulate(r["entry"], r["exit_px"], use, size, *a, lag=config.COPY_LAG_PCT)
            both, _ = simulate(r["entry"], r["exit_px"], use, size, *a, stack_fomo=True)
            results[name].append({"symbol": r["symbol"], "trader": r["trader"], "opened": r["opened"],
                                  "pnl": pnl, "lag": lag, "both": both, "ev": ev, "candles": ok})
    save_candles(ccache)
    tab = tally(results)
    os.makedirs("out", exist_ok=True)
    with open("out/sltp.csv", "w") as f:
        f.write("setting,trader,symbol,opened,pnl,pnl_lag,pnl_with_fomo_fee,events,had_candles\n")
        for name, rs in results.items():
            for x in rs:
                f.write(f"\"{name}\",{x['trader']},\"{x['symbol']}\",{x['opened']},{x['pnl']},{x['lag']},"
                        f"{x['both']},\"{' '.join(x['ev'])}\",{x['candles']}\n")
    subject, body = build_email(handles, days, size, tab, results, nocandle, len(trades))
    with open("out/sltp.html", "w") as f:
        f.write(body)
    report.send(subject, body)
    print(subject)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None, sys.argv[2] if len(sys.argv) > 2 else None)
