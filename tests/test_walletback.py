"""Copying a bare wallet address (not on FOMO): detection, live paper book wallets, and the
backtest rebuilding positions from on-chain swaps."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402
assert demo  # installs fake APIs
from scanner import chains, chart, config, copy as copybook, copyback, dex, report, solana, walletback  # noqa: E402

W = "sAdNbe1cKNMDqDsa4npB3TfL62T14uAo2MsUQfLvzLT"
assert chains.wallet_kind(W) == "solana" and chains.wallet_kind("@" + W) == "solana"
assert chains.wallet_kind("0x" + "ab" * 20) == "evm"
assert chains.wallet_kind("bystevenr") is None and chains.wallet_kind("mickeymantled") is None
assert chains.who(W) == "wallet sAdN..vzLT" and chains.who("tjr") == "@tjr"

# live paper copy: the address IS the wallet - no FOMO lookup
s = {"copies": {}}
b = copybook._book(s, W)
assert copybook.wallets(s, b) == {"solana": W, "evm": None}
b2 = copybook._book(s, "0x" + "AB" * 20)
assert copybook.wallets(s, b2)["evm"] == "0x" + "ab" * 20

# ---- backtest from chain history
NOW = time.time()
H = 3600
T1, T2, T3, T4 = NOW - 10 * 86400, NOW - 9 * 86400, NOW - 8 * 86400, NOW - 2 * 86400
SWAPS = [  # newest first, like the RPC
    ("s5", T4, [("OPENmint", "buy", 1000.0, 0.5, 0)]),                 # still holding
    ("s4", T3, [("MEMEmint", "sell", 500.0, 2.0, 0)]),                 # rest sold for 2 SOL
    ("sx", T3 - 60, []),                                               # not a swap
    ("s3", T2, [("MEMEmint", "sell", 500.0, 1.0, 0)]),                 # half sold for 1 SOL
    ("s2", T1, [("MEMEmint", "buy", 1000.0, 1.0, 0)]),                 # bought for 1 SOL
    ("s1", NOW - 50 * 86400, [("OLDmint", "buy", 10.0, 1.0, 0)]),      # long before the window
]
calls = {"sig": 0, "tx": 0}


def rpc(s, cs, on_demand=False):
    out = []
    for m, p in cs:
        if m == "getSignaturesForAddress":
            calls["sig"] += 1
            before = p[1].get("before")
            rows = [{"signature": sg, "blockTime": int(t), "err": None} for sg, t, _ in SWAPS]
            if before:
                rows = rows[[r["signature"] for r in rows].index(before) + 1:]
            out.append(rows[:PAGE[0]] + ([] if len(rows) <= PAGE[0] else []))
        else:
            calls["tx"] += 1
            out.append({"sig": p[0]})
    return out


PAGE = [1000]
solana._rpc = rpc
solana.parse_swap = lambda tx, w: [tuple(e) for sg, t, evs in SWAPS if sg == tx["sig"] for e in evs]
solana.read_holdings = lambda *a, **k: {}
chart._gt = lambda path, params=None: (
    {"data": [{"attributes": {"name": "SOL / USDC", "address": "solpool"}}]} if path.endswith("/pools")
    else {"data": {"attributes": {"ohlcv_list": [[int(NOW // H * H) - i * H, 100, 100, 100, 100, 1]
                                                 for i in range(0, 24 * 40)]}}})
dex.tokens = lambda keys: {k: {"price": 0.002 if "OPEN" in k else 0.5, "symbol": "OPN" if "OPEN" in k else "MEME",
                               "pair": "p"} for k in keys}
dex.sol_price = lambda: 100.0
config.HELIUS_API_KEY = "test"
copyback.CACHE = "out/wb_cache_test.json"
if os.path.exists(copyback.CACHE):
    os.remove(copyback.CACHE)
mail = []
report.send = lambda subj, body: mail.append((subj, body)) or True
config.COPY_SIZE_USD, config.COPY_LAG_PCT = 20, 3
copyback.OWN.clear()
copyback.run(W, 30)
subj, body = mail[-1]
assert "wallet sAdN..vzLT" in body and "wallet sAdN..vzLT made" in subj, subj
rows = {r["symbol"]: r for r in __import__("csv").DictReader(open("out/copyback.csv"))}
assert set(rows) == {"MEME", "OPN"}, rows                     # OLD is before the window
assert rows["MEME"]["status"] == "closed" and float(rows["MEME"]["move_pct"]) == 200.0, rows["MEME"]
assert rows["OPN"]["status"].startswith("still open") and abs(float(rows["OPN"]["move_pct"]) - (-96.0)) < 0.1
own = copyback.OWN[W]
assert own["n"] == 2 and abs(own["invested"] - 150) < 0.01, own      # $100 MEME + $50 OPN
assert abs(own["pnl"] - (200 - 48)) < 0.01, own                       # +$200 realised, -$48 open
assert calls["tx"] == 5, calls                                         # s1 is too old: never fetched

# a second run re-reads only the newest page and fetches nothing new
n = calls["tx"]
copyback.run(W, 30)
assert calls["tx"] == n, calls

# resumable: a busy wallet cut off by the time limit carries on next run
os.remove(copyback.CACHE)
PAGE[0] = walletback.SIG_PAGE = 2
cache, diag = {}, {"pages": 0}
real_time = walletback.time.time
walletback.history(None, W, NOW - 30 * 86400, cache, diag, deadline=real_time() + 3600)
ent = cache["w:" + W]
assert ent["done"] and len(ent["swaps"]) == 4, (ent["done"], len(ent["swaps"]))
cache2, diag2 = {}, {"pages": 0}
walletback.MAX_TX = 2                          # only 2 transactions per run
walletback.history(None, W, NOW - 30 * 86400, cache2, diag2)
e2 = cache2["w:" + W]
assert not e2["done"] and e2["cursor"] and "per-run maximum" in diag2.get("partial", ""), (e2, diag2)
for _ in range(4):
    walletback.history(None, W, NOW - 30 * 86400, cache2, {"pages": 0})
assert e2["done"] and len(e2["swaps"]) == 4, (e2["done"], sorted(e2["swaps"]))

# EVM wallets: a clear message, not a crash
d = {"pages": 0}
assert walletback.history(None, "0x" + "ab" * 20, 0, {}, d) is None and "Solana" in d["error"]
print("wallet copy OK:", subj)
