"""Base/BNB with blocked log history: a new top-trader buy is still caught by holdings snapshots."""
import os, sys, shutil, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402
from scanner import main, state  # noqa: E402

shutil.rmtree("out/demo_state", ignore_errors=True)
main.run()
k = "bsc:0x2222222222222222222222222222222222222222"
new_trader = demo.TRADERS[2]["wallets"]["evm"]           # rank #3 buys BNDOG between scans
demo.buys.setdefault(new_trader, []).append(k)
main.run()
s = state.load()
fresh = [e for e in s["trader_buys"] if e["key"] == k and e["wallet"] == new_trader
         and e["side"] == "buy" and time.time() - e["ts"] < 120]
assert fresh, "new BNB buy not detected"
print("new BNB buy detected from holdings:", fresh[0]["handle"], f"${fresh[0]['usd']}")
