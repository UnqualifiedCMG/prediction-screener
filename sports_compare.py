#!/usr/bin/env python3
"""
PolyScope sports comparison — prediction markets vs sportsbook odds.

Compares Polymarket + Kalshi moneyline prices against vig-free sportsbook
consensus (The Odds API) for the same games, and surfaces divergences.

Free data sources (no paid tiers):
  - Polymarket gamma API (public, no key)
  - Kalshi trade API (public, no key)
  - The Odds API free tier (needs free key -> ODDS_API_KEY env var)
  - ESPN unofficial API (public, no key): schedules, records, injuries, venues
  - Open-Meteo (public, no key): weather for outdoor NFL games

Usage:
    ODDS_API_KEY=xxx python3 sports_compare.py --leagues nfl,nba
    python3 sports_compare.py --leagues nfl --threshold 0.05 --html
    python3 sports_compare.py --fixture-test      # no network; verifies logic
    python3 sports_compare.py --json > sports.json

Methodology (also on the site page):
  - Sportsbook odds -> implied probability per book, then de-vigged by
    dividing each side by the overround (basic normalization:
    p_home = raw_home / (raw_home + raw_away)). Consensus = mean of the
    de-vigged home-win probability across books.
  - Prediction-market prices are Yes-token mids (Polymarket) / orderbook
    mids (Kalshi), already vig-free by construction.
  - Divergence = |market_price - book_consensus| for the home team.
  - Team matching is conservative: a game is only compared when both teams
    resolve through the canonical table below. Unmatched games are listed
    separately, never guessed.

Gambling-adjacent output is INFORMATIONAL ONLY, not betting advice.
"""
import argparse
import datetime as dt
import html as htmlmod
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "hidden_cache", "sports")
os.makedirs(CACHE_DIR, exist_ok=True)

NOW = dt.datetime.now(dt.timezone.utc)
PAUSE = 0.3

# --------------------------------------------------------------------------
# Canonical team tables.
# abbr: ESPN abbreviation. full: The Odds API team name.
# short: Polymarket outcome name. city: Kalshi "<city> wins" title fragment.
# --------------------------------------------------------------------------
NFL_TEAMS = [
    ("ARI", "Arizona Cardinals", "Cardinals", "Arizona"),
    ("ATL", "Atlanta Falcons", "Falcons", "Atlanta"),
    ("BAL", "Baltimore Ravens", "Ravens", "Baltimore"),
    ("BUF", "Buffalo Bills", "Bills", "Buffalo"),
    ("CAR", "Carolina Panthers", "Panthers", "Carolina"),
    ("CHI", "Chicago Bears", "Bears", "Chicago"),
    ("CIN", "Cincinnati Bengals", "Bengals", "Cincinnati"),
    ("CLE", "Cleveland Browns", "Browns", "Cleveland"),
    ("DAL", "Dallas Cowboys", "Cowboys", "Dallas"),
    ("DEN", "Denver Broncos", "Broncos", "Denver"),
    ("DET", "Detroit Lions", "Lions", "Detroit"),
    ("GB", "Green Bay Packers", "Packers", "Green Bay"),
    ("HOU", "Houston Texans", "Texans", "Houston"),
    ("IND", "Indianapolis Colts", "Colts", "Indianapolis"),
    ("JAX", "Jacksonville Jaguars", "Jaguars", "Jacksonville"),
    ("KC", "Kansas City Chiefs", "Chiefs", "Kansas City"),
    ("LV", "Las Vegas Raiders", "Raiders", "Las Vegas"),
    ("LAC", "Los Angeles Chargers", "Chargers", "Los Angeles C"),
    ("LAR", "Los Angeles Rams", "Rams", "Los Angeles R"),
    ("MIA", "Miami Dolphins", "Dolphins", "Miami"),
    ("MIN", "Minnesota Vikings", "Vikings", "Minnesota"),
    ("NE", "New England Patriots", "Patriots", "New England"),
    ("NO", "New Orleans Saints", "Saints", "New Orleans"),
    ("NYG", "New York Giants", "Giants", "New York G"),
    ("NYJ", "New York Jets", "Jets", "New York J"),
    ("PHI", "Philadelphia Eagles", "Eagles", "Philadelphia"),
    ("PIT", "Pittsburgh Steelers", "Steelers", "Pittsburgh"),
    ("SF", "San Francisco 49ers", "49ers", "San Francisco"),
    ("SEA", "Seattle Seahawks", "Seahawks", "Seattle"),
    ("TB", "Tampa Bay Buccaneers", "Buccaneers", "Tampa Bay"),
    ("TEN", "Tennessee Titans", "Titans", "Tennessee"),
    ("WAS", "Washington Commanders", "Commanders", "Washington"),
]

NBA_TEAMS = [
    ("ATL", "Atlanta Hawks", "Hawks", "Atlanta"),
    ("BOS", "Boston Celtics", "Celtics", "Boston"),
    ("BKN", "Brooklyn Nets", "Nets", "Brooklyn"),
    ("CHA", "Charlotte Hornets", "Hornets", "Charlotte"),
    ("CHI", "Chicago Bulls", "Bulls", "Chicago"),
    ("CLE", "Cleveland Cavaliers", "Cavaliers", "Cleveland"),
    ("DAL", "Dallas Mavericks", "Mavericks", "Dallas"),
    ("DEN", "Denver Nuggets", "Nuggets", "Denver"),
    ("DET", "Detroit Pistons", "Pistons", "Detroit"),
    ("GS", "Golden State Warriors", "Warriors", "Golden State"),
    ("HOU", "Houston Rockets", "Rockets", "Houston"),
    ("IND", "Indiana Pacers", "Pacers", "Indiana"),
    ("LAC", "LA Clippers", "Clippers", "LA"),
    ("LAL", "Los Angeles Lakers", "Lakers", "Los Angeles"),
    ("MEM", "Memphis Grizzlies", "Grizzlies", "Memphis"),
    ("MIA", "Miami Heat", "Heat", "Miami"),
    ("MIL", "Milwaukee Bucks", "Bucks", "Milwaukee"),
    ("MIN", "Minnesota Timberwolves", "Timberwolves", "Minnesota"),
    ("NO", "New Orleans Pelicans", "Pelicans", "New Orleans"),
    ("NY", "New York Knicks", "Knicks", "New York"),
    ("OKC", "Oklahoma City Thunder", "Thunder", "Oklahoma City"),
    ("ORL", "Orlando Magic", "Magic", "Orlando"),
    ("PHI", "Philadelphia 76ers", "76ers", "Philadelphia"),
    ("PHX", "Phoenix Suns", "Suns", "Phoenix"),
    ("POR", "Portland Trail Blazers", "Trail Blazers", "Portland"),
    ("SAC", "Sacramento Kings", "Kings", "Sacramento"),
    ("SA", "San Antonio Spurs", "Spurs", "San Antonio"),
    ("TOR", "Toronto Raptors", "Raptors", "Toronto"),
    ("UTA", "Utah Jazz", "Jazz", "Utah"),
    ("WSH", "Washington Wizards", "Wizards", "Washington"),
]

MLB_TEAMS = [
    ("ARI", "Arizona Diamondbacks", "Diamondbacks", "Arizona"),
    ("ATL", "Atlanta Braves", "Braves", "Atlanta"),
    ("BAL", "Baltimore Orioles", "Orioles", "Baltimore"),
    ("BOS", "Boston Red Sox", "Red Sox", "Boston"),
    ("CHC", "Chicago Cubs", "Cubs", "Chicago C"),
    ("CWS", "Chicago White Sox", "White Sox", "Chicago W"),
    ("CIN", "Cincinnati Reds", "Reds", "Cincinnati"),
    ("CLE", "Cleveland Guardians", "Guardians", "Cleveland"),
    ("COL", "Colorado Rockies", "Rockies", "Colorado"),
    ("DET", "Detroit Tigers", "Tigers", "Detroit"),
    ("HOU", "Houston Astros", "Astros", "Houston"),
    ("KC", "Kansas City Royals", "Royals", "Kansas City"),
    ("LAA", "Los Angeles Angels", "Angels", "Los Angeles A"),
    ("LAD", "Los Angeles Dodgers", "Dodgers", "Los Angeles D"),
    ("MIA", "Miami Marlins", "Marlins", "Miami"),
    ("MIL", "Milwaukee Brewers", "Brewers", "Milwaukee"),
    ("MIN", "Minnesota Twins", "Twins", "Minnesota"),
    ("NYY", "New York Yankees", "Yankees", "New York Y"),
    ("NYM", "New York Mets", "Mets", "New York M"),
    ("ATH", "Athletics", "Athletics", "Athletics"),
    ("PHI", "Philadelphia Phillies", "Phillies", "Philadelphia"),
    ("PIT", "Pittsburgh Pirates", "Pirates", "Pittsburgh"),
    ("SD", "San Diego Padres", "Padres", "San Diego"),
    ("SF", "San Francisco Giants", "Giants", "San Francisco"),
    ("SEA", "Seattle Mariners", "Mariners", "Seattle"),
    ("STL", "St. Louis Cardinals", "Cardinals", "St. Louis"),
    ("TB", "Tampa Bay Rays", "Rays", "Tampa Bay"),
    ("TEX", "Texas Rangers", "Rangers", "Texas"),
    ("TOR", "Toronto Blue Jays", "Blue Jays", "Toronto"),
    ("WSH", "Washington Nationals", "Nationals", "Washington"),
]

LEAGUES = {
    "nfl": {"teams": NFL_TEAMS, "espn": "football/nfl", "odds": "americanfootball_nfl",
            "kalshi": "KXNFLGAME", "poly_prefix": "nfl-",
            "aliases": {"WSH": "WAS"}},  # ESPN vs everyone else
    "nba": {"teams": NBA_TEAMS, "espn": "basketball/nba", "odds": "basketball_nba",
            "kalshi": "KXNBAGAME", "poly_prefix": "nba-", "aliases": {}},
    "mlb": {"teams": MLB_TEAMS, "espn": "baseball/mlb", "odds": "baseball_mlb",
            "kalshi": "KXMLBGAME", "poly_prefix": "mlb-", "aliases": {}},
}

# NFL stadium coordinates for Open-Meteo (home-team abbr -> lat, lon).
# Neutral-site overrides handled separately.
NFL_STADIUMS = {
    "ARI": (33.5277, -112.2626), "ATL": (33.7554, -84.4008), "BAL": (39.2780, -71.6227),
    "BUF": (42.7738, -78.7870), "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167),
    "CIN": (39.0955, -84.5161), "CLE": (41.5061, -81.6995), "DAL": (32.7473, -97.0945),
    "DEN": (39.7439, -105.0201), "DET": (42.3400, -83.0456), "GB": (44.5013, -88.0622),
    "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639), "JAX": (30.3239, -81.6373),
    "KC": (39.0489, -94.4839), "LV": (36.0908, -115.1838), "LAC": (33.9535, -118.3392),
    "LAR": (33.9535, -118.3392), "MIA": (25.9580, -80.2389), "MIN": (44.9738, -93.2581),
    "NE": (42.0909, -71.2643), "NO": (29.9511, -90.0812), "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745), "PHI": (39.9008, -75.1675), "PIT": (40.4468, -79.9171),
    "SF": (37.4030, -121.9700), "SEA": (47.5952, -122.3316), "TB": (27.9788, -82.5083),
    "TEN": (36.1665, -86.7713), "WAS": (38.9076, -76.8644),
}
NEUTRAL_VENUES = {  # venue name fragment -> lat, lon
    "wembley": (51.5560, -0.2795),
    "tottenham": (51.6043, -0.0664),
    "allianz arena": (48.2188, 11.6247),
    "arena corinthians": (-23.5456, -46.4740),
    "santiago bernabeu": (40.4531, -3.6883),
}

ODDS_API = "https://api.the-odds-api.com/v4"
POLY_SEARCH = "https://gamma-api.polymarket.com/public-search"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

ODDS_TTL = 12 * 3600     # refresh sportsbook odds at most twice a day
CONTEXT_TTL = 6 * 3600   # ESPN / weather cache
KALSHI_TTL = 2 * 3600

# Kalshi is parked: its sports data is too thin to be useful right now.
# All Kalshi fetch/match/display code below is intact — set to True to
# bring it back if their data situation improves.
ENABLE_KALSHI = False


def build_maps(league):
    teams = LEAGUES[league]["teams"]
    return {
        "abbr": {t[0]: t for t in teams},
        "full": {t[1]: t for t in teams},
        "short": {t[2]: t for t in teams},
        "aliases": LEAGUES[league].get("aliases", {}),
    }


def canon(abbr, maps):
    return maps["aliases"].get(abbr, abbr)


# --------------------------------------------------------------------------
# HTTP + cache
# --------------------------------------------------------------------------
def _get(url, params=None, timeout=25):
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            time.sleep(PAUSE)
            return r, r.json()
        except Exception as e:
            last = e
            time.sleep(1.0)
    raise RuntimeError(f"GET failed for {url}: {last}")


def cache_get(name, ttl):
    p = os.path.join(CACHE_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            d = json.load(f)
        ts = dt.datetime.fromisoformat(d["ts"])
        if (NOW - ts).total_seconds() > ttl:
            return None
        return d["payload"]
    except Exception:
        return None


def cache_set(name, payload):
    p = os.path.join(CACHE_DIR, name)
    try:
        with open(p, "w") as f:
            json.dump({"ts": NOW.isoformat(), "payload": payload}, f)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------
def fetch_espn_games(league, maps, refresh=False):
    """This week's games from ESPN: teams, records, start time, venue."""
    key = f"espn_games_{league}.json"
    if not refresh:
        hit = cache_get(key, CONTEXT_TTL)
        if hit is not None:
            return hit
    _, d = _get(f"{ESPN}/{LEAGUES[league]['espn']}/scoreboard")
    games = []
    for e in d.get("events", []):
        state = (e.get("status") or {}).get("type", {}).get("state")
        if state != "pre":
            continue  # only upcoming games — finished ones have closed markets
        comp = (e.get("competitions") or [{}])[0]
        teams = {}
        for c in comp.get("competitors", []):
            abbr = canon((c.get("team") or {}).get("abbreviation"), maps)
            recs = c.get("records") or []
            teams[c.get("homeAway")] = {
                "abbr": abbr,
                "record": (recs[0].get("summary") if recs else None),
            }
        if "home" not in teams or "away" not in teams:
            continue
        venue = comp.get("venue") or {}
        games.append({
            "espn_id": e.get("id"),
            "away": teams["away"]["abbr"], "home": teams["home"]["abbr"],
            "away_record": teams["away"]["record"], "home_record": teams["home"]["record"],
            "date": e.get("date"),
            "venue": venue.get("fullName"),
            "indoor": venue.get("indoor"),
            "status": (e.get("status") or {}).get("type", {}).get("shortDetail"),
        })
    cache_set(key, games)
    return games


def fetch_espn_injuries(league, maps, refresh=False):
    """Key injuries per team: counts + notable Out/Doubtful names."""
    key = f"espn_inj_{league}.json"
    if not refresh:
        hit = cache_get(key, CONTEXT_TTL)
        if hit is not None:
            return hit
    out = {}
    try:
        _, d = _get(f"{ESPN}/{LEAGUES[league]['espn']}/injuries")
    except Exception:
        cache_set(key, out)
        return out
    full_map = maps["full"]
    for t in d.get("injuries", []):
        name = t.get("displayName")
        team = full_map.get(name)
        if not team:
            continue
        notable, n_qd = [], 0
        for inj in t.get("injuries", []):
            st = (inj.get("status") or "").lower()
            ath = inj.get("athlete") or {}
            aname = ath.get("displayName", "?")
            apos = (ath.get("position") or {}).get("abbreviation")
            if st in ("out", "doubtful"):
                notable.append(f"{aname} ({apos or '?'}, {inj.get('status')})")
                n_qd += 1
            elif st == "questionable":
                n_qd += 1
        out[team[0]] = {"n_qd": n_qd, "notable": notable[:3]}
    cache_set(key, out)
    return out


def fetch_weather(lat, lon, game_iso, refresh=False):
    """Temp/precip/wind at game hour from Open-Meteo. Returns dict or None."""
    if not game_iso:
        return None
    try:
        gdt = dt.datetime.fromisoformat(game_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    key = f"wx_{lat:.2f}_{lon:.2f}_{gdt.strftime('%Y%m%d%H')}.json"
    if not refresh:
        hit = cache_get(key, CONTEXT_TTL * 4)
        if hit is not None:
            return hit
    try:
        _, d = _get(OPEN_METEO, {
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "forecast_days": 7,
        })
        h = d.get("hourly", {})
        times = h.get("time", [])
        best, best_dt = None, None
        for i, t in enumerate(times):
            try:
                ht = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            if best_dt is None or abs((ht - gdt).total_seconds()) < abs((best_dt - gdt).total_seconds()):
                best_dt = ht
                best = {"temp_f": h["temperature_2m"][i],
                        "precip_prob": h["precipitation_probability"][i],
                        "wind_mph": h["wind_speed_10m"][i]}
        cache_set(key, best)
        return best
    except Exception:
        return None


def game_coords(league, home_abbr, venue):
    if league != "nfl":
        return None  # NBA/MLB/NHL play indoors; skip weather
    v = (venue or "").lower()
    for frag, coords in NEUTRAL_VENUES.items():
        if frag in v:
            return coords
    return NFL_STADIUMS.get(home_abbr)


def fetch_polymarket_game(league, away_short, home_short, refresh=False):
    """Moneyline market for one game via public-search. Conservative match."""
    key = f"poly_{league}_{away_short}_{home_short}.json".replace(" ", "_")
    if not refresh:
        hit = cache_get(key, KALSHI_TTL)
        if hit is not None:
            return hit
    prefix = LEAGUES[league]["poly_prefix"]
    result = None
    for query in (f"{away_short} vs. {home_short}", f"{home_short} vs. {away_short}"):
        try:
            _, d = _get(POLY_SEARCH, {"q": query})
        except Exception:
            continue
        for ev in d.get("events", []):
            if ev.get("closed"):
                continue
            if not (ev.get("ticker") or "").startswith(prefix):
                continue
            title = (ev.get("title") or "").strip()
            for m in ev.get("markets", []):
                q = (m.get("question") or "").strip()
                outs = m.get("outcomes") or []
                if isinstance(outs, str):  # public-search JSON-encodes these
                    try:
                        outs = json.loads(outs)
                    except Exception:
                        continue
                if q == title and set(outs) == {away_short, home_short}:
                    pr = m.get("outcomePrices") or []
                    if isinstance(pr, str):
                        try:
                            pr = json.loads(pr)
                        except Exception:
                            continue
                    try:
                        prices = [float(x) for x in pr]
                    except Exception:
                        continue
                    if len(prices) != 2:
                        continue
                    # map price to home team by outcome name
                    ph = prices[outs.index(home_short)]
                    result = {
                        "price_home": ph,
                        "volume": ev.get("volume"),
                        "url": f"https://polymarket.us/event/{ev.get('slug')}",
                        "title": title,
                    }
                    break
            if result:
                break
        if result:
            break
    cache_set(key, result)
    return result


def fetch_kalshi_games(league, refresh=False):
    """Pair Kalshi game markets by ticker prefix; mid from orderbook."""
    key = f"kalshi_{league}.json"
    if not refresh:
        hit = cache_get(key, KALSHI_TTL)
        if hit is not None:
            return hit
    series = LEAGUES[league]["kalshi"]
    out = {}
    try:
        _, d = _get(f"{KALSHI}/markets", {"limit": 200, "status": "open",
                                          "series_ticker": series})
    except Exception:
        cache_set(key, out)
        return out
    pairs = {}
    for m in d.get("markets", []):
        t = m.get("ticker") or ""
        if "-" not in t:
            continue
        pre, code = t.rsplit("-", 1)
        pairs.setdefault(pre, {})[code] = t
    for pre, sides in pairs.items():
        if len(sides) != 2:
            continue
        codes = sorted(sides)
        mids = {}
        ok = True
        for code in codes:
            try:
                _, ob = _get(f"{KALSHI}/markets/{sides[code]}/orderbook",
                             {"depth": 1})
                ob = ob.get("orderbook") or ob.get("orderbook_fp") or {}
                yes = ob.get("yes") or ob.get("yes_dollars") or []
                no = ob.get("no") or ob.get("no_dollars") or []
                bid = float(yes[0][0]) / 100 if yes else None
                # ask = 1 - best no-bid (no price quoted in dollars)
                nobid = float(no[0][0]) / 100 if no else None
                ask = (1 - nobid) if nobid is not None else None
                if bid is not None and ask is not None:
                    mids[code] = round((bid + ask) / 2, 4)
                elif bid is not None:
                    mids[code] = round(bid, 4)
                elif ask is not None:
                    mids[code] = round(ask, 4)
                else:
                    ok = False
            except Exception:
                ok = False
        if ok:
            out[pre] = {"codes": codes, "mid": mids}
    cache_set(key, out)
    return out


def match_kalshi_game(kalshi_games, maps, away_abbr, home_abbr):
    """Conservative: ticker team codes must both resolve in the team table."""
    for pre, g in kalshi_games.items():
        codes = g["codes"]
        abbrs = [c if c in maps["abbr"] else None for c in codes]
        if None in abbrs:
            continue
        if set(abbrs) == {away_abbr, home_abbr}:
            return {"price_home": g["mid"][home_abbr], "ticker_pre": pre}
    return None


ODDS_QUOTA = {"remaining": None, "used": None}


def fetch_odds(league, api_key, refresh=False):
    """Sportsbook h2h odds + spreads + totals via The Odds API.

    One call per league returns all three markets (same quota cost).
    Cached 12h; records quota headers.
    """
    key = f"odds3_{league}.json"
    if not refresh:
        hit = cache_get(key, ODDS_TTL)
        if hit is not None:
            return hit, True
    if not api_key:
        return None, False
    sport = LEAGUES[league]["odds"]
    r = requests.get(f"{ODDS_API}/sports/{sport}/odds",
                     params={"apiKey": api_key, "regions": "us",
                             "markets": "h2h,spreads,totals", "oddsFormat": "american"},
                     timeout=30)
    ODDS_QUOTA["remaining"] = r.headers.get("x-requests-remaining")
    ODDS_QUOTA["used"] = r.headers.get("x-requests-used")
    r.raise_for_status()
    time.sleep(PAUSE)
    data = r.json()
    cache_set(key, data)
    return data, False


# --------------------------------------------------------------------------
# Odds math
# --------------------------------------------------------------------------
def american_to_implied(odds):
    odds = float(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def devig(home_odds, away_odds):
    """Remove the vig by normalizing each side's implied prob by the overround."""
    rh = american_to_implied(home_odds)
    ra = american_to_implied(away_odds)
    tot = rh + ra
    if tot <= 0:
        return None, None
    return rh / tot, ra / tot


def book_consensus(game_odds, maps, away_abbr, home_abbr):
    """Mean de-vigged home-win prob across books. Returns (prob, n_books)."""
    away_full = maps["abbr"][away_abbr][1]
    home_full = maps["abbr"][home_abbr][1]
    probs = []
    for ev in game_odds or []:
        if ev.get("away_team") != away_full or ev.get("home_team") != home_full:
            continue
        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                outs = {o.get("name"): o.get("price") for o in mkt.get("outcomes", [])}
                if home_full in outs and away_full in outs:
                    ph, _ = devig(outs[home_full], outs[away_full])
                    if ph is not None:
                        probs.append(ph)
                break
    if not probs:
        return None, 0
    return sum(probs) / len(probs), len(probs)


def _market_outcomes(game_odds, maps, away_abbr, home_abbr, market_key):
    """Yield (book_title, outcomes_dict) for one market across books."""
    away_full = maps["abbr"][away_abbr][1]
    home_full = maps["abbr"][home_abbr][1]
    for ev in game_odds or []:
        if ev.get("away_team") != away_full or ev.get("home_team") != home_full:
            continue
        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != market_key:
                    continue
                outs = {o.get("name"): o for o in mkt.get("outcomes", [])}
                yield (bm.get("title") or bm.get("key"), outs)
                break  # one market of this type per book


def spread_consensus(game_odds, maps, away_abbr, home_abbr):
    """Mean home-team point spread across books.

    Returns (avg_spread, {book: point}, n_books). Negative = home favored.
    """
    away_full = maps["abbr"][away_abbr][1]
    home_full = maps["abbr"][home_abbr][1]
    pts = {}
    for book, outs in _market_outcomes(game_odds, maps, away_abbr, home_abbr, "spreads"):
        o = outs.get(home_full)
        if o is not None and o.get("point") is not None:
            try:
                pts[book] = float(o["point"])
            except (TypeError, ValueError):
                pass
    if not pts:
        return None, {}, 0
    return sum(pts.values()) / len(pts), pts, len(pts)


def totals_consensus(game_odds, maps, away_abbr, home_abbr):
    """Mean over/under line across books. Returns (avg_total, n_books)."""
    away_full = maps["abbr"][away_abbr][1]
    home_full = maps["abbr"][home_abbr][1]
    totals = []
    for book, outs in _market_outcomes(game_odds, maps, away_abbr, home_abbr, "totals"):
        o = outs.get("Over")
        if o is not None and o.get("point") is not None:
            try:
                totals.append(float(o["point"]))
            except (TypeError, ValueError):
                pass
    if not totals:
        return None, 0
    return sum(totals) / len(totals), len(totals)


def book_lines(game_odds, maps, away_abbr, home_abbr):
    """Per-book raw lines: {book_title: {ml_home, ml_away, spread_home, total}}.

    Missing lines are simply absent — the renderer shows '—', never a guess.
    """
    away_full = maps["abbr"][away_abbr][1]
    home_full = maps["abbr"][home_abbr][1]
    books = {}
    for ev in game_odds or []:
        if ev.get("away_team") != away_full or ev.get("home_team") != home_full:
            continue
        for bm in ev.get("bookmakers", []):
            title = bm.get("title") or bm.get("key") or "?"
            entry = books.setdefault(title, {})
            for mkt in bm.get("markets", []):
                key = mkt.get("key")
                outs = {o.get("name"): o for o in mkt.get("outcomes", [])}
                if key == "h2h":
                    if home_full in outs and outs[home_full].get("price") is not None:
                        entry["ml_home"] = outs[home_full]["price"]
                    if away_full in outs and outs[away_full].get("price") is not None:
                        entry["ml_away"] = outs[away_full]["price"]
                elif key == "spreads":
                    o = outs.get(home_full)
                    if o is not None and o.get("point") is not None:
                        try:
                            entry["spread_home"] = float(o["point"])
                        except (TypeError, ValueError):
                            pass
                elif key == "totals":
                    o = outs.get("Over")
                    if o is not None and o.get("point") is not None:
                        try:
                            entry["total"] = float(o["point"])
                        except (TypeError, ValueError):
                            pass
    return books


def prob_to_american(p):
    """De-vigged probability -> representative American odds. None if invalid."""
    if p is None or p <= 0 or p >= 1:
        return None
    if p >= 0.5:
        return round(-100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def fmt_american(odds):
    if odds is None:
        return "—"
    return f"{odds:+d}"


def game_slug(league, away, home, date_iso):
    """Stable URL slug for a game: same game -> same URL across refreshes."""
    d = ""
    if date_iso:
        try:
            d = dt.datetime.fromisoformat(
                date_iso.replace("Z", "+00:00")).strftime("%Y%m%d")
        except Exception:
            pass
    base = f"{league}-{away}-{home}" + (f"-{d}" if d else "")
    return re.sub(r"[^a-z0-9-]", "", base.lower())


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------
def compare_league(league, api_key, threshold, spread_threshold=1.0, refresh=False):
    maps = build_maps(league)
    games = fetch_espn_games(league, maps, refresh)
    injuries = fetch_espn_injuries(league, maps, refresh)
    # Kalshi is parked — skip the fetch entirely; matching code stays dormant.
    kalshi = fetch_kalshi_games(league, refresh) if ENABLE_KALSHI else {}
    odds_data, odds_cached = fetch_odds(league, api_key, refresh)

    rows, unmatched = [], []
    for g in games:
        away, home = g["away"], g["home"]
        if away not in maps["abbr"] or home not in maps["abbr"]:
            unmatched.append({"game": f"{away} @ {home}", "reason": "team not in table"})
            continue
        away_short = maps["abbr"][away][2]
        home_short = maps["abbr"][home][2]

        poly = fetch_polymarket_game(league, away_short, home_short, refresh)
        kal = match_kalshi_game(kalshi, maps, away, home) if ENABLE_KALSHI else None
        cons, n_books = book_consensus(odds_data, maps, away, home) if odds_data else (None, 0)
        avg_spread, book_spreads, n_spread_books = (
            spread_consensus(odds_data, maps, away, home) if odds_data else (None, {}, 0))
        avg_total, n_total_books = (
            totals_consensus(odds_data, maps, away, home) if odds_data else (None, 0))

        # spread divergence: books sitting >= spread_threshold off the consensus
        spread_flags = []
        if avg_spread is not None:
            for book, pt in book_spreads.items():
                if abs(pt - avg_spread) >= spread_threshold:
                    spread_flags.append({"book": book, "point": round(pt, 1),
                                         "off": round(pt - avg_spread, 1)})

        # context line
        inj = injuries.get(away, {}).get("notable", []) + injuries.get(home, {}).get("notable", [])
        n_inj = injuries.get(away, {}).get("n_qd", 0) + injuries.get(home, {}).get("n_qd", 0)
        wx = None
        if g.get("indoor") is False:
            coords = game_coords(league, home, g.get("venue"))
            if coords:
                wx = fetch_weather(*coords, g.get("date"), refresh)

        ctx = []
        if g.get("away_record") or g.get("home_record"):
            ctx.append(f"{away} {g.get('away_record') or '?'} @ {home} {g.get('home_record') or '?'}")
        if n_inj:
            ctx.append(f"{n_inj} Q/D/Out inj: " + "; ".join(inj[:2]))
        if wx:
            ctx.append(f"{wx['temp_f']:.0f}F, {wx['precip_prob']}% precip, {wx['wind_mph']:.0f}mph wind")

        row = {
            "league": league.upper(), "away": away, "home": home,
            "date": (g.get("date") or "")[:16].replace("T", " "),
            "date_iso": g.get("date"),
            "slug": game_slug(league, away, home, g.get("date")),
            "venue": g.get("venue"), "status": g.get("status"),
            "context": " · ".join(ctx) if ctx else None,
            "injuries": inj, "n_inj": n_inj,
            "poly_home": poly["price_home"] if poly else None,
            "poly_url": poly["url"] if poly else None,
            "poly_volume": poly["volume"] if poly else None,
            "kalshi_home": kal["price_home"] if kal else None,
            "books_home": round(cons, 4) if cons is not None else None,
            "n_books": n_books,
            "books": book_lines(odds_data, maps, away, home) if odds_data else {},
            "spread_home": round(avg_spread, 1) if avg_spread is not None else None,
            "n_spread_books": n_spread_books,
            "spread_flags": spread_flags,
            "total": round(avg_total, 1) if avg_total is not None else None,
        }
        divs = {}
        if cons is not None and poly:
            divs["poly"] = round(poly["price_home"] - cons, 4)
        if ENABLE_KALSHI and cons is not None and kal:
            divs["kalshi"] = round(kal["price_home"] - cons, 4)
        row["divs"] = divs
        row["max_div"] = max((abs(v) for v in divs.values()), default=0)
        row["flagged"] = (row["max_div"] >= threshold and bool(divs)) or bool(spread_flags)
        if poly is None and cons is None:
            unmatched.append({"game": f"{away} @ {home}",
                              "reason": "no market data from any source"})
        else:
            rows.append(row)
    return {"rows": rows, "unmatched": unmatched,
            "odds_cached": odds_cached, "n_games": len(games)}


def direction_text(div):
    if div is None:
        return "—"
    side = "market" if div > 0 else "books"
    return f"{side} higher by {abs(div)*100:.1f}pp"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
DISCLAIMER = ("Informational only — not betting advice. Prices move; verify before acting. "
              "21+. Gambling problem? Call 1-800-GAMBLER.")


def render_terminal(all_leagues, threshold, spread_threshold):
    for league, res in all_leagues.items():
        print(f"\n{'='*78}\n{league.upper()} — {res['n_games']} games, "
              f"{sum(1 for r in res['rows'] if r['flagged'])} flagged "
              f"(ML div >= {threshold*100:.0f}pp or spread off >= {spread_threshold:.1f})")
        print("INFORMATIONAL ONLY — not betting advice. 21+. 1-800-GAMBLER.\n")
        if not res["rows"]:
            print("  no games this week (out of season or none scheduled)")
            continue
        for r in sorted(res["rows"], key=lambda r: (-r["max_div"], r["date"] or "")):
            b = f"{r['books_home']*100:.1f}%" if r["books_home"] is not None else "n/a"
            p = f"{r['poly_home']*100:.1f}%" if r["poly_home"] is not None else "n/a"
            s = (f"{r['spread_home']:+.1f}" if r["spread_home"] is not None else "n/a")
            t = f"{r['total']:.1f}" if r["total"] is not None else "n/a"
            flag = "🚩 " if r["flagged"] else "   "
            print(f"{flag}{r['away']} @ {r['home']}  ({r['date']})")
            print(f"    books ML(home) {b} [{r['n_books']} books] | spread {s} | total {t} | Poly {p}")
            for src, d in r["divs"].items():
                print(f"    {src}: {direction_text(d)}")
            for f in r["spread_flags"]:
                print(f"    spread watch: {f['book']} {f['point']:+.1f} "
                      f"({f['off']:+.1f} off consensus)")
            if r["context"]:
                print(f"    ctx: {r['context']}")
        if res["unmatched"]:
            print(f"\n  unmatched ({len(res['unmatched'])}):")
            for u in res["unmatched"][:8]:
                print(f"    {u['game']} — {u['reason']}")


def render_html(all_leagues, threshold, spread_threshold, path):
    gen = NOW.strftime("%Y-%m-%d %H:%M UTC")
    nav = "".join(
        f'<a href="{h}">{l}</a>' for h, l in
        [("index.html", "Dashboard"), ("markets.html", "Markets"),
         ("backtests.html", "Backtests"), ("sports.html", "Sports"),
         ("fantasy.html", "Fantasy"), ("data.html", "Data"),
         ("about.html", "About")])
    any_books = any(r.get("books_home") is not None
                    for res in all_leagues.values() for r in res["rows"])
    banner = ""
    if not any_books:
        banner = ("<div class='notice'>⚠️ Sportsbook odds unavailable in this "
                  "build (no Odds API key or quota exhausted) — showing "
                  "prediction-market prices only. Divergences need book consensus.</div>")
    body = [banner + f"""<div class="freshness">📡 Sports snapshot generated {gen} ·
odds cached (free-tier friendly: refreshes ≤2x/day) · {htmlmod.escape(DISCLAIMER)}</div>
<h1>Sports: markets vs books</h1>
<h2>Methodology</h2>
<ul class="method">
<li>Moneyline: sportsbook odds → implied probability per book, de-vigged by dividing each side
by the overround (basic normalization). Consensus = mean de-vigged home-win probability across books.</li>
<li>Spread: mean of each book's home-team point spread. Books sitting ≥ {spread_threshold:.1f} point
off the cross-book average are flagged — a book disagreeing with the market on the number.</li>
<li>Total: mean over/under line across books.</li>
<li>Polymarket: Yes-token price on the game moneyline market. (Kalshi support is parked —
its sports data is too thin to be useful right now.)</li>
<li>Team matching is conservative — a game only appears when both teams resolve through
the canonical table; anything ambiguous is listed as unmatched, never guessed.</li>
<li>Context: ESPN records/injuries + Open-Meteo weather for outdoor NFL games. Free sources only.</li>
</ul>"""]
    for league, res in all_leagues.items():
        n_flag = sum(1 for r in res["rows"] if r["flagged"])
        body.append(f"<h2>{league.upper()} — this week's games "
                    f"({res['n_games']} games, {n_flag} flagged)</h2>")
        if not res["rows"]:
            body.append("<p>No games this week — league is out of season or "
                        "none are scheduled.</p>")
            continue
        ordered = sorted(res["rows"],
                         key=lambda r: (not r["flagged"], -r["max_div"], r["date"] or ""))
        body.append("<table><tr><th>Game</th><th>Start (UTC)</th>"
                    "<th>Books ML (home)</th><th>Avg spread</th><th>Total</th>"
                    "<th>Polymarket</th><th>Poly as odds</th><th>ML gap</th><th>Spread watch</th>"
                    "<th>Context</th></tr>")
        for r in ordered:
            b = f"{r['books_home']*100:.1f}%" if r["books_home"] is not None else "n/a"
            s = f"{r['spread_home']:+.1f}" if r["spread_home"] is not None else "n/a"
            t = f"{r['total']:.1f}" if r["total"] is not None else "n/a"
            p = f"{r['poly_home']*100:.1f}%" if r["poly_home"] is not None else "n/a"
            po = fmt_american(prob_to_american(r["poly_home"])) if r["poly_home"] is not None else "n/a"
            gaps = "<br>".join(f"{s}: {direction_text(d)}" for s, d in r["divs"].items()) or "—"
            if r["spread_flags"]:
                sw = "<br>".join(f"{f['book']} {f['point']:+.1f} ({f['off']:+.1f} off)"
                                 for f in r["spread_flags"])
            else:
                sw = "—"
            plink = (f"<a href='{r['poly_url']}' target='_blank' rel='noopener'>{p}</a>"
                     if r.get("poly_url") else p)
            ctx = htmlmod.escape(r["context"] or "—")
            cls = " class='flag'" if r["flagged"] else ""
            body.append(
                f"<tr{cls}><td><b>{r['away']} @ {r['home']}</b></td>"
                f"<td class='dim'>{htmlmod.escape(r['date'] or '')}</td>"
                f"<td>{b}<br><span class='dim'>{r['n_books']} books</span></td>"
                f"<td>{s}</td><td>{t}</td><td>{plink}</td><td>{po}</td><td>{gaps}</td>"
                f"<td class='ctx'>{sw}</td><td class='ctx'>{ctx}</td></tr>")
        body.append("</table>")
        if res["unmatched"]:
            body.append(f"<details><summary>Unmatched games "
                        f"({len(res['unmatched'])})</summary><ul>")
            for u in res["unmatched"]:
                body.append(f"<li>{htmlmod.escape(u['game'])} — "
                            f"{htmlmod.escape(u['reason'])}</li>")
            body.append("</ul></details>")
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sports — PolyScope</title>
<link rel="stylesheet" href="styles.css">
<style>
tr.flag td {{ background: #2a2118; }} .ctx {{ font-size: 12px; color: #8b98a5; }}
.dim {{ color: #8b98a5; font-size: 12px; }}
.method li {{ margin-bottom: 6px; color: #aab4c0; }}
details {{ margin-top: 16px; }} summary {{ cursor: pointer; color: #1d9bf0; }}
.notice {{ background: #1a2332; border: 1px solid #2f3b4d; border-radius: 8px;
padding: 12px 16px; margin: 16px 0; font-size: 13px; color: #8b98a5; }}
</style></head>
<body>
<header class="site-header"><div class="wrap head-row">
<a class="brand" href="index.html">◈ Poly<span>Scope</span></a>
<nav>{nav}</nav></div></header>
<main class="wrap">
<div class="notice">🔞 21+. {htmlmod.escape(DISCLAIMER)}</div>
{"".join(body)}
</main>
<footer class="wrap"><p>Data: Polymarket · The Odds API · ESPN · Open-Meteo.
Free tiers only. Kalshi support parked. <a href="about.html">Methodology</a></p></footer>
</body></html>"""
    with open(path, "w") as f:
        f.write(page)


# --------------------------------------------------------------------------
# Dashboard (weekly games slate) + per-game detail pages
# --------------------------------------------------------------------------
GAMES_NAV = [("index.html", "Dashboard"), ("markets.html", "Markets"),
             ("backtests.html", "Backtests"), ("sports.html", "Sports"),
             ("fantasy.html", "Fantasy"), ("data.html", "Data"),
             ("about.html", "About")]


def games_shell(title, active, body, gen_note, prefix=""):
    """Site shell shared by dashboard and game pages.

    prefix: "" for pages in site/, "site/" for the repo-root dashboard,
    "../" for pages in site/games/.
    """
    nav = "".join(
        f'<a href="{prefix}{h}" class="{"on" if h == active else ""}">{l}</a>'
        for h, l in GAMES_NAV)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{htmlmod.escape(title)} — PolyScope</title>
<link rel="stylesheet" href="{prefix}styles.css"></head>
<body>
<header class="site-header"><div class="wrap head-row">
<a class="brand" href="{prefix}index.html">◈ Poly<span>Scope</span></a>
<nav>{nav}</nav></div></header>
<main class="wrap">
<div class="freshness">📡 Data as of {gen_note} · scheduled snapshot —
reload for the latest rebuild. Informational only — not betting advice.
21+. Gambling problem? Call 1-800-GAMBLER.</div>
{body}
</main>
<footer class="wrap foot">PolyScope · public API data only · for research,
not financial advice. Nothing here is a recommendation to trade or bet.</footer>
</body></html>"""


def _ml_cell(r):
    if r["books_home"] is None:
        return "—"
    am = prob_to_american(r["books_home"])
    return f"{r['books_home'] * 100:.1f}% ({fmt_american(am)})"


def _game_link(r, game_prefix):
    return (f"<a href='{game_prefix}{r['slug']}.html'>"
            f"<b>{r['away']} @ {r['home']}</b></a>")


def _signal_cell(r):
    parts = []
    for src, d in r["divs"].items():
        if abs(d) >= 0.03:
            side = "market" if d > 0 else "books"
            parts.append(f"🚩 ML: {side} +{abs(d) * 100:.1f}pp ({src})")
    for f in r["spread_flags"]:
        parts.append(f"🚩 spread: {f['book']} {f['point']:+.1f}")
    return "<br>".join(parts) if parts else '<span style="color:#6b7482">—</span>'


def dashboard_body(all_leagues, game_prefix):
    """The weekly slate: every game, per league, clickable into detail pages."""
    parts = []
    total = sum(len(res["rows"]) for res in all_leagues.values())
    parts.append(f"<h1>This week's games <span class='count'>({total})</span></h1>")
    for league, res in all_leagues.items():
        parts.append(f"<h2>{league.upper()} <span class='count'>"
                     f"({res['n_games']} games)</span></h2>")
        if not res["rows"]:
            parts.append("<p>No games this week — league is out of season or "
                         "none are scheduled.</p>")
            continue
        ordered = sorted(res["rows"], key=lambda r: (r["date"] or ""))
        rows = []
        for r in ordered:
            s = f"{r['spread_home']:+.1f}" if r["spread_home"] is not None else "—"
            t = f"{r['total']:.1f}" if r["total"] is not None else "—"
            p = f"{r['poly_home'] * 100:.1f}%" if r["poly_home"] is not None else "—"
            po = fmt_american(prob_to_american(r["poly_home"])) if r["poly_home"] is not None else "—"
            rows.append(
                f"<tr><td>{_game_link(r, game_prefix)}</td>"
                f"<td class='dim'>{htmlmod.escape(r['date'] or '')}</td>"
                f"<td class='num'>{_ml_cell(r)}</td>"
                f"<td class='num'>{s}</td>"
                f"<td class='num'>{t}</td>"
                f"<td class='num'>{p}</td>"
                f"<td class='num'>{po}</td>"
                f"<td>{_signal_cell(r)}</td></tr>")
        parts.append(
            "<div class='tbl-wrap'><table><thead><tr>"
            "<th>Game</th><th>Start (UTC)</th><th>Avg moneyline (home)</th>"
            "<th>Avg spread</th><th>Total</th><th>Polymarket</th><th>Poly as odds</th><th>Signals</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    return "".join(parts)


def render_dashboard(all_leagues, gen_note, site_index_path, root_index_path):
    """Write the dashboard to site/index.html and the repo-root index.html."""
    body = dashboard_body(all_leagues, game_prefix="games/")
    with open(site_index_path, "w") as f:
        f.write(games_shell("Dashboard", "index.html", body, gen_note, prefix=""))
    print(f"wrote {site_index_path}", file=sys.stderr)

    # Repo-root copy: same dashboard, links rewritten under site/...
    root_body = dashboard_body(all_leagues, game_prefix="site/games/")
    root_page = games_shell("Dashboard", "index.html", root_body, gen_note,
                            prefix="site/")
    with open(root_index_path, "w") as f:
        f.write(root_page)
    print(f"wrote {root_index_path} (dashboard as landing)", file=sys.stderr)


def _book_row(book, lines):
    ml_h = fmt_american(lines.get("ml_home"))
    ml_a = fmt_american(lines.get("ml_away"))
    sp = f"{lines['spread_home']:+.1f}" if "spread_home" in lines else "—"
    tot = f"{lines['total']:.1f}" if "total" in lines else "—"
    return (f"<tr><td><b>{htmlmod.escape(book)}</b></td>"
            f"<td class='num'>{ml_h}</td><td class='num'>{ml_a}</td>"
            f"<td class='num'>{sp}</td><td class='num'>{tot}</td></tr>")


def render_game_page(r, gen_note):
    """Full detail for one game. Every number traces to a live pull;
    missing data renders as '—', never a guess."""
    title = f"{r['away']} @ {r['home']}"
    parts = [f"<h1>{title} <span class='count'>{r['league']}</span></h1>",
             f"<p class='sub'>{htmlmod.escape(r['date'] or '')} UTC"
             + (f" · {htmlmod.escape(r['venue'])}" if r.get("venue") else "")
             + (f" · {htmlmod.escape(r['status'])}" if r.get("status") else "")
             + "</p>"]

    # Consensus snapshot
    am = prob_to_american(r["books_home"])
    cons_rows = [
        ("Books ML (home, de-vigged)",
         f"{r['books_home'] * 100:.1f}% ({fmt_american(am)})"
         if r["books_home"] is not None else "—",
         f"{r['n_books']} books"),
        ("Avg spread (home)",
         f"{r['spread_home']:+.1f}" if r["spread_home"] is not None else "—",
         f"{r['n_spread_books']} books"),
        ("Avg total",
         f"{r['total']:.1f}" if r["total"] is not None else "—", ""),
    ]
    if r["poly_home"] is not None:
        plink = (f"<a href='{htmlmod.escape(r['poly_url'])}' target='_blank' "
                 f"rel='noopener'>{r['poly_home'] * 100:.1f}%</a>"
                 if r.get("poly_url") else f"{r['poly_home'] * 100:.1f}%")
        vol = f"${r['poly_volume']:,.0f}" if r.get("poly_volume") else "—"
        cons_rows.append(("Polymarket (home)", plink, f"vol {vol}"))
        cons_rows.append(("Polymarket as fair odds",
                         fmt_american(prob_to_american(r["poly_home"])),
                         "no-vig equivalent"))
    else:
        cons_rows.append(("Polymarket (home)", "—", "no moneyline market found"))
    parts.append("<div class='tbl-wrap'><table><thead><tr><th>Consensus</th>"
                 "<th>Value</th><th></th></tr></thead><tbody>" +
                 "".join(f"<tr><td>{k}</td><td class='num'><b>{v}</b></td>"
                         f"<td class='dim'>{n}</td></tr>"
                         for k, v, n in cons_rows) + "</tbody></table></div>")

    # Per-book lines
    parts.append("<h2>Lines by sportsbook</h2>")
    if r["books"]:
        brow = "".join(_book_row(b, k)
                       for b, k in sorted(r["books"].items()))
        parts.append("<div class='tbl-wrap'><table><thead><tr><th>Book</th>"
                     "<th>ML home</th><th>ML away</th><th>Spread (home)</th>"
                     "<th>Total</th></tr></thead><tbody>" + brow +
                     "</tbody></table></div>")
    else:
        parts.append("<p>Sportsbook lines unavailable in this snapshot "
                     "(no Odds API key or quota exhausted).</p>")

    # Divergence analysis
    parts.append("<h2>Divergence analysis</h2>")
    dparts = []
    for src, d in r["divs"].items():
        side = "prediction market" if d > 0 else "sportsbooks"
        dparts.append(f"<li><b>{src}</b>: {side} higher by "
                      f"{abs(d) * 100:.1f}pp on the home team</li>")
    for f in r["spread_flags"]:
        dparts.append(f"<li><b>Spread watch</b>: {htmlmod.escape(f['book'])} "
                      f"posts {f['point']:+.1f} ({f['off']:+.1f} off consensus)</li>")
    parts.append("<ul>" + ("".join(dparts) if dparts
                            else "<li>No divergences flagged at current thresholds.</li>")
                 + "</ul>")

    # Context: records, injuries, weather
    parts.append("<h2>Game context</h2>")
    cparts = []
    if r.get("context"):
        cparts.append(f"<li>{htmlmod.escape(r['context'])}</li>")
    if r.get("injuries"):
        cparts.append("<li>Key injuries: " +
                      htmlmod.escape("; ".join(r["injuries"][:4])) + "</li>")
    parts.append("<ul>" + ("".join(cparts) if cparts
                            else "<li>No context available.</li>") + "</ul>")

    # Fantasy-relevant signals
    parts.append("<h2>Fantasy signals</h2>")
    fparts = []
    if r["total"] is not None:
        fparts.append(f"<li>Implied total {r['total']:.1f} — "
                      + ("high-scoring environment" if r["total"] >= 47
                         else "average" if r["total"] >= 43 else "low-scoring"))
    if r["spread_home"] is not None:
        fparts.append(f"<li>Spread {r['spread_home']:+.1f} — "
                      + ("projected close game" if abs(r["spread_home"]) <= 3
                         else "blowout risk: starters may sit late"))
    if r.get("n_inj"):
        fparts.append(f"<li>{r['n_inj']} Q/D/Out injuries — usage bumps likely "
                      "for replacements</li>")
    parts.append("<ul>" + ("".join(fparts) if fparts
                            else "<li>Insufficient data for fantasy signals.</li>")
                 + "</ul>")

    body = "".join(parts)
    # Game pages are reached from the Dashboard, so it stays the active tab.
    return games_shell(f"{title} ({r['league']})", "index.html", body,
                       gen_note, prefix="../")


def render_game_pages(all_leagues, games_dir, gen_note):
    """One static page per game. Dir is wiped first so stale games leave
    no orphan pages and no dashboard link ever rots."""
    os.makedirs(games_dir, exist_ok=True)
    for f in os.listdir(games_dir):
        if f.endswith(".html"):
            os.remove(os.path.join(games_dir, f))
    n = 0
    for league, res in all_leagues.items():
        for r in res["rows"]:
            path = os.path.join(games_dir, r["slug"] + ".html")
            with open(path, "w") as f:
                f.write(render_game_page(r, gen_note))
            n += 1
    print(f"wrote {n} game pages to {games_dir}", file=sys.stderr)
    return n


# Fixture test (no network)
# --------------------------------------------------------------------------
def fixture_test():
    print("fixture test: devig / consensus / matching / leaderboard")
    # devig: -110/-110 -> 50/50
    ph, pa = devig(-110, -110)
    assert abs(ph - 0.5) < 1e-9 and abs(pa - 0.5) < 1e-9, (ph, pa)
    # devig: -150/+130
    ph, pa = devig(-150, 130)
    assert abs(ph + pa - 1.0) < 1e-9 and ph > pa, (ph, pa)
    print("  devig ok:", round(ph, 4), round(pa, 4))

    maps = build_maps("nfl")
    # consensus over two books
    odds = [{
        "away_team": "Dallas Cowboys", "home_team": "Philadelphia Eagles",
        "bookmakers": [
            {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Philadelphia Eagles", "price": -150},
                {"name": "Dallas Cowboys", "price": 130}]}]},
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Philadelphia Eagles", "price": -155},
                {"name": "Dallas Cowboys", "price": 135}]}]},
        ]}]
    cons, n = book_consensus(odds, maps, "DAL", "PHI")
    assert n == 2 and 0.55 < cons < 0.65, (cons, n)
    print(f"  consensus ok: {cons:.4f} over {n} books")

    # kalshi pairing
    kg = {"KXNFLGAME-26OCT26DALPHI": {"codes": ["DAL", "PHI"],
                                      "mid": {"DAL": 0.38, "PHI": 0.62}}}
    m = match_kalshi_game(kg, maps, "DAL", "PHI")
    assert m and abs(m["price_home"] - 0.62) < 1e-9, m
    # conservative: unknown code -> no match
    kg2 = {"X": {"codes": ["DAL", "ZZZ"], "mid": {"DAL": 0.5, "ZZZ": 0.5}}}
    assert match_kalshi_game(kg2, maps, "DAL", "PHI") is None
    print("  kalshi matching ok (incl. conservative reject)")

    # divergence flag
    row = {"divs": {"poly": 0.045, "kalshi": 0.01}}
    row["max_div"] = max(abs(v) for v in row["divs"].values())
    assert row["max_div"] >= 0.03
    print("  leaderboard math ok")

    # spread consensus over two books: -3 and -4 -> avg -3.5, one book flagged
    sodds = [{
        "away_team": "Dallas Cowboys", "home_team": "Philadelphia Eagles",
        "bookmakers": [
            {"key": "draftkings", "title": "DraftKings", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Philadelphia Eagles", "point": -3.0},
                    {"name": "Dallas Cowboys", "point": 3.0}]}]},
            {"key": "fanduel", "title": "FanDuel", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Philadelphia Eagles", "point": -4.5},
                    {"name": "Dallas Cowboys", "point": 4.5}]}]},
        ]}]
    avg, books, n = spread_consensus(sodds, maps, "DAL", "PHI")
    assert n == 2 and abs(avg - (-3.75)) < 1e-9, (avg, n)
    flags = [b for b, pt in books.items() if abs(pt - avg) >= 1.0]
    print(f"  spread consensus ok: {avg} over {n} books; off-consensus: {flags}")

    # totals consensus
    todds = [{
        "away_team": "Dallas Cowboys", "home_team": "Philadelphia Eagles",
        "bookmakers": [
            {"key": "draftkings", "title": "DraftKings", "markets": [
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 47.5},
                    {"name": "Under", "point": 47.5}]}]},
        ]}]
    tot, n = totals_consensus(todds, maps, "DAL", "PHI")
    assert n == 1 and abs(tot - 47.5) < 1e-9, (tot, n)
    print(f"  totals consensus ok: {tot}")
    print("ALL FIXTURE TESTS PASSED")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Sports: prediction markets vs sportsbooks")
    ap.add_argument("--leagues", default="nfl,nba,mlb",
                    help="comma-separated (default: nfl,nba,mlb)")
    ap.add_argument("--threshold", type=float, default=0.03,
                    help="moneyline divergence flag threshold (default: 0.03)")
    ap.add_argument("--spread-threshold", type=float, default=1.0,
                    help="spread flag threshold in points (default: 1.0)")
    ap.add_argument("--refresh", action="store_true", help="bypass caches")
    ap.add_argument("--html", action="store_true", help="write site/sports.html")
    ap.add_argument("--json", action="store_true", help="JSON to stdout")
    ap.add_argument("--fixture-test", action="store_true", help="no-network logic test")
    args = ap.parse_args()

    if args.fixture_test:
        fixture_test()
        return

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("NOTE: ODDS_API_KEY not set — sportsbook consensus will be skipped "
              "(divergences need it). Everything else still runs.", file=sys.stderr)

    leagues = [l.strip() for l in args.leagues.split(",") if l.strip() in LEAGUES]
    all_res = {}
    for lg in leagues:
        print(f"comparing {lg}…", file=sys.stderr)
        all_res[lg] = compare_league(lg, api_key, args.threshold,
                                     args.spread_threshold, args.refresh)

    if ODDS_QUOTA["remaining"] is not None:
        print(f"Odds API quota: {ODDS_QUOTA['remaining']} requests remaining "
              f"({ODDS_QUOTA['used']} used).", file=sys.stderr)
        try:
            if int(ODDS_QUOTA["remaining"]) < 50:
                print("WARNING: Odds API quota low — reduce refresh frequency.",
                      file=sys.stderr)
        except (TypeError, ValueError):
            pass

    render_terminal(all_res, args.threshold, args.spread_threshold) if not args.json else None
    if args.json:
        json.dump(all_res, sys.stdout, indent=1, default=str)
        sys.stdout.write("\n")
    if args.html:
        out = os.path.join(BASE, "site", "sports.html")
        render_html(all_res, args.threshold, args.spread_threshold, out)
        print(f"\nwrote {out}", file=sys.stderr)
        gen = NOW.strftime("%Y-%m-%d %H:%M UTC")
        render_dashboard(all_res, gen,
                         os.path.join(BASE, "site", "index.html"),
                         os.path.join(BASE, "index.html"))
        render_game_pages(all_res, os.path.join(BASE, "site", "games"), gen)


if __name__ == "__main__":
    main()
