"""Exit alerts (+target, a buyer selling) go out only for coins you hold - never for alerts you skipped."""
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import dex, main, report, state, watchlist  # noqa: E402

sent = []
report.send = lambda subj, body: sent.append(subj) or True
shutil.rmtree("out/demo_state", ignore_errors=True)
shutil.rmtree("out/watchreq", ignore_errors=True)
main.run()
k1 = "solana:GRNDx111111111111111111111111111111111pump"
k2 = "base:0x1111111111111111111111111111111111111111"
s = state.load()
assert not s.get("watch"), "picks must not be auto-added to the watchlist"
buyer = next(p for p in s["picks"] if p["key"] == k2)["buyers"][0]


def bump_and_sell(s):
    s["trader_buys"].append({"ts": demo.NOW + 60, "sig": f"x{len(sent)}", "wallet": "w", "handle": buyer,
                             "rank": 1, "key": k2, "side": "sell", "amount": 1e5, "usd": 30})


# you skipped both alerts: nothing follows up
c = list(demo.COINS[k1]); c[1] *= 1.6; demo.COINS[k1] = tuple(c)
bump_and_sell(s)
state.save(s)
del sent[:]
main.run()
assert not any(x.startswith("EXIT ALERT") for x in sent), sent

# you bought both (ran Check my position): now the exit alerts come
s = state.load()
mk = dex.tokens([k1, k2])
for k in (k1, k2):
    s.setdefault("watch", {})[k] = watchlist._entry(mk[k], "manual", entry=mk[k]["price"])
bump_and_sell(s)
state.save(s)
main.run()
print(sent)
assert any(x.startswith("EXIT ALERT") and "GRIND" in x and "BASEY" in x for x in sent), sent
print("exit alerts OK (only for coins you hold)")
