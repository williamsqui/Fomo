"""Proven leaderboard regulars must outweigh traders who just had one lucky week."""
import os, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scanner import config, scoring, traders  # noqa: E402

day = lambda n: (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")
NOW = time.time()


def state(days, record):
    return {"lb_days": [day(i) for i in range(days - 1, -1, -1)], "trader_record": record}


def buy(handle, rank=5):
    return [{"ts": NOW - 600, "handle": handle, "rank": rank, "key": "solana:X",
             "side": "buy", "amount": 1e5, "usd": 800}]


# --- 1. not enough history yet: everybody is treated the same -----------------
early = state(5, {"veteran": {"days": [day(i) for i in range(5)], "best": 3},
                  "rookie": {"days": [day(0)], "best": 9}})
assert traders.trust(early, "veteran") == 1.0 and traders.trust(early, "rookie") == 1.0, \
    "must not rank traders before there is enough history"
assert traders.label(early, "veteran") == "", "no label while history is still building"
assert "building history" in traders.summary(early)

# --- 2. with 30 days of history the three groups separate --------------------
rec = {
    "veteran": {"days": [day(i) for i in range(30)], "best": 2},          # every day  -> proven
    "steady": {"days": [day(i) for i in range(0, 30, 3)], "best": 20},    # 1 in 3     -> regular
    "rookie": {"days": [day(0), day(1)], "best": 7},                      # 2 days     -> new
}
s = state(30, rec)
assert traders.trust(s, "veteran") == config.TRUST_PROVEN, traders.trust(s, "veteran")
assert traders.trust(s, "steady") == config.TRUST_REGULAR, traders.trust(s, "steady")
assert traders.trust(s, "rookie") == config.TRUST_UNKNOWN, traders.trust(s, "rookie")
assert traders.trust(s, "never-seen") == config.TRUST_UNKNOWN
assert traders.label(s, "veteran") == "proven" and traders.label(s, "rookie") == "new"
assert "30 of the last 30 days" in traders.describe(s, "veteran"), traders.describe(s, "veteran")

# --- 3. the same buy is worth more from the veteran than from the rookie -------
f = traders.fn(s)
vet = scoring.smart_money(buy("veteran"), "solana:X", f)
rook = scoring.smart_money(buy("rookie"), "solana:X", f)
assert vet["weight"] > rook["weight"], (vet["weight"], rook["weight"])
pts = lambda sm: scoring.MAX["smart"] * (1 - __import__("math").exp(-0.5 * sm["weight"]))
gap = pts(vet) - pts(rook)
assert gap > 3, f"trust should visibly change the smart-money points, got {gap:.1f}"

# --- 4. a coin whose only buyer is a new face must be flagged ------------------
mkt = {"price": 1.0, "mcap": 5e6, "liquidity": 3e5, "created_ms": (NOW - 9 * 86400) * 1000,
       "change": {"h1": 2, "h6": 1, "h24": 3}, "buys_h1": 30, "sells_h1": 25,
       "volume": {"h1": 1e4, "h6": 6e4, "h24": 2e5}, "symbol": "T", "chain": "solana",
       "address": "X", "pair": None, "url": "", "key": "solana:X"}
r_rook = scoring.score(mkt, rook, deep=False)
r_vet = scoring.score(mkt, vet, deep=False)
assert any("new faces" in x for x in r_rook["flags"]), r_rook["flags"]
assert not any("new faces" in x for x in r_vet["flags"])
assert any("proven" in x for x in r_vet["reasons"]), r_vet["reasons"]
assert r_vet["score"] > r_rook["score"], (r_vet["score"], r_rook["score"])

# --- 5. observing the leaderboard records today and forgets nothing recent ------
live = {"lb_days": [], "trader_record": {}}
traders.observe(live, [{"handle": "veteran", "rank": 2}, {"handle": "rookie", "rank": 7}])
traders.observe(live, [{"handle": "veteran", "rank": 4}])          # same day, no double count
assert live["lb_days"] == [day(0)] and live["trader_record"]["veteran"]["days"] == [day(0)]
assert live["trader_record"]["veteran"]["best"] == 2, "best rank must be the best ever seen"
assert "rookie" in live["trader_record"], "a trader must not be dropped the same day"

print(f"trader-reputation checks OK  (same buy: veteran {r_vet['score']} vs new face {r_rook['score']})")
