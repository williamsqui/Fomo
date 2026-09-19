# FOMO Scanner

Scans **every 10 minutes**. A coin scoring **80+** is emailed to you **straight away**. Other good coins (62–79) are saved for **8:00 and 18:00 (Vietnam time)**. Each email has **up to 3 coins**, ranked by how likely they are to reach **+50%**, and every coin's price is **re-checked live seconds before sending**. Each pick comes with a suggested dollar amount and an exit plan. It covers **Solana, Base, BNB Chain and Robinhood Chain**.

It's built to judge coins the way you would with your own money. If nothing is good enough, you get nothing. Sitting out is a valid trade.

## What happens every 10 minutes

1. **Top-100 FOMO traders.** It gets the leaderboard (7-day PnL) and each trader's Solana and EVM wallets, then reads what they bought and sold on all 4 chains. Tokens that were airdropped to their wallets are ignored.
2. **Candidates.** These are coins those traders bought in the last 6 hours, plus coins trending on FOMO.
3. **"Is this even investable?" filter:**
   - market cap **$500k – $75M**
   - liquidity **≥ $75k**, and at least 4% of market cap
   - trading for **at least 12 hours**
4. **Deep check of the best 8:**
   - **Safety:** RugCheck on Solana; GoPlus honeypot and tax checks on Base and BNB; verified-contract check on Robinhood Chain. Honeypots, high taxes, mint/freeze authority, hidden owners and whales holding over 45% are **rejected outright**.
   - **Chart (7 days of hourly candles):** trend, higher lows day over day, pullback vs. the 7-day high, volume trend, buy vs. sell volume, rejection wicks, and overextension.
   - **Longer-term health:** how long it has survived, holder count and 24h holder growth, and how widely holders are spread.
   - **Social:** X posts (bots filtered out, big accounts and top traders weighted up), Telegram community size, website, FOMO community posts, and paid-promotion warnings.
5. **Confidence score 0–100.** Severe red flags cut the score. To be sent, a coin must pass every safety check, score **62+**, and show real demand: top traders buying, or credible accounts talking about it while the chart is trending up.
6. **Position size** comes from your bankroll and the confidence score, with FOMO fees built in (see below).
7. **Freshness check before any email.** It re-pulls the live price and drops a coin if it's already up 35%+ since the top traders bought (too late to chase) or if it fell 12%+ while being checked. Instant alerts also need a top-trader buy within the last 2 hours.
8. **Exit alerts.** You get an email if a pick hits +50%, hits its stop-loss, or if the top traders who bought it start selling.
9. **Track record.** Every scored coin is logged. Once there's enough history, each pick shows the real hit rate for its score range, e.g. "41% of past picks scoring 75+ hit +50%".

## How sizing works with a $100 bankroll

FOMO charges 0.5% per trade with a **$0.95 minimum**, so getting in and out costs at least $1.90.

| Confidence | Suggested size at $100 | Fees |
|---|---|---|
| 80+ | $40 | ~4.8% |
| 70–79 | $30 | ~6.3% |
| 62–69 | $25 | ~7.6% |

- 30% of your bankroll always stays in cash.
- No more than 40% goes into one coin.
- A trade is never suggested if fees would cost more than 8% of it. If money runs out, the 3rd pick is shown as an alternate you could swap in.
- As your bankroll grows, sizing moves to a normal 2–5% of bankroll per coin. **Update `BANKROLL_USD` when your balance changes** (Settings → Secrets and variables → Actions → Variables).
- Exit plan: under $60, sell everything at +50%. Above that, sell half and move your stop to your entry price. The stop-loss sits at -30% or just under the recent support level.

## Cost: free tiers

| Service | Used for | Plan |
|---|---|---|
| [fomoapi.io](https://fomoapi.io) (unofficial) | Leaderboard, wallets, trending, FOMO posts | Free, 250k credits |
| [Helius](https://helius.dev) | Solana wallet trades | Free, 1M credits |
| Public RPCs | Base / BNB / Robinhood wallet trades | Free |
| DexScreener, GeckoTerminal | Prices, liquidity, charts, holders | Free, no key |
| RugCheck, GoPlus, Blockscout | Safety checks | Free, no key |
| [GetXAPI](https://getxapi.com) | X posts (optional) | capped at ~$8/month |
| GitHub Actions | Runs it every 10 min | Free on a **public** repo |

Scanning every 10 minutes stays on the free plans because slow-changing data is reused instead of re-fetched every scan:

- **FOMO API:** the leaderboard refreshes every 6h and trending every 2h, about 195k of the 250k free credits a month.
- **Helius:** the top 50 wallets are checked every scan and the rest every 30 minutes, about 300–600k of the 1M free credits.
- **Per coin:** chart, safety and holder data are reused for 30 minutes to 3 hours. A new top-trader buy forces a fresh chart.
- **X:** each coin is searched at most once an hour, with a hard cap of 8,000 searches (~$8) a month.

Free plans never bill you automatically. If a limit is near, the scanner slows down instead.

**Why a public repo?** Scans every 10 minutes need far more than the 2,000 free minutes a private repo gets. Public repos are unlimited. Your keys stay private because they're stored as encrypted Secrets. The run logs are set to hide picks, and the picks only go to your email. The workflow also includes a keepalive step, because GitHub otherwise pauses schedules in repos with no activity for 60 days.

## Setup (about 20 minutes, on a computer)

### 1. Get your keys
- **FOMO API:** sign up at fomoapi.io and copy the API key.
- **Helius:** sign up at helius.dev (free plan) and copy the API key.
- **GetXAPI (optional):** sign up at getxapi.com and add $5 of credit.
- **Gmail app password:** Google Account → Security → turn on 2-Step Verification → App passwords → create one and copy the 16 characters.

### 2. Put the code on GitHub
Create a new **public** repo and upload every file in this folder, including `.github/workflows`.

### 3. Add secrets
Go to Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `FOMO_API_KEY` | fomoapi.io key |
| `HELIUS_API_KEY` | Helius key |
| `GETXAPI_KEY` | GetXAPI key (optional) |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | the 16-character app password |
| `EMAIL_TO` | where the picks should go |

Then open the **Variables** tab and add `BANKROLL_USD` = `100`. The email times already default to 8:00 and 18:00 Vietnam time.

### 4. Start it
Go to **Actions** → enable workflows → **FOMO scanner** → **Run workflow** to check it runs without errors (the Actions tab shows a green tick). After that it scans every 10 minutes. You'll hear from it as soon as an 80+ coin appears, and otherwise at the next 8:00 or 18:00.

## Emails you'll get
- **"HIGH CONFIDENCE NOW: …" (any time):** a coin just scored 80+, top traders bought it in the last 2 hours, and the live price was re-checked seconds before sending. The same coin won't be alerted again for 12 hours.
- **"FOMO digest …" at 8:00 and 18:00 (Vietnam time):** the 1–3 best coins scoring 62+ that still qualify at that moment. If none do, you get a short "no picks" note listing the closest calls, so you know it's running.
- **"EXIT ALERT …" (any time):** only for coins you were sent, when one hits its +50% target or its stop-loss, or when the top traders who bought it start selling. Set `EXIT_ALERTS` = `0` to turn these off.

**How quickly will I hear?** Usually 10–20 minutes after the top traders start buying. GitHub sometimes runs its 10-minute schedule a few minutes late when it's busy. For alerts within 2–3 minutes, run `python -m scanner.main --loop 180` on an always-on server (e.g. a $5/month VPS). That speed also needs the Helius Developer plan.

## Settings (repo Variables, no code changes)

| Variable | Default | What it does |
|---|---|---|
| `BANKROLL_USD` | 100 | Your trading pot |
| `DIGEST_TIMES` | 08:00,18:00 | When to email picks (24h clock, comma-separated) |
| `TIMEZONE` | Asia/Ho_Chi_Minh | Your time zone |
| `EXIT_ALERTS` | 1 | 0 = no exit-alert emails |
| `INSTANT_SCORE` | 80 | Score that triggers an immediate email (e.g. 90 for fewer, stronger alerts) |
| `MAX_RUNUP_PCT` | 35 | Don't send a coin already up this much since the top traders bought |
| `X_MONTHLY_CALLS` | 8000 | Monthly X search cap (~$1 per 1,000) |
| `MIN_SEND_SCORE` | 62 | Minimum score for a pick to be sent. Raise it for fewer, stronger picks |
| `MIN_MCAP_USD` | 500000 | Market cap floor |
| `CHAINS` | solana,base,bsc,robinhood | Which chains to scan |

When you upgrade to the "hybrid" setup (FOMO API Starter + Helius Developer), set:

- `FOMO_MONTHLY_CREDITS=2500000`
- `HELIUS_MONTHLY_CREDITS=10000000`
- `LEADERBOARD_REFRESH_HOURS=1`
- `TRENDING_REFRESH_HOURS=1`
- `THESIS_TOKENS_PER_DAY=40`
- `X_MONTHLY_CALLS=20000`

Scoring weights are in `scanner/scoring.py`, chart rules in `scanner/chart.py`, and sizing rules in `scanner/sizing.py`. They're all commented, and you can ask Claude Code to tune them once your track record builds up.

## Test without keys
```
pip install -r requirements.txt
python tests/demo.py         # fake data for all 4 chains -> out/instant.html + out/latest.html
python tests/test_exits.py   # checks that exit alerts fire
python tests/test_freshness.py  # checks stale/pumped/dumping coins are dropped before sending
```

## Honest limits
- This is a signal scanner, **not financial advice**. Most meme coins go to zero. A 75+ score means the odds look better, not that it will go up.
- fomoapi.io is **unofficial**. If FOMO changes things, parts may stop working until updated.
- Scans run every 10 minutes (sometimes later on GitHub), so a coin can still move before you act. Exit alerts can be 10–20 minutes late. Set a stop-loss in the FOMO app if it supports one.
- Robinhood Chain has fewer safety tools. Those coins need a verified contract and a higher score to be sent.
- Public RPCs can be slow or rate-limited. If a chain keeps getting skipped, add a free Alchemy/Ankr URL as `BASE_RPC_URL` / `BSC_RPC_URL` / `ROBINHOOD_RPC_URL`.
