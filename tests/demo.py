"""End-to-end run with fake API responses for every service (no keys, no network).

    python tests/demo.py   ->  out/latest.html (the digest email)
"""
import os
import random
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# make a digest due "now" so the demo sends one 
_due = time.strftime("%H:%M", time.gmtime(time.time() + 7 * 3600 - 1800))  # Vietnam time, 30 min ago
os.environ.update(FOMO_API_KEY="demo", HELIUS_API_KEY="demo", GETXAPI_KEY="demo",
                  STATE_DIR="out/demo_state", GT_SLEEP_SEC="0", DIGEST_TIMES=_due,
                  WATCH_REQ_DIR="out/watchreq", HELIUS_RPS="100000")

from scanner import chart, copy, dex, evm, fomo, safety, socials, solana, xsocial  # noqa: E402

random.seed(3)
NOW = time.time()
LATEST_BLOCK = 5_000_000
BLOCK_TIME = 2.0

# key: symbol, price, mcap, liq, h1, buys, sells, chart shape, #trader buyers, safety profile, tg members
COINS = {
    "solana:GRNDx111111111111111111111111111111111pump": ("GRIND", 0.0021, 2_100_000, 210_000, 14, 900, 520, "up_pullback", 4, "clean", 8200),
    "base:0x1111111111111111111111111111111111111111": ("BASEY", 0.00012, 1_200_000, 140_000, 9, 400, 260, "up_break", 2, "clean", 2400),
    "bsc:0x2222222222222222222222222222222222222222": ("BNDOG", 0.0031, 3_100_000, 260_000, 4, 600, 540, "up_pullback", 1, "clean", 900),
    "robinhood:0x3333333333333333333333333333333333333333": ("HOODC", 0.0008, 800_000, 95_000, 11, 310, 190, "up_break", 2, "unknown", 1500),
    "solana:TINYx222222222222222222222222222222222pump": ("TINY", 0.0003, 300_000, 40_000, 30, 200, 90, "up_break", 2, "clean", 0),
    "bsc:0x4444444444444444444444444444444444444444": ("HONEY", 0.02, 5_000_000, 400_000, 25, 800, 30, "up_break", 1, "honeypot", 3000),
    "solana:DUMPx333333333333333333333333333333333pump": ("DUMP", 0.0009, 900_000, 90_000, -8, 150, 400, "down", 1, "clean", 400),
    "base:0x5555555555555555555555555555555555555555": ("DROP", 0.001, 1_000_000, 100_000, 2, 100, 100, "up_break", 0, "clean", 0),
}
# early-lane coin: graduated from pump.fun 14h ago, $300k, pumping with a community (like $NPC)
EARLY = "solana:NPCSx444444444444444444444444444444444pump"
COINS[EARLY] = ("NPCS", 0.0003, 300_000, 45_000, 60, 900, 480, "up_break", 0, "clean", 2500)
AGE_H = {EARLY: 14}
TRADERS = [{"rank": i + 1, "handle": f"trader{i + 1}", "pnlUsd": 200_000 - i * 1500,
            "wallets": {"solana": f"W{i + 1:03d}" + "x" * 40, "evm": "0x" + f"{i + 1:040x}"}}
           for i in range(100)]

buys = {}   # wallet -> [key]
for k, c in COINS.items():
    for t in random.sample(TRADERS[:40], c[8]):
        w = t["wallets"]["solana"] if k.startswith("solana") else t["wallets"]["evm"]
        buys.setdefault(w, []).append(k)
sells = {TRADERS[60]["wallets"]["solana"]: ["solana:DUMPx333333333333333333333333333333333pump"]}
AIRDROP_TO = TRADERS[5]["wallets"]["evm"]  # DROP was airdropped - must NOT count as a buy


class Resp:
    def __init__(self, data, headers=None, text=""):
        self._d, self.headers, self.ok, self.status_code, self.text = data, headers or {}, True, 200, text

    def json(self):
        return self._d


def candles(shape, price):
    rows, p = [], price
    for i in range(168):  # build backwards from the current price
        t = NOW - i * 3600
        if shape == "down":
            p_prev = p * (1 + random.uniform(0.004, 0.012))
        elif shape == "up_pullback" and i < 8:
            p_prev = p * (1 + random.uniform(0.01, 0.025))   # last 8h pulled back
        else:
            p_prev = p * (1 - random.uniform(0.001, 0.009))
        o, c = p_prev, p
        rows.append([t, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 20_000 + (168 - i) * 150])
        p = p_prev
    return rows


LOGS_BLOCKED = {"base", "bsc"}


def multicall_result(vals):
    w = lambda n: format(n, "064x")
    n = len(vals)
    head = w(0x20) + w(n) + "".join(w(32 * n + 128 * i) for i in range(n))
    return "0x" + head + "".join(w(1) + w(0x40) + w(32) + w(v) for v in vals)


def evm_topic(a):
    return "0x" + "0" * 24 + a[2:]


def fake_request(method, url, params=None, json=None, headers=None, **kw):
    if "fomoapi" in url:
        if "/leaderboard/tokens/graduated" in url:
            return Resp({"board": "graduated", "tokens": [{"rank": 3, "network": "solana", "holders": 1800,
                                                           "marketCapUsd": 300_000,
                                                           "token": {"address": EARLY.split(":")[1]}}]},
                        {"x-credits-cost": "250"})
        if "/leaderboard/tokens/trending" in url:
            net = {"solana": "sol", "base": "base", "bsc": "bnb", "robinhood": "robinhood"}
            return Resp({"tokens": [{"rank": i + 1, "network": net[k.split(':')[0]], "token": {"address": k.split(":")[1]}}
                                    for i, k in enumerate(list(COINS)[:6])]}, {"x-credits-cost": "250"})
        if "/leaderboard/" in url:
            return Resp({"traders": TRADERS}, {"x-credits-cost": "250"})
        if "/v2/users/" in url:                      # profile lookup (copy-trader book, followed traders)
            h = url.rsplit("/", 1)[1]
            if h.lower() == "ether_monk":
                w = TRADERS[0]["wallets"]
            else:
                w = {"solana": "F" + h[:10].ljust(10, "x") + "y" * 33, "evm": "0x" + format(abs(hash(h)) % 16 ** 40, "040x")}
            return Resp({"user": {"handle": h, "wallets": w}}, {"x-credits-cost": "2500"})
        if "/thesis/" in url:
            return Resp([{"handle": "trader3", "text": "LP burned, strong community", "likes": 90}] * 7, {"x-credits-cost": "1250"})
    if "helius" in url:
        if len(json) > 10:        # like the real free plan: big batches get HTTP 429
            return None
        out = []
        for call in json:
            if call["method"] == "getTokenLargestAccounts":
                mint = call["params"][0]
                owners = [w for w, ks in buys.items() if f"solana:{mint}" in ks]
                out.append({"id": call["id"], "result": {"value": [
                    {"address": f"ATA-{w}", "uiAmount": 1e5} for w in owners]}})
                continue
            if call["method"] == "getMultipleAccounts":
                out.append({"id": call["id"], "result": {"value": [
                    {"data": {"parsed": {"info": {"owner": a[4:]}}}} for a in call["params"][0]]}})
                continue
            if call["method"] == "getTokenAccountsByOwner" and "programId" in call["params"][1]:
                w = call["params"][0]                 # every SPL holding of one wallet (classic program)
                classic = call["params"][1]["programId"].startswith("Tokenkeg")
                mints = [k.split(":")[1] for k in buys.get(w, []) if k.startswith("solana:")] if classic else []
                out.append({"id": call["id"], "result": {"value": [
                    {"account": {"data": {"parsed": {"info": {
                        "mint": m, "tokenAmount": {"uiAmount": 1e5}}}}}} for m in mints]}})
                continue
            if call["method"] == "getTokenAccountsByOwner":
                w, mint = call["params"][0], call["params"][1]["mint"]
                has = f"solana:{mint}" in buys.get(w, [])
                out.append({"id": call["id"], "result": {"value": [{"account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"uiAmount": 1e5}}}}}}] if has else []}})
                continue
            if call["method"] == "getSignaturesForAddress":
                w = call["params"][0]
                if call["params"][1].get("until"):
                    out.append({"id": call["id"], "result": []})
                    continue
                sigs = [{"signature": f"{w}|{k}|b", "blockTime": NOW - 1800, "err": None} for k in buys.get(w, [])]
                sigs += [{"signature": f"{w}|{k}|s", "blockTime": NOW - 600, "err": None} for k in sells.get(w, [])]
                out.append({"id": call["id"], "result": sigs})
            else:
                w, k, side = call["params"][0].split("|")
                mint = k.split(":")[1]
                sol, (pre, post) = (2.5, (0, 1e5)) if side == "b" else (-3.0, (1e5, 0))
                tb = lambda a: [{"owner": w, "mint": mint, "uiTokenAmount": {"uiAmount": a}}] if a else []
                out.append({"id": call["id"], "result": {
                    "meta": {"err": None, "fee": 5000, "preBalances": [10 ** 10], "postBalances": [int((10 - sol) * 1e9) - 5000],
                             "preTokenBalances": tb(pre), "postTokenBalances": tb(post)},
                    "transaction": {"message": {"accountKeys": [{"pubkey": w}]}}}})
        return Resp(out)
    if "publicnode" in url or "robinhood.com" in url:
        chain = "base" if "base" in url else "bsc" if "bsc" in url else "robinhood"
        m, p = json["method"], json["params"]
        if m == "eth_getLogs" and chain in LOGS_BLOCKED:   # like the real free Base/BNB RPCs
            return Resp({"error": {"code": -32602, "message": "Archive requests require a personal token"}})
        if m == "eth_call" and p[0]["to"].lower() == evm.MULTICALL3.lower():
            data = p[0]["data"][10:]
            n = int(data[64:128], 16)
            tuples = data[128 + 64 * n:]
            vals = []
            for i in range(n):
                t = tuples[i * 384:(i + 1) * 384]
                token, wallet = "0x" + t[24:64], "0x" + t[288:328]
                vals.append(10 ** 24 if f"{chain}:{token}" in buys.get(wallet, []) else 0)
            return Resp({"result": multicall_result(vals)})
        if m == "eth_blockNumber":
            return Resp({"result": hex(LATEST_BLOCK)})
        if m == "eth_getBlockByNumber":
            n = int(p[0], 16)
            return Resp({"result": {"timestamp": hex(int(NOW - (LATEST_BLOCK - n) * BLOCK_TIME))}})
        if m == "eth_call":
            return Resp({"result": hex(18)})
        if m == "eth_getTransactionByHash":
            h = p[0]
            return Resp({"result": {"from": "0xdeadbeef" if "airdrop" in h else h.split("-")[1]}})
        if m == "eth_getLogs":
            f = p[0]
            frm, to = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            blk = LATEST_BLOCK - 900
            if not (frm <= blk <= to):
                return Resp({"result": []})
            logs, incoming = [], f["topics"][1] is None
            if incoming:
                for w, ks in buys.items():
                    for k in ks:
                        if k.startswith(chain + ":"):
                            logs.append({"address": k.split(":")[1], "topics": [evm.TRANSFER, evm_topic("0x" + "9" * 40), evm_topic(w)],
                                         "data": hex(10 ** 23), "transactionHash": f"0xtx-{w}-{k}", "blockNumber": hex(blk)})
                if chain == "base":
                    logs.append({"address": "0x5555555555555555555555555555555555555555",
                                 "topics": [evm.TRANSFER, evm_topic("0x" + "8" * 40), evm_topic(AIRDROP_TO)],
                                 "data": hex(10 ** 24), "transactionHash": "0xairdrop-1", "blockNumber": hex(blk)})
            return Resp({"result": logs})
    if "dexscreener" in url and "/token-profiles/latest" in url:
        return Resp([{"chainId": "solana", "tokenAddress": EARLY.split(":")[1], "url": "", "links": []},
                     {"chainId": "solana", "tokenAddress": "JUNKx555555555555555555555555555555555pump"},
                     {"chainId": "base", "tokenAddress": "0xabc"}])
    if "dexscreener" in url and "/community-takeovers/" in url:
        return Resp([])
    if "dexscreener" in url:
        chain_id, addrs = url.split("/tokens/v1/")[1].split("/")
        pairs = []
        for a in addrs.split(","):
            if a.startswith("So111"):
                pairs.append({"baseToken": {"address": a, "symbol": "SOL"}, "priceUsd": "180", "liquidity": {"usd": 1e8}})
                continue
            c = COINS.get(f"{chain_id}:{a}")
            if not c:
                continue
            pairs.append({"baseToken": {"address": a, "symbol": c[0], "name": c[0].title()},
                          "pairAddress": "POOL" + a[-6:], "dexId": "demo",
                          "priceUsd": str(c[1]), "marketCap": c[2], "liquidity": {"usd": c[3]},
                          "priceChange": {"h1": c[4], "h24": c[4] * 2},
                          "txns": {"h1": {"buys": c[5], "sells": c[6]}},
                          "volume": {"h1": c[5] * 90, "h6": c[5] * 300},
                          "pairCreatedAt": (NOW - AGE_H.get(f"{chain_id}:{a}", 9 * 24) * 3600) * 1000,
                          "url": f"https://dexscreener.com/{chain_id}/{a}",
                          "info": {"socials": [{"type": "twitter", "url": "https://x.com/demo"},
                                               {"type": "telegram", "url": f"https://t.me/{c[0].lower()}"}],
                                   "websites": [{"url": "https://example.com"}]}})
        return Resp(pairs)
    if "geckoterminal" in url:
        net = url.split("/networks/")[1].split("/")[0]
        if url.endswith("/new_pools"):
            return Resp({"data": [{"relationships": {"base_token": {"data": {"id": "solana_" + EARLY.split(":")[1]}}}},
                                  {"relationships": {"base_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112"}}}}]})
        if url.endswith("/trending_pools"):
            return Resp({"data": [{"relationships": {"base_token": {"data": {"id": f"{net}_{k.split(':')[1]}"}}}}
                                  for k in COINS if k.startswith(net + ":")]})
        if "/ohlcv/" in url:
            tail = url.split("/pools/POOL")[1].split("/")[0]
            c = next(v for k, v in COINS.items() if k.startswith(net) and k.endswith(tail))
            return Resp({"data": {"attributes": {"ohlcv_list": candles(c[7], c[1])}}})
        addr = url.split("/tokens/")[1].split("/")[0]
        c = COINS[f"{net}:{addr}"]
        return Resp({"data": {"attributes": {"holders": {"count": 4200, "distribution_percentage": {"top_10": "21.5"}},
                                             "gt_score": 70}}})
    if "rugcheck" in url:
        return Resp({"risks": [{"name": "Low amount of LP Providers", "level": "warn"}], "lpLockedPct": 100})
    if "gopluslabs" in url:
        addr = params["contract_addresses"]
        c = next(v for k, v in COINS.items() if k.endswith(addr))
        if c[9] == "unknown":
            return Resp({"code": 2, "result": {}})
        hp = "1" if c[9] == "honeypot" else "0"
        return Resp({"result": {addr: {"is_honeypot": hp, "buy_tax": "0", "sell_tax": "0.99" if hp == "1" else "0",
                                       "is_open_source": "1", "holders": [{"percent": "0.03", "is_contract": "0"}] * 10,
                                       "lp_holders": [{"percent": "0.95", "is_locked": "1"}]}}})
    if "blockscout" in url and "token-balances" in url:
        return Resp([])                               # the demo trader holds nothing on EVM
    if "t.me" in url:
        name = url.rsplit("/", 1)[1].upper()
        n = next((v[10] for v in COINS.values() if v[0] == name), 0)
        return Resp(None, text=f'<div class="tgme_page_extra">{n:,} members, 120 online</div>')
    if "getxapi" in url:
        sym = params["q"].split("$")[-1]
        n = {"GRIND": 22, "BASEY": 12, "BNDOG": 6, "HOODC": 10, "DUMP": 3}.get(sym, 0)
        tw = [{"text": f"${sym} chart looks clean #{i}", "likeCount": 30 + i * 12, "retweetCount": 4,
               "createdAt": time.strftime("%a %b %d %H:%M:%S +0000 %Y", time.gmtime(NOW - 1200)),
               "author": {"userName": "trader2" if (sym == "GRIND" and i == 0) else f"u{sym}{i}",
                          "followers": 32_000 if i < 2 else 900}} for i in range(n)]
        return Resp({"tweets": tw, "has_more": False})
    raise RuntimeError("unmocked " + url)


for mod in (dex, fomo, solana, evm, xsocial, chart, safety, socials, copy):
    mod.request = fake_request

if __name__ == "__main__":
    from scanner import main
    shutil.rmtree("out/demo_state", ignore_errors=True)
    main.run()
    print("\n--- second run (digest already sent, so no email) ---")
    main.run()
    print("\nReport written to out/latest.html")
