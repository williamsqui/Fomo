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


copyback.CACHE = "out/cb_cache_test.json"
if os.path.exists(copyback.CACHE):
    os.remove(copyback.CACHE)
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

# an API error is spelled out in the email, not hidden
from scanner import http  # noqa: E402


class R402:
    ok, status_code, text, headers = False, 402, '{"error":"credits_exhausted"}', {}


fomo.request = http.request
http.SESSION.request = lambda *a, **k: R402()
copyback.run("mickeymantled", 30)
assert "credits for this month are used up" in mail[-1][1], mail[-1][1][-800:]
print("API errors explained OK")

# page 1 = all OPEN positions (one opened 3 months ago) + newest closed; older closed on page 2.
# The old code saw the 3-month-old open trade and stopped after page 1 (Mickey's "only 10 hours").
P1 = [{"tradeId": "o1", "token": {"symbol": "OLDBAG"}, "status": "open", "avgEntryPrice": 1, "priceUsd": 1,
       "costBasisUsd": 20, "createdAt": iso(NOW - 90 * 86400)},
      {"tradeId": "c1", "token": {"symbol": "NEW"}, "status": "closed", "avgEntryPrice": 1, "avgExitPrice": 2,
       "costBasisUsd": 20, "createdAt": iso(NOW - 3600), "closedAt": iso(NOW - 1800)}]
P2 = [{"tradeId": "c2", "token": {"symbol": "WEEKOLD"}, "status": "closed", "avgEntryPrice": 1, "avgExitPrice": 3,
       "costBasisUsd": 20, "createdAt": iso(NOW - 7 * 86400), "closedAt": iso(NOW - 6 * 86400)}]
P3 = [{"tradeId": "c3", "token": {"symbol": "ANCIENT"}, "status": "closed", "avgEntryPrice": 1, "avgExitPrice": 3,
       "costBasisUsd": 20, "createdAt": iso(NOW - 40 * 86400), "closedAt": iso(NOW - 39 * 86400)}]


def fake3(method, url, params=None, **kw):
    if "/users/pager/positions" in url:
        cur = (params or {}).get("cursor")
        if cur == "start":
            return demo.Resp({"trades": P1, "cursor": "start", "nextCursor": "p2"}, {"x-credits-cost": "250"})
        if cur == "p2":
            return demo.Resp({"trades": P2, "cursor": "p2", "nextCursor": "p3"}, {"x-credits-cost": "250"})
        return demo.Resp({"trades": P3, "cursor": "p3", "nextCursor": None}, {"x-credits-cost": "250"})
    return orig(method, url, params=params, **kw)


fomo.request = fake3
copyback.run("pager", 30)
rows = {r["symbol"] for r in __import__("csv").DictReader(open("out/copyback.csv"))}
assert {"NEW", "WEEKOLD"} <= rows and "ANCIENT" not in rows and "OLDBAG" not in rows, rows
assert copyback.DIAG["pager"]["pages"] == 3, copyback.DIAG["pager"]
print("paging past old open positions OK")

# FOMO busy: first a 503 "retry in 25s", then the saved snapshot, then the live page -> live data used
copyback.time.sleep = lambda x: None
calls = {"n": 0}
LIVE = {"trades": [{"tradeId": "z", "token": {"symbol": "LIVE"}, "status": "closed", "avgEntryPrice": 1,
                    "avgExitPrice": 2, "costBasisUsd": 20, "createdAt": iso(NOW - 3600), "closedAt": iso(NOW - 60)}],
        "nextCursor": None, "source": "live-fomo"}
SNAP = {"trades": [], "stale": True, "ageSeconds": 900, "source": "captured"}


class R503:
    ok, status_code, headers = False, 503, {}
    text = '{"error":"FOMO did not answer in time","retryable":true,"retryAfterSeconds":25}'


class ROK:
    ok, status_code = True, 200

    def __init__(self, d):
        self.d, self.headers = d, {"x-credits-cost": "250"}

    def json(self):
        return self.d


def busy(method, url, **kw):
    calls["n"] += 1
    return R503() if calls["n"] == 1 else ROK(SNAP) if calls["n"] == 2 else ROK(LIVE)


fomo.request = http.request
http.SESSION.request = busy
copyback.run("busyguy", 30)
rows = {r["symbol"] for r in __import__("csv").DictReader(open("out/copyback.csv"))}
assert rows == {"LIVE"} and not copyback.DIAG["busyguy"].get("partial"), (rows, copyback.DIAG["busyguy"])
assert copyback._retry_after('503 {"retryAfterSeconds": 25}') == 25
print("busy-API retries OK")

# a huge history across runs: run 1 stops after 2 pages (time), run 2 re-reads the newest page,
# sees it already has those trades, jumps to the saved bookmark and finishes. Nothing fetched twice
# except page 1, and the email says "complete" only at the end.
def mk(i, days_ago):
    return {"tradeId": f"t{i}", "token": {"symbol": f"C{i}"}, "status": "closed", "avgEntryPrice": 1,
            "avgExitPrice": 1.5, "costBasisUsd": 20, "createdAt": iso(NOW - days_ago * 86400),
            "closedAt": iso(NOW - days_ago * 86400 + 600)}


PAGES = {"start": ([mk(1, 1), mk(2, 2)], "b"), "b": ([mk(3, 8), mk(4, 12)], "c"),
         "c": ([mk(5, 18), mk(6, 24)], "d"), "d": ([mk(7, 29), mk(8, 35)], None)}
asked = []


def fake4(method, url, params=None, **kw):
    if "/users/bigtrader/positions" in url:
        cur = (params or {}).get("cursor", "start")
        asked.append(cur)
        trades, nxt = PAGES[cur]
        return demo.Resp({"trades": trades, "cursor": cur, "nextCursor": nxt}, {"x-credits-cost": "250"})
    return orig(method, url, params=params, **kw)


fomo.request = fake4
copyback.MAX_PAGES = 2
copyback.run("bigtrader", 30)
assert asked == ["start", "b"] and not copyback.DIAG["bigtrader"]["complete"], (asked, copyback.DIAG["bigtrader"])
assert "run the backtest again to continue" in mail[-1][1]
asked.clear()
copyback.MAX_PAGES = 200
copyback.run("bigtrader", 30)
assert asked == ["start", "c", "d"], asked
got = {r["symbol"] for r in __import__("csv").DictReader(open("out/copyback.csv"))}
assert got == {f"C{i}" for i in range(1, 8)}, got          # C8 is 35 days old
assert copyback.DIAG["bigtrader"]["complete"] and "complete" in mail[-1][1]
asked.clear()
copyback.run("bigtrader", 30)                               # already complete: only the newest page
assert asked == ["start"], asked
print("resumable history OK")
