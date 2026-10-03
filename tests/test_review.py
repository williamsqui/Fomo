"""Trade review: rebuilds each trade's chart around the buy, compares winners with losers,
and replays the early lane's exits on the user's real stakes."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import chart, dex, fomo, report, review  # noqa: E402

T = time.time() - 3 * 86400
NPC = "7GUnr7krtQhJwd6ASY2VUprd9t4c64zcgCsjdmZepump"
LOSS = "LOSSx666666666666666666666666666666666pump"
demo.COINS["solana:" + NPC] = ("NPC", 0.0047, 4_700_000, 400_000, 5, 900, 700, "up_break", 0, "clean", 0)
demo.COINS["solana:" + LOSS] = ("LOSS", 0.0001, 100_000, 20_000, -5, 100, 300, "down", 0, "clean", 0)
demo.AGE_H["solana:" + NPC] = 3 * 24 + 14      # graduated 14h before the buy
demo.AGE_H["solana:" + LOSS] = 3 * 24 + 2


def candles_for(entry, path):
    """15-min candles: flat before the buy, then the given multipliers after it."""
    rows, px = [], entry
    for i in range(-200, 0):
        rows.append([T + i * 900, entry * 0.8, entry * 0.82, entry * 0.78, entry * (0.8 + 0.2 * (i + 200) / 200), 1000])
    for i, mult in enumerate(path):
        o, px = px, entry * mult
        rows.append([T + i * 900, o, max(o, px) * 1.01, min(o, px) * 0.99, px, 5000])
    return rows


PATHS = {NPC: [1.2, 1.6, 2.2, 3.0, 4.0, 4.4, 3.2, 2.9], LOSS: [0.95, 0.8, 0.6, 0.5, 0.45]}
ENTRY = {NPC: 0.0011, LOSS: 0.00012}
orig = demo.fake_request


def fake(method, url, params=None, json=None, headers=None, **kw):
    if "/positions" in url:
        return demo.Resp({"positions": [
            {"tradeId": "1", "token": {"symbol": "NPC", "address": NPC}, "status": "open", "avgEntryPrice": ENTRY[NPC],
             "costBasisUsd": 111, "realizedPnlUsd": 0, "unrealizedPnlUsd": 366,
             "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T))},
            {"tradeId": "2", "token": {"symbol": "LOSS", "address": LOSS}, "status": "closed", "avgEntryPrice": ENTRY[LOSS],
             "costBasisUsd": 30, "realizedPnlUsd": -12, "unrealizedPnlUsd": 0,
             "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T)),
             "closedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T + 3600))}]}, {"x-credits-cost": "250"})
    if "/ohlcv/minute" in url:
        tail = url.split("/pools/POOL")[1].split("/")[0]
        addr = NPC if NPC.endswith(tail) else LOSS
        return demo.Resp({"data": {"attributes": {"ohlcv_list": candles_for(ENTRY[addr], PATHS[addr])}}})
    return orig(method, url, params=params, json=json, headers=headers, **kw)


for mod in (dex, fomo, chart):
    mod.request = fake
mail = []
report.send = lambda subj, body: mail.append((subj, body)) or True
review.run("williamsqui")
subj, body = mail[-1]
assert "1 winners / 1 losers" in subj, subj
rows = {r["symbol"]: r for r in __import__("csv").DictReader(open("out/review.csv"))}
n = rows["NPC"]
assert float(n["peak_48h_pct"]) > 250 and float(n["early_rules_pnl"]) > 100, n
assert abs(float(n["mcap_entry"]) - 1_100_000) < 50_000, n["mcap_entry"]
assert 13 < float(n["age_h"]) < 15, n["age_h"]
assert float(rows["LOSS"]["early_rules_pnl"]) < 0 and "stop" in rows["LOSS"]["early_rules_how"]
assert "Winners" in body and "Early rules" in body
print(f"review OK: {subj} | NPC peak {n['peak_48h_pct']}%, early rules {n['early_rules_pnl']} "
      f"({n['early_rules_how']}), could have made {n['could_have_made']}")
