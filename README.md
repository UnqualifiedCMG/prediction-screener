# Prediction-Market Screener

A real, working screener for **Polymarket** and **Kalshi** — public APIs only,
no auth, no credentials. Built for spotting what the money is saying (and
where it's crowded), feeding @BetweenTheBids-style direction + reasoning.

## Quick start

```bash
cd ~/workspace/prediction-screener
python3 screener.py --screen volume --limit 20
```

Open the dashboard in a browser after any `--html` run:

```bash
python3 screener.py --screen all --html
# then open dashboard.html
```

## Screens

| Screen     | What it shows | Why it matters |
|------------|---------------|----------------|
| `volume`   | Highest-volume open markets | Liquidity = tradable, and volume is attention |
| `closing`  | Expiring within 7 days, by volume | Resolution edge; mispriced tails get found here |
| `movers`   | Biggest 24h price moves (needs real volume) | Something just changed — find out what |
| `extremes` | Priced ≥90% or ≤10% with high 24h volume | Crowded consensus trades; great content fuel ("everyone thinks X is a lock — is it?") |
| `new`      | Listed in the last 48h | Fresh narratives before the crowd prices them |
| `search`   | Keyword filter (`--query "Fed"`) | Track one story across both books |

## Options

```
--screen {volume,closing,movers,extremes,new,search,all}
--query TEXT        keyword for the search screen
--limit N           rows per screen (default: 20)
--min-volume $      floor on total volume (default: 1000)
--min-volume24h $   floor on 24h volume, for movers/extremes (default: 1000)
--source {poly,kalshi,both}   (default: both)
--json              machine-readable output (cron-friendly)
--html              also write dashboard.html (sortable tables, all screens)
--max-events N      Polymarket fetch cap (default: 2000)
--max-markets N     Kalshi fetch cap (default: 1000)
```

Cron-friendly example (JSON to a file his other tooling can read):

```bash
python3 screener.py --screen extremes --json > /tmp/extremes.json
```

## Reading the output

- **YES** — Yes price as a probability. `P` = Polymarket, `K` = Kalshi.
- **24H MOVE** — percentage-point move in the last 24h (Polymarket only;
  Kalshi's API doesn't expose price history).
- **VOL 24H / VOL TOT** — event-level volume on Polymarket, market-level on Kalshi.
- **CLOSES** — time until the market's end date.

## Known API limitations (verified Oct 18, 2026)

1. **Kalshi's markets endpoint is flooded with provisional multivariate
   sports-combo scaffolds.** The screener filters them two ways: it only
   requests markets closing 14+ days out, and drops provisional/MVE-leg
   markets. Consequence: **near-term Kalshi markets (sports props, weekly
   contracts) don't appear** — but those were never the point of this tool.
2. **No Kalshi price history** — the movers screen is Polymarket-only.
3. **Kalshi prices are bid/ask midpoints**, not last-trade prices.
4. **Polymarket `volume24h` is event-level**, shared across that event's
   markets; per-market 24h volume isn't exposed.
5. Stale records (close date already passed but still listed as open) are
   filtered out.

## Backtesting (`backtest.py`)

Answers "what % of markets that reach 90¢ actually resolve YES?" with real
resolved-market data — Polymarket only (Kalshi's API exposes no price
history, so "reached 90¢ at some point" is uncomputable there).

```bash
python3 backtest.py                      # 300 markets, $5k min volume, cached
python3 backtest.py --markets 500 --min-volume 10000
python3 backtest.py --refresh            # ignore cache, re-fetch everything
python3 backtest.py --html               # also write backtest.html
python3 backtest.py --json               # machine-readable output
```

What it does: pulls resolved binary Yes/No markets (gamma API,
`closed=true`, ordered by close time), reads each one's final outcomePrices
for the actual resolution, then walks its daily price history (CLOB
`prices-history` on the Yes token) to find max/min price, final-week price,
and time from first 90¢/10¢ touch to resolution. Everything is cached to
`hidden_cache/` so reruns are instant.

It reports: conditional resolution rates by price bucket (reached ≥90/80/70…%
→ % resolved YES, and the ≤10/20/30…% mirror), final-week price bands, the
"collapse split" (reached 90¢ but fell back below 80¢ vs held it), and median
time from first touch to resolution. N is shown on every bucket.

Headline finding from the first run (255 markets, Oct 18 2026): markets that
reached ≥90¢ resolved YES 96.8% of the time (n=93) — **but** the median first
touch of 90¢ was only 2.4 hours before resolution, i.e. after the outcome was
effectively decided. It's hindsight, not a tradeable edge. The honest read
and full caveats (daily sampling misses intraday spikes, selection bias,
category base rates) are printed with every run.

## The site: PolyScope (`site/`)

A static tracking site generated from live data — dashboard, markets explorer,
backtest stats, methodology. Rebuild it any time; every page is stamped with
its data timestamp.

```bash
python3 site/build.py               # build; refetches open markets if snapshot cache older than 30 min
python3 site/build.py --refresh     # force a fresh pull
python3 site/build.py --out /tmp/x  # build to a different directory
```

Preview locally:

```bash
cd ~/workspace/prediction-screener/site && python3 -m http.server 8000
# then open http://localhost:8000
```

Pages:

| Page | What it is |
|------|------------|
| `index.html` | Dashboard: top volume, biggest 24h movers, closing soon, extremes watch, fresh listings |
| `markets.html` | Searchable, sortable explorer (top 2,000 open markets by volume) |
| `stats.html` | Backtest: conditional resolution rates with N per bucket and the full caveats |
| `about.html` | Methodology, data sources, limitations, "for research, not financial advice" |

How it works: `site/build.py` pulls open markets (cached to
`hidden_cache/open_snapshot.json`, refetched only if stale), reuses
`screener.py`'s screens and `backtest.py`'s stats on the cached resolved-market
data, and writes vanilla HTML + one CSS file — no framework, no build step.
Kalshi gaps are labeled on-site (no price history → no movers; near-term
markets filtered as provisional spam).

Cron example (regenerate hourly):

```bash
0 * * * * cd ~/workspace/prediction-screener && python3 site/build.py >/tmp/polyscope.log 2>&1
```

Deployment note: the generated `site/` directory is fully static — it can be
served as-is from any static host (e.g. GitHub Pages). Colin's repo
`prediction-screener` on the `UnqualifiedCMG` GitHub account publishes it via
GitHub Pages.

## Files

- `screener.py` — the live screener. stdlib + `requests` only.
- `backtest.py` — the backtest module. stdlib + `requests` only.
- `dashboard.html` — generated by screener's `--html`; open it directly.
- `backtest.html` — generated by backtest's `--html`; the stat tables.
- `hidden_cache/` — raw API responses (closed events + per-token histories).
  Delete or use `--refresh` to rebuild.
- `README.md` — this file.

## Politeness

~0.35s pause between paged requests, 2 attempts per request, hard caps on
pages fetched. Read-only — it never places orders or touches accounts.

## Sports: prediction markets vs sportsbooks

`sports_compare.py` — the flagship sports feature. Compares Polymarket and
Kalshi moneyline prices against vig-free sportsbook consensus for the same
games and flags divergences. Informational only — not betting advice.

```bash
# free Odds API key in env (get one at https://the-odds-api.com — free tier)
export ODDS_API_KEY=your_key_here
python3 sports_compare.py --leagues nfl,nba --threshold 0.03 --html
python3 sports_compare.py --leagues nfl --json > sports.json
python3 sports_compare.py --fixture-test   # no-network logic test
```

### Setup: The Odds API key

1. Sign up at https://the-odds-api.com (free tier: 500 credits/month —
   roughly 16 requests/day; free tier covers NFL/NBA/MLB h2h markets).
2. `export ODDS_API_KEY=<key>` locally, or add it as the `ODDS_API_KEY`
   Actions secret on the repo (already done on
   `UnqualifiedCMG/prediction-screener`).
3. The script reads the key from the environment only — it is never
   hardcoded or written to any file.

Without a key, everything else still runs (Polymarket, Kalshi, ESPN,
weather) — only the sportsbook consensus column is skipped.

### Free data sources integrated

| Source | What it provides | Key? |
|--------|------------------|------|
| Polymarket gamma API | Moneyline prices per game (public-search) | No |
| Kalshi trade API | Game markets (`KXNFLGAME` etc.), mids from orderbook | No |
| The Odds API | US sportsbook h2h moneylines | Free key |
| ESPN unofficial API | Schedules, records, venues, injuries | No |
| Open-Meteo | Weather at kickoff for outdoor NFL games | No |

Odds API responses are cached 12h (≤2 refreshes/day ≈ 90 credits/month).
Quota headers are read on every call and a warning prints under 50 remaining.

### Matching methodology

- **Vig removal (basic normalization):** each book's American odds →
  implied probability per side, then each side divided by the overround
  (`p_home = raw_home / (raw_home + raw_away)`). Consensus = mean of the
  de-vigged home-win probability across books.
- **Team normalization:** one canonical table per league maps ESPN
  abbreviations ↔ Odds API full names ↔ Polymarket short names ↔ Kalshi
  ticker codes.
- **Conservative matching:** a game is compared only when both teams
  resolve through the table on every source. Ambiguous games are listed
  as *unmatched* — never guessed.
- **Polymarket lookup:** per-game events found via public-search
  (`nfl-dal-phi-2026-10-27` style tickers); the moneyline is the market
  whose question equals the event title with both team outcomes.

### Scheduled refresh

`.github/workflows/sports-refresh.yml` runs daily at 12:00 UTC: rebuilds
`site/sports.html`, commits it (plus a dated JSON snapshot under
`site/data/`) back to main, and GitHub Pages redeploys. ~3 API credits/day.

### Phase-two options (not free — parked)

- **Pinnacle / Betfair:** sharper books, but need funded accounts.
- **Unabated:** pro-grade no-vig lines, paid.
- **balldontlie.io:** NBA stats/history — now requires a (free) API key;
  not integrated, would slot in as a second NBA context source.
- **TheSportsDB:** free tier exists; ESPN already covers the need, kept
  as a fallback if ESPN's unofficial API breaks.

## Data page: everything by source

`data_page.py` builds `site/data.html` — the "everything in one place"
reference page. One tab per source, each with a one-line description, its
limitations, a "data as of" timestamp, and the raw-ish tables:

| Tab | Contents | Key needed? |
|-----|----------|-------------|
| ESPN | NFL/NBA schedules, records, venues, injury flags | No |
| Polymarket | Top 50 open markets by volume | No |
| Kalshi | Top 50 open markets by volume | No |
| Sportsbooks | h2h moneylines by book (The Odds API) | Free key (`ODDS_API_KEY`) |
| balldontlie | NBA stats placeholder | Free key (`BALLDONTLIE_API_KEY`) — not wired yet |
| Open-Meteo | Kickoff weather for outdoor NFL games | No |

```bash
python3 data_page.py --html
python3 data_page.py --refresh --html   # bypass caches
```

The daily Actions workflow rebuilds it alongside the sports page.
