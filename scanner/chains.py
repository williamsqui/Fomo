"""Chain names differ per service - map them all to one internal id."""

# internal id -> ids used by each service
CHAIN = {
    "solana":    {"dex": "solana",    "gt": "solana",    "goplus": None,   "fomo": "sol",  "explorer": "https://solscan.io/token/"},
    "base":      {"dex": "base",      "gt": "base",      "goplus": "8453", "fomo": "base", "explorer": "https://basescan.org/token/"},
    "bsc":       {"dex": "bsc",       "gt": "bsc",       "goplus": "56",   "fomo": "bnb",  "explorer": "https://bscscan.com/token/"},
    "robinhood": {"dex": "robinhood", "gt": "robinhood", "goplus": "4663", "fomo": "robinhood",
                  "explorer": "https://robinhoodchain.blockscout.com/token/"},
}
LABEL = {"solana": "Solana", "base": "Base", "bsc": "BNB", "robinhood": "Robinhood"}

# FOMO API "network" strings -> internal id
FOMO_NET = {"sol": "solana", "solana": "solana", "base": "base", "8453": "base",
            "bnb": "bsc", "bsc": "bsc", "binance": "bsc", "bnb chain": "bsc", "56": "bsc",
            "robinhood": "robinhood", "rh": "robinhood", "hood": "robinhood", "robinhood chain": "robinhood",
            "4663": "robinhood"}

# Never treat these as meme coins (quote assets, stables, wrapped natives)
QUOTE_SYMBOLS = {"SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "USDC", "USDT", "USDG", "DAI",
                 "BUSD", "FDUSD", "USD1", "USDE", "PYUSD", "CBBTC", "WBTC", "BTCB", "USDC.E"}
QUOTE_ADDRESSES = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "0x4200000000000000000000000000000000000006",   # WETH (Base)
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",   # USDC (Base)
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",   # WBNB
    "0x55d398326f99059ff775485246999027b3197955",   # USDT (BSC)
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",   # USDC (BSC)
    "0xe9e7cea3dedca5984780bafc599bd69add087d56",   # BUSD
}


def key(chain, addr):
    return f"{chain}:{norm(chain, addr)}"


def norm(chain, addr):
    return addr.lower() if chain != "solana" else addr


def split(k):
    c, a = k.split(":", 1)
    return c, a
