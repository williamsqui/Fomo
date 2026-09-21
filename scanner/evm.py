"""Watch leaderboard traders' EVM wallet (Base, BNB, Robinhood Chain) via free public RPCs.

Two methods, so every chain is covered even on free RPCs:

1. Transfer logs (eth_getLogs): finds every ERC-20 buy/sell by the 100 wallets.
   Many free RPCs refuse this ("archive requests require a token"). When that
   happens the chain is switched to method 2 only, and retried once a day.
2. Holdings snapshots (eth_call balanceOf, batched with Multicall3): for each
   candidate coin, read how much of it every leaderboard wallet holds right now.
   Only needs the latest block, which every free RPC serves. Comparing with the
   previous scan (10 min earlier) shows who bought or sold.
Incoming log transfers only count as buys when the trader sent the transaction
themselves, which filters out scam airdrops.
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
    off = s.setdefault("evm_logs_off", {}).get(chain)
    if off and time.time() - off < 86400:
        return 0  # this RPC refuses log history; holdings snapshots cover the chain
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
            msg = str(e).lower()
            if any(w in msg for w in ("archive", "personal token", "-32602", "range", "limit")):
                s["evm_logs_off"][chain] = time.time()
                s["evm_cursor"].pop(chain, None)
                log.info("%s RPC refuses log history; using holdings snapshots (retry in 24h)", chain)
                return 0
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
            log.info("%s log scan skipped: %s", chain, e)
    dedupe(s)
    return total


# ---------------------------------------------------------------- holdings snapshots
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"  # same address on Base, BNB and most EVM chains
MIN_EVENT_USD = 20.0   # ignore dust changes


def _word(n):
    return format(n, "064x")


def encode_aggregate3(token, wallets):
    """aggregate3((address target, bool allowFailure, bytes callData)[]) with balanceOf(wallet) calls."""
    n = len(wallets)
    call = lambda w: "70a08231" + "0" * 24 + w[2:].lower()  # 36 bytes -> padded to 64
    tuple_hex = lambda w: ("0" * 24 + token[2:].lower() + _word(1) + _word(0x60) + _word(36)
                           + call(w) + "0" * 56)
    body = _word(0x20) + _word(n)
    body += "".join(_word(32 * n + 192 * i) for i in range(n))
    body += "".join(tuple_hex(w) for w in wallets)
    return "0x82ad56cb" + body


def decode_aggregate3(hexdata):
    """Return list of (success, int value or None)."""
    b = bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata)
    word = lambda pos: int.from_bytes(b[pos:pos + 32], "big")
    arr = word(0)
    n = word(arr)
    base = arr + 32
    out = []
    for i in range(n):
        t = base + word(base + 32 * i)
        ok = word(t) == 1
        data_at = t + word(t + 32)
        ln = word(data_at)
        data = b[data_at + 32: data_at + 32 + ln]
        out.append((ok, int.from_bytes(data[:32], "big") if ok and ln >= 32 else None))
    return out


def balances_checked(chain, token, wallets):
    """({wallet: raw balance}, complete) via Multicall3 (fallback: batched eth_call).

    `complete` is True only if every wallet was actually read. An empty dict with
    complete=True means "nobody holds it"; complete=False means "we don't know",
    and callers must not treat that as proof of a sell-off.
    """
    out, read = {}, 0
    for i in range(0, len(wallets), 100):
        chunk = wallets[i:i + 100]
        try:
            res = rpc(chain, "eth_call", [{"to": MULTICALL3, "data": encode_aggregate3(token, chunk)}, "latest"])
            got = decode_aggregate3(res)
            for w, (ok, v) in zip(chunk, got):
                if ok:
                    read += 1
                    if v:
                        out[w] = v
            continue
        except (RPCError, ValueError, IndexError):
            pass
        body = [{"jsonrpc": "2.0", "id": j, "method": "eth_call",
                 "params": [{"to": token, "data": "0x70a08231" + "0" * 24 + w[2:]}, "latest"]}
                for j, w in enumerate(chunk)]
        for k in range(0, len(body), 25):
            r = request("POST", config.EVM_RPC[chain], json=body[k:k + 25], timeout=30)
            if r is None:
                continue
            for item in r.json() if isinstance(r.json(), list) else []:
                if item.get("error"):
                    continue
                try:
                    v = int(item.get("result") or "0x0", 16)
                except ValueError:
                    continue
                read += 1
                if v:
                    out[chunk[item["id"]]] = v
    return out, (bool(wallets) and read >= len(wallets))


def balances(chain, token, wallets):
    """{wallet: raw balance} for all wallets (see balances_checked for reliability info)."""
    return balances_checked(chain, token, wallets)[0]


def apply_snapshot(s, k, price, snap, by_addr, sell_floor=0.0):
    """Store a holdings snapshot; changes since the previous one become buy/sell events.

    snap: {wallet: token amount} for leaderboard wallets holding the coin now.
    sell_floor: for partial snapshots (Solana top-20 holders), a wallet missing from the
    snapshot only counts as a seller if its old balance was well above this floor.
    """
    hold = s.setdefault("holdings", {})
    prev = hold.get(k)
    now, events = time.time(), 0
    if prev:
        for w, amt in snap.items():
            delta = amt - prev["bal"].get(w, 0.0)
            t = by_addr.get(w)
            if t and delta * price >= MIN_EVENT_USD:
                s["trader_buys"].append({"ts": now, "sig": f"hold-{k}-{w}-{int(now)}", "wallet": w,
                                         "handle": t["handle"], "rank": t["rank"], "key": k, "side": "buy",
                                         "amount": delta, "usd": round(delta * price, 2)})
                events += 1
        for w, old in prev["bal"].items():
            new = snap.get(w)
            if new is None and old * 0.2 <= sell_floor:
                continue  # just dropped out of a partial snapshot - can't tell if they sold
            new = new or 0.0
            t = by_addr.get(w)
            if t and old > 0 and new < old * 0.2 and (old - new) * price >= MIN_EVENT_USD:
                s["trader_buys"].append({"ts": now, "sig": f"hold-{k}-{w}-{int(now)}-s", "wallet": w,
                                         "handle": t["handle"], "rank": t["rank"], "key": k, "side": "sell",
                                         "amount": old - new, "usd": round((old - new) * price, 2)})
                events += 1
    hold[k] = {"ts": now, "bal": snap}
    return events


def holding_events(s, k, price, by_addr, max_age_min=120):
    """Current holders from the latest snapshot, as 'held' events for scoring (not stored)."""
    h = (s.get("holdings") or {}).get(k)
    if not h or time.time() - h["ts"] > max_age_min * 60:
        return []
    out = []
    for w, amt in h["bal"].items():
        t = by_addr.get(w)
        if t and amt * price >= MIN_EVENT_USD:
            out.append({"ts": time.time(), "wallet": w, "handle": t["handle"], "rank": t["rank"], "key": k,
                        "side": "buy", "amount": amt, "usd": round(amt * price, 2), "held": True})
    return out


def holdings_scan(s, traders, markets, max_per_chain=8):
    """Snapshot leaderboard holdings of EVM candidate coins; diffs become buy/sell events."""
    by_addr = {t["evm"]: t for t in traders if t.get("evm")}
    wallets = list(by_addr)
    if not wallets:
        return 0
    events = 0
    per_chain = {}
    for k, m in markets.items():
        if m["chain"] != "solana" and m["chain"] in config.EVM_RPC and m["chain"] in config.CHAINS:
            per_chain.setdefault(m["chain"], []).append(m)
    for chain, ms in per_chain.items():
        ms.sort(key=lambda m: -m["volume"].get("h1", 0))
        for m in ms[:max_per_chain]:
            try:
                raw = balances(chain, m["address"], wallets)
            except RPCError as e:
                log.info("%s holdings check failed: %s", chain, e)
                break
            dec = _decimals(s, chain, m["address"])
            events += apply_snapshot(s, m["key"], m["price"], {w: v / 10 ** dec for w, v in raw.items()}, by_addr)
    dedupe(s)
    log.info("EVM holdings check: %d changes", events)
    return events
