"""Trade history for a plain Solana wallet address (someone who isn't on FOMO).

The copy backtests normally read a trader's history from the FOMO API by handle. For a bare
wallet we rebuild it from the chain instead (Helius, 1 credit per transaction):
  * getSignaturesForAddress pages back through everything the wallet signed
  * getTransaction (10 per batch) -> solana.parse_swap: token in for SOL/USDC out = buy, and back
  * SOL amounts are priced with SOL's hourly USD price at the time (GeckoTerminal)
  * buys and sells of one coin are grouped into positions shaped like FOMO's, so the copy
    backtest, the stop-loss/take-profit backtest and "their own P&L" all work unchanged
Progress is saved in the same cache as the FOMO histories, so a very busy wallet can be
fetched over several runs. EVM (0x...) wallets: live paper copying works, backtest doesn't yet.
"""
import logging
import time

from . import chains, chart, config, dex, solana

log = logging.getLogger("walletback")

MAX_TX = int(__import__("os").getenv("WALLETBACK_MAX_TX") or 15000)   # per run; 1 Helius credit each
PAD = 7 * 86400          # also read a week before the window, so sells in it find their buys
SIG_PAGE = 1000          # signatures per page (the RPC maximum)
CLOSED_LEFT = 0.02       # under 2% of what they bought left = position closed


def sol_prices(t_from):
    """{hour start: SOL price in USD} from the biggest SOL pool's hourly candles."""
    d = chart._gt(f"/networks/solana/tokens/{solana.WSOL}/pools", {"page": 1})
    pools = [p for p in (d or {}).get("data") or []
             if "usd" in ((p.get("attributes") or {}).get("name") or "").lower()] or (d or {}).get("data") or []
    out = {}
    for p in pools[:2]:
        pool = (p.get("attributes") or {}).get("address") or str(p.get("id", "")).split("_")[-1]
        before = int(time.time()) + 3600
        for _ in range(3):
            c = chart._gt(f"/networks/solana/pools/{pool}/ohlcv/hour",
                          {"aggregate": 1, "limit": 1000, "currency": "usd", "token": solana.WSOL,
                           "before_timestamp": before})
            try:
                rows = c["data"]["attributes"]["ohlcv_list"]
            except (TypeError, KeyError):
                break
            if not rows:
                break
            for r in rows:
                out[int(r[0]) // 3600 * 3600] = float(r[4])
            before = min(int(r[0]) for r in rows)
            if before <= t_from or len(rows) < 1000:
                break
        if out:
            break
    return out


def price_at(prices, ts, fallback):
    h = int(ts) // 3600 * 3600
    for k in (h, h - 3600, h + 3600, h - 7200):
        if k in prices and 20 < prices[k] < 2000:
            return prices[k]
    return fallback


def fetch(s, wallet, t_from, ent, diag, deadline):
    """Add this wallet's swaps since t_from to ent["swaps"] ({signature: [ts, [event, ...]]})."""
    swaps = ent.setdefault("swaps", {})
    seen = ent.setdefault("seen", {})            # every signature already read (swap or not)
    budget = MAX_TX
    before, catching_up = None, bool(seen)
    resume = ent.get("cursor") if not ent.get("done") else None
    while budget > 0:
        if deadline and time.time() > deadline:
            diag["partial"] = "ran out of time this run"
            break
        opts = {"limit": SIG_PAGE, **({"before": before} if before else {})}
        res = solana._rpc(s, [("getSignaturesForAddress", [wallet, opts])], on_demand=True)
        page = (res or [None])[0]
        diag["pages"] += 1
        if page is None:
            diag["error"] = f"Helius didn't answer ({solana.LAST_FAIL[0] or 'unknown error'}) - run it again"
            break
        if not page:
            ent["done"], ent["cursor"] = True, None
            break
        new = [x for x in page if x["signature"] not in seen]
        todo = [x for x in new if not x.get("err") and (x.get("blockTime") or 0) >= t_from]
        whole = True                              # did we get through this whole page?
        for i in range(0, len(todo), 10):
            if budget <= 0 or (deadline and time.time() > deadline):
                whole = False
                break
            chunk = todo[i:i + 10]
            txs = solana._rpc(s, [("getTransaction", [x["signature"], {"encoding": "jsonParsed",
                                                                        "maxSupportedTransactionVersion": 0}])
                                  for x in chunk], on_demand=True)
            if txs is None:
                diag["error"] = "Helius credit budget reached"
                return
            for x, tx in zip(chunk, txs):
                seen[x["signature"]] = 1
                ev = solana.parse_swap(tx, wallet) if tx else []
                if ev:
                    swaps[x["signature"]] = [x.get("blockTime") or 0, [list(e) for e in ev]]
            budget -= len(chunk)
            diag["tx"] = diag.get("tx", 0) + len(chunk)
        if not whole:
            diag["partial"] = ("ran out of time this run" if budget > 0 else
                               "very busy wallet: read the per-run maximum")
            break
        for x in new:
            seen[x["signature"]] = 1
        oldest = page[-1].get("blockTime") or 0
        end = len(page) < SIG_PAGE or oldest < t_from
        if catching_up and (len(new) < len(page) or end):
            catching_up = False                   # met what we already had: jump to the old bookmark
            if ent.get("done") or not resume:
                if end and not resume:
                    ent["done"], ent["cursor"] = True, None
                break
            before = resume
            continue
        if end:
            ent["done"], ent["cursor"] = True, None
            break
        before = page[-1]["signature"]
        if not catching_up:
            ent["cursor"] = before                # bookmark: the next run carries on from here
    if budget <= 0 and not ent.get("done"):
        diag["partial"] = "very busy wallet: read the per-run maximum"


def positions(ent, since, sol_px, now_px, symbols):
    """Group swaps into positions in the same shape the FOMO API uses."""
    by_mint = {}
    for sig, (ts, evs) in sorted(ent.get("swaps", {}).items(), key=lambda kv: kv[1][0]):
        px = price_at(sol_px, ts, sol_px.get("now") or 150)
        for mint, side, amt, sol, usd in evs:
            if mint in solana.QUOTES or amt <= 0:
                continue
            by_mint.setdefault(mint, []).append((ts, side, amt, sol * px + usd))
    out = []
    for mint, evs in by_mint.items():
        cur = None
        for ts, side, amt, usd in evs:
            if side == "buy":
                if cur is None:
                    cur = {"mint": mint, "opened": ts, "bought": 0.0, "cost": 0.0, "sold": 0.0, "got": 0.0,
                           "closed": None}
                cur["bought"] += amt
                cur["cost"] += usd
            elif cur is not None:                 # a sell of something bought before we looked: ignored
                cur["sold"] += min(amt, cur["bought"] - cur["sold"])
                cur["got"] += usd
                if cur["bought"] - cur["sold"] <= cur["bought"] * CLOSED_LEFT:
                    cur["closed"] = ts
                    out.append(cur)
                    cur = None
        if cur is not None:
            out.append(cur)
    res = []
    for c in out:
        if c["opened"] < since or c["bought"] <= 0 or c["cost"] <= 0:
            continue
        frac = c["sold"] / c["bought"]
        entry = c["cost"] / c["bought"]
        exit_px = c["got"] / c["sold"] if c["sold"] else None
        now = now_px.get(c["mint"])
        real = c["got"] - c["cost"] * frac
        unreal = ((c["bought"] - c["sold"]) * now - c["cost"] * (1 - frac)) if (now and not c["closed"]) else 0.0
        res.append({"tradeId": f"{c['mint']}:{c['opened']}",
                    "token": {"symbol": symbols.get(c["mint"]) or f"{c['mint'][:4]}..{c['mint'][-4:]}",
                              "address": c["mint"], "network": "sol"},
                    "status": "closed" if c["closed"] else "open",
                    "avgEntryPrice": entry, "avgExitPrice": exit_px, "priceUsd": now,
                    "costBasisUsd": round(c["cost"], 2), "boughtAmount": c["bought"], "soldAmount": c["sold"],
                    "realizedPnlUsd": round(real, 2), "unrealizedPnlUsd": round(unreal, 2),
                    "createdAt": c["opened"], "closedAt": c["closed"]})
    return res


def history(s, wallet, since, cache, diag, deadline=0):
    """FOMO-shaped positions this wallet opened since `since` (or None if it can't be read)."""
    kind = chains.wallet_kind(wallet)
    if kind != "solana":
        diag["error"] = "backtests only read Solana wallets for now (live paper copying works on Base too)"
        return None
    if not config.HELIUS_API_KEY:
        diag["error"] = "HELIUS_API_KEY secret is needed to read a wallet's history"
        return None
    ent = cache.get("w:" + wallet)
    t_from = since - PAD
    if not ent or ent.get("t_from", 0) > t_from + 3600 or time.time() - ent.get("updated", 0) > 3 * 86400:
        ent = {"t_from": t_from, "swaps": {}, "seen": {}, "cursor": None, "done": False}
    cache["w:" + wallet] = ent
    fetch(s, wallet, t_from, ent, diag, deadline)
    ent["updated"] = time.time()
    mints = sorted({e[0] for _, evs in ent["swaps"].values() for e in evs if e[0] not in solana.QUOTES})
    try:
        m = dex.tokens([chains.key("solana", x) for x in mints]) if mints else {}
    except Exception:
        log.exception("price lookup failed")
        m = {}
    now_px = {chains.split(k)[1]: v["price"] for k, v in m.items() if v.get("price")}
    symbols = {chains.split(k)[1]: v["symbol"] for k, v in m.items() if v.get("symbol")}
    sol_px = sol_prices(t_from)
    no_history = len(sol_px) < 24
    try:
        sol_px["now"] = dex.sol_price()
    except Exception:
        pass
    if no_history:
        diag["partial"] = (diag.get("partial", "") + "; " if diag.get("partial") else "") + \
            "SOL price history unavailable - used today's SOL price"
    ps = positions(ent, since, sol_px, now_px, symbols)
    diag["fetched"] = len(ps)
    diag["complete"] = bool(ent.get("done"))
    times = [v[0] for v in ent["swaps"].values() if v[0]]
    if times:
        diag["back_to"] = min(times)
    if not ent.get("done"):
        diag["partial"] = (diag.get("partial") or "not all of the period fetched yet") + \
            " - progress is saved: run the backtest again to continue further back"
    log.info("wallet %s: %d swaps, %d positions since window start, done=%s",
             wallet, len(ent["swaps"]), len(ps), ent.get("done"))
    return ps
