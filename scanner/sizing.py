"""How much to put in each pick: set by confidence, and aware of fees.

The problem with a small bankroll: FOMO charges at least $0.95 per trade, so
about $1.90 to get in and out. On a $5 position that's 38% gone before the coin
moves. So:
  1. Size from confidence (balanced: 2-5% of bankroll, like a normal risk rule)
  2. Raise it to the smallest size where fees + slippage are <= MAX_COST_PCT
  3. Never more than MAX_SINGLE_PCT in one coin, always keep RESERVE_PCT in cash
  4. If what's left can't cover a fee-efficient position, skip it
As the bankroll grows, step 1 takes over and positions become normal % sizes.
"""
import math

from . import config


def round_trip_cost(size, liquidity):
    fees = 2 * max(config.FEE_MIN_USD, size * config.FEE_PCT / 100)
    slippage = size * (4 * size / liquidity) if liquidity else 0  # constant-product impact, in + out
    return fees + slippage


def fee_floor(liquidity):
    for size in range(5, 100_000, 1):
        if round_trip_cost(size, liquidity) / size * 100 <= config.MAX_COST_PCT:
            return int(math.ceil(size / 5) * 5)
    return 100_000


def plan(picks):
    B = config.BANKROLL_USD
    budget = B * (1 - config.RESERVE_PCT / 100)
    cap = B * config.MAX_SINGLE_PCT / 100
    for p in picks:
        s = p["score"]
        pct, mult = (0.05, 1.6) if s >= 80 else (0.035, 1.2) if s >= 70 else (0.02, 1.0)
        floor = fee_floor(p["market"]["liquidity"])
        size = min(cap, max(pct * B, floor * mult))
        size = int(round(size))
        if size > budget:
            size = int(budget)
        if size < floor:
            p["size"] = 0
            p["size_note"] = (f"Skip unless you swap it in for a pick above - your remaining "
                              f"${budget:.0f} (after a {config.RESERVE_PCT:.0f}% cash reserve) is below the "
                              f"${floor} minimum where fees stay under {config.MAX_COST_PCT:.0f}%.")
            continue
        budget -= size
        cost = round_trip_cost(size, p["market"]["liquidity"])
        price = p["market"]["price"]
        p["size"] = size
        p["cost"] = cost
        p["cost_pct"] = cost / size * 100
        p["target_price"] = price * (1 + config.TARGET_GAIN_PCT / 100)
        p["stop_price"] = price * (1 - config.STOP_LOSS_PCT / 100)
        support = (p.get("chart") or {}).get("support")
        if support and price * 0.6 < support < price * 0.95:
            p["stop_price"] = max(p["stop_price"], support * 0.97)
        stop_pct = (1 - p["stop_price"] / price) * 100
        profit = size * config.TARGET_GAIN_PCT / 100 - cost
        loss = size * stop_pct / 100 + cost
        if size < 60:  # partial sells cost another $0.95 each; keep it simple
            exit_ = (f"Sell all at +{config.TARGET_GAIN_PCT:.0f}% (≈{fmt(p['target_price'])}). "
                     f"Stop-loss at -{stop_pct:.0f}% (≈{fmt(p['stop_price'])}).")
        else:
            exit_ = (f"Sell half at +{config.TARGET_GAIN_PCT:.0f}% (≈{fmt(p['target_price'])}), move stop to entry, "
                     f"let the rest run. Stop-loss at -{stop_pct:.0f}% (≈{fmt(p['stop_price'])}).")
        p["exit"] = exit_ + " Also exit if the top traders who bought start selling."
        p["size_note"] = (f"Fees ≈ ${cost:.2f} round trip ({p['cost_pct']:.1f}%). "
                          f"If it hits target: ≈ +${profit:.0f}. If stopped out: ≈ -${loss:.0f}.")
    return picks


def fmt(price):
    if price >= 1:
        return f"${price:,.4f}"
    digits = max(2, -int(math.floor(math.log10(price))) + 3) if price > 0 else 4
    return f"${price:.{digits}f}"
