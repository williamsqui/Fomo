"""Learning from the paper trades - carefully.

Every coin that passes the rules is paper-traded (tracker.py) and remembers what the
scanner knew when it picked it: score parts, chain, market cap, which top traders bought,
which warnings it carried. Once enough of those trades have closed, this module looks at
what actually made and lost money and adjusts four things:

  1. the minimum score to send a pick     (raised if lower scores keep losing)
  2. an extra score bar per chain          (if one chain keeps losing)
  3. a stricter bar for specific warnings  (e.g. "owner can mint" picks keep losing)
  4. how much each top trader's buys count (traders whose buys keep losing count less)

Guard rails, because a handful of meme-coin trades is mostly noise:
  * nothing changes until LEARN_MIN_TRADES paper trades have closed
  * each rule needs its own minimum sample before it can fire
  * it only ever makes the scanner STRICTER than your settings, never looser
  * it only uses the last LEARN_DAYS days, so old lessons fade out
  * every active adjustment is printed in your digest in plain words
It can't invent new signals - that part is still you and me reading the lessons it reports.
"""
import time

from . import config

REFRESH_H = 6


def _trades(s):
    since = time.time() - config.LEARN_DAYS * 86400
    out = []
    for p in s.get("picks") or []:
        pp = p.get("paper") or {}
        if p["ts"] >= since and pp.get("done") and pp.get("how") not in ("never filled", None):
            out.append({"score": (pp.get("feat") or {}).get("score", p.get("score", 0)),
                        "chain": p.get("chain"), "pnl": pp.get("pnl", 0.0), "win": pp.get("how") == "target",
                        "feat": pp.get("feat") or {}})
    return out


def _stat(ts):
    if not ts:
        return None
    return {"n": len(ts), "avg": round(sum(t["pnl"] for t in ts) / len(ts), 2),
            "win": round(100 * sum(t["win"] for t in ts) / len(ts))}


# ---------------------------------------------------------------- lessons (report only)
def _tests():
    pts = lambda t, k: (t["feat"].get("pts") or {}).get(k, 0)
    tests = [
        ("2+ top traders buying", lambda t: len(t["feat"].get("buyers") or []) >= 2),
        ("3+ top traders buying", lambda t: len(t["feat"].get("buyers") or []) >= 3),
        ("strong chart (12+ of 20 points)", lambda t: pts(t, "chart") >= 12),
        ("buying momentum (5+ of 10)", lambda t: pts(t, "momentum") >= 5),
        ("strong X buzz (10+ of 20)", lambda t: pts(t, "social") >= 10),
        ("market cap under $3M", lambda t: (t["feat"].get("mcap") or 0) < 3e6),
        ("already up 20%+ since top traders bought", lambda t: (t["feat"].get("runup") or 0) >= 20),
        ("score 70+", lambda t: t["score"] >= 70),
    ]
    for c in config.CHAINS:
        tests.append((f"on {c}", lambda t, c=c: t["chain"] == c))
    from .tracker import FLAG_TYPES
    for f in sorted(set(FLAG_TYPES.values())):
        tests.append((f"warning: {f}", lambda t, f=f: f in (t["feat"].get("flags") or [])))
    return tests


def lessons(trades, min_n=None):
    """[(label, with{n,avg,win}, without{...})] where both sides have enough trades, biggest gap first."""
    min_n = min_n or config.LEARN_MIN_GROUP
    rows = []
    featured = [t for t in trades if t["feat"]]
    for label, test in _tests():
        yes = [t for t in featured if test(t)]
        no = [t for t in featured if not test(t)]
        if len(yes) >= min_n and len(no) >= min_n:
            a, b = _stat(yes), _stat(no)
            rows.append((label, a, b, round(a["avg"] - b["avg"], 2)))
    return sorted(rows, key=lambda r: -abs(r[3]))


# ---------------------------------------------------------------- adjustments (applied)
def update(s, force=False):
    """Recompute what the scanner has learned (cheap; at most every REFRESH_H hours)."""
    L = s.setdefault("learned", {})
    if not force and time.time() - L.get("ts", 0) < REFRESH_H * 3600:
        return L
    trades = _trades(s)
    new = {"ts": time.time(), "n": len(trades), "min_score": None, "chain_extra": {},
           "flag_extra": {}, "trader_factor": {}, "losing": False}
    if len(trades) >= config.LEARN_MIN_TRADES:
        base = config.MIN_SEND_SCORE
        # 1. minimum score: lowest bar whose trades (score >= bar) made money
        options = []
        for bar in range(base, 81, 3):
            st = _stat([t for t in trades if t["score"] >= bar])
            if st and st["n"] >= config.LEARN_MIN_GROUP * 2:
                options.append((bar, st))
        good = [bar for bar, st in options if st["avg"] > 0]
        if good:
            new["min_score"] = good[0] if good[0] > base else None
        elif options:
            new["min_score"], new["losing"] = options[-1][0], True
        # 2. chains that keep losing need a higher score
        for c in {t["chain"] for t in trades}:
            st = _stat([t for t in trades if t["chain"] == c])
            if st["n"] >= config.LEARN_MIN_GROUP + 4 and st["avg"] < -1.0:
                new["chain_extra"][c] = 6
        # 3. warnings whose picks keep losing clearly more than the rest
        from .tracker import FLAG_TYPES
        feat = [t for t in trades if t["feat"]]
        for f in set(FLAG_TYPES.values()):
            yes = [t for t in feat if f in (t["feat"].get("flags") or [])]
            no = [t for t in feat if f not in (t["feat"].get("flags") or [])]
            a, b = _stat(yes), _stat(no)
            if a and b and a["n"] >= config.LEARN_MIN_GROUP and a["avg"] < -2 and a["avg"] < b["avg"] - 2:
                new["flag_extra"][f] = 5
        # 4. top traders whose buys keep losing (or winning)
        by = {}
        for t in feat:
            for h in t["feat"].get("buyers") or []:
                by.setdefault(h, []).append(t)
        for h, ts in by.items():
            st = _stat(ts)
            if st["n"] >= 6 and st["avg"] < -2:
                new["trader_factor"][h] = 0.6
            elif st["n"] >= 6 and st["avg"] > 3:
                new["trader_factor"][h] = 1.25
    s["learned"] = new
    return new


def trader_factor(s, handle):
    return ((s.get("learned") or {}).get("trader_factor") or {}).get(handle, 1.0)


def bar(s, r):
    """The score this coin needs after what the paper trades taught. Never below your setting."""
    L = s.get("learned") or {}
    need = max(config.MIN_SEND_SCORE, L.get("min_score") or 0)
    need += (L.get("chain_extra") or {}).get(r["market"]["chain"], 0)
    flags = " ".join(r.get("flags") or []).lower()
    from .tracker import FLAG_TYPES
    present = {v for k, v in FLAG_TYPES.items() if k in flags}
    need += sum(x for f, x in (L.get("flag_extra") or {}).items() if f in present)
    return need


def gate(s, r):
    """Apply the learned bar to a scored coin (only ever makes it stricter)."""
    need = bar(s, r)
    # paper-trade on your own rules, not the learned bar - otherwise the learner could only
    # ever see coins it already likes and would never notice a lesson going out of date
    r["paper_ok"] = bool(r.get("qualified"))
    if r.get("qualified") and r["score"] < need:
        r["qualified"] = False
        r["why_not"] = r.get("why_not", []) + [
            f"score {r['score']} is below {need}, the bar paper trading has taught for coins like this"]
    return r


def summary(s):
    """Plain-words status for the digest."""
    L = s.get("learned") or {}
    trades = _trades(s)
    out = {"n": len(trades), "need": config.LEARN_MIN_TRADES, "rules": [], "lessons": [], "losing": L.get("losing")}
    if L.get("min_score"):
        out["rules"].append(f"minimum score raised to {L['min_score']} (lower-scoring paper trades lost money)")
    for c, x in (L.get("chain_extra") or {}).items():
        out["rules"].append(f"{c} coins need +{x} points (that chain's paper trades keep losing)")
    for f, x in (L.get("flag_extra") or {}).items():
        out["rules"].append(f"coins with '{f}' need +{x} points (those picks keep losing)")
    weak = [h for h, v in (L.get("trader_factor") or {}).items() if v < 1]
    strong = [h for h, v in (L.get("trader_factor") or {}).items() if v > 1]
    if weak:
        out["rules"].append("buys count less from: " + ", ".join(sorted(weak)[:6]) + " (their picks keep losing)")
    if strong:
        out["rules"].append("buys count more from: " + ", ".join(sorted(strong)[:6]) + " (their picks keep winning)")
    for label, a, b, gap in lessons(trades)[:3]:
        out["lessons"].append(f"{label}: {a['win']}% hit, {a['avg']:+.2f}/trade over {a['n']} trades "
                              f"vs {b['avg']:+.2f} without")
    return out
