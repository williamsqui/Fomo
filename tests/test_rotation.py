"""Every investable coin gets fully scored within a few scans, even with a small refresh budget."""
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
from scanner import config, main, report, state  # noqa: E402
assert demo.COINS
config.FINALISTS = 2                       # only 2 fresh fetches per scan
report.send = lambda *a: True
shutil.rmtree("out/demo_state", ignore_errors=True)
for i in range(7):
    main.run()
s = state.load()
checked = {k for k, v in s["deep_cache"].items() if "safety" in v}
investable = {"solana:GRNDx111111111111111111111111111111111pump", "base:0x1111111111111111111111111111111111111111",
              "bsc:0x2222222222222222222222222222222222222222", "robinhood:0x3333333333333333333333333333333333333333",
              "bsc:0x4444444444444444444444444444444444444444", "solana:DUMPx333333333333333333333333333333333pump",
              "base:0x5555555555555555555555555555555555555555"}
assert investable <= checked, investable - checked
from scanner import holders, fomo  # noqa: E402
ctx = holders.context(s, fomo.leaderboard(s))
sol = ctx["sol"].get("solana:GRNDx111111111111111111111111111111111pump") or {}
assert len(sol) == 4, sol
print(f"all {len(investable)} investable coins fully checked within 7 scans (2 refreshes each); GRIND held by {len(sol)} top traders (full wallet reads)")
