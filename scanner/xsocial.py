"""X (Twitter) buzz for a coin via GetXAPI (~$0.001 per search call).

We search by contract address OR $TICKER (address = no ticker collisions),
latest tweets only, then score quality - not just volume - because meme-coin
X is full of bots and paid shills.
"""
import logging
import math
import re
from datetime import datetime, timezone

from . import config, state as st
from .http import request

log = logging.getLogger("x")


def _ts(v):
    if not v:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(str(v).replace("Z", "+0000"), fmt).timestamp()
        except ValueError:
            pass
    return None


def _n(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                pass
    return 0.0


def search(s, mint, symbol, leaderboard_handles):
    if not config.GETXAPI_KEY or not st.within_budget(s["x_calls"], config.X_MONTHLY_CALLS, config.X_PAGES_PER_TOKEN):
        return None
    sym = re.sub(r"[^A-Za-z0-9]", "", symbol or "")
    q = f'"{mint}"' + (f" OR ${sym}" if 2 <= len(sym) <= 10 else "")
    tweets, cursor = [], None
    for _ in range(config.X_PAGES_PER_TOKEN):
        params = {"q": q, "product": "Latest"}
        if cursor:
            params["cursor"] = cursor
        r = request("GET", config.GETXAPI_BASE + "/twitter/tweet/advanced_search", params=params,
                    headers={"Authorization": f"Bearer {config.GETXAPI_KEY}"})
        s["x_calls"] += 1
        if r is None:
            break
        d = r.json()
        tweets += d.get("tweets") or []
        cursor = d.get("next_cursor")
        if not d.get("has_more") or not cursor:
            break
    return analyze(tweets, leaderboard_handles)


def analyze(tweets, leaderboard_handles):
    now = datetime.now(timezone.utc).timestamp()
    lb = {h.lower() for h in leaderboard_handles if h}
    recent, authors, texts = [], {}, {}
    kol, lb_hits = set(), set()
    eng = 0.0
    for t in tweets:
        a = t.get("author") or t.get("user") or {}
        handle = (a.get("userName") or a.get("username") or a.get("screen_name") or "").lower()
        followers = _n(a, "followers", "followersCount", "followers_count")
        ts = _ts(t.get("createdAt") or t.get("created_at"))
        if ts and now - ts > 6 * 3600:
            continue
        recent.append(t)
        authors[handle] = max(authors.get(handle, 0), followers)
        eng += _n(t, "likeCount", "favorite_count") + 2 * _n(t, "retweetCount", "retweet_count") \
            + _n(t, "replyCount", "reply_count")
        norm = re.sub(r"\W+", " ", (t.get("text") or "").lower())[:80]
        texts[norm] = texts.get(norm, 0) + 1
        if followers >= 10_000:
            kol.add(handle)
        if handle in lb:
            lb_hits.add(handle)
    n = len(recent)
    dup_ratio = (sum(c for c in texts.values() if c > 1) / n) if n else 0
    tiny = sum(1 for f in authors.values() if f < 100)
    return {
        "tweets_6h": n,
        "unique_authors": len(authors),
        "engagement": round(eng),
        "kol_authors": sorted(kol)[:5],
        "leaderboard_authors": sorted(lb_hits),
        "reach": int(sum(authors.values())),
        "bot_ratio": round(max(dup_ratio, tiny / len(authors) if authors else 0), 2),
        "sample": [(t.get("text") or "")[:160] for t in sorted(
            recent, key=lambda t: -_n(t, "likeCount", "favorite_count"))[:2]],
        "log_reach": math.log10(1 + sum(authors.values())),
    }
