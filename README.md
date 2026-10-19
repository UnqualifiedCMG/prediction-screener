# PolyScope — sports prediction-market tracker

A real, working sports tracker: **Polymarket** sports markets, US sportsbook
odds, ESPN data, and fantasy projections — public/free APIs only. Built for
spotting what the money is saying (and where the books and the crowd
disagree).

Live site: https://unqualifiedcmg.github.io/prediction-screener/
(the repo-root `index.html` **is** the dashboard — no landing page).

## Quick start

```bash
cd ~/workspace/prediction-screener
python3 screener.py --screen volume --limit 20
```

## Feature flags (`screener.py`, top of file)

| Flag | Default | What it does |
|------|---------|--------------|
| `ENABLE_KALSHI` | `False` | Kalshi is **parked**: its API data is too thin to be useful (listings flooded with provisional sports-combo scaffolds, no price-history endpoint). All Kalshi fetch/match/display code is intact and tested — set to `True` to bring it back if their data situation improves. While off: no Kalshi API calls, no Kalshi columns/tabs anywhere. |
| `SPORTS_ONLY` | `True` | The site is a sports tracker. Non-sports markets are dropped at normalization using the API's own categorization (the `Sports` tag, then ticker/series prefixes like `nfl-`). Ambiguous events are excluded, never guessed. Override per-run with `--all-markets`. |

`sports_compare.py` has its own `ENABLE_KALSHI = False` (it doesn't import
screener's flag; same semantics).

## Screens (`screener.py`)

| Screen     | What it shows | Why it matters |
|------------|---------------|----------------|
| `volume`   | Highest-volume open sports markets | Liquidity = tradable, and volume is attention |
| `closing`  | Expiring within 7 days, by volume | Resolution edge; mispriced tails get found here |
| `movers`   | Biggest 24h price moves (needs real volume) | Something just changed — find out what |
| `extremes` | Priced ≥90% or ≤10% with high 24h volume | Crowded consensus trades; great content fuel |
| `new`      | Listed in the last 48h | Fresh narratives before the crowd prices them |
| `search`   | Keyword filter (`--query "NBA"`) | Track one story |

## Options

```
--screen {volume,closing,movers,extremes,new,search,all}
--query TEXT        keyword for the search screen
--limit N           rows per screen (default: 20)
--min-volume $      floor on total volume (default: 1000)
--min-volume24h $   floor on 24h volume, for movers/extremes (default: 1000)
--source {poly,kalshi,both}   (default: both; kalshi skipped while parked)
--all-markets       disable the sports-only filter for this run
--json              machine-readable output (cron-friendly)
--html              also write dashboard.html (sortable tables, all screens)
--max-events N      Polymarket fetch cap (default: 2000)
--max-markets N     Kalshi fetch cap (default: 1000, dormant)
```

## Reading the output

- **YES** — Yes price as a probability (Polymarket).
- **24H MOVE** — percentage-point move in the last 24h.
- **VOL 24H / VOL TOT** — Polymarket `volume24h` is event-level, shared
  across that event's markets; per-market 24h volume isn't exposed.
- **CLOSES** — time until the market's end date.

## Known API limitations (verified Oct 2026)

1. **Kalshi is parked, not broken.** Its markets endpoint is flooded with
   provisional multivariate sports-combo scaffolds, and it exposes no price
   history — so movers, backtests, and divergence-vs-history are uncomputable
   there. The code paths are intact behind `ENABLE_KALSHI`.
2. **Polymarket `volume24h` is event-level**, shared across that event's
   markets; per-market 24h volume isn't exposed.
3. Stale records (close date already passed but still listed as open) are
   filtered out.
4. Gamma API 422s past `offset=2000` on closed-event scans — the backtester
   works around it with end-date windows.

## Backtesting (`backtest.py`)

Answers "what % of markets that reach 90¢ actually resolve YES?" with real
resolved-market data. Polymarket only (Kalshi has no price-history API).

```bash
python3 backtest.py --html                                    # cached rerun
python3 backtest.py --markets 1500 --min-volume 1000 \
  --include-multi --windows 8 --html                          # the big run
python3 backtest.py --refresh                                 # rebuild cache
python3 backtest.py --json > results.json
```

What it does: pulls resolved binary Yes/No markets (gamma API,
`closed=true`) across end-date windows, reads each one's final outcomePrices
for the actual resolution, then walks its daily price history (CLOB
`prices-history` on the Yes token) to find max/min price, final-week price,
and time from first 90¢/10¢ touch to resolution. Everything is cached to
`hidden_cache/` so reruns are instant.

Headline finding (1,490 markets, Oct 2026): markets that reached ≥90¢
resolved YES **92.3%** of the time (n=736) — **but** the median first touch
of 90¢ was only **2.4 hours** before resolution, i.e. after the outcome was
effectively decided. Hindsight, not a tradeable edge. The refinement with
teeth: touched 90¢ and **held** ≥80¢ into the final week → 100% YES (n=677);
touched 90¢ but **collapsed** below 80¢ → 3.4% YES (n=59). Full caveats
(daily sampling misses intraday spikes, selection bias, category base rates)
are printed with every run.

Note: the backtest covers **all market categories** (sports, politics,
crypto) — it's historical analysis, not the sports tracker. A sports-only
backtest is on the roadmap. `backtest.html` is kept in the repo as the
original report; the site's **Backtests** tab (`site/backtests.html`) is the
readable research-notebook version.

## The site: PolyScope (`site/` + root `index.html`)

A static tracking site generated from live data. The repo-root `index.html`
**is the dashboard** — opening the site lands directly on it, with the tab
nav (Dashboard, Sports, Fantasy, Backtests, Data, About) to move around.

```bash
python3 site/build.py               # build; refetches open markets if snapshot cache older than 30 min
python3 site/build.py --refresh     # force a fresh pull
```

Preview locally:

```bash
cd ~/workspace/prediction-screener/site && python3 -m http.server 8000
```

Pages:

| Page | What it is |
|------|------------|
| `index.html` (root) | **The dashboard** — top volume, biggest 24h movers, closing soon, extremes watch, fresh listings (sports only) |
| `site/index.html` | Same dashboard (also reachable here) |
| `site/markets.html` | Searchable, sortable explorer (top 2,000 sports markets by volume) |
| `site/backtests.html` | Research notebook: the 90¢ findings, the hold-vs-collapse split, the hindsight caveat, coming-next stubs |
| `site/sports.html` | **All games this week** per league (NFL/NBA/MLB): avg moneyline, avg spread, totals, Polymarket price, ML + spread divergence flags, ESPN context |
| `site/fantasy.html` | Transparent fantasy projections (NFL model, NBA baseline, MLB final actuals) |
| `site/data.html` | Everything by source, tabbed: ESPN, Polymarket, Sportsbooks, balldontlie, Open-Meteo |
| `site/about.html` | Deliberately minimal (one-liner + disclaimers). Methodology lives here in the README, not on the public site. |

How it works: `site/build.py` pulls open sports markets (cached to
`hidden_cache/open_snapshot.json`, refetched only if stale) and writes
vanilla HTML + one CSS file — no framework, no build step. Every page is
stamped with its data timestamp.

Deployment: the generated files are fully static, served from the repo via
GitHub Pages (`UnqualifiedCMG/prediction-screener`).

## Sports: markets vs books (`sports_compare.py`)

The flagship page. **Every game of the week**, per league (NFL, NBA, MLB —
out-of-season leagues show "no games this week"), each row carrying:

- teams + start time, ESPN context (records, injuries, weather for outdoor NFL)
- **avg moneyline**: de-vigged home-win probability, averaged across books
- **avg spread**: mean home-team point spread across books
- **game total**: mean over/under line
- **Polymarket** game price
- **divergence flags**: moneyline (|Poly − book consensus| ≥ 3pp) and spread
  (any book ≥ 1.0 point off the cross-book average)

```bash
export ODDS_API_KEY=your_key_here   # free at https://the-odds-api.com
python3 sports_compare.py --leagues nfl,nba,mlb --threshold 0.03 \
  --spread-threshold 1.0 --html
python3 sports_compare.py --fixture-test   # no-network logic test
```

### Setup: The Odds API key

1. Sign up at https://the-odds-api.com (free tier: 500 credits/month).
2. `export ODDS_API_KEY=<key>` locally, or add it as the `ODDS_API_KEY`
   Actions secret on the repo (already done on
   `UnqualifiedCMG/prediction-screener`).
3. The script reads the key from the environment only — never hardcoded.

Without a key, everything else still runs (Polymarket, ESPN, weather) — only
the sportsbook columns are skipped. One call per league returns moneylines,
spreads, AND totals (same quota cost): ~3 credits/day.

### Free data sources integrated

| Source | What it provides | Key? |
|--------|------------------|------|
| Polymarket gamma API | Game moneyline prices (public-search) | No |
| The Odds API | US sportsbook moneylines, spreads, totals | Free key |
| ESPN unofficial API | Schedules, records, venues, injuries | No |
| Open-Meteo | Weather at kickoff for outdoor NFL games | No |

### Matching methodology

- **Vig removal (basic normalization):** each book's American odds →
  implied probability per side, then each side divided by the overround.
  Consensus = mean de-vigged home-win probability across books.
- **Team normalization:** one canonical table per league maps ESPN
  abbreviations ↔ Odds API full names ↔ Polymarket short names.
- **Conservative matching:** a game is compared only when both teams
  resolve through the table on every source. Ambiguous games are listed
  as *unmatched* — never guessed.
- **Polymarket lookup:** per-game events found via public-search
  (`nfl-dal-phi-2026-10-27` style tickers); the moneyline is the market
  whose question equals the event title with both team outcomes.

### Scheduled refresh

`.github/workflows/sports-refresh.yml` (renamed "PolyScope daily refresh")
runs daily at 12:00 UTC: rebuilds sports, dashboard (root `index.html`),
markets, data, and fantasy pages, commits them plus a dated JSON snapshot
(`site/data/sports-YYYY-MM-DD.json`) back to main, and GitHub Pages
redeploys.

## Fantasy (`fantasy.py`)

Transparent fantasy projections — the model is documented on the page, no
black boxes. `python3 fantasy.py --html` → `site/fantasy.html`.

| League | What's shown |
|--------|--------------|
| NFL (in season) | Projected PPR for top ~100 skill players: `0.65 × last-5 + 0.35 × season`, then game-total/spread/injury adjustments, cross-referenced with Polymarket game prices |
| NBA (2026-27 hasn't tipped off) | 2025-26 per-game fantasy averages as a labeled baseline — actuals, not projections |
| MLB (2026 season complete) | Final actuals: top hitters by OPS, top pitchers by K |

Free sources: Sleeper API (players + weekly stats, no key), ESPN
unofficial + core APIs (no key), The Odds API (optional — without it,
total/spread adjustments are skipped and labeled). If a league lacks free
projections, the page shows actuals + trends instead of fake predictions.

## Data page: everything by source (`data_page.py`)

`python3 data_page.py --html` → `site/data.html`. One tab per source, each
with its description, limitations, and "data as of" timestamp:

| Tab | Contents | Key needed? |
|-----|----------|-------------|
| ESPN | NFL/NBA schedules, records, venues, injury flags | No |
| Polymarket | Top 50 open **sports** markets by volume | No |
| Sportsbooks | h2h moneylines by book (The Odds API) | Free key (`ODDS_API_KEY`) |
| balldontlie | NBA stats placeholder | Free key (`BALLDONTLIE_API_KEY`) — not wired yet |
| Open-Meteo | Kickoff weather for outdoor NFL games | No |

## Files

- `screener.py` — the live screener (sports-only by default). stdlib + `requests`.
- `sports_compare.py` — markets-vs-books engine (moneyline + spread). stdlib + `requests`.
- `fantasy.py` — fantasy projections page builder. stdlib + `requests`.
- `data_page.py` — data-by-source page builder. stdlib + `requests`.
- `backtest.py` — the backtest module. stdlib + `requests`.
- `site/build.py` — dashboard/markets/about generator (also writes root `index.html`).
- `site/backtests.html` — hand-written research notebook (the Backtests tab).
- `dashboard.html` — generated by screener's `--html`; open it directly.
- `backtest.html` — original backtest report (kept as history).
- `hidden_cache/` — raw API responses. Delete or use `--refresh` to rebuild.
- `README.md` — this file.

## Politeness

~0.3s pause between paged requests, 2 attempts per request, hard caps on
pages fetched. Read-only — it never places orders or touches accounts.

## Phase-two options (parked)

- **Kalshi** — re-enable via `ENABLE_KALSHI` if their API data improves
  (needs: usable listings, price history).
- **Sports-only backtest** — rerun the backtester filtered to sports markets.
- **Pinnacle / Betfair:** sharper books, but need funded accounts.
- **balldontlie.io:** now requires a (free) API key; would slot in as a
  second NBA context source.
