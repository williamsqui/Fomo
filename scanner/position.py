"""Hold or sell? Check a coin you already own and email a clear verdict.

    python -m scanner.position <address> <gain % shown in FOMO> [position $] [chain]

Uses the same live analysis as "Check a coin" (score, chart, safety, top-100 holders)
plus your own gain/loss, then applies simple, fee-aware exit rules.
"""
import html
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import chains, check, config, report, state as st, watchlist
from .sizing import fmt

log = logging.getLogger("position")
e = html.escape

HOLD, TRIM, SELL, TAKE = "HOLD", "HOLD - TIGHTEN STOP", "SELL", "TAKE PROFIT"
COLOR = {HOLD: "#0a7d38", TRIM: "#b36b00", SELL: "#b00020", TAKE: "#0a5bd6"}


def _num(v):
    try:
        return float(str(v).replace("%", "").replace("$", "").replace(",", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def decide(r, pnl, value=None):
    """Return (verdict, headline, plus[], minus[], levels{}) for a held coin.

    r: analysis result from check.analyze;  pnl: your gain/loss % (e.g. 37.7 or -6.4);
    value: current position value in $ (optional, for fee advice).
    """
    m, sm, ch = r["market"], r["smart"], r.get("chart") or {}
    price = m["price"]
    entry = price / (1 + pnl / 100) if pnl is not None and pnl > -99 else None
    plus, minus = [], []

    # ---- evidence -------------------------------------------------------
    health = r["score"]
    verdict_txt = ch.get("verdict", "")
    if "uptrend" in verdict_txt:
        health += 8
        plus.append("chart still in an uptrend")
    elif "downtrend" in verdict_txt:
        health -= 12
        minus.append("chart has turned into a downtrend")
    live = r.get("live_holders")
    exited = r.get("exited") or []
    if sm["buyers"]:
        if live:
            plus.append(f"{len(sm['buyers'])} top-100 trader(s) still hold it right now (checked live)")
            health += 4
        else:
            # couldn't read the wallets, so this is the scanner's log, not proof they still hold
            plus.append(f"{len(sm['buyers'])} top-100 trader(s) bought it recently "
                        "(live wallet check unavailable - not confirmed they still hold)")
            health += 2
    if live and exited:
        health -= 8
        minus.append(f"{len(exited)} top-100 trader(s) who bought it earlier hold none of it now - they sold out")
    if live and not sm["buyers"] and not exited:
        minus.append("no top-100 trader holds it - you're on your own in this one")
    if sm.get("trimmed"):
        # took some profit but still holds a real position - normal, not a reason to run
        minus.append(f"trimmed a little but still holding: {', '.join(sm['trimmed'][:3])}")
    if sm["sellers"]:
        health -= 15
        minus.append(f"top-100 trader(s) selling: {', '.join(sm['sellers'][:3])}")
    ch1, ch6 = m["change"].get("h1", 0), m["change"].get("h6", 0)
    ratio = m["buys_h1"] / (m["sells_h1"] + 1)
    if ch1 < -10 and ratio < 1:
        health -= 8
        minus.append(f"selling pressure now: {ch1:+.0f}% in 1h, {m['buys_h1']} buys vs {m['sells_h1']} sells")
    elif ch1 > 3 and ratio > 1.2:
        plus.append(f"buyers in control: {ch1:+.0f}% in 1h, {m['buys_h1']} buys vs {m['sells_h1']} sells")
    if ch6 < -25:
        health -= 5
        minus.append(f"down {ch6:.0f}% over 6h")
    for f in r["flags"][:3]:
        if f not in " ".join(minus):
            minus.append(f)
    for g in r["reasons"][:4]:
        if len(plus) < 5 and "top-100 FOMO trader" not in g and "buy pressure" not in g:
            plus.append(g)

    # ---- levels -----------------------------------------------------------
    target = entry * (1 + config.TARGET_GAIN_PCT / 100) if entry else price * 1.5
    support = ch.get("support")
    stop = entry * (1 - config.STOP_LOSS_PCT / 100) if entry else price * (1 - config.STOP_LOSS_PCT / 100)
    if support and price * 0.6 < support < price:
        stop = max(stop, support * 0.97)
    if entry and pnl >= 25:
        stop = max(stop, entry * 1.02)          # in good profit: never let it turn into a loss
    levels = {"entry": entry, "price": price, "target": target, "stop": stop}

    # ---- rules -------------------------------------------------------------
    small = value is not None and value < 60
    if r.get("hard"):
        return SELL, "Sell - it now fails a safety check: " + "; ".join(r["hard"]), plus, minus, levels
    if pnl is not None and pnl <= -config.STOP_LOSS_PCT:
        return (SELL, f"Sell - you're past your -{config.STOP_LOSS_PCT:.0f}% stop-loss. "
                "Cutting here protects the rest of your bankroll.", plus, minus, levels)
    if pnl is not None and pnl >= config.TARGET_GAIN_PCT:
        if health >= 70 and not sm["sellers"] and not small:
            return (TAKE, f"Take profit on half - you hit +{config.TARGET_GAIN_PCT:.0f}%. Setup is still strong, "
                    "so let the other half run with your stop at break-even.", plus, minus, levels)
        return (TAKE, f"Take profit - you hit your +{config.TARGET_GAIN_PCT:.0f}% target"
                + (" and the setup is weakening." if health < 70 or sm["sellers"] else ".")
                + (" Your position is small, so sell it all rather than paying fees twice." if small else ""),
                plus, minus, levels)
    if sm["sellers"] and health < 55:
        return SELL, "Sell - the top traders who were in it are getting out and the setup is weakening.", plus, minus, levels
    if health >= 60:
        return HOLD, "Hold - the setup still looks healthy. Keep your stop in place.", plus, minus, levels
    if health >= 42:
        tip = " You're in profit, so consider taking some off." if pnl and pnl >= 20 and not small else ""
        return TRIM, "Hold, but tighten your stop - signals are mixed." + tip, plus, minus, levels
    if pnl is not None and pnl > -8 and small and "downtrend" not in verdict_txt and not sm["sellers"]:
        return (TRIM, "Weak setup, but you're near break-even on a small position - selling now mostly pays fees. "
                "Hold with a tight stop, or exit if it breaks below the stop.", plus, minus, levels)
    return SELL, "Sell - the setup has weakened a lot and there's no strong reason left to hold.", plus, minus, levels


def build_email(r, holders, pnl, value, verdict, headline, plus, minus, levels):
    m = r["market"]
    c = COLOR[verdict]
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    pnl_txt = f"{pnl:+.1f}%" if pnl is not None else "not given"
    val_txt = f" · position ≈${value:,.2f}" if value else ""
    lv = levels
    rows = "".join(f"<tr><td>{k}</td><td>{fmt(v)}</td><td>{d}</td></tr>" for k, v, d in (
        ("Your entry (est.)", lv["entry"], ""),
        ("Price now", lv["price"], ""),
        ("Target (+%d%%)" % config.TARGET_GAIN_PCT, lv["target"], f"{(lv['target'] / lv['price'] - 1) * 100:+.0f}% from here"),
        ("Stop-loss", lv["stop"], f"{(lv['stop'] / lv['price'] - 1) * 100:+.0f}% from here"),
    ) if v)
    fee = ""
    if value:
        cost = max(config.FEE_MIN_USD, value * config.FEE_PCT / 100)
        fee = f"<p style='font-size:12px;color:#555'>Selling now costs about ${cost:.2f} in FOMO fees ({cost / value * 100:.1f}% of this position).</p>"
    if holders:
        hold_txt = ", ".join(f"{h['handle']} (#{h['rank']}, ${h['usd']:,.0f})" for h in holders[:6])
    elif r.get("live_holders"):
        hold_txt = "none of the top 100 (every one of their wallets was checked just now)"
    else:
        hold_txt = "unknown - their wallets could not be read this run, so treat the score as less certain"
    if r.get("exited"):
        hold_txt += f" · sold out since buying: {', '.join(r['exited'][:6])}"
    pl = "".join(f"<div style='color:#0a7d38'>+ {e(x)}</div>" for x in plus[:5]) or "<div>+ nothing stands out</div>"
    mi = "".join(f"<div style='color:#b00020'>− {e(x)}</div>" for x in minus[:5]) or "<div>− no major warning signs</div>"
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2 style="margin:4px 0">Position check: ${e(m['symbol'])}</h2>
<p style="color:#555;font-size:13px;margin:0 0 10px">{chains.LABEL[m['chain']]} · checked live {now} · your gain {pnl_txt}{val_txt}</p>
<div style="border-left:6px solid {c};background:#f7f7f7;border-radius:8px;padding:12px;margin-bottom:12px">
  <div style="font-size:20px;font-weight:700;color:{c}">{verdict}</div>
  <div style="font-size:14px;margin-top:4px">{e(headline)}</div>
  <div style="font-size:12px;color:#555;margin-top:6px">Coin score now: {r['score']}/100 · chart: {e((r.get('chart') or {}).get('verdict', 'n/a'))}</div>
</div>
<div style="font-size:13px;font-weight:600">For holding</div><div style="font-size:13px;margin:2px 0 8px">{pl}</div>
<div style="font-size:13px;font-weight:600">Against holding</div><div style="font-size:13px;margin:2px 0 8px">{mi}</div>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Level</th><th>Price</th><th></th></tr>{rows}</table>
{fee}
<p style="font-size:13px"><b>Top-100 traders holding it now:</b> {e(hold_txt)}</p>
<p style="font-size:13px"><a href="{e(m['url'])}">Chart</a> · <span style="font-family:monospace;font-size:11px">{e(m['address'])}</span></p>
<p style="font-size:12px;color:#555">Want to dig deeper? Paste this email into your Claude chat and ask follow-up questions.</p>
<p style="font-size:11px;color:#888">Rules-based guidance from live data, not financial advice. Prices move fast - check the chart before acting.</p>
</body></html>"""
    return f"Position check: ${m['symbol']} - {verdict} ({pnl_txt})", body


def stop_watching(address, chain="auto"):
    """'sold' typed in the gain box: stop watching this coin."""
    m = check.find_market(address, chain)
    key = m["key"] if m else chains.key("solana" if not address.startswith("0x") else "base", address)
    watchlist.request_remove(key)
    sym = f"${m['symbol']}" if m else address[:10] + "..."
    subject = f"Stopped watching {sym}"
    report.send(subject, f"<p>Got it - you sold {e(sym)}. No more watchlist alerts for it "
                         f"(takes effect on the next scan, within 10 minutes).</p>")
    print(subject)
    return "SOLD"


WATCH_NOTE = ("<div style='background:#f3f6ff;border-radius:6px;padding:8px;font-size:12px;margin:8px 0'>"
              "<b>Now on your watchlist for {days:.0f} days.</b> Every 10 minutes the scanner checks whether the top traders "
              "holding it sell out, whether liquidity is being pulled, and your stop-loss and target - and emails you if any "
              "of them happen. When you sell, run Check my position again and type <b>sold</b> in the gain box.</div>")


def run(address, pnl, value=None, chain="auto", raw_gain=""):
    if str(raw_gain).strip().lower().startswith("sold"):
        return stop_watching(address, chain)
    s = st.load()
    m = check.find_market(address, chain)
    if not m:
        return check.not_found(address, "Position check")
    r, holders = check.analyze(s, m)
    verdict, headline, plus, minus, levels = decide(r, pnl, value)
    subject, body = build_email(r, holders, pnl, value, verdict, headline, plus, minus, levels)
    try:
        watchlist.request_add(r, holders, levels)
        body = body.replace("<p style=\"font-size:12px;color:#555\">Want to dig deeper?",
                            WATCH_NOTE.format(days=config.WATCH_DAYS)
                            + "<p style=\"font-size:12px;color:#555\">Want to dig deeper?", 1)
    except OSError as ex:           # never lose the verdict email over the watchlist
        log.warning("couldn't add to watchlist: %s", ex)
    report.send(subject, body)
    os.makedirs("out", exist_ok=True)
    with open("out/position.html", "w") as f:
        f.write(body)
    print(subject)
    return verdict


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
    a = sys.argv[1:] + ["", "", "", ""]
    if not a[0].strip():
        sys.exit("usage: python -m scanner.position <address> <gain % | sold> [position $] [chain]")
    run(a[0].strip(), _num(a[1]), _num(a[2]), (a[3] or "auto").strip().lower(), raw_gain=a[1])
