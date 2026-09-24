"""End-to-end (fake APIs): Solana buys are caught from wallet holdings, spam airdrops are not
trades, and young coins get in early only when top traders are in them."""
import os, sys, shutil, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import config, main, report, scoring, state  # noqa: E402

report.send = lambda *a: True
shutil.rmtree("out/demo_state", ignore_errors=True)
GRIND = "solana:GRNDx111111111111111111111111111111111pump"
SPAM = "solana:SPAMx999999999999999999999999999999999pump"

main.run()                                               # baseline reads
s = state.load()
assert s["wallet_cov"]["read"] == s["wallet_cov"]["tried"] > 0, s["wallet_cov"]
assert not [e for e in s["trader_buys"] if e.get("src") == "snap"], "baseline must not create buys"

new = demo.TRADERS[45]["wallets"]["solana"]              # rank 46 buys GRIND between scans
demo.buys.setdefault(new, []).append(GRIND)
for t in demo.TRADERS[10:13]:                            # and 3 wallets get a spam airdrop
    demo.buys.setdefault(t["wallets"]["solana"], []).append(SPAM)
main.run()
s = state.load()
got = [e for e in s["trader_buys"] if e["key"] == GRIND and e["wallet"] == new and e["side"] == "buy"]
assert got and got[0]["usd"] and got[0]["usd"] >= 20, got
assert not [e for e in s["trader_buys"] if e["key"] == SPAM], "spam airdrop recorded as a trade"
assert SPAM in s.get("dex_skip", {}), "unlisted coin held by 3 traders should be skipped for a day"
print(f"Solana buy caught from holdings: {got[0]['handle']} ${got[0]['usd']:,.0f}; spam airdrop ignored")

# young coins: 5h old is in only when 2+ top traders hold it
m = {"price": 1.0, "mcap": 2_000_000, "liquidity": 200_000, "created_ms": (time.time() - 5 * 3600) * 1000,
     "change": {}, "volume": {}, "buys_h1": 0, "sells_h1": 0, "key": "solana:Y", "symbol": "Y"}
assert not scoring.eligible(m, 1) and scoring.eligible(m, 2)
m["created_ms"] = (time.time() - 1.5 * 3600) * 1000
assert not scoring.eligible(m, 5), "never younger than EARLY_PAIR_AGE_HOURS"
m["created_ms"] = (time.time() - 5 * 3600) * 1000
sm = {"buyers": ["a", "b"], "buyer_ranks": {"a": 1, "b": 2}, "sellers": [], "trimmed": [], "buy_usd": 0,
      "first_buy": None, "last_buy": None, "weight": 3, "trust": {}}
r = scoring.score(m, sm)
assert any("young coin" in f for f in r["flags"]), r["flags"]
print(f"young coin rule OK (in at {config.EARLY_PAIR_AGE_HOURS:.0f}h with {config.EARLY_MIN_HOLDERS}+ top traders, flagged for the learner)")

# a DexScreener outage must not throw away a real buy (it's priced on the next scan instead)
from scanner import dex  # noqa: E402
new2 = demo.TRADERS[46]["wallets"]["solana"]
demo.buys.setdefault(new2, []).append(GRIND)
dex.request = lambda method, url, **kw: None if "dexscreener" in url else demo.fake_request(method, url, **kw)
main.run()
s = state.load()
kept = [e for e in s["trader_buys"] if e["key"] == GRIND and e["wallet"] == new2]
assert kept and kept[0]["usd"] is None, "buy lost during a DexScreener outage"
assert GRIND not in s.get("dex_skip", {}), "a failed lookup is not 'unlisted'"
dex.request = demo.fake_request
main.run()
s = state.load()
kept = [e for e in s["trader_buys"] if e["key"] == GRIND and e["wallet"] == new2]
assert kept and kept[0]["usd"], kept
print(f"DexScreener outage: buy kept and priced next scan (${kept[0]['usd']:,.0f})")
