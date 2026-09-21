"""Unlocked-liquidity check: can one wallet pull the liquidity out from under you?"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scanner import safety, scoring  # noqa: E402

EOA = lambda pct, addr="0xabc": {"address": addr, "percent": pct, "is_locked": "0", "is_contract": "0"}

# $Surplus as GoPlus reported it on Sep 21: 45.75% in one ordinary wallet, nothing locked
surplus = [EOA("0.4575", "0x9a10"), EOA("0.0951"), EOA("0.0941"), EOA("0.0795"), EOA("0.0653")]
hard, flags, good = safety.lp_risk(surplus)
assert not hard, hard
assert any("one wallet can pull 46%" in f and "rug risk" in f for f in flags), flags
assert any(k in flags[0] for k in scoring.SEVERE), "must count as a severe flag and cut the score"

# a single wallet holding most of it -> rejected outright
hard, flags, _ = safety.lp_risk([EOA("0.82"), EOA("0.18")])
assert hard and "82%" in hard[0], hard

# locked in a locker / burned -> fine
hard, flags, good = safety.lp_risk([
    {"address": "0x000000000000000000000000000000000000dead", "percent": "0.60", "is_locked": "0"},
    {"address": "0xlocker", "percent": "0.38", "is_locked": "1", "is_contract": "1", "tag": "UNCX"},
    EOA("0.02")])
assert not hard and not flags and good == ["LP 98% locked/burned"], (hard, flags, good)

# unlocked but held by contracts (multisig / position manager): softer warning only
hard, flags, _ = safety.lp_risk([{"address": "0xsafe", "percent": "0.70", "is_locked": "0", "is_contract": "1"}])
assert not hard and flags and "spread over" in flags[0], flags

# no data must never read as "safe"
hard, flags, good = safety.lp_risk([])
assert not hard and not good and "not verified" in flags[0], flags

# tolerate percentages given as 45.75 instead of 0.4575
assert "46%" in safety.lp_risk([EOA("45.75")])[1][0]

print("liquidity-lock checks OK  ($Surplus -> 'one wallet can pull 46% of the liquidity (rug risk)')")
