"""Who on the FOMO leaderboard is actually good, and who just had a lucky week?

The leaderboard ranks by 7-day PnL, which is short enough that a chunk of it is one
lucky trade rather than skill. Copying a one-week wonder is how you buy someone's exit.

So every time the leaderboard is refreshed we write down who was on it. Traders who
keep reappearing week after week get their buys weighted up; traders who appeared for
the first time two days ago get weighted down. Until we have enough days of history to
tell those apart, everyone is treated equally - no guessing.

    observe(s, traders)   record today's leaderboard
    trust(s, handle)      0.7 - 1.5 multiplier on that trader's buys
    label(s, handle)      "proven" / "regular" / "new" / "" (for emails)
"""
import time
from datetime import datetime, timezone

from . import config

PROVEN, REGULAR, NEW = "proven", "regular", "new"


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def observe(s, traders):
    """Record that these traders were on the leaderboard today. Called on every refresh."""
    day = _today()
    days = s.setdefault("lb_days", [])
    if day not in days:
        days.append(day)
        s["lb_days"] = sorted(days)[-config.TRUST_WINDOW_DAYS:]
    rec = s.setdefault("trader_record", {})
    for t in traders:
        h = t.get("handle")
        if not h:
            continue
        r = rec.setdefault(h, {"days": [], "best": 100})
        if day not in r["days"]:
            r["days"].append(day)
            r["days"] = sorted(r["days"])[-config.TRUST_WINDOW_DAYS:]
        r["best"] = min(r["best"], int(t.get("rank") or 100))
    # forget traders we haven't seen inside the window at all
    keep = set(s["lb_days"])
    s["trader_record"] = {h: r for h, r in rec.items() if set(r["days"]) & keep}


def history_days(s):
    """How many distinct days of leaderboard history we actually have."""
    return len(s.get("lb_days") or [])


def _rate(s, handle):
    r = (s.get("trader_record") or {}).get(handle)
    total = history_days(s)
    if not r or not total:
        return None, 0
    seen = len({d for d in r["days"] if d in set(s["lb_days"])})
    return seen / total, seen


def trust(s, handle):
    """Leaderboard reputation x what the paper trades taught about this trader's picks."""
    from .learn import trader_factor
    return round(_rep_trust(s, handle) * trader_factor(s, handle), 3)


def _rep_trust(s, handle):
    """Multiplier for this trader's buys. 1.0 = neutral, and neutral is the safe default.

    Returns 1.0 for everyone until there are TRUST_MIN_DAYS of history, because with
    less than that we cannot tell a regular from a fluke and pretending otherwise
    would just add confident noise to the score.
    """
    if history_days(s) < config.TRUST_MIN_DAYS:
        return 1.0
    rate, seen = _rate(s, handle)
    if rate is None:
        return config.TRUST_UNKNOWN          # not in our records at all
    if rate >= 0.60:
        return config.TRUST_PROVEN
    if rate >= 0.30:
        return config.TRUST_REGULAR
    if seen <= 2:
        return config.TRUST_UNKNOWN          # showed up once or twice, very recently
    return 1.0


def label(s, handle):
    """Short word for the emails, or "" while we're still building history."""
    if history_days(s) < config.TRUST_MIN_DAYS:
        return ""
    t = trust(s, handle)
    return PROVEN if t >= config.TRUST_PROVEN else REGULAR if t >= config.TRUST_REGULAR else NEW


def describe(s, handle):
    """e.g. 'proven: on the leaderboard 22 of the last 30 days' - or '' if too early."""
    lab = label(s, handle)
    if not lab:
        return ""
    _, seen = _rate(s, handle)
    return f"{lab}: on the leaderboard {seen} of the last {history_days(s)} days"


def fn(s):
    """A trust(handle) callable bound to this state, for passing into scoring."""
    return lambda h: trust(s, h)


def summary(s):
    """One line for the email footer."""
    d = history_days(s)
    if d < config.TRUST_MIN_DAYS:
        return (f"Trader reputation: building history ({d}/{config.TRUST_MIN_DAYS} days) - "
                "all leaderboard traders weighted equally for now.")
    rec = s.get("trader_record") or {}
    proven = sum(1 for h in rec if trust(s, h) >= config.TRUST_PROVEN)
    return f"Trader reputation: {proven} proven regulars out of {len(rec)} tracked over {d} days."


def prune(s):
    """Drop stale records (called from state.save)."""
    cutoff = (datetime.fromtimestamp(time.time() - config.TRUST_WINDOW_DAYS * 86400, timezone.utc)
              .strftime("%Y-%m-%d"))
    s["lb_days"] = [d for d in (s.get("lb_days") or []) if d >= cutoff][-config.TRUST_WINDOW_DAYS:]
    rec = {}
    for h, r in (s.get("trader_record") or {}).items():
        days = [d for d in r["days"] if d >= cutoff]
        if days:
            rec[h] = {"days": days, "best": r.get("best", 100)}
    s["trader_record"] = rec
