"""Traders you follow are tracked like the top 100, and young coins can qualify at 2h
once 2+ tracked traders hold them."""
import os, sys, shutil, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import config, fomo, holders, main, report, scoring, state, traders  # noqa: E402

report.send = lambda *a: True
shutil.rmtree("out/demo_state", ignore_errors=True)
assert len(config.FOLLOW_TRADERS) == 10 and "orangie" in config.FOLLOW_TRADERS
for _ in range(4):                       # profile lookups are spread out, 3 per scan
    main.run()
s = state.load()
tr = fomo.tracked(s)
fol = [t for t in tr if t.get("followed")]
names = {t["handle"].lower() for t in fol}
# (in the demo, ether_monk's fake wallet IS trader1's, so it's correctly not added twice)
assert {h.lower() for h in config.FOLLOW_TRADERS} - {"ether_monk"} <= names, sorted(names)
assert len(tr) == 100 + 9, len(tr)
assert "ether_monk" in s["followed"], "ether_monk's wallets should still be looked up and cached"
off = next(t for t in fol if t["handle"] == "orangie")
assert off["rank"] > 100 and traders.tag(off["rank"]) == "followed"
assert traders.rank_weight(off["rank"]) == traders.rank_weight(config.FOLLOW_RANK)
print(f"tracking {len(tr)} traders: top 100 + {len(fol)} followed")

# orangie buys GRIND -> caught on the next scan, shown as 'followed' in the reasons
GRIND = "solana:GRNDx111111111111111111111111111111111pump"
demo.buys.setdefault(off["wallet"], []).append(GRIND)
main.run()
s = state.load()
ev = [e for e in s["trader_buys"] if e["key"] == GRIND and e["handle"] == "orangie" and e["side"] == "buy"]
assert ev and ev[0]["usd"], "followed trader's buy not detected"
ctx = holders.context(s, fomo.tracked(s))
held = holders.held_events(s, ctx, GRIND, demo.COINS[GRIND][1])
assert "orangie" in {e["handle"] for e in held}
sm = scoring.smart_money([e for e in s["trader_buys"] if e["key"] == GRIND] + held, GRIND)
m = {"price": 0.0021, "mcap": 2_100_000, "liquidity": 210_000, "created_ms": (time.time() - 9 * 86400) * 1000,
     "change": {}, "volume": {}, "buys_h1": 0, "sells_h1": 0, "key": GRIND, "symbol": "GRIND"}
assert "orangie" in sm["buyers"], sm["buyers"]
only = scoring.smart_money([e for e in held if e["handle"] == "orangie"], GRIND)
r = scoring.score(m, only)
assert any("orangie (followed" in x for x in r["reasons"]), r["reasons"]
print("followed buy caught:", next(x for x in r["reasons"] if "orangie" in x)[:110])

# followed traders never get the 'new face' discount (the learner can still cut them)
s["lb_days"] = [f"2026-08-{d:02d}" for d in range(1, 21)]
assert traders.trust(s, "orangie") >= 1.0 and traders.trust(s, "some_stranger") < 1.0

# 2h early entry with 2+ tracked traders holding
m["created_ms"] = (time.time() - 2.5 * 3600) * 1000
assert config.EARLY_PAIR_AGE_HOURS == 2
assert scoring.eligible(m, 2) and not scoring.eligible(m, 1)
m["created_ms"] = (time.time() - 1.5 * 3600) * 1000
assert not scoring.eligible(m, 9)
print("2h early-entry rule OK")
