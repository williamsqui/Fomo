"""Live re-check before emailing: coins that already ran or are dumping must be dropped."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import main  # noqa: E402

k = "solana:GRNDx111111111111111111111111111111111pump"
live_price = demo.COINS[k][1]


def pick(price_when_bought, scan_price):
    t = time.time() - 3600
    return {"key": k, "market": {"symbol": "GRIND"}, "smart": {"first_buy": t + 60},
            "chart": {"closes": [[t, price_when_bought]]}}, {k: scan_price}


cases = [("fine", live_price / 1.10, live_price, True),              # +10% since buy
         ("ran too far", live_price / 1.60, live_price, False),      # +60% since buy
         ("dumping now", live_price, live_price * 1.25, False)]      # -20% during the scan
for name, bought, scanned, expect in cases:
    p, sp = pick(bought, scanned)
    ok, dropped = main.verify_live([p], sp)
    assert bool(ok) == expect, (name, dropped)
    print(f"{name:12s} -> {'sent' if ok else 'dropped: ' + dropped[0][1]}")
print("freshness checks OK")
