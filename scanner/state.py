"""Tiny JSON state store, persisted between scans (GitHub Actions cache)."""
import json
import os
import time
from datetime import datetime, timezone

from . import config

DEFAULT = {
    "month": "",
    "fomo_credits_used": 0,
    "helius_credits_used": 0,
    "x_calls": 0,
    "leaderboard": {"ts": 0, "traders": []},
    "trending": {"ts": 0, "tokens": []},
    "wallet_cursor": {},      # solana wallet -> last seen signature
    "evm_cursor": {},         # chain -> last scanned block
    "decimals": {},           # token key -> decimals
    "alerts": {},             # token key -> last emailed ts
    "holders_hist": {},       # token key -> [[ts, holders], ...]
    "digests_sent": [],       # digest slot ids already emailed
    "instant_sent": {},       # token key -> ts of last instant alert
    "deep_cache": {},         # token key -> {name: [ts, value]}
    "run_count": 0,
    "trader_buys": [],        # recent buy/sell events by leaderboard traders
    "thesis_cache": {},       # mint -> {"ts":..., "items": [...]}
    "thesis_day": {"day": "", "count": 0},
    "picks": [],              # track record
}


def _path():
    return os.path.join(config.STATE_DIR, "state.json")


def load():
    try:
        with open(_path()) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    for k, v in DEFAULT.items():
        s.setdefault(k, json.loads(json.dumps(v)))
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if s["month"] != month:  # monthly budget reset
        s.update(month=month, fomo_credits_used=0, helius_credits_used=0, x_calls=0)
    return s


def save(s):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    cutoff = time.time() - 48 * 3600
    s["trader_buys"] = [e for e in s["trader_buys"] if e["ts"] >= cutoff]
    s["picks"] = [p for p in s["picks"] if p["ts"] >= time.time() - 45 * 86400]
    s["thesis_cache"] = {k: v for k, v in s["thesis_cache"].items() if v["ts"] >= cutoff}
    s["alerts"] = {k: v for k, v in s["alerts"].items() if v >= cutoff}
    s["digests_sent"] = s["digests_sent"][-10:]
    s["instant_sent"] = {k: v for k, v in s["instant_sent"].items() if v >= cutoff}
    day = time.time() - 86400
    s["deep_cache"] = {k: {n: e for n, e in v.items() if e[0] >= day} for k, v in s["deep_cache"].items()}
    s["deep_cache"] = {k: v for k, v in s["deep_cache"].items() if v}
    week = time.time() - 7 * 86400
    s["holders_hist"] = {k: [x for x in v if x[0] >= week][-60:] for k, v in s["holders_hist"].items()
                         if v and v[-1][0] >= week}
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, _path())


def month_fraction_elapsed():
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return (now - start) / (nxt - start)


def within_budget(used, monthly, cost, headroom=1.15):
    """Pace spending evenly over the month (small headroom for bursts)."""
    allowed = monthly * min(1.0, month_fraction_elapsed() * headroom + 0.02)
    return used + cost <= allowed
