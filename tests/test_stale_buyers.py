"""A top trader who bought earlier but holds none of it now must not count as smart money.

This is the $CATE case: the scanner's log said "4 traders bought it", the live wallet
read said "none of them hold it". The live read wins.
"""
import os, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import check, position, report, state as st  # noqa: E402

assert demo.COINS
report.send = lambda subj, body: True
shutil.rmtree("out/demo_state", ignore_errors=True)

TINY = "TINYx222222222222222222222222222222222pump"
s = st.load()
m = check.find_market(TINY, "solana")
assert m, "demo coin not found"

base, holders = check.analyze(s, m)
assert base["live_holders"] is True, "live holder read should succeed in the demo"
live = {h["handle"] for h in holders}
ghost = next(f"trader{i}" for i in range(1, 101) if f"trader{i}" not in live)

# the scanner logged a buy from someone who has since sold out
s["trader_buys"].append({"ts": time.time() - 600, "handle": ghost, "rank": 5, "key": m["key"],
                         "side": "buy", "amount": 1e5, "usd": 500})
after, _ = check.analyze(s, m)

assert ghost in after["exited"], f"{ghost} sold out but wasn't flagged: {after['exited']}"
assert ghost not in after["smart"]["buyers"], "a trader who holds none of it must not count as a buyer"
assert after["score"] <= base["score"], f"stale buy inflated the score: {base['score']} -> {after['score']}"

# and the position verdict must be penalised, not rewarded, by that exit
v_after = position.decide(after, 5, 25)
assert any("sold out" in x for x in v_after[3]), f"exit not shown against holding: {v_after[3]}"
assert not any("sold out" in x for x in v_after[2]), "an exit must never appear as a reason TO hold"

# when the wallets can't be read we must say so, not assume they sold
real = check.solana_holders
check.solana_holders = lambda *a, **k: ({}, False)
blind, _ = check.analyze(s, m)
check.solana_holders = real
assert blind["live_holders"] is False and not blind["exited"], "a failed lookup must not imply a sell-off"
plus = position.decide(blind, 5, 25)[2]
assert any("not confirmed" in x for x in plus), f"unverified holders not labelled: {plus}"

print(f"stale-buyer checks OK  (score {base['score']} -> {after['score']} once {ghost}'s exit was seen)")
