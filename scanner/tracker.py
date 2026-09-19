"""Track record: did the coins we scored actually hit +50%?

Every deep-checked coin at/above TRACK_SCORE_MIN is logged with its entry price
(emailed picks are marked 'sent'). Each scan updates the highest price seen
(10-minute snapshots, so real peaks may be higher). After TRACK_WINDOW_HOURS a pick
closes as HIT or MISS. Once there is enough history, the email shows the real hit
rate for each score band next to every pick.
"""
import time

from . import config

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
            continue
        p = {"ts": time.time(), "key": k, "symbol": r["market"]["symbol"],
             "chain": r["market"]["chain"], "score": r["score"], "band": band(r["score"]),
             "sent": False, "entry": price, "max": price, "min": price, "hit": False, "closed": False}
        if k in sent:
            p.update(_sent_fields(sent[k], price))
        s["picks"].append(p)


def _sent_fields(r, price):
    return {"sent": True, "sent_ts": time.time(), "sent_price": price, "size": r.get("size", 0),
            "target": r.get("target_price") or price * (1 + config.TARGET_GAIN_PCT / 100),
            "stop": r.get("stop_price") or price * (1 - config.STOP_LOSS_PCT / 100),
            "buyers": list(r["smart"]["buyers"]), "warned": []}


def exit_checks(s, markets):
    """Warnings for coins we emailed: target hit, stop hit, or the smart money that bought is selling."""
    out = []
    for p in s["picks"]:
        if not p.get("sent") or time.time() - p["sent_ts"] > config.TRACK_WINDOW_HOURS * 3600:
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
        sold = sorted({e["handle"] for e in s["trader_buys"] if e["key"] == p["key"] and e["side"] == "sell"
                       and e["ts"] >= p["sent_ts"] and e["handle"] in p["buyers"]} - set(w))
        if sold:
            w.extend(sold)
            out.append((p, f"leaderboard trader(s) who bought are now selling: {', '.join(sold)} - consider exiting", chg))
    return out


def open_keys(s):
    return [p["key"] for p in s["picks"] if not p["closed"]]


def update(s, markets):
    target = 1 + config.TARGET_GAIN_PCT / 100
    for p in s["picks"]:
        if p["closed"]:
            continue
        m = markets.get(p["key"])
        if m and m["price"] > 0:
            p["max"] = max(p["max"], m["price"])
            p["min"] = min(p.get("min", p["entry"]), m["price"])
        if p["max"] >= p["entry"] * target:
            p["hit"] = True
        if p["hit"] or time.time() - p["ts"] > config.TRACK_WINDOW_HOURS * 3600:
            p["closed"] = True


def summary(s, days=14):
    since = time.time() - days * 86400
    done = [p for p in s["picks"] if p["ts"] >= since and p["closed"]]
    out = {}
    for _, _, name in BANDS:
        ps = [p for p in done if p.get("band") == name]
        hits = sum(p["hit"] for p in ps)
        out[name] = {"n": len(ps), "hits": hits, "rate": round(100 * hits / len(ps)) if ps else None}
    sent = [p for p in done if p.get("sent")]
    out["emailed"] = {"n": len(sent), "hits": sum(p["hit"] for p in sent),
                      "rate": round(100 * sum(p["hit"] for p in sent) / len(sent)) if sent else None}
    recent_hits = sorted([p for p in s["picks"] if p["hit"] and p["ts"] >= since],
                         key=lambda p: -p["max"] / p["entry"])[:5]
    return out, recent_hits


def calibrated(summary_, score, min_n=15):
    b = summary_.get(band(score) or "")
    if b and b["n"] >= min_n:
        return f"{b['rate']}% of past picks scoring {band(score)} hit +{config.TARGET_GAIN_PCT:.0f}% ({b['n']} tracked)"
    return None
