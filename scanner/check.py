"""Check one coin on demand and email the full rating.

    python -m scanner.check <address> [solana|base|bsc|robinhood|auto]

Runs the same checks as the scanner (safety, chart, holders, X, Telegram, FOMO posts)
plus a live look at which top-100 FOMO traders hold the coin right now and how much.
"""
import logging
import os
import sys
import time

from . import (chains, chart, config, dex, evm, fomo, report, safety, scoring, sizing, socials,
               solana, state as st, xsocial)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("check")


def find_market(address, chain):
    """Locate the coin on DexScreener; for 0x addresses try every EVM chain and keep the deepest pool."""
    address = address.strip()
    if chain in chains.CHAIN:
        opts = [chain]
    elif address.lower().startswith("0x"):
        opts = [c for c in ("base", "bsc", "robinhood") if c in chains.CHAIN]
    else:
        opts = ["solana"]
    found = dex.tokens([chains.key(c, address) for c in opts])
    return max(found.values(), key=lambda m: m["liquidity"]) if found else None


def solana_holders(s, mint, traders):
    """{wallet: token amount} for leaderboard wallets holding this mint (Helius, 1 batch)."""
    ws = [t["wallet"] for t in traders if t.get("wallet")]
    if not ws or not config.HELIUS_API_KEY:
        return {}
    out = {}
    for i in range(0, len(ws), 50):
        chunk = ws[i:i + 50]
        res = solana._rpc(s, [("getTokenAccountsByOwner", [w, {"mint": mint}, {"encoding": "jsonParsed"}])
                              for w in chunk]) or []
        for w, r in zip(chunk, res):
            amt = 0.0
            for acc in ((r or {}).get("value") or []):
                try:
                    amt += float(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                except (KeyError, TypeError, ValueError):
                    pass
            if amt > 0:
                out[w] = amt
    return out


def filter_notes(m):
    notes = []
    age_h = (time.time() * 1000 - m["created_ms"]) / 3.6e6 if m["created_ms"] else None
    if m["mcap"] < config.MIN_MCAP_USD:
        notes.append(f"market cap ${m['mcap']:,.0f} is below your ${config.MIN_MCAP_USD:,.0f} minimum")
    if m["mcap"] > config.MAX_MCAP_USD:
        notes.append(f"market cap ${m['mcap']:,.0f} is above ${config.MAX_MCAP_USD:,.0f} (+50% much less likely)")
    if m["liquidity"] < config.MIN_LIQUIDITY_USD:
        notes.append(f"liquidity ${m['liquidity']:,.0f} is below ${config.MIN_LIQUIDITY_USD:,.0f}")
    if m["mcap"] and m["liquidity"] / m["mcap"] < config.MIN_LIQ_TO_MCAP:
        notes.append("liquidity is thin compared with market cap")
    if age_h is not None and age_h < config.MIN_PAIR_AGE_HOURS:
        notes.append(f"only {age_h:.1f}h old (minimum {config.MIN_PAIR_AGE_HOURS:.0f}h)")
    return notes


def run(address, chain="auto"):
    s = st.load()
    m = find_market(address, chain)
    if not m:
        subject = f"Coin check: {address[:10]}... not found"
        body = (f"<p>Couldn't find <code>{address}</code> on DexScreener "
                f"({'any EVM chain' if address.startswith('0x') else 'Solana'}). Check the address and chain.</p>")
        report.send(subject, body)
        print(subject)
        return
    k = m["key"]
    log.info("checking %s (%s) on %s", m["symbol"], m["address"], m["chain"])

    # top-100 FOMO traders holding it right now
    traders = fomo.leaderboard(s)
    by_wallet = {t["wallet"]: t for t in traders if t.get("wallet")}
    by_wallet.update({t["evm"]: t for t in traders if t.get("evm")})
    if m["chain"] == "solana":
        held = solana_holders(s, m["address"], traders)
    else:
        raw = evm.balances(m["chain"], m["address"], [t["evm"] for t in traders if t.get("evm")])
        dec = evm._decimals(s, m["chain"], m["address"])
        held = {w: v / 10 ** dec for w, v in raw.items()}
    holders = sorted(({"handle": by_wallet[w]["handle"], "rank": by_wallet[w]["rank"],
                       "usd": amt * m["price"]} for w, amt in held.items() if w in by_wallet),
                     key=lambda h: h["rank"])
    holders = [h for h in holders if h["usd"] >= 20]

    # score: recent logged trades from the scanner + current holdings
    now = time.time()
    events = [e for e in s["trader_buys"] if e["key"] == k]
    logged = {e["handle"] for e in events if e["side"] == "buy"}
    for h in holders:
        if h["handle"] not in logged:
            events.append({"ts": now, "handle": h["handle"], "rank": h["rank"],
                           "key": k, "side": "buy", "amount": 1, "usd": round(h["usd"], 2), "held": True})
    sm = scoring.smart_money(events, k)
    traded = [e["ts"] for e in events if e["side"] == "buy" and not e.get("held") and e["handle"] in sm["buyers"]]
    sm["first_buy"], sm["last_buy"] = (min(traded), max(traded)) if traded else (None, None)

    sf = safety.check(m["chain"], m["address"])
    ch = chart.analyze(chart.candles(m["chain"], m["pair"])) if m["pair"] else None
    info = chart.token_info(m["chain"], m["address"]) or {}
    x = xsocial.search(s, m["address"], m["symbol"], [t["handle"] for t in traders])
    tg = socials.telegram_members(m.get("telegram") or info.get("telegram"))
    th = fomo.thesis(s, k)
    trending = {t["key"]: t["rank"] for t in (s.get("trending") or {}).get("tokens", []) if "key" in t}
    r = scoring.score(m, sm, x=x, thesis=th, trending_rank=trending.get(k), chart=ch, safety=sf,
                      info=info, tg=tg, deep=True)
    notes = filter_notes(m)
    if notes:
        r["why_not"] = notes + r["why_not"]
        r["qualified"] = False
    r["checked_at"] = time.time()
    sizing.plan([r])
    if not r["qualified"]:
        floor = sizing.fee_floor(m["liquidity"])
        r["size"] = 0
        r["size_note"] = (f"Not recommended by your scanner's rules. If you trade it anyway, treat it as a gamble: "
                          f"${floor} max (the smallest size where fees stay under {config.MAX_COST_PCT:.0f}%) "
                          f"and a stop-loss around -{config.STOP_LOSS_PCT:.0f}%.")

    subject, body = report.build_check(r, holders, s)
    report.send(subject, body)
    os.makedirs("out", exist_ok=True)
    with open("out/check.html", "w") as f:
        f.write(body)
    print(subject)


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        sys.exit("usage: python -m scanner.check <address> [chain]")
    run(sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "auto").strip().lower())
