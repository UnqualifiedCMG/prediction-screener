#!/usr/bin/env python3
"""
Prediction-market screener — Polymarket (sports), public API only.

Pulls live open sports markets and runs a set of screens useful for
spotting what the money is saying (and mispricing):

    volume   - highest-volume open markets (liquidity = tradable)
    closing  - expiring within 7 days, sorted by volume (resolution edge)
    movers   - biggest 24h price moves with real volume behind them
    extremes - priced >=90% or <=10% with high volume (his content fuel:
               consensus trades and crowded "locks")
    new      - markets created in the last 48h, sorted by volume
    search   - keyword filter over titles (e.g. --query "NBA")

Kalshi support is parked (ENABLE_KALSHI = False): its API data is too thin
to be useful right now (provisional sports-combo spam, no price history).
The fetch/normalize code is intact — flip the flag to bring it back.

Sports-only by default (SPORTS_ONLY = True): non-sports markets are
filtered at normalization using the API's own tags/ticker/series fields.
Pass --all-markets to analyze everything.

Outputs a clean terminal table, an HTML dashboard (no build step, just
open dashboard.html), or JSON for cron/tooling use (--json).

Usage:
    python3 screener.py --screen volume --limit 20
    python3 screener.py --screen movers --source poly
    python3 screener.py --screen search --query "NBA"
    python3 screener.py --screen extremes --html
    python3 screener.py --screen closing --json > closing.json

All data comes from live API pulls — nothing is invented. Prices are
Yes prices as probabilities (0.0-1.0), shown as %.
"""

import argparse
import datetime as dt
import json
import sys
import time
import html as htmlmod

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")

# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------

POLY_URL = "https://gamma-api.polymarket.com/events"
KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
TIMEOUT = 25
PAUSE = 0.35  # polite pause between paged requests

# --------------------------------------------------------------------------
# Feature flags
# --------------------------------------------------------------------------
# Kalshi is parked: its API data is too thin to be useful (provisional
# sports-combo spam floods listings; no price-history endpoint). The
# fetch/normalize/display code below is intact and tested — set to True
# to bring Kalshi back if their data situation improves.
ENABLE_KALSHI = False

# Sports-only: the site is a sports tracker. Non-sports markets are dropped
# at normalization using the API's own categorization (tags, then ticker /
# series prefixes). Ambiguous events are excluded, never guessed.
SPORTS_ONLY = True


def _get(url, params):
    """GET with one retry; raises on persistent failure."""
    last_err = None
    for _ in range(2):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            time.sleep(PAUSE)
            return r.json()
        except Exception as e:  # noqa: BLE001 - network flakiness
            last_err = e
            time.sleep(1.0)
    raise RuntimeError(f"GET failed for {url}: {last_err}")


def fetch_polymarket(max_events=2000):
    """All open events, paginated. Returns list of normalized records."""
    records = []
    offset = 0
    page = 100
    while offset < max_events:
        data = _get(POLY_URL, {"limit": page, "closed": "false", "offset": offset})
        if not data:
            break
        for ev in data:
            records.append(ev)
        if len(data) < page:
            break
        offset += page
    return records


def fetch_kalshi(max_markets=1000, max_pages=25):
    """Open Kalshi markets, cursor-paginated.

    Two defenses against the API's MVE sports-combo spam:
      1. min_close_ts 14 days out — the combo scaffolds all expire within
         days; real macro/politics markets live months out.
      2. skip provisional / multivariate-leg markets at fetch time.
    """
    records = []
    cursor = None
    pages = 0
    min_close = int((NOW + dt.timedelta(days=14)).timestamp())
    while len(records) < max_markets and pages < max_pages:
        params = {"limit": 200, "status": "open", "min_close_ts": min_close}
        if cursor:
            params["cursor"] = cursor
        data = _get(KALSHI_URL, params)
        batch = data.get("markets", [])
        if not batch:
            break
        records.extend(
            m for m in batch
            if not m.get("is_provisional")
            and not m.get("mve_selected_legs")
            and not m.get("mve_collection_ticker")
        )
        cursor = data.get("cursor")
        pages += 1
        if not cursor:
            break
    return records[:max_markets]


# --------------------------------------------------------------------------
# Normalization: both sources -> one record shape
# --------------------------------------------------------------------------

def _iso(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# Sports ticker/series prefixes seen on Polymarket game markets.
SPORTS_TICKER_PREFIXES = (
    "nfl-", "nba-", "mlb-", "nhl-", "wnba-", "mls-",
    "ufc-", "mma-", "pga-", "f1-", "nascar-",
    "soccer-", "epl-", "la-liga-", "serie-a-", "bundesliga-",
    "ligue-1-", "ucl-", "cricket-", "tennis-", "golf-",
)
SPORTS_SERIES_NAMES = ("nfl", "nba", "mlb", "nhl", "wnba", "mls")


def is_sports_event(ev):
    """True when the event is sports, using the API's own categorization.

    1. The event's tags contain "Sports" (exact, case-insensitive).
    2. Fallback: ticker or series starts with a known league prefix
       (nfl-, nba-, ...), which is how Polymarket names game markets.
    Ambiguous events are excluded, never guessed.
    """
    tags = [(t.get("label") or t.get("slug") or "").strip().lower()
            for t in (ev.get("tags") or [])]
    if "sports" in tags:
        return True
    ticker = (ev.get("ticker") or "").lower()
    if ticker.startswith(SPORTS_TICKER_PREFIXES):
        return True
    series = str(ev.get("seriesSlug") or ev.get("series") or "").lower()
    if (series.startswith(SPORTS_TICKER_PREFIXES)
            or series in SPORTS_SERIES_NAMES):
        return True
    return False


def normalize_polymarket(events, sports_only=SPORTS_ONLY):
    """One record per market; event volume/24h volume carried down."""
    out = []
    for ev in events:
        if sports_only and not is_sports_event(ev):
            continue  # site is sports-only; politics/crypto/culture excluded
        ev_vol = _f(ev.get("volume"))
        ev_vol24 = _f(ev.get("volume24hr"))
        ev_liq = _f(ev.get("liquidity"))
        created = _iso(ev.get("createdAt"))
        for m in ev.get("markets") or []:
            prices = m.get("outcomePrices")
            if isinstance(prices, str):  # gamma API returns a JSON-encoded string
                try:
                    prices = json.loads(prices)
                except (ValueError, TypeError):
                    prices = []
            prices = prices or []
            yes = _f(prices[0]) if prices else None
            if yes is None or yes < 0 or yes > 1:
                continue  # unresolved/no price data yet
            out.append({
                "source": "poly",
                "event": ev.get("title", "?"),
                "question": m.get("question") or ev.get("title", "?"),
                "yes": yes,
                "volume": _f(m.get("volumeNum")) or ev_vol,
                "volume24h": ev_vol24,
                "liquidity": ev_liq,
                "move24h": _f(m.get("oneDayPriceChange")),
                "created": created,
                "closes": _iso(m.get("endDate") or ev.get("endDate")),
                "url": f"https://polymarket.us/event/{ev.get('slug', '')}",
            })
    return out


def normalize_kalshi(markets):
    out = []
    for m in markets:
        if m.get("is_provisional"):
            continue  # multivariate combo scaffolding, zero tradeable volume
        bid = _f(m.get("yes_bid_dollars"))
        ask = _f(m.get("yes_ask_dollars"))
        if bid is not None and ask is not None and ask >= bid:
            yes = (bid + ask) / 2  # midpoint is a better read than bid alone
        else:
            yes = bid if bid is not None else _f(m.get("last_price_dollars"))
        if yes is None:
            continue
        out.append({
            "source": "kalshi",
            "event": m.get("title", "?"),
            "question": m.get("title", "?"),
            "yes": yes,
            "volume": _f(m.get("volume_fp")),
            "volume24h": _f(m.get("volume_24h_fp")),
            "liquidity": None,
            "move24h": None,  # Kalshi API does not expose price history
            "created": _iso(m.get("created_time")),
            "closes": _iso(m.get("expiration_time")),
            "url": f"https://kalshi.com/markets/{m.get('ticker', '')}",
        })
    return out


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

NOW = dt.datetime.now(dt.timezone.utc)


def _days_to_close(r):
    if not r["closes"]:
        return None
    return (r["closes"] - NOW).total_seconds() / 86400


def _open(r):
    """Drop stale records whose close date already passed."""
    d = _days_to_close(r)
    return d is None or d >= 0


def screen_volume(recs, limit, min_volume):
    rows = [r for r in recs if (r["volume"] or 0) >= min_volume and _open(r)]
    return sorted(rows, key=lambda r: r["volume"] or 0, reverse=True)[:limit]


def screen_closing(recs, limit, min_volume):
    rows = []
    for r in recs:
        d = _days_to_close(r)
        if d is None or d < 0 or d > 7:
            continue
        if (r["volume"] or 0) < min_volume:
            continue
        rows.append(r)
    return sorted(rows, key=lambda r: r["volume"] or 0, reverse=True)[:limit]


def screen_movers(recs, limit, min_volume):
    rows = [r for r in recs
            if r["move24h"] is not None
            and (r["volume24h"] or 0) >= min_volume
            and _open(r)]
    return sorted(rows, key=lambda r: abs(r["move24h"]), reverse=True)[:limit]


def screen_extremes(recs, limit, min_volume):
    rows = [r for r in recs
            if (r["yes"] >= 0.90 or r["yes"] <= 0.10)
            and (r["volume24h"] or 0) >= min_volume
            and _open(r)]
    return sorted(rows, key=lambda r: r["volume24h"] or 0, reverse=True)[:limit]


def screen_new(recs, limit, min_volume):
    cutoff = NOW - dt.timedelta(hours=48)
    rows = [r for r in recs
            if r["created"] and r["created"] >= cutoff
            and (r["volume"] or 0) >= min_volume
            and _open(r)]
    return sorted(rows, key=lambda r: r["volume"] or 0, reverse=True)[:limit]


def screen_search(recs, limit, min_volume, query):
    q = query.lower()
    rows = [r for r in recs
            if q in (r["question"] or "").lower()
            and (r["volume"] or 0) >= min_volume
            and _open(r)]
    return sorted(rows, key=lambda r: r["volume"] or 0, reverse=True)[:limit]


SCREENS = {
    "volume":   ("Most liquid open markets", screen_volume),
    "closing":  ("Closing within 7 days (by volume)", screen_closing),
    "movers":   ("Biggest 24h price moves (real volume only)", screen_movers),
    "extremes": ("Priced >=90% or <=10% with high 24h volume", screen_extremes),
    "new":      ("Listed in the last 48h", screen_new),
    "search":   ("Keyword search", screen_search),
}


# --------------------------------------------------------------------------
# Output: terminal
# --------------------------------------------------------------------------

def _fmt_pct(x):
    return f"{x * 100:5.1f}%" if x is not None else "   -- "


def _fmt_money(x):
    if x is None:
        return "      -- "
    if x >= 1_000_000:
        return f"${x / 1_000_000:6.2f}M"
    if x >= 1_000:
        return f"${x / 1_000:6.1f}K"
    return f"${x:7.0f}"


def _fmt_days(r):
    d = _days_to_close(r)
    if d is None:
        return "  --  "
    if d < 1:
        return f"{d * 24:4.0f}h  "
    return f"{d:4.0f}d  "


def render_terminal(title, rows, screen):
    now = NOW.strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n=== {title}  [{len(rows)} shown, {now}] ===")
    hdr = f"{'YES':>6} {'24H MOVE':>9} {'VOL 24H':>9} {'VOL TOT':>9} {'CLOSES':>6}  MARKET"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        move = (f"{r['move24h'] * 100:+.1f}pp" if r["move24h"] is not None else "    --  ")
        q = r["question"]
        if len(q) > 72:
            q = q[:69] + "..."
        src = "P" if r["source"] == "poly" else "K"
        print(f"{_fmt_pct(r['yes'])} {move:>9} {_fmt_money(r['volume24h'])} "
              f"{_fmt_money(r['volume'])} {_fmt_days(r)} [{src}] {q}")
    if screen == "movers" and ENABLE_KALSHI:
        print("\nNote: Kalshi rows never appear here — its API exposes no price history.")
    print()

# --------------------------------------------------------------------------
# Output: HTML dashboard (static file, sortable, no build step)
# --------------------------------------------------------------------------

def render_html(all_results, path):
    """all_results: {screen_name: (title, rows)}."""
    now = NOW.strftime("%Y-%m-%d %H:%M UTC")
    tabs = []
    bodies = []
    for i, (name, (title, rows)) in enumerate(all_results.items()):
        active = "active" if i == 0 else ""
        tabs.append(f'<button class="tab {active}" onclick="showTab(\'{name}\')">{title} ({len(rows)})</button>')
        trs = []
        for r in rows:
            move = f"{r['move24h'] * 100:+.1f}pp" if r["move24h"] is not None else "—"
            d = _days_to_close(r)
            closes = f"{d * 24:.0f}h" if d is not None and d < 1 else (f"{d:.0f}d" if d is not None else "—")
            q = htmlmod.escape(r["question"])
            url = htmlmod.escape(r["url"] or "#")
            trs.append(
                f"<tr><td>{r['yes'] * 100:.1f}%</td><td>{move}</td>"
                f"<td data-v='{r['volume24h'] or 0}'>{_fmt_money(r['volume24h'])}</td>"
                f"<td data-v='{r['volume'] or 0}'>{_fmt_money(r['volume'])}</td>"
                f"<td>{closes}</td><td>{r['source']}</td>"
                f"<td class='q'><a href='{url}' target='_blank'>{q}</a></td></tr>"
            )
        bodies.append(
            f"<div id='tab-{name}' class='pane {active}'>"
            f"<h2>{htmlmod.escape(title)}</h2>"
            f"<table><thead><tr>"
            f"<th onclick='sortT(this)'>Yes %</th><th onclick='sortT(this)'>24h move</th>"
            f"<th onclick='sortT(this)'>Vol 24h</th><th onclick='sortT(this)'>Vol total</th>"
            f"<th>Closes in</th><th>Src</th><th>Market</th>"
            f"</tr></thead><tbody>{''.join(trs) or '<tr><td colspan=7>No results.</td></tr>'}</tbody></table></div>"
        )
    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Prediction-market screener — {now}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:20px auto;padding:0 16px;background:#0f1115;color:#e6e6e6}}
.tab{{background:#1c1f26;color:#cfd3dc;border:1px solid #333;padding:8px 14px;margin:0 4px 12px 0;cursor:pointer;border-radius:6px}}
.tab.active{{background:#2f6fed;color:#fff;border-color:#2f6fed}}
.pane{{display:none}}.pane.active{{display:block}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;padding:8px;cursor:pointer;color:#9aa4b2;border-bottom:2px solid #333;white-space:nowrap}}
td{{padding:7px 8px;border-bottom:1px solid #222;vertical-align:top}}
td.q{{max-width:560px}}a{{color:#7fb3ff;text-decoration:none}}
.meta{{color:#8a93a3;margin-bottom:14px}}
</style></head><body>
<h1>🔮 Prediction-market screener</h1>
<div class="meta">Generated {now} · Yes prices as % · sports markets only · click headers to sort</div>
{''.join(tabs)}
{''.join(bodies)}
<script>
function showTab(n){{document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
document.getElementById('tab-'+n).classList.add('active');
event.target.classList.add('active');}}
function sortT(th){{const tb=th.closest('table').querySelector('tbody');const ci=[...th.parentNode.children].indexOf(th);
const rows=[...tb.rows];const asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
rows.sort((a,b)=>{{let x=a.cells[ci].dataset.v??a.cells[ci].textContent,y=b.cells[ci].dataset.v??b.cells[ci].textContent;
x=parseFloat(x)||x;y=parseFloat(y)||y;return (x>y?1:x<y?-1:0)*(asc?1:-1);}});
rows.forEach(r=>tb.appendChild(r));}}
</script></body></html>"""
    with open(path, "w") as f:
        f.write(page)
    return path


# --------------------------------------------------------------------------
# Output: JSON (cron / tooling friendly)
# --------------------------------------------------------------------------

def to_json(title, rows):
    return {
        "screen": title,
        "generated_utc": NOW.isoformat(),
        "rows": [{
            "source": r["source"],
            "event": r["event"],
            "question": r["question"],
            "yes_probability": round(r["yes"], 4),
            "volume_total": r["volume"],
            "volume_24h": r["volume24h"],
            "move_24h_pp": round(r["move24h"] * 100, 2) if r["move24h"] is not None else None,
            "created_utc": r["created"].isoformat() if r["created"] else None,
            "closes_utc": r["closes"].isoformat() if r["closes"] else None,
            "url": r["url"],
        } for r in rows],
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Prediction-market screener (Polymarket sports)")
    ap.add_argument("--screen", choices=list(SCREENS) + ["all"], default="volume",
                    help="which screen to run (default: volume)")
    ap.add_argument("--query", default="", help="keyword for --screen search")
    ap.add_argument("--limit", type=int, default=20, help="rows per screen (default: 20)")
    ap.add_argument("--min-volume", type=float, default=1000,
                    help="minimum total volume $ to include (default: 1000)")
    ap.add_argument("--min-volume24h", type=float, default=1000,
                    help="minimum 24h volume $ for movers/extremes (default: 1000)")
    ap.add_argument("--source", choices=["poly", "kalshi", "both"], default="both",
                    help="data source (default: both; kalshi is parked unless ENABLE_KALSHI)")
    ap.add_argument("--all-markets", action="store_true",
                    help="disable the sports-only filter (analyze every category)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    ap.add_argument("--html", action="store_true",
                    help="also write dashboard.html (runs all screens)")
    ap.add_argument("--max-events", type=int, default=2000, help="Polymarket fetch cap")
    ap.add_argument("--max-markets", type=int, default=1000, help="Kalshi fetch cap")
    args = ap.parse_args()

    if args.screen == "search" and not args.query:
        ap.error("--screen search requires --query")

    sports_only = SPORTS_ONLY and not args.all_markets

    recs = []
    if args.source in ("poly", "both"):
        print("Fetching Polymarket…", file=sys.stderr)
        recs += normalize_polymarket(fetch_polymarket(args.max_events),
                                     sports_only=sports_only)
    if args.source in ("kalshi", "both"):
        if ENABLE_KALSHI:
            print("Fetching Kalshi…", file=sys.stderr)
            recs += normalize_kalshi(fetch_kalshi(args.max_markets))
        else:
            print("NOTE: Kalshi is parked (ENABLE_KALSHI=False) — skipping Kalshi fetch.",
                  file=sys.stderr)
    print(f"Loaded {len(recs)} market records.", file=sys.stderr)

    names = list(SCREENS) if (args.screen == "all" or args.html) else [args.screen]
    results = {}
    for name in names:
        title, fn = SCREENS[name]
        if name == "search":
            rows = fn(recs, query=args.query, min_volume=args.min_volume, limit=args.limit)
        elif name in ("movers", "extremes"):
            rows = fn(recs, limit=args.limit, min_volume=args.min_volume24h)
        elif name == "new":
            rows = fn(recs, limit=args.limit, min_volume=0)  # new markets rarely have volume yet
        else:
            rows = fn(recs, limit=args.limit, min_volume=args.min_volume)
        results[name] = (title, rows)

    if args.html:
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        render_html(results, path)
        print(f"Wrote {path}", file=sys.stderr)

    if args.json:
        if args.screen == "all":
            print(json.dumps({n: to_json(t, r) for n, (t, r) in results.items()}, indent=1))
        else:
            title, rows = results[args.screen]
            print(json.dumps(to_json(title, rows), indent=1))
    elif not args.html or args.screen != "all":
        # terminal tables for the requested screen(s)
        show = names if args.screen == "all" else [args.screen]
        for name in show:
            title, rows = results[name]
            render_terminal(title, rows, name)


if __name__ == "__main__":
    main()
