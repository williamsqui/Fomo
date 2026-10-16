import time
import logging
import requests

log = logging.getLogger("http")
LAST_ERROR = [""]          # body of the most recent failed response (e.g. an RPC's refusal message)
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "fomo-scanner/1.0"


def request(method, url, *, retries=3, timeout=20, **kw):
    """HTTP with backoff on 429/5xx. Returns Response or None on failure."""
    LAST_ERROR[0] = ""
    for attempt in range(retries):
        try:
            r = SESSION.request(method, url, timeout=timeout, **kw)
        except requests.RequestException as e:
            log.warning("%s %s failed: %s", method, url.split("?")[0], e)
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt + 1)
            continue
        if r.status_code == 402:
            LAST_ERROR[0] = f"402 {r.text[:200]}"
            log.error("Out of credits: %s", url.split("?")[0])
            return None
        if not r.ok:
            LAST_ERROR[0] = f"{r.status_code} {r.text[:200]}"
            log.warning("%s %s -> %s %s", method, url.split("?")[0], r.status_code, r.text[:200])
            return None
        return r
    return None
