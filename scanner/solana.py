"""Watch leaderboard traders' Solana wallets for new buys/sells (Helius free RPC).

Cost: 1 credit per getSignaturesForAddress + 1 per getTransaction.
Top 50 wallets every scan, the rest every 3rd scan (~290k/month), plus parsed txs.
"""
import logging
import time

from . import chains, config, state as st
from .http import request

log = logging.getLogger("solana")

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT
QUOTES = STABLES | {WSOL}


def _rpc(s, calls):
    """calls = [(method, params), ...] sent as one JSON-RPC batch."""
    cost = len(calls)
    if not st.within_budget(s["helius_credits_used"], config.HELIUS_MONTHLY_CREDITS, cost):
        return None
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
            for i, (m, p) in enumerate(calls)]
    r = request("POST", f"https://mainnet.helius-rpc.com/?api-key={config.HELIUS_API_KEY}",
                json=body, timeout=30)
    if r is None:
        return None
    s["helius_credits_used"] += cost
    out = r.json()
    if isinstance(out, dict):
        out = [out]
    out.sort(key=lambda x: x.get("id", 0))
    return [o.get("result") for o in out]


def parse_swap(tx, wallet):
    """Return list of (mint, side, token_amount, sol_spent, usd_spent) for this wallet."""
    if not tx or (tx.get("meta") or {}).get("err"):
        return []
    meta = tx["meta"]
    deltas = {}
    for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(key) or []:
            if b.get("owner") != wallet:
                continue
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            deltas[b["mint"]] = deltas.get(b["mint"], 0.0) + sign * amt

    keys = tx["transaction"]["message"]["accountKeys"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    sol_delta = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        if i == 0:
            sol_delta += meta.get("fee", 0) / 1e9
    sol_delta += deltas.pop(WSOL, 0.0)
    stable_delta = sum(deltas.pop(m, 0.0) for m in list(deltas) if m in STABLES)

    events = []
    for mint, d in deltas.items():
        if abs(d) < 1e-12:
            continue
        if d > 0 and (sol_delta < -0.001 or stable_delta < -0.5):
            events.append((mint, "buy", d, max(0, -sol_delta), max(0, -stable_delta)))
        elif d < 0 and (sol_delta > 0.001 or stable_delta > 0.5):
            events.append((mint, "sell", -d, max(0, sol_delta), max(0, stable_delta)))
    return events


def scan_wallets(s, traders, sol_price):
    now = time.time()
    since = now - config.LOOKBACK_HOURS * 3600
    new_events = 0
    for t in traders:
        w = t.get("wallet")
        if not w or not config.HELIUS_API_KEY:
            continue
        opts = {"limit": 25}
        if s["wallet_cursor"].get(w):
            opts["until"] = s["wallet_cursor"][w]
        res = _rpc(s, [("getSignaturesForAddress", [w, opts])])
        if res is None:
            log.info("Helius budget pacing reached; stopping wallet scan")
            break
        sigs = [x for x in (res[0] or []) if not x.get("err") and (x.get("blockTime") or 0) >= since]
        if res[0]:
            s["wallet_cursor"][w] = res[0][0]["signature"]
        sigs = sigs[:config.MAX_TX_PER_WALLET_PER_RUN]
        if not sigs:
            continue
        txs = _rpc(s, [("getTransaction", [x["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]) for x in sigs])
        for sig, tx in zip(sigs, txs or []):
            for mint, side, amt, sol, usd in parse_swap(tx, w):
                if mint in chains.QUOTE_ADDRESSES:
                    continue
                s["trader_buys"].append({
                    "ts": sig.get("blockTime") or now, "sig": sig["signature"],
                    "wallet": w, "handle": t["handle"], "rank": t["rank"],
                    "key": chains.key("solana", mint), "side": side, "amount": amt,
                    "usd": round(usd + sol * sol_price, 2)})
                new_events += 1
    dedupe(s)
    log.info("Solana wallet scan: %d new trader swaps", new_events)
    return new_events


def dedupe(s):
    """Same tx can be seen twice if a run is retried."""
    seen, uniq = set(), []
    for e in s["trader_buys"]:
        k = (e["sig"], e["key"], e["side"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    s["trader_buys"] = uniq
