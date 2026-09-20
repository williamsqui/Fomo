"""On-demand coin check: finds the coin, lists top traders holding it, emails a verdict."""
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import check, report  # noqa: E402
assert demo.COINS

sent = []
report.send = lambda subj, body: sent.append((subj, body)) or True
shutil.rmtree("out/demo_state", ignore_errors=True)
for addr in ("GRNDx111111111111111111111111111111111pump",        # Solana, 4 top traders hold it
             "0x2222222222222222222222222222222222222222",         # BNB, auto-detect chain
             "TINYx222222222222222222222222222222222pump",         # too small -> would not send
             "NotARealCoin111111111111111111111111111111"):
    check.run(addr)
for subj, body in sent:
    print(subj, "|", ("holding: " + body.split("holding it: ")[1].split("<")[0]) if "holding it: " in body else "")
assert "would send" in sent[0][0] and "BNDOG" in sent[1][0] and "would not send" in sent[2][0] and "not found" in sent[3][0]
print("coin check OK")
