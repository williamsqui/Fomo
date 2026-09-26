"""One scan (runs every 10 minutes).

leaderboard -> what each trader's wallet holds now vs last scan (Solana) / their transfers +
holdings snapshots (Base/BNB/Robinhood) -> candidate coins (fresh buys, trending, and every
coin top traders are still in) -> investable filter ($500k+ mcap, liquidity, age - 4h instead
of 12h when 2+ top traders hold it) -> quick score (current holders always count; trims are
not exits; old bags count half)
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

from . import (chains, chart, config, dex, early, evm, fomo, holders, report, safety, scoring, sizing, socials,
               solana, state as st, tracker, traders as reputation, watchlist, xsocial, learn, copy)

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


def verify_live(picks, scan_prices, s=None):
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
        # price when the top traders got in: their first buy in the last 6h, else the earliest
        # buy we logged for the traders still holding (up to 48h back)
        base = price_at((p.get("chart") or {}).get("closes"), p["smart"].get("first_buy") or p.get("entry_ts"))
        p["runup"] = (m["price"] / base - 1) * 100 if base else None
        h6 = (m.get("change") or {}).get("h6")
        if drop > config.MAX_DROP_SINCE_SCAN_PCT:
            dropped.append((p, f"fell {drop:.0f}% while being checked"))
        elif p["runup"] is not None and p["runup"] > config.MAX_RUNUP_PCT:
            dropped.append((p, f"already +{p['runup']:.0f}% since the top traders bought - too late"))
        elif p["runup"] is None and h6 is not None and h6 > config.RUNUP_H6_MAX:
            # holders only, no buy time known: don't chase a coin that just ran
            dropped.append((p, f"already +{h6:.0f}% in the last 6h - too late"))
        elif s is not None and (leaving := still_in(s, p)):
            dropped.append((p, leaving))
        else:
            ok.append(p)
    for p, why in dropped:
        log.info("dropped %s: %s", p["market"]["symbol"], why)
    return ok, dropped


def still_in(s, p):
    """Re-read the holders' wallets live seconds before emailing (Solana, a few credits).
    Returns a reason to drop the pick if most of them just left, else ''."""
    ws = p.get("holder_wallets") or []
    if not ws or not p["key"].startswith("solana:"):
        return ""
    from .check import solana_holders
    live, complete = solana_holders(s, chains.split(p["key"])[1], [{"wallet": w} for w in ws])
    if not complete:
        return ""
    left = sum(1 for w in ws if live.get(w, 0) > 0)
    if left * 2 < len(ws):
        return f"only {left} of the {len(ws)} top traders who held it still do (checked live) - they're leaving"
    return ""


def run():
    s = st.load()
    if not config.FOMO_API_KEY:
        log.error("Missing FOMO_API_KEY secret")
        sys.exit(1)
    now = time.time()
    s["run_count"] += 1
    watchlist.merge_requests(s)   # coins added/removed via "Check my position"
    watchlist.drop_auto(s)        # only coins you hold are watched (no more auto-added alerts)
    added = s.pop("_watch_added", [])
    if added:                     # proof the request really arrived
        report.send(*watchlist.build_added_email(added))
    learn.update(s)               # what the paper trades have taught (refreshed every 6h)

    # 1. leaderboard + followed traders (top 50 and followed every run, the rest every 3rd run)
    traders = fomo.tracked(s)             # the top 100 + the traders you follow
    handles = [t["handle"] for t in traders]
    every = s["run_count"] % config.SLOW_WALLET_EVERY == 0
    scan_traders = [t for t in traders if every or t["rank"] <= config.FAST_WALLETS or t.get("followed")]

    # 2. what they traded since the last scan
    if "solana" in config.CHAINS and config.HELIUS_API_KEY:
        solana.scan_wallets(s, scan_traders)          # full holdings of each wallet, diffed
        live = {t["wallet"] for t in traders if t.get("wallet")}
        s["wallet_snap"] = {w: v for w, v in (s.get("wallet_snap") or {}).items() if w in live}
    evm.scan_wallets(s, traders)
    ctx = holders.context(s, traders)
    cutoff = now - config.LOOKBACK_HOURS * 3600
    recent = [e for e in s["trader_buys"] if e["ts"] >= cutoff]
    new_buy_keys = {e["key"] for e in recent if e["side"] == "buy" and e["ts"] >= now - 20 * 60}
    # wallet-diff changes still waiting for a price (this scan's, or a DexScreener hiccup earlier)
    unpriced = [e for e in s["trader_buys"] if e.get("src") == "snap" and e.get("usd") is None]

    # 3. candidates: fresh buys, trending, and every coin top traders are still IN
    trending = {t["key"]: t["rank"] for t in fomo.trending(s) if "key" in t}
    gt_trend = [k for c in config.CHAINS for k in chart.trending_tokens(s, c)]
    held_keys = holders.candidate_keys(s, ctx, now)
    cands = list(dict.fromkeys([e["key"] for e in recent if e["side"] == "buy"] + list(trending) + gt_trend
                               + held_keys))
    cands = [k for k in cands if chains.split(k)[1] not in chains.QUOTE_ADDRESSES]
    watch = [p["key"] for p in s["picks"] if p.get("sent") and now - p["sent_ts"] < config.TRACK_WINDOW_HOURS * 3600]
    markets = dex.tokens(list(dict.fromkeys(cands + tracker.open_keys(s) + watch + watchlist.keys(s)
                                            + [e["key"] for e in unpriced])))
    failed = set(dex.FAILED)           # lookups that errored: unknown, NOT "not listed"
    for e in s["trader_buys"]:  # holdings diffs / EVM transfers only have token amounts - price them
        if e.get("usd") is None and e["key"] in markets:
            e["usd"] = round(e["amount"] * markets[e["key"]]["price"], 2)
    # wallet-diff changes on coins nobody can trade (spam airdrops, dust) are not trades. If the
    # lookup itself failed, keep the change and price it next scan (give up after 6h).
    def junk(e):
        if e["key"] in failed and now - e.get("seen", e["ts"]) < 6 * 3600:
            return False
        m = markets.get(e["key"])
        return not m or (e["usd"] or 0) < evm.MIN_EVENT_USD or m["liquidity"] < 10_000
    bad = {id(e) for e in unpriced if junk(e)}
    s["trader_buys"] = [e for e in s["trader_buys"] if id(e) not in bad]
    for k in held_keys:                # unlisted / untradeable: stop looking it up for a while
        if k in failed:
            continue
        if k not in markets:
            s["dex_skip"][k] = now + 6 * 3600
        elif markets[k]["liquidity"] < 10_000:
            s["dex_skip"][k] = now + 24 * 3600
    # 3b. Base/BNB/Robinhood: check what the top traders hold right now (works on free RPCs)
    investable = {k: m for k, m in markets.items() if k in cands and scoring.eligible(m, config.EARLY_MIN_HOLDERS)}
    evm.holdings_scan(s, traders, investable)
    ctx = holders.context(s, traders)
    recent = [e for e in s["trader_buys"] if e["ts"] >= cutoff]
    new_buy_keys = {e["key"] for e in recent if e["side"] == "buy" and e["ts"] >= now - 20 * 60}
    tracker.update(s, markets)
    # follow-up emails only for coins you actually hold (added via "Check my position")
    exits = tracker.exit_checks(s, markets, only={k for k in watchlist.keys(s) if watchlist.held(s, k)})
    if exits and config.EXIT_ALERTS:
        report.send(*report.build_exit(exits))
        log.info("Exit alerts: %s", [(p["symbol"], m) for p, m, _ in exits])
    if config.COPY_TRADER:
        copy.scan(s)                        # shadow book: paper-trade whatever one trader buys
    # early lane (high risk): fresh launchpad coins, its own alerts + paper record
    if config.EARLY_LANE and "solana" in config.CHAINS:
        try:
            e_alerts, e_papers, e_markets = early.scan(s, ctx, reputation.fn(s))
            early.step_paper(s, e_markets)
            for r in e_papers:
                early.open_paper(s, r)
            due = early.due_alerts(s, e_alerts)
            due = early.verify(due, {r["key"]: r["market"]["price"] for r in due}) if due else []
            if due:
                report.send(*early.build_email(due))
                early.mark_sent(s, due)
                log.info("Early alert sent: %s", [r["market"]["symbol"] for r in due])
            markets.update({k: v for k, v in e_markets.items() if k in watchlist.keys(s)})
        except Exception:
            log.exception("early lane failed this scan (main scan continues)")
    watched = watchlist.check(s, markets)   # coins you hold: top traders leaving, liquidity, stop/target
    if watched and config.EXIT_ALERTS:
        report.send(*watchlist.build_email(watched))

    # 4. investable filter + quick score (trades in the last 6h + what top traders hold right now)
    def events_for(k, m):
        # the same truth "Check my position" uses: a trader whose wallet (read after their buy)
        # holds none of it now has left, whatever we logged; everyone holding it counts, however
        # long ago they bought; and a seller who still holds is a trim, not an exit
        ev = holders.drop_exited(s, k, [e for e in s["trader_buys"] if e["key"] == k])
        return ev + holders.held_events(s, ctx, k, m["price"])

    def entry_ts(k, sm):
        """Earliest buy we logged (48h) for the traders in it now - for the too-late check."""
        ts = [e["ts"] for e in s["trader_buys"] if e["key"] == k and e["side"] == "buy"
              and not e.get("held") and e["handle"] in sm["buyers"]]
        return min(ts) if ts else None

    quick = {}
    for k in cands:
        m = markets.get(k)
        if not m or m["symbol"].upper() in chains.QUOTE_SYMBOLS:
            continue
        n_in = len(holders.held_events(s, ctx, k, m["price"]))
        if not scoring.eligible(m, n_in):
            continue
        sm = scoring.smart_money(events_for(k, m), k, reputation.fn(s))
        quick[k] = (m, sm, scoring.score(m, sm, trending_rank=trending.get(k)))

    # 5. full check of EVERY investable coin. Slow-changing data (chart, safety, holders, X) is
    # cached per coin; each scan fetches fresh data for up to FINALISTS coins - new top-trader
    # buys first, then the best quick scores, then whichever coin was refreshed longest ago -
    # so every investable coin gets a full, fresh check every few scans.
    def last_fetch(k):
        return max((e[0] for e in (s["deep_cache"].get(k) or {}).values()), default=0)

    order = sorted(quick, key=lambda k: (k not in new_buy_keys, -quick[k][2]["score"]))
    must = order[:max(1, config.FINALISTS // 3)]
    rest = sorted(order[len(must):], key=last_fetch)
    fetch_set = set(must + rest[:config.FINALISTS - len(must)])
    log.info("%d candidates, %d investable, refreshing %d this scan", len(cands), len(quick), len(fetch_set))

    def peek(k, name):
        e = (s["deep_cache"].get(k) or {}).get(name)
        return e[1] if e else None

    deep, x_used = [], 0
    for i, k in enumerate(order):
        m, _, _ = quick[k]
        fetch = k in fetch_set
        if not fetch and peek(k, "safety") is None:
            continue  # never fully checked yet - its turn comes in a later scan
        fresh_signal = k in new_buy_keys
        if fetch:
            sf, _ = cached(s, k, "safety", config.SAFETY_TTL_MIN, lambda: safety.check(m["chain"], m["address"]))
        else:
            sf = peek(k, "safety")
        ch, info, growth = None, {}, None
        x = th = tg = None
        if sf["ok"]:  # don't spend rate limits / credits on coins that already failed safety
            if fetch:
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
                else:
                    x = peek(k, "x")
                tg, _ = cached(s, k, "tg", config.INFO_TTL_MIN,
                               lambda: socials.telegram_members(m.get("telegram") or info.get("telegram")))
            else:
                ch, info, x, tg = peek(k, "chart"), peek(k, "info") or {}, peek(k, "x"), peek(k, "tg")
                growth = holder_growth(s, k, info.get("holders"), False)
            th = fomo.thesis(s, k) if i < 3 else (s["thesis_cache"].get(k) or {}).get("items")
        sm = scoring.smart_money(events_for(k, m), k, reputation.fn(s))
        r = scoring.score(m, sm, x=x, thesis=th, trending_rank=trending.get(k), chart=ch, safety=sf,
                          info=info, tg=tg, holder_growth=growth, deep=True)
        r["entry_ts"] = entry_ts(k, sm)
        r["holder_wallets"] = holders.wallets(s, ctx, k, m["price"])
        learn.gate(s, r)          # stricter bar where paper trades keep losing
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
    stats = {"traders": len(traders), "events": len(recent), "candidates": len(cands), "held": len(held_keys),
             "eligible": len(quick), "deep": len(deep), "fresh": len(fetch_set), "by_chain": by_chain}
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
        hot, _ = verify_live(hot[:config.MAX_PICKS], scan_prices, s)
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
        picks, dropped = verify_live(qualified[:config.MAX_PICKS + 2], scan_prices, s) if qualified else ([], [])
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
