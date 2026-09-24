"""Shadow one FOMO trader: paper-trade everything they buy.

A second, separate paper-trading book that ignores the scanner's scoring completely and
just copies one person (COPY_TRADER, default ether_monk). It answers one question:
"would blindly following this trader have made money?"

How it sees their trades without paying for a trade feed: every scan it reads what the
wallet HOLDS now and compares it with the last scan.
  * a coin appears, or grows 5%+          -> they bought  -> open a paper trade
  * a coin drops to under half            -> they sold    -> close it ("their exit")
Solana costs 2 Helius credits per scan; Base and Robinhood are read free from Blockscout.
BNB Chain has no free holdings feed, so buys there are missed (it says so in the email).

Each chain keeps its OWN snapshot and is only compared with itself when that chain was read
successfully this scan. Without that, one failed read (a Blockscout hiccup) wiped a chain from
the snapshot and the next good scan saw every coin on it as a brand-new buy.

Each paper trade is scored two ways, so you can see whose exits are better:
  * their exit  - you get out when they do (or after COPY_MAX_DAYS)
  * your rules  - +50% target, -30% stop, or 48h, the same rules as your own paper trading
Both include FOMO fees and slippage. Nothing here is ever emailed as a buy alert - it is a
measurement, not a signal.
"""
import logging
import time

from . import chains, config, dex, solana
from .http import request
from .tracker import trade_pnl

log = logging.getLogger("copy")

TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]
BLOCKSCOUT = {"base": "https://base.blockscout.com",
              "robinhood": "https://robinhoodchain.blockscout.com"}
GREW, SHRANK = 1.05, 0.5


UNNAMED = ("", "?", "UNKNOWN", "unknown", "Unknown", "None")


def named(sym):
    """DexScreener returns "UNKNOWN"/"?" for tokens it has no metadata for (LP and receipt
    tokens, brand-new mints). Those are not coins we can copy or show a ticker for."""
    return (sym or "").strip() not in UNNAMED


def _book(s):
    b = s.setdefault("copy", {})
    b.setdefault("handle", config.COPY_TRADER)
    b.setdefault("wallets", {})
    b.setdefault("trades", [])
    b.setdefault("started", 0)
    b.setdefault("snaps", {})            # chain -> {token key: amount}, one baseline per chain
    b.setdefault("unnamed", 0)           # their buys we couldn't price/name (not copied)
    if b.pop("snap", None) and not b["snaps"]:
        b["snaps"] = {}                  # old single-chain snapshot: start the baselines again
        b["started"] = 0
    if not b.get("cleaned"):             # once: drop trades the old partial-read bug invented
        b["trades"] = [t for t in b["trades"]
                       if t["mirror"]["done"] or named(t.get("symbol"))]
        b["cleaned"] = True
    return b


QUOTES = {a.lower() for a in chains.QUOTE_ADDRESSES} | set(chains.QUOTE_ADDRESSES)


def _skip(chain, addr):
    """Stablecoins, wrapped natives and other non-meme balances are not trades."""
    return addr in QUOTES or addr.lower() in QUOTES


# ------------------------------------------------------------------ whose wallets
def wallets(s):
    """Their Solana / EVM addresses: from your settings, the leaderboard, or the FOMO API."""
    b = _book(s)
    if config.COPY_SOLANA_WALLET or config.COPY_EVM_WALLET:
        b["wallets"] = {"solana": config.COPY_SOLANA_WALLET or None,
                        "evm": (config.COPY_EVM_WALLET or "").lower() or None}
        return b["wallets"]
    if b["wallets"].get("solana") or b["wallets"].get("evm"):
        return b["wallets"]
    handle = b["handle"].lower().lstrip("@")
    for t in (s.get("leaderboard") or {}).get("traders") or []:       # free if they're top 100
        if (t.get("handle") or "").lower().lstrip("@") == handle:
            b["wallets"] = {"solana": t.get("wallet"), "evm": t.get("evm")}
            log.info("copy: %s wallets from the leaderboard", handle)
            return b["wallets"]
    from .fomo import _get                                            # 2,500 credits, once
    d = _get(s, f"/v2/users/{handle}", 2500)
    w = ((d or {}).get("user") or d or {}).get("wallets") or {}
    if w:
        b["wallets"] = {"solana": w.get("solana"), "evm": (w.get("evm") or "").lower() or None}
        log.info("copy: %s wallets resolved from the FOMO API", handle)
    return b["wallets"]


# ------------------------------------------------------------------ what they hold now
def solana_holdings(s, w):
    res = solana._rpc(s, [("getTokenAccountsByOwner", [w, {"programId": p}, {"encoding": "jsonParsed"}])
                          for p in TOKEN_PROGRAMS])
    if res is None or all(r is None for r in res):
        return None
    out = {}
    for r in res:
        for acc in ((r or {}).get("value") or []):
            try:
                info = acc["account"]["data"]["parsed"]["info"]
                amt = float(info["tokenAmount"]["uiAmount"] or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if amt > 0 and not _skip("solana", info["mint"]):
                out[chains.key("solana", info["mint"])] = amt
    return out


def evm_holdings(chain, w):
    base = BLOCKSCOUT.get(chain)
    if not base or not w:
        return None
    r = request("GET", f"{base}/api/v2/addresses/{w}/token-balances", retries=2)
    if r is None:
        return None
    out = {}
    for row in (r.json() if isinstance(r.json(), list) else []):
        tok = row.get("token") or {}
        if (tok.get("type") or "ERC-20") != "ERC-20":
            continue
        try:
            amt = int(row.get("value") or 0) / 10 ** int(tok.get("decimals") or 18)
        except (TypeError, ValueError):
            continue
        if amt > 0 and tok.get("address") and not _skip(chain, tok["address"]):
            out[chains.key(chain, tok["address"])] = amt
    return out


def holdings(s, w):
    """{chain: {token key: amount}} - a chain maps to None when we could not read it."""
    out = {}
    if w.get("solana") and "solana" in config.CHAINS and config.HELIUS_API_KEY:
        out["solana"] = solana_holdings(s, w["solana"])
    for chain in BLOCKSCOUT:
        if chain in config.CHAINS and w.get("evm"):
            out[chain] = evm_holdings(chain, w["evm"])
    return out


# ------------------------------------------------------------------ the paper book
def tradeable(m):
    """Only copy a buy we can actually name and price - not LP/receipt/unnamed tokens."""
    if not m or m["price"] <= 0:
        return False
    sym = (m.get("symbol") or "").strip()
    if not named(sym) or sym.upper() in chains.QUOTE_SYMBOLS:
        return False
    return m["liquidity"] >= config.MIN_LIQUIDITY_USD / 3


def _open(b, k, m, now):
    if any(t["key"] == k and not t["mirror"]["done"] for t in b["trades"]):
        return None
    price = m["price"]
    t = {"key": k, "symbol": m["symbol"], "chain": m["chain"], "ts": now, "entry": price, "prev": price,
         "size": config.COPY_SIZE_USD, "high": price,
         "mirror": {"done": False}, "rules": {"done": False,
                                              "target": price * (1 + config.TARGET_GAIN_PCT / 100),
                                              "stop": price * (1 - config.STOP_LOSS_PCT / 100)}}
    b["trades"].append(t)
    return t


def _close(part, t, how, price, now):
    part.update(done=True, how=how, exit=price, exit_ts=now,
                pnl=trade_pnl(t["size"], t["entry"], price), hours=round((now - t["ts"]) / 3600, 1))


def scan(s):
    """One pass: spot their buys and sells, then move every open paper trade along."""
    b = _book(s)
    w = wallets(s)
    if not (w.get("solana") or w.get("evm")):
        return []
    now = time.time()
    reads = holdings(s, w)
    bought, sold, read_ok = [], [], []
    for chain, held in reads.items():
        if held is None:
            log.info("copy: couldn't read %s's %s balances this scan - that chain is left alone",
                     b["handle"], chain)
            continue
        read_ok.append(chain)
        prev = b["snaps"].get(chain)
        b["snaps"][chain] = held
        if prev is None:                  # first good read of this chain: baseline only
            log.info("copy: baseline of %d coins %s already holds on %s",
                     len(held), b["handle"], chain)
            continue
        bought += [k for k, a in held.items() if a > (prev.get(k, 0) * GREW if k in prev else 0)]
        sold += [k for k, a in prev.items() if held.get(k, 0) < a * SHRANK]
    if not read_ok:
        log.info("copy: couldn't read any of %s's wallets this scan", b["handle"])
        return []
    if not b["started"]:
        b["started"] = now
        return []

    keys = list({*bought, *sold, *[t["key"] for t in b["trades"] if not t["mirror"]["done"]]})
    markets = dex.tokens(keys) if keys else {}
    opened = []
    for k in bought:
        m = markets.get(k)
        if not tradeable(m):
            b["unnamed"] = b.get("unnamed", 0) + 1
            log.info("copy: %s bought %s but DexScreener can't name/price it - skipped",
                     b["handle"], k)
            continue
        t = _open(b, k, m, now)
        if t:
            opened.append(t)
            log.info("copy: %s bought %s", b["handle"], m["symbol"])

    for t in b["trades"]:
        m = markets.get(t["key"])
        price = m["price"] if m and m["price"] > 0 else None
        if price:
            t["high"] = max(t["high"], price)
        if not t["mirror"]["done"]:
            if t["key"] in sold and price:
                _close(t["mirror"], t, "they sold", price, now)
            elif now - t["ts"] > config.COPY_MAX_DAYS * 86400 and price:
                _close(t["mirror"], t, f"still holding after {config.COPY_MAX_DAYS:.0f}d", price, now)
        r = t["rules"]
        if not r["done"] and price:
            prev_px = t.get("prev") or price     # two scans in a row, like your own paper trades
            if max(price, prev_px) <= r["stop"]:
                _close(r, t, "stop", price, now)
            elif min(price, prev_px) >= r["target"]:
                _close(r, t, "target", price, now)
            elif now - t["ts"] > config.TRACK_WINDOW_HOURS * 3600:
                _close(r, t, "time", price, now)
        if price:
            t["prev"] = price
    cutoff = now - 45 * 86400
    b["trades"] = [t for t in b["trades"] if t["ts"] >= cutoff][-200:]
    return opened


# ------------------------------------------------------------------ results
def _tally(parts):
    done = [p for p in parts if p.get("done")]
    if not done:
        return {"n": 0, "wins": 0, "pnl": 0.0, "avg": None}
    pnl = [p["pnl"] for p in done]
    return {"n": len(done), "wins": sum(1 for p in done if p["pnl"] > 0),
            "pnl": round(sum(pnl), 2), "avg": round(sum(pnl) / len(pnl), 2)}


def label(t):
    """A name for the email: their symbol, or a short address when we never got one."""
    sym = (t.get("symbol") or "").strip()
    if named(sym):
        return sym
    a = chains.split(t["key"])[1]
    return f"{a[:4]}..{a[-4:]}"


def summary(s, days=30):
    b = s.get("copy") or {}
    ts = [t for t in (b.get("trades") or []) if t["ts"] >= time.time() - days * 86400]
    if not b.get("started"):
        return None
    return {"handle": b.get("handle", config.COPY_TRADER), "size": config.COPY_SIZE_USD,
            "since": b["started"], "open": sum(1 for t in ts if not t["mirror"]["done"]),
            "their": _tally([t["mirror"] for t in ts]), "yours": _tally([t["rules"] for t in ts]),
            "recent": sorted([t for t in ts if t["mirror"].get("done")],
                             key=lambda t: -t["mirror"]["exit_ts"])[:5],
            "holding": [label(t) for t in ts if not t["mirror"]["done"]][:8],
            "unnamed": b.get("unnamed", 0),
            "read": sorted(b.get("snaps") or {}),
            "chains": "Solana, Base and Robinhood (BNB buys aren't visible for free)"}
