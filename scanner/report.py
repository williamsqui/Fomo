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

from . import chains, config, tracker
from .sizing import fmt

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
    rows = "".join(
        f"<tr><td>{name}</td><td>{v['n']}</td><td>{v['hits']}</td>"
        f"<td>{'-' if v['rate'] is None else str(v['rate']) + '%'}</td></tr>"
        for name, v in summ.items())
    hit_list = ", ".join(f"${e(p['symbol'])} +{(p['max'] / p['entry'] - 1) * 100:.0f}%" for p in hits) or "none yet"
    return f"""<h3 style="margin:18px 0 6px">Track record (14 days, +{config.TARGET_GAIN_PCT:.0f}% within {config.TRACK_WINDOW_HOURS}h)</h3>
<table style="border-collapse:collapse;font-size:13px;width:100%" border="1" cellpadding="4">
<tr style="background:#f3f3f3"><th>Score band</th><th>Closed</th><th>Hit</th><th>Hit rate</th></tr>{rows}</table>
<p style="font-size:13px">Recent hits: {hit_list}</p>"""


def footer(s, stats):
    return f"""<p style="font-size:11px;color:#888">Checked {stats['traders']} leaderboard traders · {stats['events']} trader trades in {config.LOOKBACK_HOURS}h ·
{stats['candidates']} coins seen ({e(' · '.join(f"{chains.LABEL.get(c, c)} {n[0]} seen/{n[1]} investable" for c, n in sorted((stats.get('by_chain') or {}).items())))}) · {stats['eligible']} passed the {_money(config.MIN_MCAP_USD)}+ market cap / liquidity / age filters · {stats['deep']} fully checked.<br>
Bankroll setting ${config.BANKROLL_USD:.0f} (update BANKROLL_USD as it changes). Budget: FOMO API {s['fomo_credits_used']:,}/{config.FOMO_MONTHLY_CREDITS:,} ·
Helius {s['helius_credits_used']:,}/{config.HELIUS_MONTHLY_CREDITS:,} · X ${s['x_calls'] * 0.001:.2f}<br>
This is a signal scanner, not financial advice. Meme coins can go to zero - only trade money you can afford to lose.</p>"""


def build(picks, summ, hits, s, stats, alerts, instant=False):
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    cards = "".join(card(i, r, summ, r["key"] in alerts) for i, r in enumerate(picks))
    funded = sum(1 for r in picks if r.get("size"))
    names = ", ".join(f"${r['market']['symbol']} {r['score']}" for r in picks)
    subject = (f"HIGH CONFIDENCE NOW: {names}" if instant else f"FOMO digest {now}: {names}")
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2 style="margin:4px 0">{"High-confidence alert" if instant else "Digest"}: {funded} pick{'s' if funded != 1 else ''} to buy now{f" + {len(picks) - funded} alternate" if len(picks) > funded else ""}</h2>
<p style="color:#555;font-size:13px;margin:0 0 12px">{f"Scored {config.INSTANT_SCORE}+ with top-trader buying in the last {config.INSTANT_SIGNAL_MAX_AGE_MIN} min. Price re-checked seconds before sending. Sizes assume your full bankroll is free - scale down if you already hold other picks." if instant else f"Ranked by confidence of reaching +{config.TARGET_GAIN_PCT:.0f}%. Every coin passed the safety checks, cleared {config.MIN_SEND_SCORE}/100, and had its price re-checked right before sending. Fewer than 3 means the rest weren't good enough."}</p>
{cards}{track_table(summ, hits)}{footer(s, stats)}</body></html>"""
    return subject, body


def build_status(summ, hits, s, stats, near, dropped=()):
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%b %d %H:%M")
    near_html = "".join(f"<li>${e(r['market']['symbol'])} ({chains.LABEL[r['market']['chain']]}) {r['score']}/100 - "
                        f"{e('; '.join(r['why_not'])[:140])}</li>" for r in near[:5]) or "<li>none</li>"
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2>No picks right now - scanner is running</h2>
<p style="font-size:14px">Nothing currently meets the bar. Sitting out is a valid trade. Closest calls:</p>
<ul style="font-size:13px">{near_html}</ul>{"".join(f"<p style='font-size:13px'>Skipped ${e(p['market']['symbol'])}: {e(why)}</p>" for p, why in dropped)}{track_table(summ, hits)}{footer(s, stats)}</body></html>"""
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
