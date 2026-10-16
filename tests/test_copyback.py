"""Copy-trade backtest: $20 per trade on each trader's own FOMO history, with fees, slippage
and copy lag, plus the money you'd have needed."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import config, copyback, fomo, report  # noqa: E402

iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
NOW = time.time()
HIST = {
    "winner": [  # one 5x, one -50%, one still open at +20%, one received (not bought), one too old
        {"tradeId": "a", "token": {"symbol": "MOON"}, "status": "closed", "avgEntryPrice": 1.0, "avgExitPrice": 5.0,
         "costBasisUsd": 100, "realizedPnlUsd": 400, "boughtAmount": 100, "soldAmount": 100,
         "createdAt": iso(NOW - 5 * 86400), "closedAt": iso(NOW - 4 * 86400)},
        {"tradeId": "b", "token": {"symbol": "RUG"}, "status": "closed", "avgEntryPrice": 2.0, "avgExitPrice": 1.0,
         "costBasisUsd": 50, "realizedPnlUsd": -25, "boughtAmount": 25, "soldAmount": 25,
         "createdAt": iso(NOW - 4.5 * 86400), "closedAt": iso(NOW - 3 * 86400)},
        {"tradeId": "c", "token": {"symbol": "HOLD"}, "status": "open", "avgEntryPrice": 1.0, "priceUsd": 1.2,
         "costBasisUsd": 40, "unrealizedPnlUsd": 8, "boughtAmount": 40, "createdAt": iso(NOW - 2 * 86400)},
        {"tradeId": "d", "token": {"symbol": "GIFT"}, "status": "open", "avgEntryPrice": None, "priceUsd": 3,
         "costBasisUsd": 0, "createdAt": iso(NOW - 86400)},
        {"tradeId": "e", "token": {"symbol": "OLD"}, "status": "closed", "avgEntryPrice": 1.0, "avgExitPrice": 9.0,
         "costBasisUsd": 100, "createdAt": iso(NOW - 60 * 86400), "closedAt": iso(NOW - 59 * 86400)}],
    "loser": [
        {"tradeId": "f", "token": {"symbol": "DUD"}, "status": "closed", "avgEntryPrice": 1.0, "avgExitPrice": 0.7,
         "costBasisUsd": 30, "realizedPnlUsd": -9, "boughtAmount": 30, "soldAmount": 30,
         "createdAt": iso(NOW - 3 * 86400), "closedAt": iso(NOW - 2.9 * 86400)}],
}
orig = demo.fake_request


def fake(method, url, params=None, **kw):
    if "/positions" in url:
        h = url.split("/v2/users/")[1].split("/")[0]
        if h == "ghost":
            return demo.Resp({"available": False})
        return demo.Resp({"positions": HIST.get(h, [])}, {"x-credits-cost": "250"})
    return orig(method, url, params=params, **kw)


fomo.request = fake
mail = []
report.send = lambda subj, body: mail.append((subj, body)) or True
config.COPY_SIZE_USD, config.COPY_LAG_PCT = 20, 3
copyback.run("winner,loser,ghost", 30)
subj, body = mail[-1]
rows = {r["symbol"]: r for r in __import__("csv").DictReader(open("out/copyback.csv"))}
assert set(rows) == {"MOON", "RUG", "HOLD", "DUD"}, set(rows)       # GIFT (received) and OLD (>30d) skipped
assert 70 < float(rows["MOON"]["pnl"]) < 80 and float(rows["MOON"]["pnl_lag"]) < float(rows["MOON"]["pnl"])
assert float(rows["RUG"]["pnl"]) < -10 and rows["HOLD"]["status"].startswith("still open")
assert "Copy backtest (30d, $20/trade)" in subj and "@ghost" in body and "Money needed" in body
t = copyback.tally([copyback.copy_trade(p, 20, 3) | {"trader": "w"} for p in HIST["winner"][:3]], 20)
assert t["peak"] == 2 and t["capital"] == 40, t     # MOON and RUG overlapped
print("copy backtest OK:", subj)

# other shapes the API might use: nested under "data", epoch-millisecond dates, numbers as text
HIST["shapes"] = {"data": {"positions": [
    {"tradeId": "x", "token": {"symbol": "MS"}, "status": "CLOSED", "avgEntryPrice": "1.0", "avgExitPrice": "3.0",
     "costBasisUsd": "50", "openedAt": int((NOW - 2 * 86400) * 1000), "closedAt": int((NOW - 86400) * 1000)}]}}
fake_orig = fake


def fake2(method, url, params=None, **kw):
    if "/users/shapes/positions" in url:
        return demo.Resp(HIST["shapes"], {"x-credits-cost": "250"})
    return fake_orig(method, url, params=params, **kw)


fomo.request = fake2
copyback.run("shapes", 30)
rows = {r["symbol"]: r for r in __import__("csv").DictReader(open("out/copyback.csv"))}
assert "MS" in rows and float(rows["MS"]["pnl"]) > 30, rows
assert "What the FOMO API returned" in mail[-1][1]
print("API shape variants OK")
