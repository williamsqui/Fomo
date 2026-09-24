"""Shadow book: paper-trade whatever one trader buys, scored by their exits and by your rules."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scanner import config, copy, report  # noqa: E402

K = "solana:TINY"
PRICE = {"solana:GRIND": 1.0, K: 1.0}
HOLD = {"solana:GRIND": 100.0}
BASE = {"base:0xaaa": 50.0}
FAIL = set()          # chains whose read fails on this scan

copy.holdings = lambda s, w: {"solana": None if "solana" in FAIL else dict(HOLD),
                              "base": None if "base" in FAIL else dict(BASE)}
copy.wallets = lambda s: {"solana": "W1", "evm": None}
PRICE["base:0xaaa"] = 2.0
copy.dex.tokens = lambda keys: {k: {"key": k, "price": PRICE[k], "symbol": SYM.get(k, k.split(":")[1]),
                                    "chain": k.split(":")[0], "liquidity": 500_000}
                                for k in keys if k in PRICE}
SYM = {}
s = {}

# first scan is only a baseline: we must not "buy" everything they already hold
assert copy.scan(s) == [] and not s["copy"]["trades"]

# they buy TINY -> one paper trade at the live price
HOLD[K] = 5000.0
opened = copy.scan(s)
assert [t["symbol"] for t in opened] == ["TINY"], opened
t = s["copy"]["trades"][0]
assert t["entry"] == 1.0 and not t["mirror"]["done"] and not t["rules"]["done"]

# +60%: your rules take profit at +50% (two scans in a row), they are still holding
PRICE[K] = 1.6
copy.scan(s)
assert not t["rules"]["done"], "one scan alone must not close it"
copy.scan(s)
assert t["rules"]["how"] == "target" and 13 < t["rules"]["pnl"] < 16, t["rules"]
assert not t["mirror"]["done"], "they haven't sold yet"

# they sell out -> their exit is recorded too, at the price then
PRICE[K] = 1.4
HOLD.pop(K)
copy.scan(s)
assert t["mirror"]["how"] == "they sold" and 8 < t["mirror"]["pnl"] < 11, t["mirror"]
assert t["mirror"]["pnl"] < t["rules"]["pnl"], "in this example your rules beat their exit"

# a second coin that they never sell closes on the time limit, and a loser stops out
HOLD["solana:GRIND"] = 100.0
PRICE["solana:LOSS"] = 1.0
HOLD["solana:LOSS"] = 10.0
copy.scan(s)
loss = s["copy"]["trades"][-1]
PRICE["solana:LOSS"] = 0.6
copy.scan(s); copy.scan(s)
assert loss["rules"]["how"] == "stop" and loss["rules"]["pnl"] < -11, loss["rules"]
loss["ts"] -= (config.COPY_MAX_DAYS + 1) * 86400
copy.scan(s)
assert loss["mirror"]["how"].startswith("still holding after"), loss["mirror"]

# a failed read on one chain must not look like they sold everything on it, and the
# next good read must not look like they bought it all back (the old partial-read bug)
before = len(s["copy"]["trades"])
FAIL.add("base")
copy.scan(s)
assert len(s["copy"]["trades"]) == before, "a failed chain read must not open trades"
FAIL.clear()
assert copy.scan(s) == [], "a chain coming back must not look like fresh buys"
assert len(s["copy"]["trades"]) == before
open_keys = [t["key"] for t in s["copy"]["trades"] if not t["mirror"]["done"]]
assert "base:0xaaa" not in open_keys, open_keys

# a buy DexScreener can't name is counted, not copied
SYM["solana:MYSTERY"] = "UNKNOWN"
PRICE["solana:MYSTERY"] = 0.5
HOLD["solana:MYSTERY"] = 999.0
assert copy.scan(s) == [], "an unnamed token must not become a paper trade"
assert s["copy"]["unnamed"] == 1, s["copy"]["unnamed"]

# stablecoins and wrapped natives are never trades
assert copy._skip("base", "0x833589fcD6eDb6E08f4c7C32D4f71b54bdA02913")
assert copy._skip("solana", "So11111111111111111111111111111111111111112")

c = copy.summary(s)
assert c["their"]["n"] == 2 and c["yours"]["n"] == 2 and c["handle"] == config.COPY_TRADER
html = report.copy_table(s)
for want in ("Copying @", "Their exits", "Your rules", "$TINY", "skipped"):
    assert want in html, (want, html[:400])
print(f"copy-trader checks OK  (their exits {c['their']['pnl']:+.2f} vs your rules {c['yours']['pnl']:+.2f} on 2 trades)")
