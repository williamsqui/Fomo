"""Regression for the Sep 21 false SELLs ($STONKEX, $SCHIFFY).

Both coins had top-100 whales holding $100k+ live, yet scored ~20/100 and got SELL:
 - traders who bought more than 6h ago were dropped from the score even though still holding
 - a whale trimming a little of a big bag was counted as "selling"
A genuine exit must still produce SELL.
"""
import os, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import check, position, report, state as st  # noqa: E402

assert demo.COINS
report.send = lambda subj, body: True
G = "GRNDx111111111111111111111111111111111pump"
NOW = time.time()


def ev(k, h, side, age_h, amt):
    return {"ts": NOW - age_h * 3600, "handle": h, "rank": 10, "key": k,
            "side": side, "amount": amt, "usd": 500}


def run(extra, keep=None):
    shutil.rmtree("out/demo_state", ignore_errors=True)
    s = st.load()
    m = check.find_market(G, "solana")
    _, h = check.analyze(s, m)
    hs = [x["handle"] for x in h]
    s["trader_buys"] += extra(m["key"], hs)
    real = check.solana_holders
    if keep is not None:           # simulate some of them having sold out completely
        def fake(s_, mint, traders):
            out, ok = real(s_, mint, traders)
            wmap = {t["wallet"]: t["handle"] for t in traders}
            return {w: a for w, a in out.items() if wmap.get(w) in keep(hs)}, ok
        check.solana_holders = fake
    try:
        r, _ = check.analyze(s, m)
    finally:
        check.solana_holders = real
    return r, position.decide(r, -14, 21)[0], hs


base, v0, hs = run(lambda k, hs: [])
assert v0 == "HOLD", v0

# STONKEX: bought 10h ago, still holding -> must still count
r, v, _ = run(lambda k, hs: [ev(k, h, "buy", 10, 1e5) for h in hs])
assert len(r["smart"]["buyers"]) == 4, r["smart"]["buyers"]
assert r["score"] >= base["score"] - 2, (base["score"], r["score"])
assert v == "HOLD", v

# SCHIFFY: bought 10h ago, one trimmed 10% an hour ago, all still holding -> trim, not a sell
r, v, _ = run(lambda k, hs: [ev(k, h, "buy", 10, 1e5) for h in hs] + [ev(k, hs[0], "sell", 1, 1e4)])
assert not r["smart"]["sellers"], f"a trader still holding was scored as a seller: {r['smart']['sellers']}"
assert r["smart"]["trimmed"] == [hs[0]], r["smart"]["trimmed"]
assert v == "HOLD", v

# a real exit: they bought, sold everything, hold nothing now -> SELL must still fire
r, v, _ = run(lambda k, hs: [ev(k, h, "buy", 3, 1e5) for h in hs] + [ev(k, h, "sell", 1, 1e5) for h in hs],
              keep=lambda hs: set())
assert len(r["smart"]["sellers"]) == 4 and not r["smart"]["buyers"], r["smart"]
assert v == "SELL", f"a genuine exit by every top trader must still say SELL, got {v}"

print(f"false-sell checks OK  (aged/trimmed holders: HOLD at {base['score']}; real exit: SELL at {r['score']})")
