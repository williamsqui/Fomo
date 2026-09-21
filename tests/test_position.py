"""Hold/sell verdicts for coins you already own."""
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import position, report  # noqa: E402
assert demo.COINS
sent = []
report.send = lambda subj, body: sent.append(subj) or True
shutil.rmtree("out/demo_state", ignore_errors=True)
G, D = "GRNDx111111111111111111111111111111111pump", "DUMPx333333333333333333333333333333333pump"
cases = [
    (G, 10, 30, "HOLD"),              # strong coin, small gain -> hold
    (G, 62, 50, "TAKE PROFIT"),       # hit +50% target -> take profit
    (G, -35, 20, "SELL"),             # past the -30% stop -> sell
    (D, 5, 40, "SELL"),               # weak coin, a top trader dumped -> sell
    ("0x4444444444444444444444444444444444444444", 3, 25, "SELL"),   # honeypot -> sell
]
for addr, pnl, val, want in cases:
    got = position.run(addr, pnl, val)
    assert got == want, (addr[:6], pnl, got, want)
for sub in sent:
    print(sub)
print("position checks OK")
