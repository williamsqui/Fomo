"""Non-X social signals: Telegram community size, website, paid promotion."""
import re

from .http import request


def telegram_members(url_or_handle):
    """Public t.me preview page shows 'N members' / 'N subscribers'. Free, no key."""
    if not url_or_handle:
        return None
    h = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", str(url_or_handle)).strip("/@ ")
    if not h or "+" in h or "joinchat" in h:
        return None
    r = request("GET", f"https://t.me/{h}", retries=1, timeout=10)
    if r is None:
        return None
    m = re.search(r"([\d\s,.]+)\s+(members|subscribers)", r.text)
    if not m:
        return None
    try:
        return int(re.sub(r"[^\d]", "", m.group(1)))
    except ValueError:
        return None
