"""Watch leaderboard traders' EVM wallet (Base, BNB, Robinhood Chain) via free public RPCs.

One eth_getLogs query per block chunk finds every ERC-20 Transfer into or out of
any of the 100 wallets. Incoming transfers only count as buys when the trader
sent the transaction themselves, which filters out scam airdrops.
"""
import logging
import time

from . import chains, config
from .http import request
from .solana import dedupe

log = logging.getLogger("evm")
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class RPCError(Exception):
    pass


def rpc(chain, method, params):
    r = request("POST", config.EVM_RPC[chain], timeout=30,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if r is None:
        raise RPCError(f"{chain} {method} failed")
    d = r.json()
    if "error" in d:
        raise RPCError(f"{chain} {method}: {d['error']}")
    return d.get("result")


def _topic(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def _decimals(s, chain, token):
    k = chains.key(chain, token)
    if k not in s["decimals"]:
        try:
            res = rpc(chain, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
            s["decimals"][k] = int(res, 16) if res and res != "0x" else 18
        except (RPCError, ValueError):
            return 18
    return s["decimals"][k]


def scan_chain(s, chain, traders):
    by_addr = {t["evm"]: t for t in traders if t.get("evm")}
    if not by_addr:
        return 0
    latest = int(rpc(chain, "eth_blockNumber", []), 16)
    older = rpc(chain, "eth_getBlockByNumber", [hex(latest - 1000), False])
    newer = rpc(chain, "eth_getBlockByNumber", [hex(latest), False])
    block_time = max(0.05, (int(newer["timestamp"], 16) - int(older["timestamp"], 16)) / 1000)
    now_ts = int(newer["timestamp"], 16)

    start = s["evm_cursor"].get(chain)
    lookback = int(config.LOOKBACK_HOURS * 3600 / block_time)
    start = latest - lookback if start is None else start + 1
    start = max(start, latest - config.EVM_LOG_CHUNK * config.EVM_MAX_CHUNKS)
    wtopics = [_topic(a) for a in by_addr]

    logs_in, logs_out, scanned_to = [], [], start - 1
    for frm in range(start, latest + 1, config.EVM_LOG_CHUNK):
        to = min(latest, frm + config.EVM_LOG_CHUNK - 1)
        try:
            logs_in += rpc(chain, "eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                   "topics": [TRANSFER, None, wtopics]}]) or []
            logs_out += rpc(chain, "eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                    "topics": [TRANSFER, wtopics]}]) or []
        except RPCError as e:
            log.warning("%s; stopping at block %d", e, scanned_to)
            break
        scanned_to = to
        time.sleep(0.2)
    s["evm_cursor"][chain] = scanned_to

    events, sender_cache = 0, {}
    for side, logs, idx in (("buy", logs_in, 2), ("sell", logs_out, 1)):
        for lg in logs:
            token = lg["address"].lower()
            if token in chains.QUOTE_ADDRESSES or len(lg.get("topics", [])) < 3:
                continue
            wallet = "0x" + lg["topics"][idx][-40:].lower()
            t = by_addr.get(wallet)
            if not t:
                continue
            txh = lg["transactionHash"]
            if side == "buy":  # only count it if the trader sent the tx (no airdrops)
                if txh not in sender_cache:
                    try:
                        tx = rpc(chain, "eth_getTransactionByHash", [txh]) or {}
                        sender_cache[txh] = (tx.get("from") or "").lower()
                    except RPCError:
                        sender_cache[txh] = ""
                if sender_cache[txh] != wallet:
                    continue
            try:
                raw = int(lg["data"], 16) if lg.get("data") not in (None, "0x") else 0
            except ValueError:
                continue
            amount = raw / 10 ** _decimals(s, chain, token)
            ts = now_ts - (latest - int(lg["blockNumber"], 16)) * block_time
            s["trader_buys"].append({"ts": ts, "sig": txh, "wallet": wallet, "handle": t["handle"],
                                     "rank": t["rank"], "key": chains.key(chain, token),
                                     "side": side, "amount": amount, "usd": None})
            events += 1
    return events


def scan_wallets(s, traders):
    total = 0
    for chain in config.CHAINS:
        if chain == "solana":
            continue
        try:
            n = scan_chain(s, chain, traders)
            log.info("%s wallet scan: %d trader transfers", chain, n)
            total += n
        except RPCError as e:
            log.warning("%s scan skipped: %s", chain, e)
    dedupe(s)
    return total
