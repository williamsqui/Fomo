"""Run once, then pump one pick +60% and have a buyer of another pick sell -> exit alerts."""
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import main, report, state  # noqa: E402

sent = []
report.send = lambda subj, body: sent.append(subj) or True
shutil.rmtree("out/demo_state", ignore_errors=True)
main.run()
k1 = "solana:GRNDx111111111111111111111111111111111pump"
k2 = "base:0x1111111111111111111111111111111111111111"
c = list(demo.COINS[k1]); c[1] *= 1.6; demo.COINS[k1] = tuple(c)
s = state.load()
buyer = next(p for p in s["picks"] if p["key"] == k2)["buyers"][0]
s["trader_buys"].append({"ts": demo.NOW + 60, "sig": "x", "wallet": "w", "handle": buyer, "rank": 1,
                         "key": k2, "side": "sell", "amount": 1e5, "usd": 30})
state.save(s)
main.run()
print(sent)
assert any(x.startswith("EXIT ALERT") and "GRIND" in x and "BASEY" in x for x in sent), sent
print("exit alerts OK")
