"""Stop-loss / take-profit backtest: candle replay, fees, conservative ordering, report."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402
assert demo  # installs fake APIs
from scanner import config, copyback, dex, chart, report, sltp  # noqa: E402

config.FEE_MIN_USD, config.FEE_PCT = 0.95, 0.5
sltp.FEE_PCT = 2
sim = sltp.simulate
C = lambda *bars: [[i * 900, *b] for i, b in enumerate(bars)]   # (o, h, l, c)

# no candles, no settings: 2% in, 2% out, 1% slippage each way
p, ev = sim(1.0, 2.0, [], 20)
exp = (20 - 0.4) / 1.01 * 2 * 0.99 * 0.98 - 20
assert abs(p - exp) < 0.02 and ev == [], (p, exp)

# dips -60% then runs to 3x: a -50% stop gets shaken out, following their exit wins
dip = C((1, 1.05, 0.4, 0.5), (0.5, 3.2, 0.5, 3))
assert sim(1.0, 3.0, dip, 20, stop=-50)[0] < -10
assert sim(1.0, 3.0, dip, 20)[0] > 30
# gap down through the stop fills at the open, not the stop
gap = C((1, 1, 0.98, 0.99), (0.3, 0.31, 0.2, 0.25))
pg, evg = sim(1.0, 0.1, gap, 20, stop=-25)
assert evg == ["stop"] and -15.5 < pg < -13.5, pg
# same candle touches stop and target: stop wins
both = C((1, 2.5, 0.5, 1))
assert sim(1.0, 1.0, both, 20, stop=-25, tps=[(100, "all")])[1] == ["stop"]
# TP +100% half, then breakeven stop catches the rest on the way back down
run_ = C((1, 2.2, 0.95, 2.1), (2.1, 2.1, 0.9, 0.9))
pr, evr = sim(1.0, 0.2, run_, 20, tps=[(100, 0.5)], be=True)
assert evr == ["TP+100%", "stop after TP"] and pr > 7, (pr, evr)
# without breakeven the rest rides down to their exit
assert sim(1.0, 0.2, run_, 20, tps=[(100, 0.5)], be=False)[0] < pr
# stacking FOMO's fee costs more per sell, more so with ladders
assert sim(1.0, 2.0, [], 20, stack_fomo=True)[0] < p - 1.8

# full run with fake history + fake candles
NOW = time.time()
iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
t1, t2 = NOW - 5 * 86400, NOW - 3 * 86400
HIST = [{"tradeId": "a", "token": {"symbol": "DIP", "address": "Dip111"}, "status": "closed", "avgEntryPrice": 1.0,
         "avgExitPrice": 3.0, "costBasisUsd": 50, "boughtAmount": 50, "soldAmount": 50,
         "createdAt": iso(t1), "closedAt": iso(t1 + 7200)},
        {"tradeId": "b", "token": {"symbol": "PUMP", "address": "Pump11"}, "status": "closed", "avgEntryPrice": 1.0,
         "avgExitPrice": 0.3, "costBasisUsd": 50, "boughtAmount": 50, "soldAmount": 50,
         "createdAt": iso(t2), "closedAt": iso(t2 + 7200)},
        {"tradeId": "c", "token": {"symbol": "NOPOOL", "address": "Gone11"}, "status": "closed", "avgEntryPrice": 1.0,
         "avgExitPrice": 1.5, "costBasisUsd": 50, "boughtAmount": 50, "soldAmount": 50,
         "createdAt": iso(t2), "closedAt": iso(t2 + 3600)}]
copyback.history = lambda s, h, since, cache=None: HIST
dex.tokens = lambda keys: {k: {"pair": "pool_" + k.split(":")[1]} for k in keys if "Gone" not in k}
BARS = {"pool_Dip111": (int(t1), [(1, 1.05, 0.4, 0.5), (0.5, 3.2, 0.5, 3)]),
        "pool_Pump11": (int(t2), [(1, 2.3, 0.95, 2.2), (2.2, 2.2, 0.3, 0.3)])}
calls = []


def gt(path, params=None):
    calls.append(path)
    pool = path.split("/pools/")[1].split("/")[0]
    t0, bars = BARS[pool]
    return {"data": {"attributes": {"ohlcv_list": [[t0 + 60 + i * 900, *b, 1000] for i, b in enumerate(bars)]}}}


chart._gt = gt
sltp.CANDLES = "out/sltp_candles_test.json"
copyback.CACHE = "out/sltp_cb_test.json"
for f in (sltp.CANDLES, copyback.CACHE):
    if os.path.exists(f):
        os.remove(f)
sltp.time.sleep = lambda s: None
mail = []
report.send = lambda subj, body: mail.append((subj, body)) or True
config.COPY_SIZE_USD = 20
sltp.run("bystevenr", 30)
subj, body = mail[-1]
assert "SL/TP backtest" in subj and "Follow their exits only" in body, subj
assert "1 of 3 trades had no usable price candles" in body, body[-900:]
rows = list(__import__("csv").DictReader(open("out/sltp.csv")))
get = lambda setting, sym: next(r for r in rows if r["setting"] == setting and r["symbol"] == sym)
assert float(get("Stop -50%", "DIP")["pnl"]) < 0 < float(get("Follow their exits only (no SL/TP)", "DIP")["pnl"])
assert float(get("TP +100% sell half", "PUMP")["pnl"]) > 0 > float(get("Follow their exits only (no SL/TP)", "PUMP")["pnl"])
assert get("Stop -50%", "NOPOOL")["had_candles"] == "False"
# second run reuses cached candles
n = len(calls)
sltp.run("bystevenr", 30)
assert len(calls) == n, "candles should come from the cache"
print("sltp OK:", subj)
