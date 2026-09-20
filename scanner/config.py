"""All settings come from environment variables (GitHub Secrets / Variables).

Defaults are tuned for: free API tiers, a ~$100 bankroll, balanced risk,
and "investor mindset" safety filters. Override any of them without code changes.
"""
import os


def _int(name, default):
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _list(name, default):
    return [x.strip() for x in (os.getenv(name) or default).split(",") if x.strip()]


# ---- API keys -------------------------------------------------------------
FOMO_API_KEY = os.getenv("FOMO_API_KEY", "")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
GETXAPI_KEY = os.getenv("GETXAPI_KEY", "")          # optional
FOMO_BASE = os.getenv("FOMO_BASE", "https://api.fomoapi.io")
GETXAPI_BASE = os.getenv("GETXAPI_BASE", "https://api.getxapi.com")
DEX_BASE = "https://api.dexscreener.com"
GT_BASE = "https://api.geckoterminal.com/api/v2"

# ---- Chains ----------------------------------------------------------------
CHAINS = _list("CHAINS", "solana,base,bsc,robinhood")
# Free public RPCs. If one gets rate-limited, drop in a free Alchemy/Ankr/QuickNode URL.
EVM_RPC = {
    "base": os.getenv("BASE_RPC_URL") or "https://base-rpc.publicnode.com",
    "bsc": os.getenv("BSC_RPC_URL") or "https://bsc-rpc.publicnode.com",
    "robinhood": os.getenv("ROBINHOOD_RPC_URL") or "https://rpc.mainnet.chain.robinhood.com",
}
EVM_LOG_CHUNK = _int("EVM_LOG_CHUNK", 1000)       # blocks per eth_getLogs call
EVM_MAX_CHUNKS = _int("EVM_MAX_CHUNKS", 30)       # safety cap per chain per run

# ---- Email ----------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").replace(" ", "")  # Gmail shows app passwords with spaces
EMAIL_TO = os.getenv("EMAIL_TO") or SMTP_USER
# Scans run every 10 min. Coins scoring INSTANT_SCORE+ are emailed right away;
# other qualifying coins wait for the digest times (local 24h clock).
INSTANT_SCORE = _int("INSTANT_SCORE", 80)
INSTANT_SIGNAL_MAX_AGE_MIN = _int("INSTANT_SIGNAL_MAX_AGE_MIN", 120)  # newest top-trader buy must be this fresh
MAX_RUNUP_PCT = _float("MAX_RUNUP_PCT", 35)    # skip if already up this much since top traders bought
MAX_DROP_SINCE_SCAN_PCT = _float("MAX_DROP_SINCE_SCAN_PCT", 12)  # skip if dumping while we checked it
REALERT_HOURS = _int("REALERT_HOURS", 12)      # don't instant-alert the same coin again within this
DIGEST_TIMES = _list("DIGEST_TIMES", "08:00,18:00")
TIMEZONE = os.getenv("TIMEZONE") or "Asia/Ho_Chi_Minh"   # Vietnam time (UTC+7)
EXIT_ALERTS = (os.getenv("EXIT_ALERTS") or "1") not in ("0", "false", "no")

# ---- Picks ---------------------------------------------------------------
MAX_PICKS = _int("MAX_PICKS", 3)
MIN_SEND_SCORE = _int("MIN_SEND_SCORE", 62)   # below this = not worth your money
FINALISTS = _int("FINALISTS", 12)             # coins whose chart/safety/holders are refreshed each scan

# ---- Leaderboard -------------------------------------------------------------
LEADERBOARD_WINDOW = os.getenv("LEADERBOARD_WINDOW", "7d")
LEADERBOARD_SIZE = _int("LEADERBOARD_SIZE", 100)
LOOKBACK_HOURS = _int("LOOKBACK_HOURS", 6)

# ---- Safety filters ("would I put my own money in this?") -------------------
MIN_MCAP_USD = _float("MIN_MCAP_USD", 500_000)
MAX_MCAP_USD = _float("MAX_MCAP_USD", 75_000_000)   # above this, +50% is much rarer
MIN_LIQUIDITY_USD = _float("MIN_LIQUIDITY_USD", 75_000)
MIN_LIQ_TO_MCAP = _float("MIN_LIQ_TO_MCAP", 0.04)
MIN_PAIR_AGE_HOURS = _float("MIN_PAIR_AGE_HOURS", 12)
MAX_TOP10_HOLDERS_PCT = _float("MAX_TOP10_HOLDERS_PCT", 45)
MAX_TAX_PCT = _float("MAX_TAX_PCT", 5)

# ---- Money ----------------------------------------------------------------
BANKROLL_USD = _float("BANKROLL_USD", 100)
RESERVE_PCT = _float("RESERVE_PCT", 30)        # always keep this % in cash
MAX_SINGLE_PCT = _float("MAX_SINGLE_PCT", 40)  # never more than this % in one coin
FEE_MIN_USD = _float("FEE_MIN_USD", 0.95)      # FOMO minimum fee per trade
FEE_PCT = _float("FEE_PCT", 0.5)               # FOMO fee % per trade
MAX_COST_PCT = _float("MAX_COST_PCT", 8)       # skip trades where fees+slippage > this %
STOP_LOSS_PCT = _float("STOP_LOSS_PCT", 30)

# ---- Free-tier budgets -------------------------------------------------------
FOMO_MONTHLY_CREDITS = _int("FOMO_MONTHLY_CREDITS", 250_000)
LEADERBOARD_REFRESH_HOURS = _int("LEADERBOARD_REFRESH_HOURS", 6)
TRENDING_REFRESH_HOURS = _int("TRENDING_REFRESH_HOURS", 2)
THESIS_TOKENS_PER_DAY = _int("THESIS_TOKENS_PER_DAY", 2)
HELIUS_MONTHLY_CREDITS = _int("HELIUS_MONTHLY_CREDITS", 1_000_000)
MAX_TX_PER_WALLET_PER_RUN = _int("MAX_TX_PER_WALLET_PER_RUN", 10)
X_TOKENS_PER_RUN = _int("X_TOKENS_PER_RUN", 4)
X_PAGES_PER_TOKEN = _int("X_PAGES_PER_TOKEN", 1)
GT_SLEEP_SEC = _float("GT_SLEEP_SEC", 6.5)     # GeckoTerminal keyless ~10 calls/min
X_MONTHLY_CALLS = _int("X_MONTHLY_CALLS", 8000)  # GetXAPI cap (~$8/month)
FAST_WALLETS = _int("FAST_WALLETS", 50)        # top N wallets checked every run...
SLOW_WALLET_EVERY = _int("SLOW_WALLET_EVERY", 3)  # ...ranks below N every 3rd run (Helius free tier)
# per-coin cache (minutes) so 10-minute scans stay inside free limits
CHART_TTL_MIN = _int("CHART_TTL_MIN", 30)
INFO_TTL_MIN = _int("INFO_TTL_MIN", 180)
SAFETY_TTL_MIN = _int("SAFETY_TTL_MIN", 180)
X_TTL_MIN = _int("X_TTL_MIN", 60)

# ---- Track record ---------------------------------------------------------
TARGET_GAIN_PCT = _float("TARGET_GAIN_PCT", 50)
TRACK_SCORE_MIN = _int("TRACK_SCORE_MIN", 50)
TRACK_WINDOW_HOURS = _int("TRACK_WINDOW_HOURS", 48)

STATE_DIR = os.getenv("STATE_DIR", "state")
