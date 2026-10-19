#!/usr/bin/env python3
"""
PolyScope "Data" page — everything we track, by source, in one place.

One tab per source: ESPN, Polymarket, Kalshi, Sportsbooks (The Odds API),
balldontlie, Open-Meteo. Each tab carries a one-line description, its
limitations, a "data as of" timestamp, and the raw-ish tables.

Usage:
    python3 data_page.py --html            # write site/data.html
    python3 data_page.py --refresh --html  # bypass caches

Free sources need no key. The Odds API reads ODDS_API_KEY from the
environment; balldontlie needs a free key (BALLDONTLIE_API_KEY) and is
shown as not-configured until one exists.
"""
import argparse
import datetime as dt
import html as htmlmod
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import screener  # noqa: E402
import sports_compare as sc  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)
DATA_TTL = 6 * 3600
TOP_N = 50

TABS = [
    ("espn", "ESPN"),
    ("polymarket", "Polymarket"),
    ("kalshi", "Kalshi"),
    ("sportsbooks", "Sportsbooks"),
    ("balldontlie", "balldontlie"),
    ("weather", "Open-Meteo"),
]

SOURCE_INFO = {
    "espn": ("Schedules, scores, team records, venues, and injury reports "
             "via ESPN's unofficial API.",
             "Unofficial and undocumented — can change or break without notice. "
             "Injury details are brief; statuses (Questionable/Out) are the reliable part."),
    "polymarket": ("Open prediction markets with Yes prices, volume, and close dates.",
                   "API lists only active markets (survivorship bias). Prices are "
                   "Yes-token mids; thin markets can be stale or gamed."),
    "kalshi": ("Open Kalshi markets with Yes mids from the orderbook.",
               "No price-history API, so no movers screen. Near-term listings are "
               "flooded with provisional sports-combo scaffolds (filtered here)."),
    "sportsbooks": ("US sportsbook moneylines (h2h) via The Odds API, de-vigged "
                    "into consensus probabilities.",
                    "Free tier: 500 credits/month, NFL/NBA/MLB h2h only. Needs a free "
                    "API key. Odds move fast — this is a snapshot, not a live feed."),
    "balldontlie": ("NBA stats and history — team records, standings, game logs.",
                    "Requires a free API key (set BALLDONTLIE_API_KEY). Not yet "
                    "wired in — shown here as a placeholder until configured."),
    "weather": ("Kickoff weather (temp, precipitation chance, wind) for outdoor "
                "NFL games via Open-Meteo.",
                "Forecast, not observed weather — check game day. Dome/retractable-roof "
                "games are skipped; only games ESPN flags as outdoor get a row."),
}


def cached(key, ttl, fn):
    hit = None if getattr(sys, "_refresh", False) else sc.cache_get(key, ttl)
    if hit is not None:
        return hit, True
    val = fn()
    sc.cache_set(key, val)
    return val, False


def get_espn():
    out = {}
    for league in ("nfl", "nba"):
        maps = sc.build_maps(league)
        games = sc.fetch_espn_games(league, maps)
        inj = sc.fetch_espn_injuries(league, maps)
        out[league] = {"games": games, "injuries": inj}
    return out


def get_polymarket():
    evs = screener.fetch_polymarket(max_events=2000)
    recs = screener.normalize_polymarket(evs)
    recs.sort(key=lambda r: r["volume"] or 0, reverse=True)
    return recs[:TOP_N]


def get_kalshi():
    mkts = screener.fetch_kalshi(max_markets=2000)
    recs = screener.normalize_kalshi(mkts)
    recs.sort(key=lambda r: r["volume"] or 0, reverse=True)
    return recs[:TOP_N]


def get_sportsbooks():
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        return {"configured": False}
    out = {"configured": True, "leagues": {}}
    for league in ("nfl", "nba"):
        data, _ = sc.fetch_odds(league, key)
        rows = []
        for ev in (data or [])[:25]:
            books = []
            for bm in ev.get("bookmakers", [])[:6]:
                for mkt in bm.get("markets", []):
                    if mkt.get("key") == "h2h":
                        outs = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                        books.append((bm.get("title") or bm.get("key"),
                                      outs.get(ev.get("home_team")), outs.get(ev.get("away_team"))))
            rows.append({"away": ev.get("away_team"), "home": ev.get("home_team"),
                         "commence": (ev.get("commence_time") or "")[:16].replace("T", " "),
                         "books": books})
        out["leagues"][league] = rows
    return out


def get_weather():
    maps = sc.build_maps("nfl")
    games = sc.fetch_espn_games("nfl", maps)
    rows = []
    for g in games:
        if g.get("indoor") is not False:
            continue
        coords = sc.game_coords("nfl", g["home"], g.get("venue"))
        if not coords:
            continue
        wx = sc.fetch_weather(*coords, g.get("date"))
        if wx:
            rows.append({"game": f"{g['away']} @ {g['home']}",
                         "venue": g.get("venue"),
                         "kickoff": (g.get("date") or "")[:16].replace("T", " "),
                         **wx})
    return rows


def pct(x):
    return f"{x*100:.1f}%" if x is not None else "n/a"


def money(x):
    if x is None:
        return "n/a"
    try:
        return f"${x:,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def esc(x):
    return htmlmod.escape(str(x if x is not None else "—"))


def tab_espn(data):
    h = []
    for league, d in data.items():
        h.append(f"<h3>{league.upper()} — this week</h3><table>"
                 "<tr><th>Game</th><th>Kickoff (UTC)</th><th>Venue</th>"
                 "<th>Records</th><th>Status</th><th>Key injuries</th></tr>")
        for g in d["games"][:20]:
            inj = d["injuries"].get(g["away"], {})
            inj2 = d["injuries"].get(g["home"], {})
            notable = (inj.get("notable", []) + inj2.get("notable", []))[:3]
            h.append(
                f"<tr><td><b>{esc(g['away'])} @ {esc(g['home'])}</b></td>"
                f"<td>{esc((g.get('date') or '')[:16].replace('T', ' '))}</td>"
                f"<td>{esc(g.get('venue'))}</td>"
                f"<td>{esc(g.get('away_record'))} / {esc(g.get('home_record'))}</td>"
                f"<td>{esc(g.get('status'))}</td>"
                f"<td class='ctx'>{esc('; '.join(notable) if notable else '—')}</td></tr>")
        h.append("</table>")
    return "".join(h)


def tab_markets(recs, source):
    h = ["<table><tr><th>Market</th><th>Yes</th><th>Volume</th>"
         "<th>Closes</th></tr>"]
    for r in recs:
        h.append(
            f"<tr><td class='q'><a href='{esc(r['url'])}' target='_blank' "
            f"rel='noopener'>{esc(r['question'][:90])}</a></td>"
            f"<td>{pct(r['yes'])}</td><td>{money(r['volume'])}</td>"
            f"<td>{esc(str(r['closes'])[:10] if r['closes'] else '—')}</td></tr>")
    h.append("</table>")
    h.append(f"<p class='dim'>Top {len(recs)} by volume. Full explorer on the "
             f"<a href='markets.html'>Markets</a> page.</p>")
    return "".join(h)


def tab_sportsbooks(data):
    if not data.get("configured"):
        return ("<div class='notice'>⚠️ Sportsbook odds not configured — set the "
                "<b>ODDS_API_KEY</b> environment variable (free key at "
                "the-odds-api.com) to populate this tab. The divergence leaderboard "
                "on the <a href='sports.html'>Sports</a> page needs it too.</div>")
    h = []
    for league, rows in data["leagues"].items():
        h.append(f"<h3>{league.upper()} — h2h moneylines by book</h3>")
        for r in rows[:15]:
            h.append(f"<p><b>{esc(r['away'])} @ {esc(r['home'])}</b> "
                     f"<span class='dim'>{esc(r['commence'])}</span></p><table>"
                     "<tr><th>Book</th><th>Home odds</th><th>Away odds</th></tr>")
            for bname, ho, ao in r["books"]:
                h.append(f"<tr><td>{esc(bname)}</td><td>{esc(ho)}</td><td>{esc(ao)}</td></tr>")
            h.append("</table>")
    return "".join(h)


def tab_balldontlie():
    key = os.environ.get("BALLDONTLIE_API_KEY")
    if not key:
        return ("<div class='notice'>🏀 balldontlie needs a free API key "
                "(set <b>BALLDONTLIE_API_KEY</b>). Once configured, this tab will "
                "show NBA team records, standings, and recent form as a second "
                "opinion next to ESPN.</div>"
                "<p class='dim'>Planned tables: standings by conference, "
                "last-10 form per team, season averages for key players.</p>")
    return "<p>balldontlie key present — tables coming in the next build.</p>"


def tab_weather(rows):
    if not rows:
        return "<p>No outdoor NFL games upcoming (or all indoors this week).</p>"
    h = ["<table><tr><th>Game</th><th>Venue</th><th>Kickoff (UTC)</th>"
         "<th>Temp</th><th>Precip %</th><th>Wind</th></tr>"]
    for r in rows:
        h.append(f"<tr><td><b>{esc(r['game'])}</b></td><td>{esc(r['venue'])}</td>"
                 f"<td>{esc(r['kickoff'])}</td><td>{r['temp_f']:.0f}°F</td>"
                 f"<td>{r['precip_prob']}%</td><td>{r['wind_mph']:.0f} mph</td></tr>")
    h.append("</table>")
    return "".join(h)


def render(data, stamps, path):
    gen = NOW.strftime("%Y-%m-%d %H:%M UTC")
    nav = "".join(
        f'<a href="{h}">{l}</a>' for h, l in
        [("index.html", "Dashboard"), ("markets.html", "Markets"),
         ("stats.html", "Stats"), ("sports.html", "Sports"),
         ("data.html", "Data"), ("about.html", "About")])
    tab_btns, tab_divs = [], []
    bodies = {
        "espn": tab_espn(data["espn"]),
        "polymarket": tab_markets(data["polymarket"], "poly"),
        "kalshi": tab_markets(data["kalshi"], "kalshi"),
        "sportsbooks": tab_sportsbooks(data["sportsbooks"]),
        "balldontlie": tab_balldontlie(),
        "weather": tab_weather(data["weather"]),
    }
    for i, (key, label) in enumerate(TABS):
        desc, limits = SOURCE_INFO[key]
        tab_btns.append(
            f"<button class='tabbtn{' on' if i == 0 else ''}' "
            f"onclick=\"showTab('{key}')\">{label}</button>")
        tab_divs.append(
            f"<div class='tab' id='tab-{key}' style='display:{'block' if i == 0 else 'none'}'>"
            f"<p class='lede'>{esc(desc)}</p>"
            f"<p class='dim'>⚠️ {esc(limits)}</p>"
            f"<p class='dim'>📡 Data as of {esc(stamps.get(key, gen))}</p>"
            f"{bodies[key]}</div>")
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data — PolyScope</title>
<link rel="stylesheet" href="styles.css">
<style>
.tabbtn {{ background: #1a2332; color: #e7e9ea; border: 1px solid #2f3b4d;
border-radius: 8px 8px 0 0; padding: 10px 16px; cursor: pointer; font-size: 14px; }}
.tabbtn.on {{ background: #2f3b4d; border-bottom: 2px solid #1d9bf0; }}
.tabbtn:hover {{ border-color: #1d9bf0; }}
.tabbar {{ display: flex; gap: 4px; flex-wrap: wrap; margin: 20px 0 0; }}
.tab {{ border: 1px solid #2f3b4d; border-radius: 0 8px 8px 8px; padding: 20px; }}
.lede {{ color: #aab4c0; }} .ctx {{ font-size: 12px; color: #8b98a5; }}
.dim {{ color: #8b98a5; font-size: 12px; }}
.notice {{ background: #1a2332; border: 1px solid #2f3b4d; border-radius: 8px;
padding: 12px 16px; margin: 16px 0; font-size: 13px; color: #8b98a5; }}
</style>
<script>
function showTab(k) {{
  document.querySelectorAll('.tab').forEach(t => t.style.display = 'none');
  document.querySelectorAll('.tabbtn').forEach(b => b.classList.remove('on'));
  document.getElementById('tab-' + k).style.display = 'block';
  event.target.classList.add('on');
}}
</script></head>
<body>
<header class="site-header"><div class="wrap head-row">
<a class="brand" href="index.html">◈ Poly<span>Scope</span></a>
<nav>{nav}</nav></div></header>
<main class="wrap">
<div class="freshness">📡 Page generated {gen} · every tab stamped with its own data time ·
free sources only · informational, not betting advice · 21+ · 1-800-GAMBLER</div>
<h1>Data, by source</h1>
<p class="lede">Everything PolyScope tracks, straight from each tap — no blending,
no hiding. Each tab says what the source is and where it lies to you.</p>
<div class="tabbar">{"".join(tab_btns)}</div>
{"".join(tab_divs)}
</main>
<footer class="wrap"><p>Sources: ESPN · Polymarket · Kalshi · The Odds API ·
balldontlie · Open-Meteo · <a href="about.html">Methodology</a></p></footer>
</body></html>"""
    with open(path, "w") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser(description="Build the Data reference page")
    ap.add_argument("--html", action="store_true", help="write site/data.html")
    ap.add_argument("--refresh", action="store_true", help="bypass caches")
    args = ap.parse_args()
    sys._refresh = args.refresh

    stamps, data = {}, {}
    print("pulling ESPN…", file=sys.stderr)
    data["espn"], _ = cached("data_espn.json", DATA_TTL, get_espn)
    stamps["espn"] = NOW.strftime("%Y-%m-%d %H:%M UTC")
    print("pulling Polymarket…", file=sys.stderr)
    data["polymarket"], _ = cached("data_poly.json", DATA_TTL, get_polymarket)
    stamps["polymarket"] = NOW.strftime("%Y-%m-%d %H:%M UTC")
    print("pulling Kalshi…", file=sys.stderr)
    data["kalshi"], _ = cached("data_kalshi.json", DATA_TTL, get_kalshi)
    stamps["kalshi"] = NOW.strftime("%Y-%m-%d %H:%M UTC")
    print("pulling sportsbooks…", file=sys.stderr)
    data["sportsbooks"], _ = cached("data_odds.json", sc.ODDS_TTL, get_sportsbooks)
    stamps["sportsbooks"] = NOW.strftime("%Y-%m-%d %H:%M UTC")
    print("pulling weather…", file=sys.stderr)
    data["weather"], _ = cached("data_wx.json", DATA_TTL, get_weather)
    stamps["weather"] = NOW.strftime("%Y-%m-%d %H:%M UTC")
    # balldontlie: status only, no fetch yet
    stamps["balldontlie"] = NOW.strftime("%Y-%m-%d %H:%M UTC")

    if args.html:
        out = os.path.join(BASE, "site", "data.html")
        render(data, stamps, out)
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(json.dumps({k: f"{len(v) if isinstance(v, list) else 'n/a'} rows"
                          for k, v in data.items()}, indent=1))


if __name__ == "__main__":
    main()
