"""Oct 1 crash: a free RPC returned nothing for an old block and the WHOLE scan died.
Now that chain is handled, an archive refusal turns log reading off for a day, and any part of
the scan that breaks is skipped (and named in the email footer) while the rest still runs."""
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402  (installs fake APIs)
assert demo.COINS
from scanner import copy, evm, http, main, report, state  # noqa: E402

# 1. empty block replies (what crashed the scan)
calls = []


def rpc_none_blocks(chain, method, params):
    calls.append(method)
    if method == "eth_blockNumber":
        return hex(5_000_000)
    if method == "eth_getBlockByNumber":
        return None
    if method == "eth_getLogs":
        return []
    return None


real_rpc = evm.rpc
evm.rpc = rpc_none_blocks
s = {"evm_cursor": {}, "trader_buys": [], "decimals": {}}
tr = [{"evm": "0x" + "1" * 40, "handle": "a", "rank": 1}]
assert evm.scan_chain(s, "bsc", tr) == 0 and "eth_getLogs" in calls, "must fall back, not crash"

# 2. publicnode's 403 "Archive requests require a personal token" -> logs off for a day
evm.rpc = real_rpc


class R403:
    ok, status_code, text = False, 403, '{"error":{"code":-32602,"message":"Archive requests require a personal token"}}'


http.SESSION.request = lambda *a, **k: R403()
try:
    evm.rpc("base", "eth_getLogs", [{}])
    raise AssertionError("should raise")
except evm.RPCError as ex:
    assert "Archive" in str(ex), ex

# 3. any broken part of the scan is skipped and reported; alerts still go out
evm.rpc = real_rpc
sent = []
report.send = lambda subj, body: sent.append((subj, body)) or True
shutil.rmtree("out/demo_state", ignore_errors=True)
copy.scan = lambda s: 1 / 0                     # simulate a bug in one part
main.run()
st = state.load()
assert any("copy book" in x for x in st["scan_errors"]), st["scan_errors"]
digest = [b for subj, b in sent if subj.startswith("FOMO digest")]
assert digest and "Skipped this scan because of an error" in digest[0], [x[0] for x in sent]
print("resilience OK:", st["scan_errors"])
