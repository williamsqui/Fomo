"""Early lane: finds fresh launchpad coins like $NPC, rejects rugs and chases, paper-trades
with the +100% / trailing / -35% exits, alerts at 60+, and is judged by its own rules."""
import os, sys, shutil, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import config, early, main, position, report, safety, state  # noqa: E402

NOW = time.time()
NPC = "solana:7GUnr7krtQhJwd6ASY2VUprd9t4c64zcgCsjdmZepump"


def mkt(**kw):
    m = {"key": NPC, "address": NPC.split(":")[1], "symbol": "NPC", "chain": "solana", "price": 0.00031, "mcap": 310_000, "liquidity": 48_000,
         "created_ms": (NOW - 14 * 3600) * 1000, "change": {"m5": 4, "h1": 55, "h6": 90}, "buys_h1": 1100,
         "sells_h1": 700, "volume": {"h1": 90_000, "h6": 240_000}, "twitter": "x.com/wenpcfamily",
         "website": "https://wenpc.family/", "telegram": None, "url": ""}
    m.update(kw)
    return m


clean = {"ok": True, "verified": True, "hard": [], "lp_locked": 100.0,
         "flags": ["copycat ticker (another verified coin uses this symbol)"], "good": []}

# ---- $NPC before its big run: one top trader, community, FOMO Graduated board -> alert ---------
r = early.score(mkt(), {"mcap0": 180_000, "holders0": 700, "profile": True}, [("RareDualJay", 1.0)],
                {"holders": 1400, "top10_pct": 22}, clean, grad_rank=4)
assert early.in_range(r["market"]) and not r["hard"] and not r["chasing"], r
assert r["score"] >= config.EARLY_ALERT_SCORE, (r["score"], r["points"])
assert any("copycat" in f for f in r["flags"]), "copycat must be a warning, not a reject"
print(f"$NPC early phase -> {r['score']}/100 {r['points']}")

# ---- the same coin once it's +777% in an hour is a chase, not an entry --------------------------
late = early.score(mkt(change={"m5": 20, "h1": 777, "h6": 1668}), {}, [("RareDualJay", 1.0)], {}, clean)
assert late["chasing"], late

# ---- rugs are rejected however good the momentum is ---------------------------------------------
bad = dict(clean, hard=["Mint Authority still enabled"])
assert early.score(mkt(), {}, [], {}, bad)["hard"]
assert early.score(mkt(), {}, [], {}, dict(clean, lp_locked=20))["hard"], "unburned LP must be a hard fail"
assert early.score(mkt(), {}, [], {"top10_pct": 60}, clean)["hard"]
assert not early.in_range(mkt(created_ms=(NOW - 5 * 60) * 1000)), "first minutes after graduating are skipped"
assert not early.in_range(mkt(mcap=5_000_000)), "big coins belong to the main lane"

# ---- RugCheck 'Copycat token' is now a warning in the main lane too ------------------------------
class R:
    def json(self):
        return {"risks": [{"name": "Copycat token", "level": "warn"}], "lpLockedPct": 64.7}
safety.request = lambda *a, **k: R()
sf = safety._rugcheck("7GUnr7krtQhJwd6ASY2VUprd9t4c64zcgCsjdmZepump")
assert sf["ok"] and not sf["hard"] and any("copycat" in f for f in sf["flags"]), sf
safety.request = demo.fake_request

# ---- paper trade: half at +100%, rest on a 30% drop from the high --------------------------------
s = {"early": {}}
t = early.open_paper(s, {"key": NPC, "score": 66, "market": mkt(price=1.0), "launchpad": "pump.fun"}, now=NOW)
for px in (1.3, 2.1, 3.4, 2.3):
    early.step_paper(s, {NPC: {"price": px}}, now=NOW + 600)
assert t["done"] and t["how"].startswith("trailing") and t["parts"][0] == [0.5, 2.1], t
assert 17 < t["pnl"] < 20, t["pnl"]        # $20 -> half sold at 2.1x, half at 2.3x = ~$38 back after fees/slippage
# stop, and a rug that vanishes
s2 = {"early": {}}
t2 = early.open_paper(s2, {"key": NPC, "score": 55, "market": mkt(price=1.0)}, now=NOW)
early.step_paper(s2, {NPC: {"price": 0.6}}, now=NOW + 600)
assert t2["how"] == "stop" and -10 < t2["pnl"] < -8, t2
s3 = {"early": {}}
t3 = early.open_paper(s3, {"key": NPC, "score": 55, "market": mkt(price=1.0)}, now=NOW)
early.step_paper(s3, {}, now=NOW + 3 * 86400)
assert t3["done"] and t3["pnl"] <= -19, t3
print(f"paper exits OK: 3.4x run {t['pnl']:+.2f}, stop {t2['pnl']:+.2f}, rug {t3['pnl']:+.2f} (on $20)")

# ---- daily alert cap -------------------------------------------------------------------------------
s4 = {"early": {}}
rs = [{"key": f"solana:C{i}pump", "score": 70, "market": mkt(key=f"solana:C{i}pump")} for i in range(10)]
sent = 0
for _ in range(10):
    d = early.due_alerts(s4, rs)
    early.mark_sent(s4, d)
    sent += len(d)
assert sent == config.EARLY_MAX_ALERTS_PER_DAY, sent

# ---- position check judges a small coin by the early rules ----------------------------------------
pr = {"market": mkt(), "smart": {"buyers": ["RareDualJay"], "sellers": [], "trimmed": []}, "score": 47,
      "chart": {"verdict": "recovering"}, "flags": [], "reasons": [], "hard": [], "safety": clean,
      "live_holders": True, "exited": []}
v, head, plus, minus, lv = position.decide(pr, 120.0, value=240)
assert v == position.TAKE and "half" in head and "30%" in head, (v, head)
assert lv["tgt_pct"] == config.EARLY_TP_PCT
v2, head2, _, minus2, _ = position.decide(dict(pr, hard=["Freeze Authority still enabled"]), 10.0, value=20)
assert v2 == position.SELL and any("fails a safety check" in x for x in minus2), minus2
print("position check:", v, "-", head)

# ---- end to end with fake APIs: discovery, paper, alert email, watchlist, digest -------------------
sent_mail = []
report.send = lambda subj, body: sent_mail.append((subj, body)) or True
shutil.rmtree("out/demo_state", ignore_errors=True)
demo.COINS[demo.EARLY] = ("NPCS", 0.0003, 300_000, 45_000, 60, 900, 480, "up_break", 0, "clean", 2500)
holder = demo.TRADERS[4]["wallets"]["solana"]             # a top trader holds it -> alert-worthy
demo.buys.setdefault(holder, []).append(demo.EARLY)
main.run()
main.run()
st = state.load()
b = st["early"]
assert demo.EARLY in b["seen"] and "FOMO graduated" in b["seen"][demo.EARLY]["src"], b["seen"].get(demo.EARLY)
assert any(x["key"] == demo.EARLY for x in b["trades"]), "no paper trade opened"
mails = [m for m in sent_mail if m[0].startswith("EARLY")]
assert mails and "$NPCS" in mails[0][0] and "HIGH RISK" in mails[0][1], [m[0] for m in sent_mail]
assert demo.EARLY not in (st.get("watch") or {}), "early alerts must not auto-add to the watchlist"
assert "Early lane" in early.table(st)
print("end to end:", mails[0][0], "| watching", len(b["seen"]), "coins | paper trades", len(b["trades"]))

# ======================= review follow-ups ==========================================================
# RugCheck down -> never alert / paper a fresh coin on unverified safety
down = {"ok": True, "verified": False, "hard": [], "flags": ["RugCheck unavailable"], "good": []}
assert early.score(mkt(), {}, [("x", 1.0)], {}, down)["hard"]
# ...but a coin you HOLD isn't sold just because RugCheck is down
v3, _, _, _, _ = position.decide(dict(pr, safety=down), 10.0, value=40)
assert v3 != position.SELL, v3

# a danger-level copycat stays a hard fail; in the main lane a copycat warning is a severe flag
class RD:
    def json(self):
        return {"risks": [{"name": "Copycat token", "level": "danger"}], "lpLockedPct": 100}
safety.request = lambda *a, **k: RD()
assert safety._rugcheck("Xpump")["hard"]
safety.request = demo.fake_request
from scanner import scoring  # noqa: E402
assert any("copycat" in x for x in scoring.SEVERE)

# main-lane coins that merely slipped under $500k keep the main-lane rules
old = mkt(key="base:0xabc", created_ms=(NOW - 30 * 86400) * 1000, mcap=450_000)
v4, h4, _, _, lv4 = position.decide(dict(pr, market=old, score=65), -32.0, value=40)
assert v4 == position.SELL and lv4["tgt_pct"] == config.TARGET_GAIN_PCT, (v4, h4)

# watchlist: +100% target email says "sell half", then a trailing-stop email after a 30% drop
from scanner import watchlist  # noqa: E402
ws = {"watch": {}, "picks": []}
_m = mkt(price=1.0)                                       # you bought it and ran Check my position
ws["watch"][NPC] = watchlist._entry(_m, "manual", entry=1.0, stop=0.65, target=2.0)
it = ws["watch"][NPC]
watchlist._balances = lambda s_, it_: ({}, False)
out = []
for px in (2.05, 3.0, 2.0):
    out += watchlist.check(ws, {NPC: mkt(price=px, liquidity=48_000)})
msgs = [m for _, found in out for _, m, _ in found]
assert any("+100% target" in x for x in msgs) and any("fell 30% from its high" in x for x in msgs), msgs
print("review follow-ups OK")
