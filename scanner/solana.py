"""Watch leaderboard traders' Solana wallets for new buys/sells (Helius free RPC).

Cost: 2 credits per wallet read (one per token program).
Top 50 wallets every scan, the rest every 3rd scan (~575k of the 1M free credits a month).
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


LAST_FAIL = [""]          # why the most recent Helius call failed ("budget" / "rate limit")
_next_ok = [0.0]


def _rpc(s, calls, on_demand=False):
    """calls = [(method, params), ...]. Returns results in order (None for any that failed).

    Helius' free plan allows 10 requests/second and counts every call inside a batch, so a
    50-call batch gets rejected outright (HTTP 429). Before this fix that silently turned
    "check all 100 top-trader wallets" into "couldn't read any of them". Now calls go out in
    batches of at most HELIUS_BATCH, spaced to stay under HELIUS_RPS.
    Returns None only if nothing could be sent at all (monthly budget pacing reached).
    """
    # on-demand checks (a button you pressed) only need to fit the monthly total; the
    # every-10-minutes scanner is also paced evenly across the month
    fits = (s["helius_credits_used"] + len(calls) <= config.HELIUS_MONTHLY_CREDITS if on_demand else
            st.within_budget(s["helius_credits_used"], config.HELIUS_MONTHLY_CREDITS, len(calls)))
    if not fits:
        LAST_FAIL[0] = "budget"
        return None
    out = []
    for i in range(0, len(calls), config.HELIUS_BATCH):
        chunk = calls[i:i + config.HELIUS_BATCH]
        body = [{"jsonrpc": "2.0", "id": j, "method": m, "params": p} for j, (m, p) in enumerate(chunk)]
        res = None
        for attempt in range(3):
            wait = _next_ok[0] - time.time()
            if wait > 0:
                time.sleep(wait)
            _next_ok[0] = time.time() + len(chunk) / config.HELIUS_RPS
            r = request("POST", f"https://mainnet.helius-rpc.com/?api-key={config.HELIUS_API_KEY}",
                        json=body, timeout=30, retries=1)
            if r is not None:
                res = r.json()
                break
            LAST_FAIL[0] = "rate limit"
            time.sleep(1.5 * (attempt + 1))
        if res is None:
            out += [None] * len(chunk)
            continue
        s["helius_credits_used"] += len(chunk)
        res = [res] if isinstance(res, dict) else res
        by_id = {x.get("id"): x.get("result") for x in res}
        out += [by_id.get(j) for j in range(len(chunk))]
    return out


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


TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",     # SPL Token
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]     # Token-2022 (newer launches)
CHANGE = 0.02        # balance moves smaller than 2% are rounding / rebases, not trades
OUT = 0.2            # under 20% of what they had = sold out (anything less is a trim)


def read_holdings(s, wallets):
    """{wallet: {mint: amount}} for each wallet read COMPLETELY this scan.

    Both token programs must come back for a wallet to count as read - half a read would make
    every coin on the missing program look sold.
    """
    out = {}
    calls = lambda ws: [("getTokenAccountsByOwner", [w, {"programId": p}, {"encoding": "jsonParsed"}])
                        for w in ws for p in TOKEN_PROGRAMS]
    dead = 0                                          # consecutive batches where nothing came back
    for i in range(0, len(wallets), 5):              # 5 wallets = 10 calls = one Helius batch
        chunk = wallets[i:i + 5]
        res = _rpc(s, calls(chunk))
        if res is None:                               # monthly pacing: leave the rest for later
            break
        whole_fail = all(r is None for r in res)
        if dead >= 2:                                 # Helius is down: don't burn the whole run
            log.info("Solana holdings: Helius not answering - stopping this scan's wallet reads")
            break
        got_any = not whole_fail
        for j, w in enumerate(chunk):
            parts = res[2 * j:2 * j + 2]
            if any(r is None for r in parts) and (not whole_fail or dead == 0):
                # one whale wallet with thousands of token accounts can make the whole batch too
                # big - give each wallet one try on its own so the others still get read
                parts = _rpc(s, calls([w])) or [None, None]
            if any(r is None for r in parts):
                continue
            got_any = True
            bal = {}
            for r in parts:
                for acc in (r.get("value") or []):
                    try:
                        info = acc["account"]["data"]["parsed"]["info"]
                        ta = info["tokenAmount"]
                        amt = float(ta.get("uiAmountString") or ta.get("uiAmount") or 0)
                    except (KeyError, TypeError, ValueError):
                        continue
                    if amt > 0 and info["mint"] not in QUOTES:
                        bal[info["mint"]] = bal.get(info["mint"], 0.0) + amt
            out[w] = bal
        dead = 0 if got_any else dead + 1             # batch AND single retries all failed
    return out


def scan_wallets(s, traders, now=None):
    """Read what each trader's wallet holds and turn changes since their last read into events.

    Replaces reading transactions. That approach fetched a wallet's 25 newest transactions,
    parsed only 10, and marked all 25 as seen - so a busy trader's buys (or a wallet full of
    spam airdrops) were silently lost, and a sell with no matching buy scored as a full exit.
    A holdings diff can't miss a position change, whatever happened in between.
    Events carry usd=None; main.py prices them with DexScreener and drops unpriced dust/spam.
    """
    now = now or time.time()
    snaps = s.setdefault("wallet_snap", {})
    by = {t["wallet"]: t for t in traders if t.get("wallet")}
    if not by or not config.HELIUS_API_KEY:
        return 0
    read = read_holdings(s, list(by))
    events = 0
    for w, bal in read.items():
        prev = snaps.get(w)
        snaps[w] = {"ts": now, "bal": bal}
        if prev is None:                  # first read of this wallet: baseline, not a buy
            continue
        t, old_bal = by[w], prev["bal"]
        # The change happened some time since the last read. Normally that's 10-30 min, but after
        # a gap (Helius outage, skipped runs) a buy from hours ago must not look brand new: date
        # it at the earliest possible time so it can't pass as a fresh signal or dodge "too late".
        # (Sells keep the time we saw them, so an exit alert is never dated before your buy.)
        gap = now - prev["ts"] > config.HOLDINGS_MAX_AGE_MIN * 60
        for mint in set(bal) | set(old_bal):
            new, old = bal.get(mint, 0.0), old_bal.get(mint, 0.0)
            if new > old * (1 + CHANGE):
                side, amt = "buy", new - old
            elif new < old * (1 - CHANGE):
                side, amt = "sell", old - new
            else:
                continue
            when = prev["ts"] if gap and side == "buy" else now
            e = {"ts": when, "sig": f"snap-{w}-{mint}-{int(now)}-{side}", "wallet": w,
                 "handle": t["handle"], "rank": t["rank"], "key": chains.key("solana", mint),
                 "side": side, "amount": amt, "usd": None, "src": "snap", "left": new, "seen": now}
            if gap:
                e["gap"] = True
            if side == "sell":
                e["out"] = new < old * OUT
            s["trader_buys"].append(e)
            events += 1
    s["wallet_cov"] = {"read": len(read), "tried": len(by), "ts": now}
    if len(read) < len(by):
        log.info("Solana holdings: read %d/%d wallets (%s)", len(read), len(by), LAST_FAIL[0] or "errors")
    log.info("Solana holdings scan: %d position changes", events)
    return events


def holder_index(s, by_sol, now=None):
    """{token key: {wallet: amount}} from recent full wallet reads of current leaderboard traders."""
    now = now or time.time()
    idx = {}
    for w, sn in (s.get("wallet_snap") or {}).items():
        if w in by_sol and now - sn["ts"] <= config.HOLDINGS_MAX_AGE_MIN * 60:
            for mint, amt in sn["bal"].items():
                idx.setdefault(chains.key("solana", mint), {})[w] = amt
    return idx


def dedupe(s):
    """Same tx can be seen twice if a run is retried."""
    seen, uniq = set(), []
    for e in s["trader_buys"]:
        k = (e["sig"], e["key"], e["side"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    s["trader_buys"] = uniq
