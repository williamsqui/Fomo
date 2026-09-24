"""Watchlist: keep an eye on coins you hold and email you the moment something changes.

What gets watched
  * every coin the scanner emails you as a pick (automatically, from the moment it's sent)
  * every coin you run "Check my position" on
  for WATCH_DAYS days, or until you run "Check my position" with the gain box set to "sold".

What triggers an email (each reason only once per coin)
  * a top-100 trader who held it sells out (balance down 80%+), or sells about half
  * one wallet can now pull LP_UNLOCKED_HARD_PCT%+ of the liquidity, or it newly fails a
    safety check (Base/BNB, via GoPlus)
  * the liquidity pool shrinks LIQ_DROP_ALERT_PCT%+ since watching began
  * price hits your stop-loss or +50% target (for picks, the normal exit alert covers the
    first 48h, so the watchlist only takes over after that - no double emails)

Cost: Base/BNB wallet reads are free (one multicall per coin). Solana costs 1 Helius credit
per watched trader per scan. GoPlus is checked at most every WATCH_LP_EVERY_MIN per coin.

"Check my position" runs as a separate workflow and can't write to the scanner's saved state,
so it leaves a request in its own small cache (watchreq/requests.json). The scanner picks it
up on its next run.
"""
import html
import json
import logging
import os
import time
import uuid

from . import chains, config, evm, safety
from .sizing import fmt
from .traders import tag

log = logging.getLogger("watchlist")
e = html.escape
SOLD_OUT, HALF = 0.2, 0.5


# ------------------------------------------------------------ requests (from Check my position)
def _req_path():
    return os.path.join(config.WATCH_REQ_DIR, "requests.json")


def _write_request(req):
    os.makedirs(config.WATCH_REQ_DIR, exist_ok=True)
    try:
        with open(_req_path()) as f:
            reqs = json.load(f).get("requests", [])
    except (FileNotFoundError, json.JSONDecodeError):
        reqs = []
    week = time.time() - (config.WATCH_DAYS + 1) * 86400
    reqs = [r for r in reqs if r.get("ts", 0) >= week] + [{**req, "id": uuid.uuid4().hex, "ts": time.time()}]
    with open(_req_path(), "w") as f:
        json.dump({"requests": reqs}, f)


def request_add(r, holders, levels):
    """Called by "Check my position": watch this coin from now on."""
    m = r["market"]
    _write_request({"action": "add", "entry": _entry(
        m, "manual", entry=levels.get("entry") or m["price"], stop=levels.get("stop"), target=levels.get("target"),
        holders={h["wallet"]: {"handle": h["handle"], "rank": h["rank"], "base": h.get("amount")}
                 for h in holders if h.get("wallet")},
        lp_top=r.get("lp_top"))})


def request_remove(key):
    _write_request({"action": "remove", "key": key})


def merge_requests(s):
    """Apply requests left by the "Check my position" workflow (each exactly once)."""
    try:
        with open(_req_path()) as f:
            reqs = json.load(f).get("requests", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    seen = s.setdefault("watch_seen", [])
    done, added = 0, []
    for q in sorted(reqs, key=lambda q: q.get("ts", 0)):
        if q.get("id") in seen:
            continue
        seen.append(q.get("id"))
        if q.get("action") == "remove":
            s.setdefault("watch", {}).pop(q.get("key"), None)
        elif q.get("action") == "add" and q.get("entry"):
            old = s.setdefault("watch", {}).get(q["entry"]["key"])
            new = q["entry"]
            if old:  # re-checked a coin already watched: fresh levels, keep what was already alerted
                new["alerted"] = old.get("alerted", [])
            else:
                added.append(new)
            s["watch"][new["key"]] = new
        done += 1
    s["watch_seen"] = seen[-300:]
    s["_watch_added"] = added          # for the "now watching" confirmation email
    return done


# ------------------------------------------------------------ entries
def _entry(m, source, entry, stop=None, target=None, holders=None, lp_top=None):
    now = time.time()
    return {"key": m["key"], "chain": m["chain"], "address": m["address"], "symbol": m["symbol"],
            "url": m.get("url", ""), "source": source, "added": now, "expires": now + config.WATCH_DAYS * 86400,
            "entry": entry, "stop": stop or entry * (1 - config.STOP_LOSS_PCT / 100),
            "target": target or entry * (1 + config.TARGET_GAIN_PCT / 100),
            "holders": holders or {}, "liq0": m.get("liquidity") or 0, "lp_top": lp_top, "lp_ts": 0,
            "alerted": []}


def add_pick(s, r, traders):
    """Called by the scanner for every coin it emails you."""
    m, w = r["market"], s.setdefault("watch", {})
    if m["key"] in w:
        return
    field = "wallet" if m["chain"] == "solana" else "evm"
    by_handle = {t["handle"]: t for t in traders if t.get(field)}
    holders = {by_handle[h][field]: {"handle": h, "rank": by_handle[h]["rank"], "base": None}
               for h in r["smart"]["buyers"] if h in by_handle}
    w[m["key"]] = _entry(m, "pick", entry=m["price"], stop=r.get("stop_price"), target=r.get("target_price"),
                         holders=holders)


def keys(s):
    return list((s.get("watch") or {}).keys())


def summary(s):
    w = s.get("watch") or {}
    if not w:
        return ""
    return "Watching for exit signals: " + ", ".join(f"${x['symbol']}" for x in w.values())


# ------------------------------------------------------------ each scan
def _balances(s, it):
    """({wallet: amount}, complete) for the traders we're watching in this coin."""
    wallets = list(it["holders"])
    if not wallets:
        return {}, True
    if it["chain"] == "solana":
        from .check import solana_holders
        return solana_holders(s, it["address"], [{"wallet": w} for w in wallets])
    try:
        raw, ok = evm.balances_checked(it["chain"], it["address"], wallets)
    except Exception as ex:  # RPC hiccup: say nothing this scan rather than guess
        log.info("watch balances failed for %s: %s", it["symbol"], ex)
        return {}, False
    dec = evm._decimals(s, it["chain"], it["address"])
    return {w: v / 10 ** dec for w, v in raw.items()}, ok


def _open_pick(s, key):
    for p in s.get("picks") or []:
        if p["key"] == key and p.get("sent") and time.time() - p["sent_ts"] <= config.TRACK_WINDOW_HOURS * 3600:
            return p
    return None


def check(s, markets):
    """Run every watched coin's checks. Returns [(entry, [(reason_id, message, advice)])]."""
    now, out = time.time(), []
    w = s.setdefault("watch", {})
    for k in list(w):
        it = w[k]
        if now > it["expires"]:
            w.pop(k)
            continue
        found, m = [], markets.get(k)
        price = m["price"] if m and m["price"] > 0 else None
        pick = _open_pick(s, k)
        warned = pick.get("warned", []) if pick else []

        def hit(rid, msg, advice):
            if rid not in it["alerted"]:
                it["alerted"].append(rid)
                found.append((rid, msg, advice))

        # 1. are the top traders who were in it still in?
        amts, ok = _balances(s, it)
        it["checked"], it["read_ok"] = now, ok
        if ok:
            left = 0
            for wal, h in list(it["holders"].items()):
                amt = amts.get(wal, 0.0)
                if h.get("base") is None:            # first look at a pick: set the baseline
                    if amt > 0:
                        h["base"] = amt
                        left += 1
                    else:
                        it["holders"].pop(wal)
                    continue
                h["base"] = max(h["base"], amt)      # bought more -> new high-water mark
                usd = f" (~${h['base'] * price:,.0f})" if price else ""
                if amt < h["base"] * SOLD_OUT:
                    if h["handle"] not in warned:
                        hit(f"out:{wal}", f"{h['handle']} ({tag(h['rank'])}) sold out{usd}",
                            "The traders you followed in are leaving. Run Check my position now - "
                            "unless the others are still adding, I'd sell.")
                    if pick and h["handle"] not in warned:
                        warned.append(h["handle"])
                else:
                    left += 1
                    if amt < h["base"] * HALF:
                        hit(f"half:{wal}", f"{h['handle']} ({tag(h['rank'])}) sold about half their position",
                            "Taking profit, not necessarily leaving - worth a position check.")
            it["still_in"], it["tracked"] = left, len(it["holders"])
            # (skip when the only exits were already reported by the normal exit alert)
            if it["holders"] and left == 0 and any(a.startswith("out:") for a in it["alerted"]):
                hit("none_left", "none of the top traders who were in it are left",
                    "The main reason to hold is gone. I'd sell unless the chart is still clearly rising.")

        # 2. can someone pull the liquidity? (Base/BNB via GoPlus, every WATCH_LP_EVERY_MIN)
        if it["chain"] in ("base", "bsc") and now - it.get("lp_ts", 0) >= config.WATCH_LP_EVERY_MIN * 60:
            it["lp_ts"] = now
            sf = safety.check(it["chain"], it["address"])
            if sf.get("lp_top") is not None:
                it["lp_top"] = sf["lp_top"]
                if sf["lp_top"] >= config.LP_UNLOCKED_HARD_PCT:
                    hit("lp", f"one wallet can now pull {sf['lp_top']:.0f}% of the liquidity",
                        "That's the rug-risk line: they could drain most of the pool and a stop-loss "
                        "can't save you. I'd sell.")
            if sf.get("hard"):
                hit("safety", "now fails a safety check: " + "; ".join(sf["hard"][:2]), "Sell.")

        # 3. is liquidity being pulled?
        if m and it.get("liq0") and m["liquidity"] <= it["liq0"] * (1 - config.LIQ_DROP_ALERT_PCT / 100):
            hit("liq", f"liquidity dropped from ${it['liq0']:,.0f} to ${m['liquidity']:,.0f} "
                       f"({(m['liquidity'] / it['liq0'] - 1) * 100:.0f}%)",
                "Liquidity leaving is how rugs start, and it makes selling cost more. I'd sell.")

        # 4. stop / target (picks: the normal exit alert covers the first 48h)
        if price and not pick:
            if price <= it["stop"]:
                hit("stop", f"hit your stop-loss ({fmt(price)}, {(price / it['entry'] - 1) * 100:+.0f}% from entry)",
                    "Sell to protect your bankroll.")
            elif price >= it["target"]:
                hit("target", f"hit your +{config.TARGET_GAIN_PCT:.0f}% target ({fmt(price)})",
                    "Take profit - all of it if your position is under $60.")
        if price:
            it["last_price"] = price
        if found:
            out.append((it, found))
    return out


def build_email(alerts):
    names = ", ".join("$" + it["symbol"] for it, _ in alerts)
    first = alerts[0][1][0][1]
    items = ""
    for it, found in alerts:
        price = it.get("last_price")
        chg = f" · {(price / it['entry'] - 1) * 100:+.0f}% since entry" if price and it.get("entry") else ""
        link = f'<a href="{e(it["url"])}">Chart</a> · ' if it.get("url") else ""
        rows = "".join(f"<li style='margin:4px 0'><b>{e(msg)}</b><br><span style='color:#555'>{e(adv)}</span></li>"
                       for _, msg, adv in found)
        items += (f"<div style='border-left:5px solid #b00020;background:#fdf3f3;border-radius:6px;padding:10px;margin:0 0 10px'>"
                  f"<div style='font-size:17px;font-weight:700'>${e(it['symbol'])} "
                  f"<span style='font-size:12px;color:#555;font-weight:400'>{chains.LABEL.get(it['chain'], it['chain'])}"
                  f"{f' · price {fmt(price)}' if price else ''}{chg}</span></div>"
                  f"<ul style='margin:6px 0 4px 18px;padding:0;font-size:14px'>{rows}</ul>"
                  f"<div style='font-size:12px'>{link}"
                  f"<span style='font-family:monospace'>{e(it['address'])}</span></div></div>")
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2 style="margin:4px 0;color:#b00020">Watchlist alert</h2>
<p style="font-size:13px;color:#555;margin:0 0 10px">Something changed on a coin you hold. Checked within the last 10 minutes.</p>
{items}
<p style="font-size:12px;color:#555">Stop watching a coin: Actions → Check my position → type <b>sold</b> in the gain box.
Coins drop off automatically after {config.WATCH_DAYS:.0f} days.</p>
<p style="font-size:11px;color:#888">Rules-based guidance from live data, not financial advice.</p></body></html>"""
    return f"WATCHLIST ALERT: {names} - {first}"[:150], body


def status(s):
    """One line per watched coin: proof the scanner is really looking at it."""
    now, rows = time.time(), []
    for it in (s.get("watch") or {}).values():
        ago = int((now - it["checked"]) / 60) if it.get("checked") else None
        price = it.get("last_price")
        rows.append({"symbol": it["symbol"], "source": "you" if it["source"] == "manual" else "scanner pick",
                     "ago": ago, "ok": it.get("read_ok"), "still_in": it.get("still_in"),
                     "tracked": it.get("tracked", len(it.get("holders") or {})),
                     "chg": (price / it["entry"] - 1) * 100 if price and it.get("entry") else None,
                     "days_left": max(0, (it["expires"] - now) / 86400)})
    return rows


def describe(it):
    """Short text for a single coin, e.g. for the position-check email."""
    if not it or not it.get("checked"):
        return None
    ago = int((time.time() - it["checked"]) / 60)
    who = (f"{it.get('still_in', 0)} of {it.get('tracked', 0)} top traders you're following still in"
           if it.get("read_ok") else "wallet read failed on the last scan - it will retry")
    return f"last checked by the scanner {ago} min ago · {who}"


def build_added_email(added):
    """Confirmation that a coin from Check my position really reached the scanner."""
    items = ""
    for it in added:
        names = ", ".join(f"{h['handle']} ({tag(h['rank'])})" for h in it["holders"].values()) or "none (no top trader holds it)"
        items += (f"<li style='margin:6px 0'><b>${e(it['symbol'])}</b> - following {len(it['holders'])} top trader(s): "
                  f"{e(names)}. Stop-loss {fmt(it['stop'])}, target {fmt(it['target'])}.</li>")
    body = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;padding:8px;color:#111">
<h2 style="margin:4px 0">Now watching every 10 minutes</h2><ul style="font-size:14px">{items}</ul>
<p style="font-size:13px">You'll get a WATCHLIST ALERT if one of those traders sells out or sells half, if liquidity is pulled,
or if the price hits your stop-loss or target. Each digest also lists every coin being watched and when it was last checked.</p>
<p style="font-size:12px;color:#555">When you sell: Actions → Check my position → type <b>sold</b> in the gain box.</p></body></html>"""
    return "Now watching: " + ", ".join("$" + it["symbol"] for it in added), body
