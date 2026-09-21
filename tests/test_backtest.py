"""Backtest on fake history: signals by trader count, exits by the rules, fees, gaps, missing data."""
import csv, os, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(FOMO_API_KEY="demo", HELIUS_API_KEY="demo", STATE_DIR="out/bt_state", GT_SLEEP_SEC="0",
                  BT_HELIUS_RPS="100000", BT_DAYS="30", BT_SKIP_DAYS="7", BT_LAG_MIN="20")
shutil.rmtree("out/bt_state", ignore_errors=True)
from scanner import backtest, chart, fomo, report  # noqa: E402

NOW = time.time()
D, H = 86400, 3600
TR = [{"rank": i + 1, "handle": f"t{i + 1}", "wallets": {"solana": f"W{i + 1}" + "x" * 40}} for i in range(3)]


def pump(t, T):     # steady uptrend into the signal, then +60% within 10h
    base = 1.0 + 0.002 * max(0, (t - (T - 200 * H)) / H)
    return base * (1 + min(0.6, max(0, (t - T) / H) * 0.07)) if t > T else base


def rug(t, T):      # uptrend, then a gap straight through the -30% stop 5h after entry
    base = 1.0 + 0.002 * max(0, (t - (T - 200 * H)) / H)
    return base * (0.45 if t > T + 5 * H else 1.0)


def flat(t, T):
    return 1.0


COINS = {  # mint: (signal time, buyers, price path)
    "PUMPmint": (NOW - 20 * D, ["t1", "t2", "t3"], pump),
    "RUGmint": (NOW - 15 * D, ["t1", "t2"], rug),
    "FLATmint": (NOW - 12 * D, ["t1"], flat),
    "DEADmint": (NOW - 10 * D, ["t2"], flat),        # no price data anywhere: dead coin
    "OLDmint": (NOW - 60 * D, ["t1", "t2"], pump),    # outside the 30-day window
}
SIGS = {t["wallets"]["solana"]: [] for t in TR}
for mint, (T, who, _) in COINS.items():
    for j, h in enumerate(who):
        w = f"W{h[1:]}" + "x" * 40
        SIGS[w].append({"signature": f"{w}|{mint}", "blockTime": int(T + j * 1800), "err": None})
for w in SIGS:
    SIGS[w].sort(key=lambda x: -x["blockTime"])


class Resp:
    def __init__(self, d): self._d, self.ok, self.status_code, self.headers = d, True, 200, {"x-credits-cost": "250"}
    def json(self): return self._d


def fake(method, url, params=None, json=None, **kw):
    if "fomoapi" in url:
        return Resp({"traders": TR})
    if "helius" in url:
        out = []
        for c in json:
            if c["method"] == "getSignaturesForAddress":
                out.append({"id": c["id"], "result": SIGS.get(c["params"][0], [])})
            else:
                w, mint = c["params"][0].split("|")
                tb = lambda a: [{"owner": w, "mint": mint, "uiTokenAmount": {"uiAmount": a}}] if a else []
                out.append({"id": c["id"], "result": {
                    "meta": {"err": None, "fee": 5000, "preBalances": [10 * 10 ** 9], "postBalances": [8 * 10 ** 9],
                             "preTokenBalances": tb(0), "postTokenBalances": tb(1e6)},
                    "transaction": {"message": {"accountKeys": [{"pubkey": w}]}}}})
        return Resp(out)
    if "geckoterminal" in url:
        if "/tokens/" in url:
            mint = url.split("/tokens/")[1].split("/")[0]
            if mint == "DEADmint":
                return Resp({"data": []})
            return Resp({"data": [{"id": f"solana_POOL{mint}", "attributes": {
                "base_token_price_usd": "1.0", "fdv_usd": "2000000", "name": f"{mint[:-4]} / SOL",
                "pool_created_at": "2025-01-01T00:00:00Z"}}]})
        pool = url.split("/pools/")[1].split("/")[0][4:]
        T, _, path = COINS[pool]
        until = params["before_timestamp"]
        rows, t = [], until - 1000 * 900
        while t < until:
            o, c = path(t, T), path(t + 900, T)
            rows.append([t, o, max(o, c), min(o, c), c, 5000])
            t += 900
        return Resp({"data": {"attributes": {"ohlcv_list": rows[::-1]}}})
    raise RuntimeError("unmocked " + url)


backtest.request = chart.request = fomo.request = fake
sent = []
report.send = lambda subj, body: sent.append((subj, body)) or True

results, meta = backtest.run()
by = {(r["mint"], r["tier"]): r for r in results}

# signals: PUMP reaches 1, 2 and 3 traders; RUG 1 and 2; FLAT 1; OLD is outside the window
assert {k for k in by} == {("PUMPmint", 1), ("PUMPmint", 2), ("PUMPmint", 3), ("RUGmint", 1), ("RUGmint", 2),
                           ("FLATmint", 1)}, sorted(by)
assert meta["no_data"] == 1, meta                                     # DEAD: no price history
assert by[("PUMPmint", 3)]["how"] == "target" and by[("PUMPmint", 3)]["pnl"] > 10
rug2 = by[("RUGmint", 2)]
assert rug2["how"] == "stop" and rug2["exit"] < rug2["entry"] * 0.7, "a gap must fill below the stop, not at it"
assert rug2["pnl"] < -14, rug2["pnl"]
assert by[("FLATmint", 1)]["how"] == "time" and by[("FLATmint", 1)]["pnl"] < 0, "flat = lose the fees"
# entry is 20 minutes after the signal, not at the signal
assert by[("PUMPmint", 1)]["entry"] >= 1.0

subj, body = sent[0]
assert "Backtest" in subj and "best case" in body and "Hit +50%" in body
with open("out/backtest.csv") as f:
    assert len(list(csv.DictReader(f))) == len(results)
print(f"backtest checks OK  ({len(results)} simulated trades; subject: {subj})")

# scanner-like trades are picked out, and the verdict reads the right way round
assert by[("RUGmint", 2)]["scanner_like"] and by[("PUMPmint", 3)]["scanner_like"], \
    [(k, r["chart_pts"], r["runup"], r["filtered"]) for k, r in by.items()]
assert not by[("FLATmint", 1)]["scanner_like"], "one trader is not enough for the scanner"
fake_rows = lambda pnl, how: [{**by[("RUGmint", 2)], "pnl": pnl, "how": how} for _ in range(20)]
m = {**meta, "truncated": False}
assert "LOST money" in backtest.build_report(fake_rows(-4.0, "stop"), m)[0]
assert "made +$6.00 per" in backtest.build_report(fake_rows(6.0, "target"), m)[0]
print("backtest verdict checks OK")
