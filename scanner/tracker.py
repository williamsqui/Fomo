"""Track record: did the coins we scored actually hit +50%?

Every deep-checked coin at/above TRACK_SCORE_MIN is logged with its entry price
(emailed picks are marked 'sent'). Each scan updates the highest price seen
(10-minute snapshots, so real peaks may be higher). After TRACK_WINDOW_HOURS a pick
closes as HIT or MISS. Once there is enough history, the email shows the real hit
rate for each score band next to every pick.
"""
import time

from . import config, holders

BANDS = [(75, 101, "75+"), (62, 75, "62-74"), (50, 62, "50-61")]


def band(score):
    for lo, hi, name in BANDS:
        if lo <= score < hi:
            return name
    return None


def log_picks(s, scored, sent):
    """sent = {key: pick dict} for coins emailed this run."""
    open_ = {p["key"]: p for p in s["picks"] if not p["closed"]}
    for r in scored:
        k, price = r["key"], r["market"]["price"]
        if r["score"] < config.TRACK_SCORE_MIN or price <= 0:
            continue
        if k in open_:
            if k in sent and not open_[k].get("sent"):  # upgraded to an emailed pick
                open_[k].update(_sent_fields(sent[k], price))
            if r.get("paper_ok", r.get("qualified")) and not open_[k].get("paper"):  # now passes the rules
                open_[k]["paper"] = {"pending": True, "since": time.time(), "feat": features(r)}
            continue
        p = {"ts": time.time(), "key": k, "symbol": r["market"]["symbol"],
             "chain": r["market"]["chain"], "score": r["score"], "band": band(r["score"]),
             "sent": False, "entry": price, "max": price, "min": price, "hit": False, "closed": False,
             "pair": r["market"].get("pair", ""), "prev": price}
        if r.get("paper_ok", r.get("qualified")):
            # paper trade: "bought" PAPER_LAG_MIN later at the live price, closed by your rules
            p["paper"] = {"pending": True, "feat": features(r)}
        if k in sent:
            p.update(_sent_fields(sent[k], price))
        s["picks"].append(p)


FLAG_TYPES = {  # warning text -> short category the learner can count
    "mint": "owner can mint", "rug risk": "rug risk", "unlocked": "liquidity unlocked",
    "chasing": "chasing a pump", "botted": "botted X chatter", "distribution": "distribution (red volume)",
    "new faces": "only new-face traders", "overextended": "overextended chart",
    "7-day high": "far below 7-day high", "sold:": "a top trader sold", "downtrend": "downtrend",
    "young coin": "young coin (early entry)",
}


def features(r):
    """What the scanner knew when it picked this coin - so paper results can be traced back."""
    m, sm = r["market"], r.get("smart") or {}
    flags = " ".join(r.get("flags") or []).lower()
    return {"score": r["score"], "chain": m.get("chain"), "mcap": m.get("mcap"),
            "pts": {k: round(v, 1) for k, v in (r.get("points") or {}).items()},
            "buyers": list(sm.get("buyers") or [])[:8], "runup": r.get("runup"),
            "flags": sorted({v for k, v in FLAG_TYPES.items() if k in flags})}


def _sent_fields(r, price):
    return {"sent": True, "sent_ts": time.time(), "sent_price": price, "size": r.get("size", 0),
            "target": r.get("target_price") or price * (1 + config.TARGET_GAIN_PCT / 100),
            "stop": r.get("stop_price") or price * (1 - config.STOP_LOSS_PCT / 100),
            "buyers": list(r["smart"]["buyers"]), "warned": []}


def exit_checks(s, markets, only=None):
    """Warnings for coins we emailed: target hit, stop hit, or the smart money that bought is selling."""
    out = []
    for p in s["picks"]:
        if not p.get("sent") or time.time() - p["sent_ts"] > config.TRACK_WINDOW_HOURS * 3600:
            continue
        if only is not None and p["key"] not in only:     # you didn't buy it: no follow-ups
            continue
        m = markets.get(p["key"])
        price = m["price"] if m else None
        chg = (price / p["sent_price"] - 1) * 100 if price else None
        w = p["warned"]
        if price and price >= p["target"] and "target" not in w:
            w.append("target")
            out.append((p, f"hit the +{config.TARGET_GAIN_PCT:.0f}% target - take profit", chg))
        if price and price <= p["stop"] and "stop" not in w:
            w.append("stop")
            out.append((p, "hit the stop-loss - exit to protect your bankroll", chg))
        # only real exits: a whale trimming 10% is taking profit, not leaving (holders.sold_out)
        sold = sorted({e["handle"] for e in s["trader_buys"] if e["key"] == p["key"] and e["side"] == "sell"
                       and e["ts"] >= p["sent_ts"] and e["handle"] in p["buyers"]
                       and holders.sold_out(s, e)} - set(w))
        if sold:
            w.extend(sold)
            out.append((p, f"leaderboard trader(s) who bought are now selling: {', '.join(sold)} - consider exiting", chg))
    return out


def paper_open(p):
    pp = p.get("paper")
    return bool(pp) and not pp.get("done")


def open_keys(s):
    return [p["key"] for p in s["picks"] if not p["closed"] or paper_open(p)]


def trade_pnl(size, entry, exit_px, slip_pct=None):
    """$ result of one round trip: FOMO fee each way (0.5%, min $0.95) + slippage each way."""
    slip = (config.PAPER_SLIPPAGE_PCT if slip_pct is None else slip_pct) / 100
    fee_in = max(config.FEE_MIN_USD, size * config.FEE_PCT / 100)
    tokens = (size - fee_in) / (entry * (1 + slip))
    gross = tokens * exit_px * (1 - slip)
    fee_out = max(config.FEE_MIN_USD, gross * config.FEE_PCT / 100)
    return round(gross - fee_out - size, 2)


def _paper(p, price, now):
    """Advance one paper trade using this scan's live price (10-minute snapshots)."""
    pp = p["paper"]
    if pp.get("pending"):
        if now - pp.get("since", p["ts"]) < config.PAPER_LAG_MIN * 60:
            return
        pp.update(pending=False, entry=price, entry_ts=now, size=config.PAPER_SIZE_USD, prev=price,
                  target=price * (1 + config.TARGET_GAIN_PCT / 100),
                  stop=price * (1 - config.STOP_LOSS_PCT / 100))
        return
    prev = pp.get("prev", price)
    pp["prev"] = price
    # a stop or target only counts when two scans in a row agree, so one bad price from the
    # data feed can't "win" or "lose" a trade by itself
    how = ("stop" if max(price, prev) <= pp["stop"] else "target" if min(price, prev) >= pp["target"]
           else "time" if now - pp["entry_ts"] > config.TRACK_WINDOW_HOURS * 3600 else None)
    if how:
        pp.update(done=True, how=how, exit=price, exit_ts=now,
                  pnl=trade_pnl(pp["size"], pp["entry"], price))


def update(s, markets):
    target = 1 + config.TARGET_GAIN_PCT / 100
    now = time.time()
    for p in s["picks"]:
        m = markets.get(p["key"])
        price = m["price"] if m and m["price"] > 0 else None
        if paper_open(p):
            if price:
                p["last"] = price
                _paper(p, price, now)
            elif now - p["ts"] > (config.TRACK_WINDOW_HOURS + 6) * 3600:
                # coin vanished from the market data (often a rug): close at the last price seen
                pp = p["paper"]
                if pp.get("pending"):
                    pp.update(done=True, how="never filled", pnl=0.0)
                else:
                    last = p.get("last", pp["entry"])
                    pp.update(done=True, how="no price data", exit=last, exit_ts=now,
                              pnl=trade_pnl(pp["size"], pp["entry"], last))
        if p["closed"]:
            continue
        if price and m.get("pair") and p.get("pair") and m["pair"] != p["pair"]:
            p["pair"], p["prev"] = m["pair"], None      # price source changed pool: don't trust this jump
        elif price:
            # a new high only counts once two scans in a row see it (filters one-off bad prices,
            # like the "+385,008%" JUPCAT reading)
            prev = p.get("prev") or price
            p["max"] = max(p["max"], min(price, prev))
            p["min"] = min(p.get("min", p["entry"]), max(price, prev))
            p["prev"] = price
        if p["max"] >= p["entry"] * target:
            p["hit"] = True
        if p["hit"] or time.time() - p["ts"] > config.TRACK_WINDOW_HOURS * 3600:
            p["closed"] = True


SUSPECT_GAIN = 50      # a 50x "gain" inside 48h is almost always a bad price, not a trade


def _suspect(p):
    return p["max"] / p["entry"] > SUSPECT_GAIN if p.get("entry") else True


def summary(s, days=14):
    """Hit rates per score band - fair version.

    Only picks whose full 48h window has passed are counted. Before, a hit closed at once but
    a miss only after 48h, so in the first two days almost every closed pick was a hit and the
    table showed a meaningless 100%.
    """
    now = time.time()
    since = now - days * 86400
    window = config.TRACK_WINDOW_HOURS * 3600
    recent = [p for p in s["picks"] if p["ts"] >= since and not _suspect(p)]
    done = [p for p in recent if now - p["ts"] >= window]
    waiting = [p for p in recent if now - p["ts"] < window]
    out = {}
    for _, _, name in BANDS:
        ps = [p for p in done if p.get("band") == name]
        hits = sum(p["hit"] for p in ps)
        out[name] = {"n": len(ps), "hits": hits, "rate": round(100 * hits / len(ps)) if ps else None,
                     "open": sum(1 for p in waiting if p.get("band") == name)}
    sent = [p for p in done if p.get("sent")]
    out["emailed"] = {"n": len(sent), "hits": sum(p["hit"] for p in sent),
                      "rate": round(100 * sum(p["hit"] for p in sent) / len(sent)) if sent else None,
                      "open": sum(1 for p in waiting if p.get("sent"))}
    out["paper"] = paper_summary(s, days)
    from . import learn
    out["learn"] = learn.summary(s)
    recent_hits = sorted([p for p in recent if p["hit"]], key=lambda p: -p["max"] / p["entry"])[:5]
    return out, recent_hits


def calibrated(summary_, score, min_n=15):
    b = summary_.get(band(score) or "")
    if b and b["n"] >= min_n:
        return f"{b['rate']}% of past picks scoring {band(score)} hit +{config.TARGET_GAIN_PCT:.0f}% ({b['n']} tracked)"
    return None


def paper_summary(s, days=14):
    """Paper-trading results: every coin that passed all rules, bought 20 min later, your exits."""
    since = time.time() - days * 86400
    trades = [p for p in s["picks"] if p.get("paper") and p["ts"] >= since]
    done = [p for p in trades if p["paper"].get("done") and p["paper"].get("how") != "never filled"]
    rows = {}
    for name in [b[2] for b in BANDS] + ["all"]:
        ps = [p for p in done if name == "all" or p.get("band") == name]
        pnl = [p["paper"]["pnl"] for p in ps]
        rows[name] = {"n": len(ps),
                      "wins": sum(p["paper"]["how"] == "target" for p in ps),
                      "stops": sum(p["paper"]["how"] == "stop" for p in ps),
                      "pnl": round(sum(pnl), 2),
                      "avg": round(sum(pnl) / len(pnl), 2) if pnl else None}
    rows["open"] = sum(1 for p in trades if not p["paper"].get("done"))
    rows["size"] = config.PAPER_SIZE_USD
    return rows
