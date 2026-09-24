"""Email: up to 3 ranked picks with confidence, $ amount, exit plan, reasons, risks."""
import html
import logging
import time
import smtplib
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

from . import chains, config, copy as copybook, tracker, traders as reputation, watchlist
from .sizing import fmt
from .traders import tag

log = logging.getLogger("report")
TIER_COLOR = {"HIGH": "#0a7d38", "MEDIUM": "#b36b00", "LOW": "#6b6b6b"}
e = html.escape
MEDAL = ["#1", "#2", "#3", "#4", "#5"]


def _money(v):
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}k" if v >= 1e3 else f"${v:.0f}"


def card(i, r, summ, alerted_before):
    m = r["market"]
    c = TIER_COLOR[r["tier"]]
    ch = m["change"]
    cal = tracker.calibrated(summ, r["score"]) or "Not enough history yet to turn this into a real hit rate."
    reasons = "".join(f"<li>{e(x)}</li>" for x in r["reasons"][:9])
    flags = "".join(f"<li style='color:#b00020'>⚠ {e(x)}</li>" for x in r["flags"][:5])
    chart = r.get("chart") or {}
    xs = f"https://x.com/search?q={quote(m['address'])}&f=live"
    explorer = chains.CHAIN[m["chain"]]["explorer"] + m["address"]
    if r.get("size"):
        money = (f"<div style='background:#eef7f0;border-radius:6px;padding:8px;margin:8px 0;font-size:14px'>"
                 f"<b>Suggested: ${r['size']}</b> at ≈{fmt(m['price'])}<br>{e(r['size_note'])}<br>"
                 f"<b>Plan:</b> {e(r['exit'])}</div>")
    else:
        money = f"<div style='background:#f6f6f6;border-radius:6px;padding:8px;margin:8px 0;font-size:14px'>{e(r['size_note'])}</div>"
    again = " · <i>you were alerted earlier - still looks good</i>" if alerted_before else ""
    tz = ZoneInfo(config.TIMEZONE)
    fresh = []
    if r.get("checked_at"):
        fresh.append("price checked live at " + datetime.fromtimestamp(r["checked_at"], tz).strftime("%H:%M"))
    sm = r["smart"]
    if sm.get("last_buy"):
        fresh.append(f"latest top-trader buy {int((time.time() - sm['last_buy']) / 60)} min ago")
    if r.get("runup") is not None:
        fresh.append(f"{r['runup']:+.0f}% since they started buying")
    fresh = " · ".join(fresh)
    return f"""
<div style="border:1px solid #ddd;border-left:6px solid {c};border-radius:8px;padding:12px;margin:0 0 14px">
  <div style="font-size:18px;font-weight:700">{MEDAL[i]} ${e(m['symbol'])}
    <span style="font-size:12px;color:#555;font-weight:400">{chains.LABEL[m['chain']]}</span>
    <span style="float:right;color:{c}">{r['score']}/100</span></div>
  <div style="color:{c};font-size:13px;font-weight:600">{r['tier']} confidence{again}</div>
  <div style="color:#555;font-size:12px;margin:2px 0 4px">{e(cal)}</div>
  {f'<div style="color:#0a5bd6;font-size:12px;margin:0 0 4px">{e(fresh)}</div>' if fresh else ''}
  <div style="color:#333;font-size:13px">MC {_money(m['mcap'])} · Liq {_money(m['liquidity'])} ·
    1h {ch.get('h1', 0):+.0f}% · 24h {ch.get('h24', 0):+.0f}% · Chart: {e(chart.get('verdict', 'n/a'))}</div>
  {money}
  <div style="font-size:13px;font-weight:600;margin-top:6px">Why</div>
  <ul style="margin:4px 0 6px 18px;padding:0;font-size:13px">{reasons}</ul>
  {f"<div style='font-size:13px;font-weight:600'>Risks</div><ul style='margin:4px 0 6px 18px;padding:0;font-size:13px'>{flags}</ul>" if flags else ""}
  <div style="font-size:13px"><a href="{e(m['url'])}">Chart</a> · <a href="{xs}">X posts</a> · <a href="{e(explorer)}">Contract</a>
    {f' · <a href="{e(m["telegram"])}">Telegram</a>' if m.get('telegram') else ''}</div>
  <div style="font-family:monospace;font-size:11px;color:#666;word-break:break-all;margin-top:4px">{e(m['address'])}</div>
</div>"""


def track_table(summ, hits):
    label = {"emailed": "Emailed to you"}
    rows = "".join(
        f"<tr><td>{label.get(name, name)}</td><td>{v['n']}</td><td>{v['hits']}</td>"
        f"<td>{'-' if v['rate'] is None else str(v['rate']) + '%'}</td><td>{v.get('open', 0)}</td></tr>"
        for name, v in summ.items() if name not in ("paper", "learn"))
    hit_list = ", ".join(f"${e(p['symbol'])} +{(p['max'] / p['entry'] - 1) * 100:.0f}%" for p in hits) or "none yet"
    return f"""<h3 style="margin:18px 0 6px">Scanner track record (14 days, +{config.TARGET_GAIN_PCT:.0f}% within {config.TRACK_WINDOW_HOURS}h)</h3>
<p style="font-size:12px;color:#555;margin:0 0 6px">Every coin the scanner scored 50+, whether it was emailed or not - not your own trades.
A coin only counts once its full {config.TRACK_WINDOW_HOURS}h has passed, so hits and misses are judged the same way. Highs must show on two scans in a row.</p>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Score band</th><th>Judged</th><th>Hit +{config.TARGET_GAIN_PCT:.0f}%</th><th>Hit rate</th><th>Still in 48h</th></tr>{rows}</table>
<p style="font-size:13px">Recent hits: {hit_list}</p>{paper_table(summ.get("paper"))}{learn_box(summ.get("learn"))}"""


def watch_table(s):
    """Every coin being watched and when the scanner last looked - so you can see it working."""
    rows = watchlist.status(s)
    if not rows:
        return ""
    out = ""
    for r in rows:
        when = "not yet" if r["ago"] is None else f"{r['ago']} min ago"
        if r["ok"] is False:
            when += " (wallet read failed, retrying)"
        still = "-" if r["still_in"] is None else f"{r['still_in']} of {r['tracked']}"
        chg = "-" if r["chg"] is None else f"{r['chg']:+.0f}%"
        out += (f"<tr><td>${e(r['symbol'])}</td><td>{r['source']}</td><td>{when}</td>"
                f"<td>{still}</td><td>{chg}</td></tr>")
    return (f"<h3 style='margin:18px 0 6px'>Coins being watched every 10 minutes</h3>"
            f"<table style='border-collapse:collapse;font-size:13px;width:100%' border='1' cellpadding='4'>"
            f"<tr style='background:#f3f3f3'><th>Coin</th><th>Added by</th><th>Last checked</th>"
            f"<th>Top traders still in</th><th>Since entry</th></tr>{out}</table>")


def copy_table(s):
    """The shadow book: what copying one trader would have done, on paper."""
    c = copybook.summary(s)
    if not c:
        return ""
    days = max(0.1, (time.time() - c["since"]) / 86400)
    row = lambda name, v: (f"<tr><td>{name}</td><td>{v['n']}</td>"
                           f"<td>{'-' if not v['n'] else str(round(100 * v['wins'] / v['n'])) + '%'}</td>"
                           f"<td>{_usd(v['avg'])}</td>"
                           f"<td style='color:{'#0a7d38' if v['pnl'] > 0 else '#b00020' if v['pnl'] < 0 else '#111'}'>"
                           f"<b>{_usd(v['pnl'])}</b></td></tr>")
    recent = ", ".join(f"${e(t['symbol'])} {_usd(t['mirror']['pnl'])} ({e(t['mirror']['how'])})"
                       for t in c["recent"]) or "none closed yet"
    holding = ", ".join("$" + e(x) for x in c["holding"]) or "nothing"
    n_un = c.get("unnamed") or 0
    unnamed = (f" · {n_un} of their buys skipped (DexScreener has no price or ticker for them,"
               " usually LP/receipt tokens or a mint that's minutes old)") if n_un else ""
    return f"""<h3 style="margin:18px 0 6px">Copying @{e(c['handle'])} (paper only)</h3>
<p style="font-size:12px;color:#555;margin:0 0 6px">Every coin they buy is paper-bought at ${c['size']:.0f} within 10 minutes of
their wallet showing it, then closed two ways: when <b>they</b> sell (or after {config.COPY_MAX_DAYS:.0f} days), and by <b>your</b> rules
(+{config.TARGET_GAIN_PCT:.0f}% / -{config.STOP_LOSS_PCT:.0f}% / {config.TRACK_WINDOW_HOURS}h). Fees and slippage included. Watching {c['chains']}.
Running {days:.1f} days · {c['open']} open: {holding}{unnamed}</p>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Exit style</th><th>Closed</th><th>Winners</th><th>Avg / trade</th><th>Total</th></tr>
{row("Their exits", c["their"])}{row("Your rules", c["yours"])}</table>
<p style="font-size:12px;color:#555">Last closed: {recent}</p>"""


def learn_box(L):
    """What the paper trades have taught the scanner, in plain words."""
    if not L:
        return ""
    if L["n"] < L["need"]:
        body = (f"Still learning: {L['n']} of {L['need']} closed paper trades so far. Nothing is adjusted until then - "
                "a handful of meme-coin trades is mostly luck.")
    else:
        rules = "".join(f"<li>{e(x)}</li>" for x in L["rules"]) or "<li>no changes needed - your settings are holding up</li>"
        les = "".join(f"<li>{e(x)}</li>" for x in L["lessons"]) or "<li>no clear pattern yet</li>"
        warn = ("<p style='color:#b00020;margin:4px 0'><b>Even the strictest bar is losing money on paper. "
                "Consider pausing real trades until this turns positive.</b></p>" if L.get("losing") else "")
        body = (f"{warn}<b>Adjustments now active</b> (based on {L['n']} closed paper trades, last 30 days; "
                f"they only ever make the scanner stricter):<ul style='margin:4px 0 6px 18px;padding:0'>{rules}</ul>"
                f"<b>Biggest differences so far</b><ul style='margin:4px 0 0 18px;padding:0'>{les}</ul>")
    return (f"<h3 style='margin:18px 0 6px'>What paper trading has taught the scanner</h3>"
            f"<div style='font-size:13px;background:#f6f6f6;border-radius:6px;padding:8px'>{body}</div>")


def _usd(v):
    return "-" if v is None else f"{'+' if v > 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


def paper_table(pp):
    """Paper trading: what following every qualifying pick by the rules would have made."""
    if not pp:
        return ""
    names = [n for n in pp if n not in ("open", "size")]
    rows = "".join(
        f"<tr><td>{'<b>All</b>' if n == 'all' else n}</td><td>{v['n']}</td><td>{v['wins']}</td><td>{v['stops']}</td>"
        f"<td style='color:{'#0a7d38' if v['pnl'] > 0 else '#b00020' if v['pnl'] < 0 else '#111'}'>{_usd(v['pnl'])}</td>"
        f"<td>{_usd(v['avg'])}</td></tr>" for n in names for v in [pp[n]])
    note = ("No closed paper trades yet - each one takes up to 48h." if not pp["all"]["n"] else
            f"{pp['all']['n']} closed, {pp['open']} still open.")
    return f"""<h3 style="margin:18px 0 6px">Paper trading (14 days, ${pp['size']:.0f} per trade)</h3>
<p style="font-size:12px;color:#555;margin:0 0 6px">Every coin that passed all the rules is "bought" 20 min after the signal at the live price,
then closed at +{config.TARGET_GAIN_PCT:.0f}%, -{config.STOP_LOSS_PCT:.0f}% or after {config.TRACK_WINDOW_HOURS}h. FOMO fees and 1% slippage each way included.
Prices are checked every 10 min, so real fills will differ a little. {note}</p>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Score</th><th>Trades</th><th>+50% hit</th><th>Stopped</th><th>Total P&amp;L</th><th>Avg / trade</th></tr>{rows}</table>"""


def coverage(s):
    c = s.get("wallet_cov")
    if not c:
        return ""
    held = sum(1 for sn in (s.get("wallet_snap") or {}).values()
               if time.time() - sn["ts"] <= config.HOLDINGS_MAX_AGE_MIN * 60)
    return (f"Solana top-trader wallets read this scan: {c['read']}/{c['tried']} "
            f"({held} with holdings under {config.HOLDINGS_MAX_AGE_MIN} min old).<br>")


def footer(s, stats):
    return f"""<p style="font-size:11px;color:#888">Checked {stats['traders']} traders (top 100 + {len(config.FOLLOW_TRADERS)} you follow) · {stats['events']} trader trades in {config.LOOKBACK_HOURS}h ·
{stats['candidates']} coins seen{f" (incl. {stats['held']} top traders are holding)" if stats.get('held') else ''} ({e(' · '.join(f"{chains.LABEL.get(c, c)} {n[0]} seen/{n[1]} investable" for c, n in sorted((stats.get('by_chain') or {}).items())))}) · {stats['eligible']} passed the {_money(config.MIN_MCAP_USD)}+ market cap / liquidity / age filters · {stats['deep']} fully scored ({stats.get('fresh', stats['deep'])} refreshed this scan).<br>
{coverage(s)}{e(reputation.summary(s))}<br>
{(e(watchlist.summary(s)) + '<br>') if watchlist.summary(s) else ''}
Bankroll setting ${config.BANKROLL_USD:.0f} (update BANKROLL_USD as it changes). Budget: FOMO API {s['fomo_credits_used']:,}/{config.FOMO_MONTHLY_CREDITS:,} ·
Helius {s['helius_credits_used']:,}/{config.HELIUS_MONTHLY_CREDITS:,} · X ${s['x_calls'] * 0.001:.2f}<br>
This is a signal scanner, not financial advice. Meme coins can go to zero - only trade money you can afford to lose.</p>"""


def build(picks, summ, hits, s, stats, alerts, instant=False):
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    cards = "".join(card(i, r, summ, r["key"] in alerts) for i, r in enumerate(picks))
    held = [w["symbol"] for w in (s.get("watch") or {}).values() if w.get("source") == "manual"]
    if held:
        cards = (f"<div style='background:#fff6e5;border-left:5px solid #b36b00;border-radius:6px;padding:8px;"
                 f"font-size:13px;margin:0 0 12px'><b>One-trade rule:</b> you're holding "
                 f"{e(', '.join('$' + x for x in held))}. Skip this unless its position check says SELL or "
                 f"TIGHTEN STOP - then sell it first and use the money here.</div>") + cards
    funded = sum(1 for r in picks if r.get("size"))
    names = ", ".join(f"${r['market']['symbol']} {r['score']}" for r in picks)
    subject = (f"HIGH CONFIDENCE NOW: {names}" if instant else f"FOMO digest {now}: {names}")
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2 style="margin:4px 0">{"High-confidence alert" if instant else "Digest"}: {funded} pick{'s' if funded != 1 else ''} to buy now{f" + {len(picks) - funded} alternate" if len(picks) > funded else ""}</h2>
<p style="color:#555;font-size:13px;margin:0 0 12px">{f"Scored {config.INSTANT_SCORE}+ with top-trader buying in the last {config.INSTANT_SIGNAL_MAX_AGE_MIN} min. Price re-checked seconds before sending. Sizes assume your full bankroll is free - scale down if you already hold other picks." if instant else f"Ranked by confidence of reaching +{config.TARGET_GAIN_PCT:.0f}%. Every coin passed the safety checks, cleared {config.MIN_SEND_SCORE}/100, and had its price re-checked right before sending. Fewer than 3 means the rest weren't good enough."}</p>
{cards}{watch_table(s)}{copy_table(s)}{track_table(summ, hits)}{footer(s, stats)}</body></html>"""
    return subject, body


WEAK_LABEL = {"smart": "no top-100 or followed traders buying or holding", "chart": "weak or unclear chart",
              "momentum": "little buying momentum right now", "setup": "weak setup (liquidity / safety / holders)",
              "social": "little X or Telegram buzz", "community": "not trending on FOMO"}


def _short(txt, n=110):
    return txt if len(txt) <= n else txt[:n - 1].rstrip() + "…"


def near_call(r):
    """One close call: score, gap to the bar, what's good, what's holding it back, address to check."""
    from .scoring import MAX
    m = r["market"]
    good = [_short(x) for x in r["reasons"][:2]] or ["nothing stands out yet"]
    bad = [w for w in r["why_not"] if not w.startswith("score ")]
    bad += r["flags"][:2]
    pts = r.get("points") or {}
    weak = sorted((k for k in MAX if k in pts and pts[k] / MAX[k] < 0.25 and not (k == "social" and not r.get("x_checked"))),
                  key=lambda k: pts[k] / MAX[k])
    bad += [WEAK_LABEL[k] for k in weak]
    bad = list(dict.fromkeys(_short(b) for b in bad))[:3]
    if not bad and pts:
        k = min((k for k in MAX if k in pts), key=lambda k: pts[k] / MAX[k])
        bad = [f"weakest area: {WEAK_LABEL[k].split(' (')[0]} ({pts[k]:g}/{MAX[k]} points)"]
    gap = config.MIN_SEND_SCORE - r["score"]
    gap_txt = f"{gap} point{'s' if gap != 1 else ''} short" if gap > 0 else "blocked by a safety rule"
    goods = "".join(f"<div style='color:#0a7d38'>+ {e(g)}</div>" for g in good)
    bads = "".join(f"<div style='color:#b00020'>− {e(b)}</div>" for b in bad)
    return f"""
<div style="border:1px solid #ddd;border-radius:8px;padding:10px;margin:0 0 10px;font-size:13px">
  <div style="font-size:15px;font-weight:700">${e(m['symbol'])} <span style="font-size:12px;color:#555;font-weight:400">{chains.LABEL[m['chain']]}</span>
    <span style="float:right">{r['score']}/100</span></div>
  <div style="color:#555;font-size:12px;margin-bottom:4px">{gap_txt} · MC {_money(m['mcap'])} · 1h {m['change'].get('h1', 0):+.0f}% · 24h {m['change'].get('h24', 0):+.0f}%</div>
  {goods}{bads}
  <div style="margin-top:4px"><a href="{e(m['url'])}">Chart</a> ·
    <span style="font-family:monospace;font-size:11px;color:#666;word-break:break-all">{e(m['address'])}</span></div>
</div>"""


def build_status(summ, hits, s, stats, near, dropped=()):
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    near = [r for r in near if not r.get("hard")]  # failed safety = never a buy, don't list it
    near_html = "".join(near_call(r) for r in near[:5]) or "<p>none</p>"
    skipped = "".join(f"<p style='font-size:13px'>Skipped ${e(p['market']['symbol'])}: {e(why)}</p>" for p, why in dropped)
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2 style="margin:4px 0">No picks right now - scanner is running</h2>
<p style="font-size:14px">Nothing currently meets the {config.MIN_SEND_SCORE}/100 bar. Sitting out is a valid trade.
Closest calls below - copy an address into <b>Check a coin</b> for the full breakdown.</p>
{near_html}{skipped}{watch_table(s)}{copy_table(s)}{track_table(summ, hits)}{footer(s, stats)}</body></html>"""
    return f"FOMO digest {now}: no qualifying picks", body


def build_exit(alerts):
    items = "".join(
        f"<li style='margin-bottom:8px'><b>${e(p['symbol'])}</b> ({chains.LABEL[p['chain']]})"
        f"{f' {chg:+.0f}% since alert' if chg is not None else ''}: {e(msg)}</li>" for p, msg, chg in alerts)
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2 style="color:#b00020">Action needed on a pick</h2><ul style="font-size:14px">{items}</ul>
<p style="font-size:11px;color:#888">Checked every 10 minutes, so prices may have moved a little. Not financial advice.</p></body></html>"""
    names = ", ".join("$" + p["symbol"] for p, _, _ in alerts)
    return f"EXIT ALERT: {names}", body


def build_check(r, holders, s):
    m = r["market"]
    summ, _ = tracker.summary(s)
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    if r["qualified"]:
        verdict = (f"<div style='background:#eef7f0;border-radius:8px;padding:10px;font-size:14px'>"
                   f"<b>Scanner verdict: would send this</b> - passes every filter and scores {r['score']}/100.</div>")
    else:
        items = "".join(f"<li>{e(w)}</li>" for w in r["why_not"])
        verdict = (f"<div style='background:#fdeeee;border-radius:8px;padding:10px;font-size:14px'>"
                   f"<b>Scanner verdict: would NOT send this</b><ul style='margin:6px 0 0 18px;padding:0'>{items}</ul></div>")
    if holders:
        rows = "".join(f"<tr><td>{tag(h['rank'])}</td><td>{e(h['handle'])}</td><td>{_money(h['usd'])}</td></tr>" for h in holders[:15])
        total = sum(h["usd"] for h in holders)
        hold = (f"<h3 style='margin:14px 0 6px'>Top-100 FOMO traders holding it: {len(holders)} (≈{_money(total)})</h3>"
                f"<table style='border-collapse:collapse;font-size:13px;width:100%' border='1' cellpadding='4'>"
                f"<tr style='background:#f3f3f3'><th>Rank</th><th>Trader</th><th>Position now</th></tr>{rows}</table>")
    elif r.get("live_holders"):
        hold = "<h3 style='margin:14px 0 6px'>Top-100 FOMO traders holding it: none (all 100 wallets checked just now)</h3>"
    else:
        why = {"budget": "Your Helius free credits are being paced for the rest of the month, so Solana wallet "
                         "reads are paused until the budget catches up.",
               "rate limit": "Helius rate-limited the request (too many at once). Running the check again usually works."
               }.get(r.get("holders_why"), "Their wallets could not be read this run.")
        hold = ("<h3 style='margin:14px 0 6px'>Top-100 FOMO traders holding it: unknown</h3>"
                f"<p style='font-size:13px;color:#b36b00'>{e(why)} The smart-money part of this score is unverified, "
                "so treat the score as less certain.</p>")
    if r.get("exited"):
        hold += (f"<p style='font-size:13px;color:#b00020'>Sold out since buying: "
                 f"{e(', '.join(r['exited'][:8]))} - these earlier buys are excluded from the score.</p>")
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2 style="margin:4px 0">Coin check: ${e(m['symbol'])} - {r['score']}/100</h2>
<p style="color:#555;font-size:13px;margin:0 0 10px">{chains.LABEL[m['chain']]} · checked {now} · same rules as your scanner</p>
{verdict}{hold}<div style="height:12px"></div>{card(0, r, summ, False)}
<p style="font-size:11px;color:#888">Holdings are read live from the chain. Positions under $20 are ignored. Not financial advice.</p>
</body></html>"""
    return f"Coin check: ${m['symbol']} {r['score']}/100 ({'would send' if r['qualified'] else 'would not send'})", body


def send(subject, body):
    if not (config.SMTP_USER and config.SMTP_PASSWORD and config.EMAIL_TO):
        log.warning("SMTP not configured; skipping email")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, config.SMTP_USER, config.EMAIL_TO
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.sendmail(config.SMTP_USER, [a.strip() for a in config.EMAIL_TO.split(",")], msg.as_string())
    log.info("Email sent to %s", config.EMAIL_TO)
    return True
