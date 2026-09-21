"""Paper trading: entry 20 min late at the live price, +50% / -30% / 48h exits, fees and slippage."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scanner import config, report, tracker  # noqa: E402

H = 3600


def pick(key, score, qualified=True):
    return {"key": key, "score": score, "qualified": qualified,
            "market": {"price": 1.0, "symbol": key.upper(), "chain": "solana"}, "smart": {"buyers": []}}


s = {"picks": []}
tracker.log_picks(s, [pick("win", 78), pick("lose", 70), pick("flat", 64), pick("junk", 58, qualified=False),
                      pick("gone", 66)], {})
assert sum(1 for p in s["picks"] if p.get("paper")) == 4, "every qualified coin gets a paper trade, others don't"

# pretend 25 minutes passed: the paper trade fills at THIS price, not the signal price
for p in s["picks"]:
    p["ts"] -= 25 * 60
mk = lambda prices: {f"solana:{k}" if False else k: {"price": v} for k, v in prices.items()}
tracker.update(s, mk({"win": 1.10, "lose": 1.10, "flat": 1.10, "gone": 1.10}))
pp = {p["key"]: p["paper"] for p in s["picks"] if p.get("paper")}
assert all(not v["pending"] and abs(v["entry"] - 1.10) < 1e-9 for v in pp.values()), pp

# target hit, stop hit (with a gap below the stop), still open
tracker.update(s, mk({"win": 1.70, "lose": 0.70, "flat": 1.20}))
assert pp["win"]["how"] == "target" and pp["win"]["pnl"] > 0, pp["win"]
assert pp["lose"]["how"] == "stop" and pp["lose"]["exit"] == 0.70, "a gap through the stop fills at the real price"
assert not pp["flat"].get("done")

# 48h later: flat closes on time; the coin with no price data (rugged / delisted) closes at its last price
for v in pp.values():
    if v.get("entry_ts"):
        v["entry_ts"] -= 49 * H
for p in s["picks"]:
    p["ts"] -= 55 * H
tracker.update(s, mk({"flat": 1.05}))
assert pp["flat"]["how"] == "time" and pp["flat"]["pnl"] < 0, pp["flat"]
assert pp["gone"]["how"] == "no price data", pp["gone"]

# fee maths: $30 in, +50% before costs -> less than $15 after fees and slippage
won = tracker.trade_pnl(30, 1.0, 1.5)
assert 11 < won < 13, won   # +50% on $30 nets ~$11.76 after fees + slippage
flat = tracker.trade_pnl(30, 1.0, 1.0)
assert -2.7 < flat < -2.3, f"a flat round trip should cost ~$2.48 in fees+slippage, got {flat}"

summ = tracker.paper_summary(s)
assert summ["all"]["n"] == 4 and summ["all"]["wins"] == 1 and summ["all"]["stops"] == 1, summ["all"]
html = report.paper_table(summ)
assert "Paper trading" in html and "Total P&amp;L" in html
print(f"paper-trading checks OK  (4 trades: {summ['all']['wins']} win, {summ['all']['stops']} stop, "
      f"total {summ['all']['pnl']:+.2f} on ${config.PAPER_SIZE_USD:.0f} each)")
