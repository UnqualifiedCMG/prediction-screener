#!/usr/bin/env python3
"""
PolyScope Fantasy tab — transparent fantasy projections for NFL, NBA, MLB.

The model is deliberately simple and fully documented on the page — no
black boxes. Per league:

  NFL (in season): projected PPR =
      base = 0.65 * last-5 PPR + 0.35 * season PPR
      total_adj = (game_total - 44.0) * 0.10        # higher totals help everyone
      spread_adj: team favored by 7+  -> RB *1.08, QB *0.95
                  team underdog by 7+ -> WR/TE *1.05
      usage: +12% if a teammate at the same position is Out/Doubtful
    Cross-referenced with the site's own data: sportsbook totals/spreads
    (The Odds API), ESPN injuries, Polymarket game price.

  NBA (2026-27 hasn't tipped off): 2025-26 per-game averages as the labeled
    baseline. No fake projections — actuals, clearly marked.
    Fantasy score = pts + 1.2*reb + 1.5*ast + 3*stl + 3*blk_stl
    (Sleeper combines blocks+steals in blk_stl; documented on page.)

  MLB (2026 season complete): final actuals only — top hitters and pitchers
    from ESPN's leaders endpoint. No projections; the season is over.

Free sources only: Sleeper API (players + weekly stats, no key), ESPN
unofficial + core APIs (no key), The Odds API (optional — needs free key;
without it, total/spread adjustments are skipped and labeled as such).

Everything is informational estimates, not betting advice.
"""
import argparse
import datetime as dt
import html as htmlmod
import json
import os
import re
import sys
import time

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import sports_compare as sc  # noqa: E402  (ESPN games/injuries, odds, maps)

NOW = dt.datetime.now(dt.timezone.utc)
CACHE_DIR = os.path.join(BASE, "hidden_cache", "fantasy")
os.makedirs(CACHE_DIR, exist_ok=True)

SLEEPER = "https://api.sleeper.app/v1"
ESPN_CORE_MLB = "https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/seasons/2026"
TOP_N = 100

NAV = [("index.html", "Dashboard"), ("markets.html", "Markets"),
       ("backtests.html", "Backtests"), ("sports.html", "Sports"),
       ("fantasy.html", "Fantasy"), ("data.html", "Data"), ("about.html", "About")]

DISCLAIMER = ("Projections are informational estimates, not betting advice. "
              "21+. Gambling problem? Call 1-800-GAMBLER.")


# --------------------------------------------------------------------------
# cache + http
# --------------------------------------------------------------------------
def _cache_get(name, ttl):
    p = os.path.join(CACHE_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            d = json.load(f)
        if (NOW - dt.datetime.fromisoformat(d["ts"])).total_seconds() > ttl:
            return None
        return d["payload"]
    except Exception:
        return None


def _cache_set(name, payload):
    try:
        with open(os.path.join(CACHE_DIR, name), "w") as f:
            json.dump({"ts": NOW.isoformat(), "payload": payload}, f)
    except Exception:
        pass


def _get_json(url, params=None, timeout=30):
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            time.sleep(0.25)
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.0)
    raise RuntimeError(f"GET failed for {url}: {last}")


# --------------------------------------------------------------------------
# Sleeper: players + weekly stats
# --------------------------------------------------------------------------
def sleeper_players(sport, refresh=False):
    key = f"sleeper_players_{sport}.json"
    if not refresh:
        hit = _cache_get(key, 7 * 86400)
        if hit is not None:
            return hit
    d = _get_json(f"{SLEEPER}/players/{sport}")
    _cache_set(key, d)
    return d


def sleeper_week_stats(sport, season, season_type, week, refresh=False):
    key = f"sleeper_stats_{sport}_{season}_{season_type}_{week}.json"
    if not refresh:
        hit = _cache_get(key, 7 * 86400)
        if hit is not None:
            return hit
    try:
        d = _get_json(f"{SLEEPER}/stats/{sport}/{season_type}/{season}/{week}")
    except Exception:
        d = {}
    _cache_set(key, d)
    return d


def sleeper_nfl_state(refresh=False):
    key = "sleeper_nfl_state.json"
    if not refresh:
        hit = _cache_get(key, 86400)
        if hit is not None:
            return hit
    d = _get_json(f"{SLEEPER}/state/nfl")
    _cache_set(key, d)
    return d


# --------------------------------------------------------------------------
# NFL
# --------------------------------------------------------------------------
NFL_POSITIONS = ("QB", "RB", "WR", "TE")
NFL_TEAM_ALIASES = {"OAK": "LV"}  # stale codes in Sleeper data


def _sleeper_team_nfl(code, maps):
    code = NFL_TEAM_ALIASES.get(code, code)
    return code if code in maps["abbr"] else None


def nfl_projections(refresh=False):
    """Returns (rows, meta). Each row: player, pos, team, opp, proj, last5,
    season, status, signals."""
    maps = sc.build_maps("nfl")
    players = sleeper_players("nfl", refresh)
    state = sleeper_nfl_state(refresh)
    season = state.get("season", "2026")
    cur_week = int(state.get("display_week") or state.get("week") or 6)

    # weekly PPR for completed weeks (weeks with real stat payloads)
    weeks = {}
    for w in range(1, cur_week + 1):
        d = sleeper_week_stats("nfl", season, "regular", w, refresh)
        if d and any("pts_ppr" in v for v in d.values()):
            weeks[w] = d
    done_weeks = sorted(weeks)
    if not done_weeks:
        return [], {"error": "no completed weeks yet"}

    # this week's games: team -> (opp, date, home_flag)
    games = sc.fetch_espn_games("nfl", maps, refresh)
    team_game = {}
    for g in games:
        team_game[g["away"]] = (g["home"], g["date"], False)
        team_game[g["home"]] = (g["away"], g["date"], True)

    injuries = sc.fetch_espn_injuries("nfl", maps, refresh)

    # odds: totals + spreads per game (needs key; else skip gracefully)
    odds_data = None
    api_key = os.environ.get("ODDS_API_KEY")
    if api_key:
        try:
            odds_data, _ = sc.fetch_odds("nfl", api_key, refresh)
        except Exception:
            odds_data = None
    game_lines = {}
    if odds_data:
        for g in games:
            tot, _ = sc.totals_consensus(odds_data, maps, g["away"], g["home"])
            spr, _, _ = sc.spread_consensus(odds_data, maps, g["away"], g["home"])
            game_lines[(g["away"], g["home"])] = (tot, spr)

    # Polymarket game price (blowout vs close read)
    poly_price = {}
    for g in games:
        try:
            a_short = maps["abbr"][g["away"]][2]
            h_short = maps["abbr"][g["home"]][2]
            pm = sc.fetch_polymarket_game("nfl", a_short, h_short, refresh)
            if pm:
                poly_price[(g["away"], g["home"])] = pm["price_home"]
        except Exception:
            pass

    # candidate pool: skill positions, active team, ranked by search_rank
    cands = []
    for pid, p in players.items():
        if p.get("position") not in NFL_POSITIONS:
            continue
        team = p.get("team")
        if not team:
            continue
        team = _sleeper_team_nfl(team, maps)
        if not team:
            continue
        sr = p.get("search_rank")
        cands.append((sr if sr is not None else 9999, pid, p, team))
    cands.sort(key=lambda x: x[0])
    cands = cands[:TOP_N]

    pos_group = {"QB": "QB", "RB": "RB", "WR": "WRTE", "TE": "WRTE"}
    rows = []
    for _, pid, p, team in cands:
        pos = p["position"]
        # per-week PPR series
        series = []
        for w in done_weeks:
            v = weeks[w].get(pid, {}).get("pts_ppr")
            if v is not None:
                series.append(float(v))
        if not series:
            continue
        last5 = sum(series[-5:]) / len(series[-5:])
        season_avg = sum(series) / len(series)
        base = 0.65 * last5 + 0.35 * season_avg

        signals = []
        proj, played = None, True
        gm = team_game.get(team)
        if gm is None:
            played = False
            signals.append("bye week")
            opp = "—"
        else:
            opp, gdate, is_home = gm
            opp = f"{'vs' if is_home else '@'} {opp}"
            # game lines keyed by (away, home)
            away_t, home_t = (team, gm[0]) if is_home else (gm[0], team)
            tot, spr = game_lines.get((away_t, home_t), (None, None))
            proj = base
            if tot is not None:
                adj = (tot - 44.0) * 0.10
                proj += adj
                signals.append(f"total {tot:.1f}")
            else:
                signals.append("total n/a (no odds key)")
            if spr is not None:
                team_spr = spr if is_home else -spr  # negative = favored
                signals.append(f"spread {spr:+.1f}")
                if team_spr <= -7:
                    if pos == "RB":
                        proj *= 1.08
                        signals.append("big favorite: RB +8%")
                    elif pos == "QB":
                        proj *= 0.95
                        signals.append("big favorite: QB -5%")
                elif team_spr >= 7 and pos in ("WR", "TE"):
                    proj *= 1.05
                    signals.append("big underdog: WR/TE +5%")
            pp = poly_price.get((away_t, home_t))
            if pp is not None:
                signals.append(f"Poly {pp*100:.0f}%")

        # usage bump: teammate at same position Out/Doubtful
        inj = injuries.get(team, {})
        bumped = False
        for note in inj.get("notable", []):
            m = re.search(r"\((\w+),\s*(Out|Doubtful)\)", note)
            if m and pos_group.get(m.group(1), m.group(1)) == pos_group[pos]:
                if played and proj is not None:
                    proj *= 1.12
                signals.append(f"teammate {m.group(1)} {m.group(2).lower()} (+12%)")
                bumped = True
                break

        status = p.get("injury_status") or "—"
        rows.append({
            "player": p.get("full_name", "?"), "pos": pos, "team": team,
            "opp": opp, "proj": round(proj, 1) if proj is not None else None,
            "last5": round(last5, 1), "season": round(season_avg, 1),
            "status": status, "signals": "; ".join(signals) if signals else "—",
            "_sort": proj if proj is not None else -1,
        })
    rows.sort(key=lambda r: -r["_sort"])
    meta = {"weeks": done_weeks, "season": season, "week": cur_week,
            "odds": bool(odds_data)}
    return rows, meta


# --------------------------------------------------------------------------
# NBA (2026-27 hasn't tipped off — 2025-26 per-game averages as baseline)
# --------------------------------------------------------------------------
def nba_baseline(refresh=False):
    """Returns (rows, meta). Baseline = 2025-26 regular-season per-game avg."""
    players = sleeper_players("nba", refresh)
    # gather 2025 regular-season weeks until empty
    seasons = {}
    for w in range(1, 30):
        d = sleeper_week_stats("nba", 2025, "regular", w, refresh)
        if not d:
            break
        seasons[w] = d
    if not seasons:
        return [], {"error": "no 2025-26 data"}

    def fp(v):
        return (v.get("pts", 0) + 1.2 * v.get("reb", 0) + 1.5 * v.get("ast", 0)
                + 3 * v.get("stl", 0) + 3 * v.get("blk_stl", 0))

    cands = []
    for pid, p in players.items():
        if p.get("position") not in ("PG", "SG", "SF", "PF", "C"):
            continue
        if not p.get("team"):
            continue
        sr = p.get("search_rank")
        cands.append((sr if sr is not None else 9999, pid, p))
    cands.sort(key=lambda x: x[0])
    cands = cands[:TOP_N]

    rows = []
    for _, pid, p in cands:
        games, total = 0, 0.0
        for w, d in seasons.items():
            v = d.get(pid)
            if v and v.get("pts") is not None:
                games += 1
                total += fp(v)
        if games < 10:
            continue
        avg = total / games
        rows.append({
            "player": p.get("full_name", "?"), "pos": p.get("position"),
            "team": p.get("team"), "proj": round(avg, 1),
            "games": games, "status": p.get("injury_status") or "—",
        })
    rows.sort(key=lambda r: -r["proj"])
    return rows, {"season": "2025-26", "note": "2026-27 season hasn't tipped off"}


# --------------------------------------------------------------------------
# MLB (2026 season complete — final actuals, no projections)
# --------------------------------------------------------------------------
def _resolve_athlete(ref, refresh=False):
    key = "mlb_ath_" + re.sub(r"\D", "", ref)[-8:] + ".json"
    hit = None if refresh else _cache_get(key, 30 * 86400)
    if hit is not None:
        return hit
    try:
        d = _get_json(ref, timeout=20)
    except Exception:
        return {"name": "?", "team": "?"}
    tabbr = "?"
    team = d.get("team")
    tref = team.get("$ref") if isinstance(team, dict) else None
    if tref:
        tkey = "mlb_team_" + re.sub(r"\D", "", tref)[-6:] + ".json"
        thit = None if refresh else _cache_get(tkey, 30 * 86400)
        if thit is None:
            try:
                thit = _get_json(tref, timeout=20)
                _cache_set(tkey, thit)
            except Exception:
                thit = {}
        tabbr = thit.get("abbreviation") or "?"
    out = {"name": d.get("displayName", "?"), "team": tabbr}
    _cache_set(key, out)
    return out


def mlb_finals(refresh=False):
    """Top hitters (OPS) and pitchers (K) — 2026 regular-season actuals."""
    cats = {c["name"]: c for c in
            _get_json(ESPN_CORE_MLB + "/types/2/leaders").get("categories", [])}
    by_ath = {}
    for cat in ("ERA", "WHIP"):
        for l in cats.get(cat, {}).get("leaders", []):
            aid = re.sub(r"\D", "", l["athlete"]["$ref"])[-8:]
            by_ath.setdefault(aid, {})[cat] = l.get("value")

    hitters, pitchers = [], []
    for l in cats.get("OPS", {}).get("leaders", [])[:25]:
        a = _resolve_athlete(l["athlete"]["$ref"], refresh)
        dv = l.get("displayValue") or ""
        m = re.search(r"(\d+) HR.*?(\d+) RBI.*?(\d+) R.*?(\d+) SB", dv)
        hitters.append({
            "player": a["name"], "team": a["team"],
            "avg": f"{l.get('value', 0):.3f}",
            "hr": m.group(1) if m else "—", "rbi": m.group(2) if m else "—",
            "r": m.group(3) if m else "—", "sb": m.group(4) if m else "—",
            "ops": f"{cats['OPS']['leaders'] and l.get('value', 0):.3f}"
            if False else f"{l.get('value', 0):.3f}",
        })
    for l in cats.get("strikeouts", {}).get("leaders", [])[:15]:
        a = _resolve_athlete(l["athlete"]["$ref"], refresh)
        aid = re.sub(r"\D", "", l["athlete"]["$ref"])[-8:]
        era = by_ath.get(aid, {}).get("ERA")
        whip = by_ath.get(aid, {}).get("WHIP")
        m = re.search(r"([\d.]+) IP", l.get("displayValue") or "")
        pitchers.append({
            "player": a["name"], "team": a["team"],
            "k": int(l.get("value", 0)),
            "era": f"{era:.2f}" if era else "—",
            "whip": f"{whip:.3f}" if whip else "—",
            "ip": m.group(1) if m else "—",
        })
    pitchers.sort(key=lambda r: -r["k"])
    return hitters, pitchers


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _nav():
    return "".join(f'<a href="{h}">{l}</a>' for h, l in NAV)


def _th(headers):
    return "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"


def render(nfl_rows, nfl_meta, nba_rows, nba_meta, mlb_h, mlb_p, path):
    gen = NOW.strftime("%Y-%m-%d %H:%M UTC")
    nav = _nav()

    # NFL table
    nfl_trs = []
    for r in nfl_rows:
        proj = f"<b>{r['proj']:.1f}</b>" if r["proj"] is not None else "—"
        nfl_trs.append(
            f"<tr><td><b>{htmlmod.escape(r['player'])}</b></td><td>{r['pos']}</td>"
            f"<td>{r['team']}</td><td>{htmlmod.escape(r['opp'])}</td><td>{proj}</td>"
            f"<td>{r['last5']:.1f}</td><td>{r['season']:.1f}</td>"
            f"<td>{htmlmod.escape(r['status'])}</td>"
            f"<td class='ctx'>{htmlmod.escape(r['signals'])}</td></tr>")
    nfl_sec = f"""<h2>NFL — week {nfl_meta.get('week')} projections</h2>
<table><thead>{_th(["Player", "Pos", "Team", "Matchup", "Proj PPR",
"Last-5", "Season", "Status", "Signals"])}</thead>
<tbody>{"".join(nfl_trs) or "<tr><td colspan='9'>No data.</td></tr>"}</tbody></table>"""

    # NBA table
    nba_trs = []
    for r in nba_rows:
        nba_trs.append(
            f"<tr><td><b>{htmlmod.escape(r['player'])}</b></td><td>{r['pos']}</td>"
            f"<td>{r['team']}</td><td><b>{r['proj']:.1f}</b></td>"
            f"<td>{r['games']}</td><td>{htmlmod.escape(r['status'])}</td></tr>")
    nba_sec = f"""<h2>NBA — 2025-26 baseline</h2>
<p class="note">The 2026-27 season hasn't tipped off. These are last season's
per-game fantasy averages — a baseline for draft/rankings, not a week-1 projection.</p>
<table><thead>{_th(["Player", "Pos", "Team", "Baseline FP", "Games", "Status"])}</thead>
<tbody>{"".join(nba_trs) or "<tr><td colspan='6'>No data.</td></tr>"}</tbody></table>"""

    # MLB tables
    mlb_hrs = "".join(
        f"<tr><td><b>{htmlmod.escape(r['player'])}</b></td><td>{htmlmod.escape(r['team'])}</td>"
        f"<td>{r['avg']}</td><td>{r['hr']}</td><td>{r['rbi']}</td>"
        f"<td>{r['r']}</td><td>{r['sb']}</td><td>{r['ops']}</td></tr>"
        for r in mlb_h)
    mlb_prs = "".join(
        f"<tr><td><b>{htmlmod.escape(r['player'])}</b></td><td>{htmlmod.escape(r['team'])}</td>"
        f"<td>{r['ip']}</td><td>{r['k']}</td><td>{r['era']}</td><td>{r['whip']}</td></tr>"
        for r in mlb_p)
    mlb_sec = f"""<h2>MLB — 2026 final actuals</h2>
<p class="note">Season complete. Final regular-season numbers — actuals, not projections.</p>
<h3>Top hitters by OPS</h3>
<table><thead>{_th(["Player", "Team", "AVG", "HR", "RBI", "R", "SB", "OPS"])}</thead>
<tbody>{mlb_hrs or "<tr><td colspan='8'>No data.</td></tr>"}</tbody></table>
<h3>Top pitchers by strikeouts</h3>
<table><thead>{_th(["Player", "Team", "IP", "K", "ERA", "WHIP"])}</thead>
<tbody>{mlb_prs or "<tr><td colspan='6'>No data.</td></tr>"}</tbody></table>"""

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fantasy — PolyScope</title>
<link rel="stylesheet" href="styles.css">
<style>
.ctx{{font-size:12px;color:#8b98a5}} .note{{color:#8b98a5;font-size:13px}}
.method li{{margin-bottom:6px;color:#aab4c0}}
.notice{{background:#1a2332;border:1px solid #2f3b4d;border-radius:8px;
padding:12px 16px;margin:16px 0;font-size:13px;color:#8b98a5}}
table{{font-size:13px}}
</style></head>
<body>
<header class="site-header"><div class="wrap head-row">
<a class="brand" href="index.html">◈ Poly<span>Scope</span></a>
<nav>{nav}</nav></div></header>
<main class="wrap">
<div class="notice">🔞 21+. {htmlmod.escape(DISCLAIMER)}</div>
<div class="freshness">📡 Generated {gen} · Sleeper + ESPN free data ·
projections are estimates, not advice</div>
<h1>Fantasy</h1>
{nfl_sec}
{nba_sec}
{mlb_sec}
<h2>The model (no black boxes)</h2>
<ul class="method">
<li><b>NFL projected PPR</b> = 0.65 × last-5-game PPR average + 0.35 × season
PPR average, then: +(game_total − 44) × 0.10 for game environment.</li>
<li><b>Blowout adjustments</b> (from the sportsbook spread): team favored by 7+
→ RB ×1.08, QB ×0.95. Team underdog by 7+ → WR/TE ×1.05.</li>
<li><b>Usage bump</b>: ×1.12 when a teammate at the same position is listed
Out/Doubtful on the ESPN injury report.</li>
<li><b>Signals</b> show the inputs: game total, spread, Polymarket game price,
and which adjustments fired — so you can audit every number.</li>
<li><b>NBA</b>: 2025-26 per-game fantasy averages
(pts + 1.2·reb + 1.5·ast + 3·stl + 3·blk_stl; Sleeper bundles blocks+steals)
as a preseason baseline. Not a projection.</li>
<li><b>MLB</b>: final 2026 actuals only. No model, no projections.</li>
</ul>
<h2>Limitations</h2>
<ul class="method">
<li>Without an Odds API key, total/spread adjustments are skipped and the
projection is just form-based — the page says so per player.</li>
<li>Last-5 averages overweight recent hot streaks; rookies and returning
injured players have thin samples.</li>
<li>ESPN injury reports lag real news by hours. Verify before acting.</li>
<li>Sleeper's weekly stats can revise after stat corrections.</li>
</ul>
</main>
<footer class="wrap"><p>Data: Sleeper · ESPN · The Odds API · Polymarket.
Free tiers only. <a href="about.html">About</a></p></footer>
</body></html>"""
    with open(path, "w") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser(description="Build the Fantasy projections page")
    ap.add_argument("--html", action="store_true", help="write site/fantasy.html")
    ap.add_argument("--refresh", action="store_true", help="bypass caches")
    args = ap.parse_args()

    print("NFL projections…", file=sys.stderr)
    nfl_rows, nfl_meta = nfl_projections(args.refresh)
    print(f"  {len(nfl_rows)} players", file=sys.stderr)
    print("NBA baseline…", file=sys.stderr)
    nba_rows, nba_meta = nba_baseline(args.refresh)
    print(f"  {len(nba_rows)} players", file=sys.stderr)
    print("MLB finals…", file=sys.stderr)
    mlb_h, mlb_p = mlb_finals(args.refresh)
    print(f"  {len(mlb_h)} hitters, {len(mlb_p)} pitchers", file=sys.stderr)

    if args.html:
        out = os.path.join(BASE, "site", "fantasy.html")
        render(nfl_rows, nfl_meta, nba_rows, nba_meta, mlb_h, mlb_p, out)
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(json.dumps({
            "nfl": len(nfl_rows), "nba": len(nba_rows),
            "mlb_hitters": len(mlb_h), "mlb_pitchers": len(mlb_p),
            "nfl_meta": nfl_meta,
        }, indent=1))


if __name__ == "__main__":
    main()
