"""Replay of $SI (Sep 2026): 8 top traders bought, 5 sold out, 2 trimmed, 3 still held.

The old 10-minute scan saw 0 buyers and 2 "sellers" at the moment the coin pumped, while
Check my position (live wallet reads) saw 3 holders. Both must now agree.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HELIUS_API_KEY", "demo")
from scanner import holders, main, scoring, solana, tracker  # noqa: E402

MINT = "DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP"
K = "solana:" + MINT
NAMES = ["0xdetweiler", "macdegods", "rasmr", "FullPinkYak", "NachSOL", "frankdegods", "looc", "motiyawerey"]
RANKS = [4, 42, 51, 7, 12, 19, 33, 60]
TRADERS = [{"handle": h, "rank": r, "wallet": f"W{i}"} for i, (h, r) in enumerate(zip(NAMES, RANKS))]
PRICE = 0.0101

WALLET = {}          # wallet -> {mint: amount} what the fake RPC returns
FAIL = set()         # wallets whose read fails


def fake_rpc(s, calls, on_demand=False):
    out = []
    for method, params in calls:
        w = params[0]
        if w in FAIL:
            out.append(None)
            continue
        prog = params[1].get("programId")
        # put everything on the classic token program; Token-2022 returns empty
        bal = WALLET.get(w, {}) if prog == solana.TOKEN_PROGRAMS[0] else {}
        out.append({"value": [{"account": {"data": {"parsed": {"info": {
            "mint": m, "tokenAmount": {"uiAmount": a}}}}}} for m, a in bal.items()]})
    return out


solana._rpc = fake_rpc
s = {"trader_buys": [], "helius_credits_used": 0}
T0 = time.time() - 40 * 3600

# scan 1 (40h ago): baseline, nobody holds it
solana.scan_wallets(s, TRADERS, now=T0)
assert not s["trader_buys"], "a first read is a baseline, never a buy"
# scan 2: all 8 bought (amounts roughly by the $ they hold today)
for i, a in enumerate([5e6, 2e7, 4.3e6, 3e6, 3e6, 2e6, 2e6, 1e6]):
    WALLET[f"W{i}"] = {MINT: a}
solana.scan_wallets(s, TRADERS, now=T0 + 600)
buys = [e for e in s["trader_buys"] if e["side"] == "buy"]
assert len(buys) == 8, buys
for e in buys:
    e["usd"] = round(e["amount"] * 0.003, 2)

# a failed read of one wallet must not look like that wallet sold
FAIL.add("W0")
solana.scan_wallets(s, TRADERS, now=T0 + 1200)
assert not [e for e in s["trader_buys"] if e["side"] == "sell"], "failed read produced a fake sell"
FAIL.clear()

# 2 hours before the check: 5 sold out, the two whales trimmed ~10%, rasmr untouched
NOW = time.time()
for i in range(3, 8):
    WALLET[f"W{i}"] = {}
WALLET["W0"] = {MINT: 4.5e6}
WALLET["W1"] = {MINT: 1.8e7}
solana.scan_wallets(s, TRADERS, now=NOW - 2 * 3600)
sells = {e["handle"]: e for e in s["trader_buys"] if e["side"] == "sell"}
assert set(sells) == set(NAMES) - {"rasmr"}, sells.keys()
assert sells["0xdetweiler"]["out"] is False and sells["macdegods"]["out"] is False
assert all(sells[h]["out"] for h in NAMES[3:])
for e in sells.values():
    e["usd"] = round(e["amount"] * PRICE, 2)
# current read, all fresh
solana.scan_wallets(s, TRADERS, now=NOW)

# ---- the 10-minute scan's view now matches the live position check -------------------------
ctx = holders.context(s, TRADERS)
m = {"key": K, "chain": "solana", "price": PRICE}
events = [e for e in s["trader_buys"] if e["key"] == K] + holders.held_events(s, ctx, K, PRICE)
sm = scoring.smart_money(events, K)
print("scan view  -> buyers:", sm["buyers"], "| sold out:", sm["sellers"], "| trimmed:", sm["trimmed"])
assert sm["buyers"] == ["0xdetweiler", "macdegods", "rasmr"], sm["buyers"]
assert sm["trimmed"] == ["0xdetweiler", "macdegods"], sm["trimmed"]
assert set(sm["sellers"]) == set(NAMES[3:]), sm["sellers"]

# old behaviour for comparison: 6h buys only, no holder reads -> the whales look like sellers
old = scoring.smart_money([{x: v for x, v in e.items() if x != "out"} for e in s["trader_buys"] if e["key"] == K], K)
assert old["buyers"] == [] and "0xdetweiler" in old["sellers"], old
print("old view   -> buyers:", old["buyers"], "| 'sellers':", old["sellers"][:3], "...")

# ---- stays on the candidate list although every buy is > 6h old -----------------------------
assert K in holders.candidate_keys(s, ctx), "held by 3 top traders -> must stay watched"

# ---- exit alerts: trims don't count, sell-outs do --------------------------------------------
assert not holders.sold_out(s, sells["0xdetweiler"]) and holders.sold_out(s, sells["looc"])
pick = {"key": K, "sent": True, "sent_ts": NOW - 3 * 3600, "sent_price": 0.009, "target": 1, "stop": 0,
        "buyers": ["0xdetweiler", "macdegods", "looc"], "warned": [], "symbol": "SI"}
s["picks"] = [pick]
out = tracker.exit_checks(s, {K: {"price": PRICE}})
msgs = " ".join(msg for _, msg, _ in out)
assert "looc" in msgs and "0xdetweiler" not in msgs and "macdegods" not in msgs, msgs
print("exit alert ->", msgs)

# ---- the too-late guard still works for holder-only signals ----------------------------------
entry = min(e["ts"] for e in buys)
p = {"key": K, "market": {"symbol": "SI"}, "smart": {"first_buy": None}, "entry_ts": entry,
     "chart": {"closes": [[entry - 60, 0.003]]}}
main.dex.tokens = lambda keys: {K: {"key": K, "symbol": "SI", "price": PRICE, "change": {"h6": 125},
                                    "liquidity": 345_000}}
ok, dropped = main.verify_live([p], {K: PRICE})
assert not ok and "too late" in dropped[0][1], dropped
p2 = {"key": K, "market": {"symbol": "SI"}, "smart": {"first_buy": None}, "chart": None}
ok, dropped = main.verify_live([p2], {K: PRICE})
assert not ok and "6h" in dropped[0][1], dropped
print("too late   ->", dropped[0][1])

# ---- the day before the pump: flat price, 8 holders -> it would have been let through --------
early = {"key": K, "market": {"symbol": "SI"}, "smart": {"first_buy": None}, "entry_ts": entry,
         "chart": {"closes": [[entry - 60, 0.0030]]}}
main.dex.tokens = lambda keys: {K: {"key": K, "symbol": "SI", "price": 0.0031, "change": {"h6": 3},
                                    "liquidity": 214_000}}
ok, _ = main.verify_live([early], {K: 0.0031})
assert ok, "flat accumulation phase must not be blocked as 'too late'"

# ---- live re-read right before an email: most holders just left -> drop ----------------------
import scanner.check as chk  # noqa: E402
chk.solana_holders = lambda s_, mint, ts: ({"W2": 1.0}, True)
p3 = {"key": K, "market": {"symbol": "SI"}, "smart": {"first_buy": None}, "entry_ts": entry,
      "chart": {"closes": [[entry - 60, 0.0030]]}, "holder_wallets": ["W0", "W1", "W2"]}
ok, dropped = main.verify_live([p3], {K: 0.0031}, s)
assert not ok and "leaving" in dropped[0][1], dropped
print("live check ->", dropped[0][1])
print("$SI replay OK")

# ======================= review follow-ups =======================================================
# (a) a trim stays a trim even when that wallet's latest read failed / is too old
trim = dict(sells["0xdetweiler"]); trim["ts"] = time.time() - 600
sm2 = scoring.smart_money([trim], K)
assert sm2["sellers"] == [] and sm2["trimmed"] == ["0xdetweiler"], sm2

# (b) a buy found after a long gap between reads is dated at the earliest possible time
s2 = {"trader_buys": [], "helius_credits_used": 0}
WALLET.clear()
solana.scan_wallets(s2, TRADERS[:1], now=time.time() - 5 * 3600)       # read, then 5h of nothing
WALLET["W0"] = {MINT: 1e6}
solana.scan_wallets(s2, TRADERS[:1])
gb = s2["trader_buys"][0]
assert gb.get("gap") and time.time() - gb["ts"] > 4 * 3600, gb           # not a "fresh" buy
# ...but a sell after a gap keeps the time we saw it (exit alerts must not be dated too early)
WALLET["W0"] = {}
solana.scan_wallets(s2, TRADERS[:1], now=time.time() + 3 * 3600)
gs = [e for e in s2["trader_buys"] if e["side"] == "sell"][0]
assert gs["ts"] > time.time() + 2 * 3600, gs

# (c) EVM: a holdings snapshot older than a sell can't prove it was only a trim
KE = "base:0xabc"
s3 = {"holdings": {KE: {"ts": time.time() - 600, "bal": {"0xw": 1000.0}}}, "trader_buys": []}
sell = {"key": KE, "wallet": "0xw", "side": "sell", "amount": 1000.0, "ts": time.time() - 60}
assert holders.sold_out(s3, sell), "stale snapshot hid a real exit"
s3["holdings"][KE]["ts"] = time.time()
assert not holders.sold_out(s3, sell), "fresh snapshot shows they still hold 1000"

# (d) a trader whose fresh wallet read shows 0 no longer counts as a buyer (matches check.py)
s4 = {"trader_buys": [], "wallet_snap": {"W9": {"ts": time.time(), "bal": {}}}}
ev = [{"key": K, "wallet": "W9", "handle": "gone", "rank": 5, "side": "buy", "amount": 1e6,
       "usd": 900, "ts": time.time() - 3600}]
assert holders.drop_exited(s4, K, ev) == [], "exited trader still counted as a buyer"
print("review follow-ups OK")

# ======================= second review ===========================================================
# (e) one huge wallet fails its batch every scan: the other 4 in that batch are still read
calls_seen = []


def whale_rpc(s_, calls, on_demand=False):
    calls_seen.append(len(calls))
    if any(p[0] == "W1" for _, p in calls) and len(calls) > 2:
        return [None] * len(calls)                    # batch containing the whale is too big
    return fake_rpc(s_, calls)


solana._rpc = whale_rpc
got = solana.read_holdings({"helius_credits_used": 0}, [f"W{i}" for i in range(5)])
assert set(got) == {"W0", "W1", "W2", "W3", "W4"}, got.keys()
solana._rpc = fake_rpc

# (f) Helius fully down: stop after a couple of batches instead of burning the whole run
calls_seen.clear()
solana._rpc = lambda s_, c, on_demand=False: calls_seen.append(1) or [None] * len(c)
got = solana.read_holdings({"helius_credits_used": 0}, [f"X{i}" for i in range(100)])
assert got == {} and len(calls_seen) <= 8, len(calls_seen)
solana._rpc = fake_rpc

# (g) trimming down to dust is an exit, not a trim
dust = {"key": K, "handle": "d", "rank": 9, "side": "sell", "amount": 1e6, "usd": 1000.0,
        "left": 100.0, "out": False, "ts": time.time() - 60}
assert scoring.smart_money([dust], K)["sellers"] == ["d"]

# (h) a gap buy can't count as a fresh signal
gapbuy = {"key": K, "handle": "g", "rank": 9, "side": "buy", "amount": 1e6, "usd": 900.0,
          "ts": time.time() - 100 * 60, "gap": True}
assert scoring.smart_money([gapbuy], K)["last_buy"] is None

# (i) EVM: a partial balance read doesn't overwrite the snapshot (no fake sells, no lost buys)
from scanner import evm  # noqa: E402
KE2 = "base:0x1234"
s5 = {"holdings": {KE2: {"ts": time.time() - 900, "bal": {"0xa": 500.0, "0xb": 700.0}}}, "trader_buys": [],
      "decimals": {KE2: 0}}
evm.balances_checked = lambda chain, token, wallets: ({"0xa": 500}, False)
evm._decimals = lambda s_, c, a: 0
tr = [{"evm": "0xa", "handle": "a", "rank": 1}, {"evm": "0xb", "handle": "b", "rank": 2}]
evm.holdings_scan(s5, tr, {KE2: {"key": KE2, "chain": "base", "address": "0x1234", "symbol": "E",
                                  "price": 1.0, "volume": {"h1": 1}}})
assert s5["holdings"][KE2]["bal"]["0xb"] == 700.0 and not s5["trader_buys"], s5
print("second-review follow-ups OK")
