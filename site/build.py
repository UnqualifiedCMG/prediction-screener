#!/usr/bin/env python3
"""
PolyScope — static prediction-market tracking site generator.

Pulls live Polymarket sports data, runs the screener screens and the
backtest, and emits a static site to site/. Designed for cron regeneration:
fast (caches API data in hidden_cache/), idempotent (rewrites the same
files), and every page is stamped with its data timestamp.

Kalshi is parked (see screener.ENABLE_KALSHI) — no Kalshi fetching or
display anywhere in the generated site until their data improves.

Usage:
    python3 site/build.py               # build; refetches open markets if cache older than 30 min
    python3 site/build.py --refresh     # force a fresh pull of open markets
    python3 site/build.py --out DIR     # alternate output dir (default: site/)

Preview locally:
    cd ~/workspace/prediction-screener/site && python3 -m http.server 8000

Cron example (regenerate every hour):
    0 * * * * cd ~/workspace/prediction-screener && python3 site/build.py >/tmp/polyscope.log 2>&1

Public read-only APIs only. stdlib + requests. Nothing is invented: every
number on the site traces back to a live API pull, and gaps are labeled
as gaps instead of being filled in.
"""

import argparse
import datetime as dt
import html as htmlmod
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
CACHE = os.path.join(BASE, "hidden_cache")

import screener          # noqa: E402
import backtest          # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)
SNAPSHOT_TTL = 30 * 60  # refetch open markets if cache older than this

# Backtest sample parameters (mirror the "big run")
BT_WANT = 1500
BT_MIN_VOLUME = 1000
BT_INCLUDE_MULTI = True
BT_WINDOWS = 8


# --------------------------------------------------------------------------
# Data loading (cached, cron-friendly)
# --------------------------------------------------------------------------

def _ser_dt(x):
    return x.isoformat() if x else None


def _de_dt(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _ser_rec(r):
    r = dict(r)
    r["created"] = _ser_dt(r.get("created"))
    r["closes"] = _ser_dt(r.get("closes"))
    return r


def _de_rec(r):
    r = dict(r)
    r["created"] = _de_dt(r.get("created"))
    r["closes"] = _de_dt(r.get("closes"))
    return r


def load_open_markets(refresh=False):
    """
    Open-market snapshot: normalized records + fetch timestamp.
    Returns (records, fetched_at). Cached so cron rebuilds are fast and
    polite; refetches only when forced or the cache is stale.
    """
    path = os.path.join(CACHE, "open_snapshot.json")
    if not refresh and os.path.exists(path):
        with open(path) as f:
            snap = json.load(f)
        age = (NOW - dt.datetime.fromisoformat(snap["fetched_at"])).total_seconds()
        if age < SNAPSHOT_TTL:
            print(f"Using cached open snapshot ({len(snap['records'])} markets, {age/60:.0f} min old).",
                  file=sys.stderr)
            return [_de_rec(r) for r in snap["records"]], snap["fetched_at"]

    print("Fetching open markets (Polymarket)…", file=sys.stderr)
    recs = screener.normalize_polymarket(screener.fetch_polymarket(2000))
    if screener.ENABLE_KALSHI:
        print("Fetching open markets (Kalshi)…", file=sys.stderr)
        try:
            recs += screener.normalize_kalshi(screener.fetch_kalshi(1000))
        except Exception as e:  # noqa: BLE001 - Kalshi flakiness shouldn't kill the build
            print(f"Kalshi fetch failed ({e}); building with Polymarket only.", file=sys.stderr)
    else:
        print("Kalshi is parked (screener.ENABLE_KALSHI=False) — Polymarket only.",
              file=sys.stderr)
    # Defensive: strip any Kalshi records lingering in a stale cache snapshot.
    if not screener.ENABLE_KALSHI:
        recs = [r for r in recs if r.get("source") != "kalshi"]
    fetched_at = NOW.isoformat()
    with open(path, "w") as f:
        json.dump({"fetched_at": fetched_at, "records": [_ser_rec(r) for r in recs]}, f)
    return recs, fetched_at


def load_backtest_stats():
    """
    Conditional-resolution stats from resolved markets. Uses the windowed
    closed-events cache if present (the big run), else the legacy cache.
    Price histories are cached per token; missing ones are fetched politely.
    """
    win_path = os.path.join(CACHE, "closed_events_windowed.json")
    if os.path.exists(win_path):
        with open(win_path) as f:
            events = json.load(f)
        windows_used = BT_WINDOWS
    else:
        events = backtest.fetch_closed_events(refresh=False)
        windows_used = 0
    picks, skips = backtest.pick_markets(events, BT_WANT, BT_MIN_VOLUME,
                                         include_multi=BT_INCLUDE_MULTI)
    rows, no_hist = backtest.analyze(picks)
    return {
        "n": len(rows),
        "events_scanned": len(events),
        "min_volume": BT_MIN_VOLUME,
        "multi": BT_INCLUDE_MULTI,
        "windows": windows_used,
        "buckets": backtest.bucket_stats(rows),
        "final_week": backtest.final_week_stats(rows),
        "times": backtest.time_stats(rows),
        "collapse": backtest.collapse_split(rows),
        "skips": skips,
        "no_hist": no_hist,
    }


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

NAV = [("index.html", "Dashboard"), ("markets.html", "Markets"),
       ("backtests.html", "Backtests"), ("sports.html", "Sports"),
       ("fantasy.html", "Fantasy"), ("data.html", "Data"), ("about.html", "About")]


def shell(title, active, body, fetched_note, gen_note):
    links = "".join(
        f'<a href="{href}" class="{ "on" if href == active else ""}">{label}</a>'
        for href, label in NAV)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{htmlmod.escape(title)} — PolyScope</title>
<link rel="stylesheet" href="styles.css"></head>
<body>
<header class="site-header"><div class="wrap head-row">
<a class="brand" href="index.html">◈ Poly<span>Scope</span></a>
<nav>{links}</nav></div></header>
<main class="wrap">
<div class="freshness">📡 Data as of {fetched_note} · site generated {gen_note} ·
this is a scheduled snapshot — reload for the latest rebuild.</div>
{body}
</main>
<footer class="wrap foot">PolyScope · public API data only · for research,
not financial advice. Nothing here is a recommendation to trade.</footer>
</body></html>"""


def _fmt_ts(iso):
    try:
        return dt.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:  # noqa: BLE001
        return "?"


def market_row(r, show_move=True):
    q = htmlmod.escape(r["question"])
    url = htmlmod.escape(r["url"] or "#")
    src = r["source"]
    badge = (f'<span class="badge poly">POLY</span>' if src == "poly"
             else '<span class="badge kalshi">KALSHI</span>')
    move = r.get("move24h")
    if show_move and move is not None:
        cls = "pos" if move >= 0 else "neg"
        move_s = f'<span class="{cls}">{move * 100:+.1f}pp</span>'
    elif show_move:
        move_s = '<span style="color:#6b7482">—</span>'
    else:
        move_s = ""
    d = screener._days_to_close(r)
    if d is None:
        closes = "—"
    elif d < 1:
        closes = f"{d * 24:.0f}h"
    else:
        closes = f"{d:.0f}d"
    return (f"<tr><td class='q'><a href='{url}' target='_blank' rel='noopener'>{q}</a></td>"
            f"<td class='num'><b>{r['yes'] * 100:.1f}%</b></td>"
            + (f"<td class='num'>{move_s}</td>" if show_move else "")
            + f"<td class='num'>{screener._fmt_money(r['volume24h'])}</td>"
            f"<td class='num'>{screener._fmt_money(r['volume'])}</td>"
            f"<td class='num'>{closes}</td>"
            f"<td>{badge}</td></tr>")


def market_table(rows, title, show_move=True, anchor=None):
    move_th = "<th>24h move</th>" if show_move else ""
    body = "".join(market_row(r, show_move) for r in rows) or \
        "<tr><td colspan='7'>No results in this snapshot.</td></tr>"
    aid = f' id="{anchor}"' if anchor else ""
    return (f"<h2{aid}>{htmlmod.escape(title)} <span class='count'>({len(rows)})</span></h2>"
            f"<div class='tbl-wrap'><table><thead><tr><th>Market</th><th>Yes %</th>"
            f"{move_th}<th>Vol 24h</th><th>Vol total</th><th>Closes in</th><th>Src</th>"
            f"</tr></thead><tbody>{body}</tbody></table></div>")


def render_markets(recs):
    # Cap the explorer for page weight; top 2000 by volume.
    recs = sorted(recs, key=lambda r: r["volume"] or 0, reverse=True)[:2000]
    rows = []
    for i, r in enumerate(recs):
        q = htmlmod.escape(r["question"])
        url = htmlmod.escape(r["url"] or "#")
        d = screener._days_to_close(r)
        closes = "—" if d is None else (f"{d * 24:.0f}h" if d < 1 else f"{d:.0f}d")
        move = r.get("move24h")
        move_s = f"{move * 100:+.1f}" if move is not None else ""
        rows.append(
            f"<tr data-src='{r['source']}' data-q='{htmlmod.escape(r['question'].lower())}'>"
            f"<td class='q'><a href='{url}' target='_blank' rel='noopener'>{q}</a></td>"
            f"<td class='num' data-v='{r['yes']:.4f}'><b>{r['yes'] * 100:.1f}%</b></td>"
            f"<td class='num' data-v='{move if move is not None else -999}'>{move_s}</td>"
            f"<td class='num' data-v='{r['volume24h'] or 0}'>{screener._fmt_money(r['volume24h'])}</td>"
            f"<td class='num' data-v='{r['volume'] or 0}'>{screener._fmt_money(r['volume'])}</td>"
            f"<td class='num'>{closes}</td>"
            f"<td>{'POLY' if r['source'] == 'poly' else 'KALSHI'}</td></tr>")
    return f"""
<h1>Markets explorer</h1>
<div class="controls">
  <input type="text" id="q" placeholder="Search markets… (e.g. Fed, NBA, election)"
         oninput="filterRows()">
  <div class="seg" id="srcseg">
    <button class="on" data-s="all" onclick="setSrc('all',this)">All</button>
    <button data-s="poly" onclick="setSrc('poly',this)">Polymarket</button>
  </div>
</div>
<div class="tbl-wrap"><table id="mx"><thead><tr>
<th class="sortable" onclick="sortT(this)">Market</th>
<th class="sortable" onclick="sortT(this)">Yes %</th>
<th class="sortable" onclick="sortT(this)">24h Δpp</th>
<th class="sortable" onclick="sortT(this)">Vol 24h</th>
<th class="sortable" onclick="sortT(this)">Vol total</th>
<th>Closes in</th><th>Src</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table></div>
<p class="sub" id="rcount"></p>
<script>
let curSrc = 'all';
function setSrc(s, el) {{
  curSrc = s;
  document.querySelectorAll('#srcseg button').forEach(b => b.classList.remove('on'));
  el.classList.add('on');
  filterRows();
}}
function filterRows() {{
  const q = document.getElementById('q').value.toLowerCase();
  let n = 0;
  document.querySelectorAll('#mx tbody tr').forEach(tr => {{
    const okQ = !q || tr.dataset.q.includes(q);
    const okS = curSrc === 'all' || tr.dataset.src === curSrc;
    const show = okQ && okS;
    tr.style.display = show ? '' : 'none';
    if (show) n++;
  }});
  document.getElementById('rcount').textContent = n.toLocaleString() + ' markets shown';
}}
function sortT(th) {{
  const tb = th.closest('table').querySelector('tbody');
  const ci = [...th.parentNode.children].indexOf(th);
  const asc = th.dataset.asc !== '1';
  th.dataset.asc = asc ? '1' : '0';
  const rows = [...tb.rows];
  rows.sort((a, b) => {{
    let x = a.cells[ci].dataset.v ?? a.cells[ci].textContent.trim();
    let y = b.cells[ci].dataset.v ?? b.cells[ci].textContent.trim();
    const nx = parseFloat(x), ny = parseFloat(y);
    if (!isNaN(nx) && !isNaN(ny)) {{ x = nx; y = ny; }}
    return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1);
  }});
  rows.forEach(r => tb.appendChild(r));
}}
filterRows();
</script>
"""


def render_stats(st):
    c, t = st["collapse"], st["times"]
    bro = []
    for r in st["buckets"]:
        yr = f"{r['yes_rate_ge'] * 100:.1f}%" if r["yes_rate_ge"] is not None else "—"
        nr = f"{r['no_rate_le'] * 100:.1f}%" if r["no_rate_le"] is not None else "—"
        bro.append(f"<tr><td class='num'>≥{r['threshold']:.0%}</td><td class='num'>{r['n_ge']}</td>"
                   f"<td class='num'><b>{yr}</b></td>"
                   f"<td class='num'>≤{1 - r['threshold']:.0%}</td><td class='num'>{r['n_le']}</td>"
                   f"<td class='num'><b>{nr}</b></td></tr>")
    fro = []
    for r in st["final_week"]:
        yr = f"{r['yes_rate'] * 100:.1f}%" if r["yes_rate"] is not None else "—"
        fro.append(f"<tr><td>{r['band']}</td><td class='num'>{r['n']}</td>"
                   f"<td class='num'><b>{yr}</b></td></tr>")
    lim = "".join(f"<li>{htmlmod.escape(x)}</li>" for x in backtest.LIMITATIONS)
    t90, t10 = t["to_90"], t["to_10"]
    t90s = f"n={t90['n']}, median {t90['median']}d, mean {t90['mean']}d" if t90 else "no data"
    t10s = f"n={t10['n']}, median {t10['median']}d, mean {t10['mean']}d" if t10 else "no data"

    insight = ""
    if t90 and t90["median"] is not None and t90["median"] < 1.0:
        insight = f"""<div class="warn"><b>Read this first.</b> 90¢ is typically first
touched only {t90['median'] * 24:.1f} hours before resolution — i.e. <i>after</i> the
outcome is effectively decided. The high hit rate below is mostly hindsight, not a
tradeable edge: by the time a market prints 90¢, the information is already public.
The real question is whether you can get in <i>before</i> the crowd moves it there.</div>"""

    scope = (f"{st['n']:,} resolved Polymarket markets · min volume ${st['min_volume']:,.0f} · "
             f"{st['events_scanned']:,} closed events scanned"
             + (" · multi-outcome outcomes included as binary records" if st["multi"] else ""))
    return f"""
<h1>Backtest: do extreme prices resolve?</h1>
<p class="sub">{scope}. Daily price samples per market. Every bucket shows its N —
treat small-N buckets as anecdote, not signal.</p>
{insight}
<div class="stat-grid">
  <div class="stat-card"><div class="k">Reached ≥90¢ → resolved YES</div>
    <div class="v" style="color:var(--up)">{c['rate']}%</div>
    <div class="d">n={c['reached_90']} markets</div></div>
  <div class="stat-card"><div class="k">Reached ≤10¢ → resolved NO</div>
    <div class="v" style="color:var(--up)">{next((f"{b['no_rate_le'] * 100:.1f}%" for b in st['buckets'] if b['threshold'] == 0.90 and b['no_rate_le'] is not None), "—")}</div>
    <div class="d">mirror image, same idea</div></div>
  <div class="stat-card"><div class="k">Held ≥80¢ into final week</div>
    <div class="v">{c['held_rate']}%</div>
    <div class="d">n={c['held_n']} — "the crowd was right"</div></div>
  <div class="stat-card"><div class="k">Collapsed below 80¢</div>
    <div class="v" style="color:var(--down)">{c['fell_rate']}%</div>
    <div class="d">n={c['fell_n']} — small sample, read carefully</div></div>
</div>

<h2>Conditional resolution rates by price level</h2>
<p class="sub">"Reached this price at any point" → how it resolved.</p>
<div class="tbl-wrap"><table><thead><tr>
<th>Reached ≥</th><th>N</th><th>Resolved YES</th>
<th>Reached ≤</th><th>N</th><th>Resolved NO</th>
</tr></thead><tbody>{''.join(bro)}</tbody></table></div>

<h2>Price in final 7 days → resolved YES</h2>
<div class="tbl-wrap"><table><thead><tr>
<th>Final-week band</th><th>N</th><th>Resolved YES</th>
</tr></thead><tbody>{''.join(fro)}</tbody></table></div>

<h2>Time from first touch to resolution</h2>
<ul>
<li>First hit 90¢ → resolution: <b>{t90s}</b></li>
<li>First hit 10¢ → resolution: <b>{t10s}</b></li>
</ul>

<div class="warn"><b>Before you trade on any of this</b><ul>{lim}</ul></div>
"""


def render_about():
    # Deliberately minimal: the site may be sold one day, so this page
    # reveals as little as possible. Methodology lives in the repo README,
    # not on the public site.
    return """
<h1>About</h1>
<p>PolyScope tracks sports prediction-market prices, sportsbook odds, and related sports data.</p>
<p>For research, not financial advice. Nothing here is a recommendation to trade or bet.</p>
<p>&#x1F51E; 21+. Gambling problem? Call 1-800-GAMBLER.</p>
"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build the PolyScope static site")
    ap.add_argument("--refresh", action="store_true",
                    help="force a fresh pull of open markets (ignore snapshot cache)")
    ap.add_argument("--out", default=os.path.join(BASE, "site"),
                    help="output directory (default: site/)")
    args = ap.parse_args()

    out = args.out
    os.makedirs(out, exist_ok=True)

    recs, fetched_at = load_open_markets(refresh=args.refresh)

    fetched_note = _fmt_ts(fetched_at)
    gen_note = NOW.strftime("%Y-%m-%d %H:%M UTC")

    # NOTE: the dashboard (index.html, repo root included) is now the weekly
    # games slate, written by sports_compare.py --html. build.py no longer
    # writes any dashboard content — only markets/about here.
    pages = {
        "markets.html": ("Markets explorer", render_markets(recs)),
        "about.html": ("About & methodology", render_about()),
    }
    # NOTE: stats.html (old backtest dashboard) is no longer generated or linked.
    # The "Backtests" nav tab points to backtests.html, a hand-written research
    # article kept in site/. backtest.html is kept in the repo as history.
    for fname, (title, body) in pages.items():
        path = os.path.join(out, fname)
        with open(path, "w") as f:
            f.write(shell(title, fname, body, fetched_note, gen_note))
        print(f"Wrote {path}", file=sys.stderr)

    # styles.css lives next to build.py; copy it into the output dir
    import shutil
    css_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles.css")
    if os.path.abspath(css_src) != os.path.abspath(os.path.join(out, "styles.css")):
        shutil.copy(css_src, os.path.join(out, "styles.css"))

    print(f"Done: {len(recs)} open sports markets.", file=sys.stderr)


if __name__ == "__main__":
    main()
