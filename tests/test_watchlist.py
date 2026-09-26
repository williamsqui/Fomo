"""Watchlist: the $Surplus scenario - you hold a Base coin, then things change."""
import copy, os, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["WATCH_REQ_DIR"] = "out/watchreq"
import demo  # noqa: E402  (installs fake APIs)
from scanner import config, dex, position, report, safety, state as st, tracker, watchlist  # noqa: E402

sent = []
report.send = lambda subj, body: sent.append((subj, body)) or True
for d in ("out/demo_state", "out/watchreq"):
    shutil.rmtree(d, ignore_errors=True)

BASEY = "0x1111111111111111111111111111111111111111"
KEY = "base:" + BASEY

# 1. "Check my position" adds it to the watchlist and says so in the email
v = position.run(BASEY, -8.1, 27.29, "auto", raw_gain="-8.1")
assert v in ("HOLD", "HOLD - TIGHTEN STOP", "SELL", "TAKE PROFIT")
assert "Now on your watchlist" in sent[-1][1]
s = st.load()
assert watchlist.merge_requests(s) == 1 and KEY in s["watch"]
it = s["watch"][KEY]
assert len(it["holders"]) == 2 and all(h["base"] for h in it["holders"].values()), it["holders"]
assert watchlist.merge_requests(s) == 0, "a request must only be applied once"

markets = dex.tokens([KEY])
assert watchlist.check(s, markets) == [], "nothing changed -> no email"

# 2. one of the top traders sells out -> one alert, and never the same alert twice
seller = next(iter(it["holders"]))
demo.buys[seller].remove(KEY)
out = watchlist.check(s, markets)
assert len(out) == 1 and "sold out" in out[0][1][0][1], out
assert watchlist.check(s, markets) == [], "same reason must not email again"

# 3. the last one leaves too -> "none left"
other = [w for w in it["holders"] if w != seller][0]
demo.buys[other].remove(KEY)
msgs = [m for _, f in watchlist.check(s, markets) for _, m, _ in f]
assert any("sold out" in m for m in msgs) and any("none of the top traders" in m for m in msgs), msgs

# 4. one wallet can now pull 52% of the liquidity (GoPlus, rate-limited to every 30 min)
real = safety.check
safety.check = lambda chain, addr: {"lp_top": 52.0, "hard": [], "flags": [], "good": [], "ok": True}
it["lp_ts"] = 0
msgs = [m for _, f in watchlist.check(s, markets) for _, m, _ in f]
assert any("pull 52%" in m for m in msgs), msgs
it["lp_ts"] = time.time()                                    # checked just now -> not re-queried
safety.check = lambda *a: (_ for _ in ()).throw(AssertionError("GoPlus called too often"))
watchlist.check(s, markets)
safety.check = real

# 5. liquidity pulled 40%, then price through the stop
drained = copy.deepcopy(markets)
drained[KEY]["liquidity"] = it["liq0"] * 0.6
drained[KEY]["price"] = it["stop"] * 0.9
msgs = [m for _, f in watchlist.check(s, drained) for _, m, _ in f]
assert any("liquidity dropped" in m for m in msgs) and any("stop-loss" in m for m in msgs), msgs

# 6. the email reads well
subj, body = watchlist.build_email([(it, [("x", "0xFalcs (#77) sold out (~$136,471)", "I'd sell.")])])
assert subj.startswith("WATCHLIST ALERT: $BASEY") and "type <b>sold</b>" in body

# 7. typing "sold" stops the watching
position.run(BASEY, None, None, "auto", raw_gain="sold")
assert sent[-1][0].startswith("Stopped watching")
watchlist.merge_requests(s)
assert KEY not in s["watch"]

# 8. alert emails no longer add coins to the watchlist (old auto-added ones are cleared out);
#    a coin you DO hold that was also a pick: inside the first 48h the normal exit alert owns
#    stop/target and trader exits, so the watchlist doesn't double-email
s["watch"]["base:0xold"] = {"source": "pick", "symbol": "OLD"}
s["watch"]["solana:Early1pump"] = {"source": "early", "symbol": "E1"}
assert watchlist.drop_auto(s) and "base:0xold" not in s["watch"] and "solana:Early1pump" not in s["watch"]
traders = demo.TRADERS
tr = [{"rank": t["rank"], "handle": t["handle"], "wallet": t["wallets"]["solana"], "evm": t["wallets"]["evm"]}
      for t in traders]
buyer = next(t for t in tr if any(KEY in ks for w, ks in demo.buys.items() if w == t["evm"])) \
    if any(KEY in ks for ks in demo.buys.values()) else tr[0]
demo.buys.setdefault(buyer["evm"], []).append(KEY)
pick = {"market": markets[KEY], "smart": {"buyers": [buyer["handle"]]}, "score": 74, "qualified": True, "key": KEY}
tracker.log_picks(s, [pick], {KEY: pick})
assert KEY not in s["watch"] and not watchlist.held(s, KEY), "an alert must not start watching a coin"
position.run(BASEY, 3, 30, "auto", raw_gain="3")            # you bought it: now it's watched
watchlist.merge_requests(s)
assert watchlist.held(s, KEY) and s["watch"][KEY]["source"] == "manual"
_o = watchlist.check(s, markets); assert _o == [], [(i["symbol"], f) for i, f in _o]   # baseline read
low = copy.deepcopy(markets)
low[KEY]["price"] = s["watch"][KEY]["stop"] * 0.8
assert watchlist.check(s, low) == [], "stop inside 48h belongs to the normal exit alert"
p = next(p for p in s["picks"] if p["key"] == KEY)
p["warned"].append(buyer["handle"])                          # exit alert already told you
demo.buys[buyer["evm"]].remove(KEY)
assert watchlist.check(s, markets) == [], "a trader the exit alert already reported must not email twice"

# 9. watching ends by itself after WATCH_DAYS
s["watch"][KEY]["expires"] = time.time() - 1
watchlist.check(s, markets)
assert KEY not in s["watch"]
print(f"watchlist checks OK  (sold-out, none-left, LP {config.LP_UNLOCKED_HARD_PCT:.0f}%+, liquidity pull, stop, "
      "'sold', no auto-watch, no double emails, expiry)")

# 10. Solana works the same way (Helius wallet reads for just the watched traders)
G = "GRNDx111111111111111111111111111111111pump"
position.run(G, 12, 30, "auto", raw_gain="12")
watchlist.merge_requests(s)
gk = "solana:" + G
gm = dex.tokens([gk])
assert gk in s["watch"] and len(s["watch"][gk]["holders"]) >= 2
used = s["helius_credits_used"]
assert watchlist.check(s, gm) == []
assert s["helius_credits_used"] - used == len(s["watch"][gk]["holders"]), "1 credit per watched trader per scan"
w0 = next(iter(s["watch"][gk]["holders"]))
demo.buys[w0].remove(gk)
msgs = [m for _, f in watchlist.check(s, gm) for _, m, _ in f]
assert any("sold out" in m for m in msgs), msgs
print("watchlist Solana checks OK")
