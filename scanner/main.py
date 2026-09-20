"""One scan (runs every 10 minutes).

leaderboard -> trader wallets (Solana + Base/BNB/Robinhood) -> candidate coins
-> investable filter ($500k+ mcap, liquidity, age) -> quick score
-> deep check of finalists (chart, rug check, holders, X, Telegram, FOMO posts; cached per coin)
-> keep only coins I'd actually buy
-> 80+  : re-check the live price and signal age, then email immediately
-> 62-79: held for the 8:00 / 18:00 digest (re-checked live at send time too)

    python -m scanner.main              # one scan (what GitHub Actions runs)
    python -m scanner.main --loop 180   # scan every 3 min forever (for an always-on server)
"""
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import (chains, chart, config, dex, evm, fomo, report, safety, scoring, sizing, socials,
               solana, state as st, tracker, xsocial)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("main")


def cached(s, key, name, ttl_min, fn, force=False):
    """Per-coin cache stored in state so 10-minute scans don't burn free-tier limits."""
    c = s["deep_cache"].setdefault(key, {})
    e = c.get(name)
    if e and not force and time.time() - e[0] < ttl_min * 60:
        return e[1], False
    v = fn()
    if v is not None:
        c[name] = [time.time(), v]
        return v, True
    return (e[1] if e else None), False


def holder_growth(s, key, holders, fresh):
    if not isinstance(holders, int) or holders <= 0:
        return None
    hist = s["holders_hist"].setdefault(key, [])
    if fresh:
        hist.append([time.time(), holders])
    old = [h for h in hist if time.time() - h[0] >= 18 * 3600]
    if not old:
        return None
    ref = old[-1][1]
    return (holders / ref - 1) * 100 if ref else None


def due_digest(s):
    """Return the digest slot id if one is due (slot time passed <3h ago and not yet sent)."""
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    for day in (now.date(), now.date() - timedelta(days=1)):
        for t in config.DIGEST_TIMES:
            hh, mm = (int(x) for x in t.split(":"))
            slot = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            sid = slot.strftime("%Y-%m-%d %H:%M")
            if timedelta(0) <= now - slot < timedelta(hours=3) and sid not in s["digests_sent"]:
                return sid
    return None


def price_at(closes, ts):
    """Hourly close at or just before ts (price when the top traders bought)."""
    if not closes or not ts:
        return None
    before = [c for t, c in closes if t <= ts]
    return before[-1] if before else None


def verify_live(picks, scan_prices):
    """Re-pull prices right before emailing. Drop anything that ran too far or is dumping."""
    live = dex.tokens([p["key"] for p in picks])
    ok, dropped = [], []
    for p in picks:
        m = live.get(p["key"])
        if not m:
            dropped.append((p, "couldn't refresh the price"))
            continue
        p["market"] = m
        p["checked_at"] = time.time()
        drop = (1 - m["price"] / scan_prices[p["key"]]) * 100 if scan_prices.get(p["key"]) else 0
        base = price_at((p.get("chart") or {}).get("closes"), p["smart"].get("first_buy"))
        p["runup"] = (m["price"] / base - 1) * 100 if base else None
        if drop > config.MAX_DROP_SINCE_SCAN_PCT:
            dropped.append((p, f"fell {drop:.0f}% while being checked"))
        elif p["runup"] is not None and p["runup"] > config.MAX_RUNUP_PCT:
            dropped.append((p, f"already +{p['runup']:.0f}% since the top traders bought - too late"))
        else:
            ok.append(p)
    for p, why in dropped:
        log.info("dropped %s: %s", p["market"]["symbol"], why)
    return ok, dropped


def run():
    s = st.load()
    if not config.FOMO_API_KEY:
        log.error("Missing FOMO_API_KEY secret")
        sys.exit(1)
    now = time.time()
    s["run_count"] += 1

    # 1. leaderboard + wallets (top 50 every run, the rest every 3rd run)
    traders = fomo.leaderboard(s)
    handles = [t["handle"] for t in traders]
    every = s["run_count"] % config.SLOW_WALLET_EVERY == 0
    scan_traders = [t for t in traders if every or t["rank"] <= config.FAST_WALLETS]

    # 2. what they traded since the last scan
    if "solana" in config.CHAINS and config.HELIUS_API_KEY:
        solana.scan_wallets(s, scan_traders, dex.sol_price())
    evm.scan_wallets(s, traders)
    cutoff = now - config.LOOKBACK_HOURS * 3600
    recent = [e for e in s["trader_buys"] if e["ts"] >= cutoff]
    new_buy_keys = {e["key"] for e in recent if e["side"] == "buy" and e["ts"] >= now - 20 * 60}

    # 3. candidates
    trending = {t["key"]: t["rank"] for t in fomo.trending(s) if "key" in t}
    gt_trend = [k for c in config.CHAINS for k in chart.trending_tokens(s, c)]
    cands = list(dict.fromkeys([e["key"] for e in recent if e["side"] == "buy"] + list(trending) + gt_trend))
    cands = [k for k in cands if chains.split(k)[1] not in chains.QUOTE_ADDRESSES]
    watch = [p["key"] for p in s["picks"] if p.get("sent") and now - p["sent_ts"] < config.TRACK_WINDOW_HOURS * 3600]
    markets = dex.tokens(cands + tracker.open_keys(s) + watch)
    for e in s["trader_buys"]:  # EVM transfers only have token amounts - price them
        if e.get("usd") is None and e["key"] in markets:
            e["usd"] = round(e["amount"] * markets[e["key"]]["price"], 2)
    # 3b. Base/BNB/Robinhood: check what the top traders hold right now (works on free RPCs)
    investable = {k: m for k, m in markets.items() if k in cands and scoring.eligible(m)}
    evm.holdings_scan(s, traders, investable)
    recent = [e for e in s["trader_buys"] if e["ts"] >= cutoff]
    new_buy_keys = {e["key"] for e in recent if e["side"] == "buy" and e["ts"] >= now - 20 * 60}
    tracker.update(s, markets)
    exits = tracker.exit_checks(s, markets)
    if exits and config.EXIT_ALERTS:
        report.send(*report.build_exit(exits))
        log.info("Exit alerts: %s", [(p["symbol"], m) for p, m, _ in exits])

    # 4. investable filter + quick score
    quick = {}
    for k in cands:
        m = markets.get(k)
        if not scoring.eligible(m) or m["symbol"].upper() in chains.QUOTE_SYMBOLS:
            continue
        sm = scoring.smart_money(s["trader_buys"], k)
        quick[k] = (m, sm, scoring.score(m, sm, trending_rank=trending.get(k)))
    order = sorted(quick, key=lambda k: -quick[k][2]["score"])[:config.FINALISTS]
    log.info("%d candidates, %d investable, deep-checking %d", len(cands), len(quick), len(order))

    # 5. deep check (slow-changing data cached per coin; new smart-money buys force a fresh chart)
    deep, x_used = [], 0
    for i, k in enumerate(order):
        m, sm, _ = quick[k]
        fresh_signal = k in new_buy_keys
        sf, _ = cached(s, k, "safety", config.SAFETY_TTL_MIN, lambda: safety.check(m["chain"], m["address"]))
        ch, info, growth = None, {}, None
        x = th = tg = None
        if sf["ok"]:  # don't spend rate limits / credits on coins that already failed safety
            ch, _ = cached(s, k, "chart", config.CHART_TTL_MIN,
                           lambda: chart.analyze(chart.candles(m["chain"], m["pair"])) if m["pair"] else None,
                           force=fresh_signal)
            info, got = cached(s, k, "info", config.INFO_TTL_MIN, lambda: chart.token_info(m["chain"], m["address"]))
            info = info or {}
            growth = holder_growth(s, k, info.get("holders"), got)
            if x_used < config.X_TOKENS_PER_RUN:
                x, got = cached(s, k, "x", config.X_TTL_MIN,
                                lambda: xsocial.search(s, m["address"], m["symbol"], handles))
                x_used += 1 if got else 0
            else:  # over this run's X quota: reuse the last result if there is one
                x = ((s["deep_cache"].get(k) or {}).get("x") or [0, None])[1]
            th = fomo.thesis(s, k) if i < 3 else (s["thesis_cache"].get(k) or {}).get("items")
            tg, _ = cached(s, k, "tg", config.INFO_TTL_MIN,
                           lambda: socials.telegram_members(m.get("telegram") or info.get("telegram")))
        r = scoring.score(m, sm, x=x, thesis=th, trending_rank=trending.get(k), chart=ch, safety=sf,
                          info=info, tg=tg, holder_growth=growth, deep=True)
        deep.append(r)
        log.info("%3d %-6s %-9s %-8s %s", r["score"], r["tier"], m["chain"], m["symbol"],
                 "QUALIFIED" if r["qualified"] else "; ".join(r["why_not"])[:120])
    deep.sort(key=lambda r: -r["score"])
    qualified = [r for r in deep if r["qualified"]]
    scan_prices = {r["key"]: r["market"]["price"] for r in qualified}

    summ, hits = tracker.summary(s)
    by_chain = {}
    for k in cands:
        by_chain.setdefault(chains.split(k)[0], [0, 0])[0] += 1
    for k in quick:
        by_chain[chains.split(k)[0]][1] += 1
    stats = {"traders": len(traders), "events": len(recent), "candidates": len(cands),
             "eligible": len(quick), "deep": len(deep), "by_chain": by_chain}
    log.info("coins seen/investable by chain: %s", by_chain)
    os.makedirs("out", exist_ok=True)
    sent = {}

    # 6a. instant alert: 80+ with a fresh signal, re-verified live
    realert = now - config.REALERT_HOURS * 3600
    hot = [r for r in qualified if r["score"] >= config.INSTANT_SCORE
           and s["instant_sent"].get(r["key"], 0) < realert
           and (r["smart"].get("last_buy") is None
                or now - r["smart"]["last_buy"] <= config.INSTANT_SIGNAL_MAX_AGE_MIN * 60)]
    if hot:
        hot, _ = verify_live(hot[:config.MAX_PICKS], scan_prices)
        if hot:
            hot = sizing.plan(hot)
            subject, body = report.build(hot, summ, hits, s, stats, set(), instant=True)
            report.send(subject, body)
            with open("out/instant.html", "w") as f:
                f.write(body)
            for p in hot:
                s["instant_sent"][p["key"]] = now
                s["alerts"][p["key"]] = now
                sent[p["key"]] = p
            log.info("Instant alert sent: %s", [p["market"]["symbol"] for p in hot])

    # 6b. digest at 8:00 / 18:00: everything that still qualifies, re-verified live
    digest = due_digest(s)
    if digest:
        picks, dropped = verify_live(qualified[:config.MAX_PICKS + 2], scan_prices) if qualified else ([], [])
        picks = sizing.plan(picks[:config.MAX_PICKS])
        if picks:
            subject, body = report.build(picks, summ, hits, s, stats,
                                         {k for k, t in s["instant_sent"].items() if t >= realert})
        else:
            subject, body = report.build_status(summ, hits, s, stats, deep, dropped)
        report.send(subject, body)
        s["digests_sent"].append(digest)
        for p in picks:
            s["alerts"][p["key"]] = now
            sent.setdefault(p["key"], p)
        log.info("Digest %s sent with %d pick(s)", digest, len(picks))
        with open("out/latest.html", "w") as f:
            f.write(body)
    elif not hot:
        log.info("Scan done: %d qualifying (none 80+ with a fresh signal); next digest at %s",
                 len(qualified), " / ".join(config.DIGEST_TIMES))

    tracker.log_picks(s, deep, sent)
    st.save(s)


def main():
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        every = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 180
        while True:
            start = time.time()
            try:
                run()
            except Exception:
                log.exception("scan failed; retrying next loop")
            time.sleep(max(10, every - (time.time() - start)))
    else:
        run()


if __name__ == "__main__":
    main()
