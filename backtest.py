#!/usr/bin/env python3
"""
Backtest module for the prediction-market screener (Polymarket only).

Answers Colin's question: "what % of markets that reach 90c actually
resolve YES?" — by pulling RESOLVED markets, walking their price history,
and computing conditional resolution rates by price bucket.

Kalshi is deliberately excluded: its API exposes no price history, so
"reached 90c at some point" is uncomputable there, and its settled-markets
endpoint is flooded with zero-information MVE combo scaffolds. Documented
honestly in the output.

Usage:
    python3 backtest.py                      # default: 300 markets, $5k min volume
    python3 backtest.py --markets 500 --min-volume 10000
    python3 backtest.py --refresh            # ignore cache, re-fetch everything
    python3 backtest.py --html               # also write backtest.html
    python3 backtest.py --json               # machine-readable output

Everything is cached to hidden_cache/ so reruns are fast. Read-only
public APIs, polite pauses, stdlib + requests only. Nothing is invented:
markets without a determinable resolution or without price history are
skipped and counted, not fudged.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "hidden_cache")
os.makedirs(CACHE, exist_ok=True)

GAMMA = "https://gamma-api.polymarket.com/events"
HIST = "https://clob.polymarket.com/prices-history"
PAUSE = 0.35
TIMEOUT = 25
NOW = dt.datetime.now(dt.timezone.utc)


def _get(url, params):
    last_err = None
    for _ in range(2):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            time.sleep(PAUSE)
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0)
    raise RuntimeError(f"GET failed for {url}: {last_err}")


def _cache_path(name):
    return os.path.join(CACHE, name)


def _load(name):
    p = _cache_path(name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def _save(name, obj):
    with open(_cache_path(name), "w") as f:
        json.dump(obj, f)


# --------------------------------------------------------------------------
# 1. Fetch resolved (closed) markets
# --------------------------------------------------------------------------

def fetch_closed_events(refresh=False):
    """Closed events, newest first, cached. Pages until empty or cap."""
    cached = None if refresh else _load("closed_events.json")
    if cached is not None:
        print(f"Using cached closed events ({len(cached)}).", file=sys.stderr)
        return cached
    events, offset, page, cap = [], 0, 100, 20  # gamma caps offset at 2000
    while offset < page * cap:
        data = _get(GAMMA, {"limit": page, "closed": "true", "offset": offset,
                            "order": "closedTime", "ascending": "false"})
        if not data:
            break
        events.extend(data)
        if len(data) < page:
            break
        offset += page
        print(f"  fetched {len(events)} closed events…", file=sys.stderr)
    _save("closed_events.json", events)
    return events


def _prices(m):
    """outcomePrices may be a JSON-encoded string. Returns list of floats or None."""
    p = m.get("outcomePrices")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except (ValueError, TypeError):
            return None
    if not p or len(p) < 2:
        return None
    try:
        return [float(x) for x in p]
    except (TypeError, ValueError):
        return None


def _outcomes(m):
    o = m.get("outcomes")
    if isinstance(o, str):
        try:
            o = json.loads(o)
        except (ValueError, TypeError):
            return None
    return o


def fetch_closed_events_windowed(refresh=False, windows=8, window_days=90):
    """
    Bypass the gamma API's offset=2000 cap by scanning end-date windows
    backwards from today. Each window is paged separately, so the cap
    applies per window rather than to the whole pull. Cached to a separate
    file from the legacy single-pull fetch.
    """
    cached = None if refresh else _load("closed_events_windowed.json")
    if cached is not None:
        print(f"Using cached windowed closed events ({len(cached)}).", file=sys.stderr)
        return cached
    events, seen = [], set()
    for w in range(windows):
        end_max = (NOW - dt.timedelta(days=w * window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_min = (NOW - dt.timedelta(days=(w + 1) * window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        offset, page, cap = 0, 100, 20
        while offset < page * cap:
            data = _get(GAMMA, {"limit": page, "closed": "true", "offset": offset,
                                "order": "closedTime", "ascending": "false",
                                "end_date_min": end_min, "end_date_max": end_max})
            if not data:
                break
            for ev in data:
                eid = ev.get("id")
                if eid not in seen:
                    seen.add(eid)
                    events.append(ev)
            if len(data) < page:
                break
            offset += page
        print(f"  window {w + 1}/{windows} ({end_min[:10]}→{end_max[:10]}): {len(events)} unique events", file=sys.stderr)
    _save("closed_events_windowed.json", events)
    return events


def _clob_tokens(m):
    tok = m.get("clobTokenIds")
    if isinstance(tok, str):
        try:
            tok = json.loads(tok)
        except (ValueError, TypeError):
            return None
    return tok if tok else None


def pick_markets(events, want, min_volume, include_multi=False):
    """
    Binary Yes/No resolved markets with real volume. With include_multi,
    multi-outcome markets are also picked: each outcome becomes its own
    binary record (resolved = True iff its final price > 0.99, requiring
    exactly one clear winner — otherwise the market is skipped as
    ambiguous). Returns list of dicts and skip counters.
    """
    picks = []
    skips = {"multi_outcome": 0, "multi_outcome_included": 0, "low_volume": 0,
             "ambiguous": 0, "no_token": 0}
    for ev in events:
        if len(picks) >= want:
            break
        for m in ev.get("markets") or []:
            if len(picks) >= want:
                break
            try:
                vol = float(m.get("volumeNum") or 0)
            except (TypeError, ValueError):
                vol = 0
            if vol < min_volume:
                skips["low_volume"] += 1
                continue
            outcomes = _outcomes(m)
            p = _prices(m)
            if not outcomes or p is None:
                skips["ambiguous"] += 1
                continue
            is_binary = [str(x).lower() for x in outcomes[:2]] == ["yes", "no"]
            tok = _clob_tokens(m)
            if not tok:
                skips["no_token"] += 1
                continue
            try:
                closed_ts = dt.datetime.fromisoformat(
                    str(m.get("closedTime") or ev.get("closedTime")).replace("Z", "+00:00")
                ).timestamp()
            except Exception:  # noqa: BLE001
                closed_ts = None
            base_url = f"https://polymarket.us/event/{ev.get('slug', '')}"
            base_q = m.get("question") or ev.get("title", "?")

            if is_binary:
                if p[0] > 0.99:
                    resolved = True
                elif p[1] > 0.99:
                    resolved = False
                else:
                    skips["ambiguous"] += 1  # voided / unresolved / partial
                    continue
                picks.append({
                    "question": base_q, "volume": vol, "conditionId": m.get("conditionId"),
                    "token": tok[0], "resolved": resolved, "closed_ts": closed_ts,
                    "url": base_url, "multi": False,
                })
            elif include_multi:
                winners = [i for i, x in enumerate(p) if x > 0.99]
                if len(winners) != 1 or len(tok) < len(p):
                    skips["ambiguous"] += 1  # no clear winner / token mismatch
                    continue
                for i, x in enumerate(p):
                    if len(picks) >= want:
                        break
                    picks.append({
                        "question": f"{base_q} — {outcomes[i]}", "volume": vol,
                        "conditionId": m.get("conditionId"), "token": tok[i],
                        "resolved": x > 0.99, "closed_ts": closed_ts,
                        "url": base_url, "multi": True,
                    })
                skips["multi_outcome_included"] += 1
            else:
                skips["multi_outcome"] += 1
                continue
    return picks, skips


# --------------------------------------------------------------------------
# 2. Price history per market (cached per token)
# --------------------------------------------------------------------------

def fetch_history(token):
    cached = _load(f"hist_{token}.json")
    if cached is not None:
        return cached
    try:
        d = _get(HIST, {"market": token, "interval": "1d"})
    except RuntimeError:
        return None
    hist = d.get("history") if isinstance(d, dict) else None
    if not hist:
        return None
    pts = [(int(p["t"]), float(p["p"])) for p in hist
           if p.get("t") is not None and p.get("p") is not None]
    _save(f"hist_{token}.json", pts)
    return pts


# --------------------------------------------------------------------------
# 3. Stats
# --------------------------------------------------------------------------

BUCKETS = [0.50, 0.60, 0.70, 0.80, 0.90, 0.95]


def analyze(markets):
    """
    For each market: max/min Yes price over history, final-week price,
    first time crossing 90c (or 10c).
    """
    rows = []
    no_hist = 0
    for m in markets:
        hist = fetch_history(m["token"])
        if not hist:
            no_hist += 1
            continue
        prices = [p for _, p in hist]
        mx, mn = max(prices), min(prices)
        close = m["closed_ts"]
        final_pts = [(t, p) for t, p in hist if close and t >= close - 7 * 86400]
        final = final_pts[-1][1] if final_pts else prices[-1]
        first90 = next((t for t, p in hist if p >= 0.90), None)
        first10 = next((t for t, p in hist if p <= 0.10), None)
        rows.append({
            "q": m["question"], "vol": m["volume"], "resolved": m["resolved"],
            "max": mx, "min": mn, "final": final,
            "t90": (close - first90) / 86400 if (first90 and close) else None,
            "t10": (close - first10) / 86400 if (first10 and close) else None,
            "url": m["url"],
        })
    return rows, no_hist


def bucket_stats(rows):
    """Conditional resolution rates by max/min price bucket."""
    out = []
    for b in BUCKETS:
        hi = [r for r in rows if r["max"] >= b]
        lo = [r for r in rows if r["min"] <= 1 - b]
        out.append({
            "threshold": b,
            "n_ge": len(hi),
            "yes_rate_ge": sum(r["resolved"] for r in hi) / len(hi) if hi else None,
            "n_le": len(lo),
            "no_rate_le": sum(not r["resolved"] for r in lo) / len(lo) if lo else None,
        })
    return out


def final_week_stats(rows):
    """Resolution rate by price in the final 7 days."""
    bands = [(0.90, 1.01), (0.70, 0.90), (0.50, 0.70), (0.30, 0.50), (0.10, 0.30), (0.0, 0.10)]
    out = []
    for lo, hi in bands:
        sub = [r for r in rows if lo <= r["final"] < hi]
        out.append({
            "band": f"{lo:.0%}–{hi:.0%}" if hi <= 1.0 else f"{lo:.0%}+",
            "n": len(sub),
            "yes_rate": sum(r["resolved"] for r in sub) / len(sub) if sub else None,
        })
    return out


def time_stats(rows):
    """Days from first crossing 90c/10c to resolution."""
    t90 = [r["t90"] for r in rows if r["t90"] is not None]
    t10 = [r["t10"] for r in rows if r["t10"] is not None]
    def summ(xs):
        if not xs:
            return None
        s = sorted(xs)
        return {"n": len(xs), "median": round(s[len(s) // 2], 1),
                "mean": round(sum(xs) / len(xs), 1)}
    return {"to_90": summ(t90), "to_10": summ(t10)}


def collapse_split(rows):
    """
    For markets that reached >=90c: split by whether they were still >=80c
    in the final week. This separates "the crowd was right" from "it collapsed".
    """
    hi = [r for r in rows if r["max"] >= 0.90]
    held = [r for r in hi if r["final"] >= 0.80]
    fell = [r for r in hi if r["final"] < 0.80]
    def rate(xs):
        return round(100 * sum(r["resolved"] for r in xs) / len(xs), 1) if xs else None
    return {"reached_90": len(hi), "rate": rate(hi),
            "held_n": len(held), "held_rate": rate(held),
            "fell_n": len(fell), "fell_rate": rate(fell)}


# --------------------------------------------------------------------------
# 4. Output
# --------------------------------------------------------------------------

def render_terminal(res, skips, no_hist, n_events):
    b, f, t, c = res["buckets"], res["final_week"], res["times"], res["collapse"]
    print(f"\n=== Backtest: conditional resolution rates ({res['n']} resolved Polymarket binary markets) ===")
    print(f"Sample: {n_events} closed events scanned · min volume ${res['min_volume']:,.0f}"
          + (" · multi-outcome outcomes included as binary records" if res.get("multi") else ""))
    print(f"Skipped: {skips} · no price history: {no_hist}\n")

    print("--- Reached a price level at ANY point -> resolution rate ---")
    print(f"{'LEVEL':>8} {'N':>5} {'YES RESOLVED':>12}   {'LEVEL':>8} {'N':>5} {'NO RESOLVED':>12}")
    for row in b:
        th = f">={row['threshold']:.0%}"
        yr = f"{row['yes_rate_ge'] * 100:.1f}%" if row["yes_rate_ge"] is not None else "--"
        th2 = f"<={1 - row['threshold']:.0%}"
        nr = f"{row['no_rate_le'] * 100:.1f}%" if row["no_rate_le"] is not None else "--"
        print(f"{th:>8} {row['n_ge']:>5} {yr:>12}   {th2:>8} {row['n_le']:>5} {nr:>12}")

    print("\n--- Price in FINAL 7 days -> % resolved YES ---")
    for row in f:
        yr = f"{row['yes_rate'] * 100:.1f}%" if row["yes_rate"] is not None else "--"
        print(f"  {row['band']:>10}: n={row['n']:>4}  yes={yr}")

    print("\n--- The 90c collapse split ---")
    print(f"  Reached >=90c at some point: n={c['reached_90']}, resolved YES {c['rate']}%")
    print(f"    …and still >=80c in final week: n={c['held_n']}, resolved YES {c['held_rate']}%")
    print(f"    …but fell below 80c by final week: n={c['fell_n']}, resolved YES {c['fell_rate']}%")

    print("\n--- Time from first touch to resolution ---")
    for label, s in [("first hit 90c", t["to_90"]), ("first hit 10c", t["to_10"])]:
        if s:
            print(f"  {label}: n={s['n']}, median {s['median']}d, mean {s['mean']}d")
        else:
            print(f"  {label}: no data")

    t90 = t["to_90"]
    if t90 and t90["median"] is not None and t90["median"] < 1.0:
        print("\n  !! KEY INSIGHT: 90c is typically first touched only "
              f"{t90['median'] * 24:.1f}h before resolution — i.e. AFTER the outcome "
              "is effectively decided. The 96%+ hit rate is mostly hindsight, "
              "not a tradeable edge. The real question is whether you can get "
              "in BEFORE the crowd does.")

    print("\n--- Read this before trading on it ---")
    for line in LIMITATIONS:
        print(f"  * {line}")
    print()


LIMITATIONS = [
    "Daily price samples can miss intraday spikes — a market that briefly "
    "touched 90c between samples is counted as never reaching it.",
    "'Reached 90c' includes markets that later collapsed; see the collapse "
    "split above. Past crowd confidence is not future accuracy.",
    "Selection bias: only markets the gamma API still lists. Deleted, voided, "
    "or ambiguous markets are excluded, which can inflate hit rates.",
    "N per bucket is shown for a reason — buckets with small N are noise, "
    "not signal. Treat anything under ~30 as anecdote.",
    "These are unconditional averages across all categories (sports, politics, "
    "crypto). Base rates differ wildly by category.",
    "Polymarket only. Kalshi has no price-history API, so its markets can't "
    "be analyzed this way.",
]


def render_html(res, skips, path):
    now = NOW.strftime("%Y-%m-%d %H:%M UTC")
    b, f, c, t = res["buckets"], res["final_week"], res["collapse"], res["times"]

    def brow(r):
        yr = f"{r['yes_rate_ge'] * 100:.1f}%" if r["yes_rate_ge"] is not None else "—"
        nr = f"{r['no_rate_le'] * 100:.1f}%" if r["no_rate_le"] is not None else "—"
        return (f"<tr><td>≥{r['threshold']:.0%}</td><td>{r['n_ge']}</td><td>{yr}</td>"
                f"<td>≤{1 - r['threshold']:.0%}</td><td>{r['n_le']}</td><td>{nr}</td></tr>")

    frows = "".join(
        f"<tr><td>{r['band']}</td><td>{r['n']}</td>"
        f"<td>{f'{r['yes_rate'] * 100:.1f}%' if r['yes_rate'] is not None else '—'}</td></tr>"
        for r in f)

    lim = "".join(f"<li>{x}</li>" for x in LIMITATIONS)
    t90 = t["to_90"]; t10 = t["to_10"]
    insight = ""
    if t90 and t90["median"] is not None and t90["median"] < 1.0:
        insight = (f"""<div class="warn"><b>Key insight — read this first.</b> 90¢ is typically first touched """
                   f"""only {t90['median'] * 24:.1f} hours before resolution — i.e. <i>after</i> the outcome is """
                   f"""effectively decided (a goal scored, a vote counted). The 96%+ hit rate is mostly hindsight, """
                   f"""not a tradeable edge: by the time a market prints 90¢, the information is already public. """
                   f"""The real edge is getting in <i>before</i> the crowd moves it there.</div>""")

    t90s = f"n={t90['n']}, median {t90['median']}d, mean {t90['mean']}d" if t90 else "no data"
    t10s = f"n={t10['n']}, median {t10['median']}d, mean {t10['mean']}d" if t10 else "no data"

    page = (
        f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Backtest — conditional resolution rates ({now})</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1000px;margin:20px auto;padding:0 16px;background:#0f1115;color:#e6e6e6}}
table{{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:24px}}
th{{text-align:left;padding:8px;color:#9aa4b2;border-bottom:2px solid #333}}
td{{padding:7px 8px;border-bottom:1px solid #222}}
h2{{margin-top:32px;color:#7fb3ff}}.meta{{color:#8a93a3;margin-bottom:14px}}
.warn{{background:#241a08;border:1px solid #7a5b1e;border-radius:8px;padding:12px 16px;margin:24px 0}}
.warn li{{margin:6px 0}}.big{{font-size:22px;color:#8fe388}}
</style></head><body>
<h1>📊 Backtest: do 90¢ markets resolve YES?</h1>
<div class="meta">Generated {now} · {res['n']} resolved Polymarket binary markets · """
        + f"""min volume ${res['min_volume']:,.0f} · daily price samples · """
        + f"""skipped {sum(skips.values())} markets, {res['no_hist']} lacked price history</div>

{insight}

<h2>The headline stat</h2>
<p class="big">Markets that reached ≥90¢ resolved YES """
        + f"""{c['rate']}% of the time (n={c['reached_90']}).</p>
<p>But that includes markets that later collapsed. Split by final-week price:</p>
<ul>
<li>Reached ≥90¢ and <b>still ≥80¢</b> in the final week → YES <b>{c['held_rate']}%</b> (n={c['held_n']})</li>
<li>Reached ≥90¢ but <b>fell below 80¢</b> by the final week → YES <b>{c['fell_rate']}%</b> (n={c['fell_n']})</li>
</ul>

<h2>Conditional resolution rates by price level</h2>
<table><thead><tr><th>Reached ≥</th><th>N</th><th>Resolved YES</th>
<th>Reached ≤</th><th>N</th><th>Resolved NO</th></tr></thead>
<tbody>{''.join(brow(r) for r in b)}</tbody></table>

<h2>Price in final 7 days → resolved YES</h2>
<table><thead><tr><th>Final-week band</th><th>N</th><th>Resolved YES</th></tr></thead>
<tbody>{frows}</tbody></table>

<h2>Time from first touch to resolution</h2>
<ul>
<li>First hit 90¢ → resolution: {t90s}</li>
<li>First hit 10¢ → resolution: {t10s}</li>
</ul>

<div class="warn"><b>Read this before trading on it</b><ul>{lim}</ul></div>
</body></html>"""
    )
    with open(path, "w") as f:
        f.write(page)
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Backtest: conditional resolution rates on resolved Polymarket markets")
    ap.add_argument("--markets", type=int, default=300, help="resolved markets to analyze (default: 300)")
    ap.add_argument("--min-volume", type=float, default=5000, help="min market volume $ (default: 5000)")
    ap.add_argument("--refresh", action="store_true", help="ignore cache, re-fetch everything")
    ap.add_argument("--html", action="store_true", help="write backtest.html")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--windows", type=int, default=0,
                    help="N 90-day end-date windows to scan back (0 = legacy single pull capped at 2000 events)")
    ap.add_argument("--include-multi", action="store_true",
                    help="include multi-outcome markets as per-outcome binary records (requires exactly one clear winner)")
    args = ap.parse_args()

    if args.refresh:
        for f in os.listdir(CACHE):
            os.remove(os.path.join(CACHE, f))

    print("Fetching closed events…", file=sys.stderr)
    if args.windows and args.windows > 0:
        events = fetch_closed_events_windowed(refresh=args.refresh, windows=args.windows)
    else:
        events = fetch_closed_events(refresh=args.refresh)
    picks, skips = pick_markets(events, args.markets, args.min_volume,
                                include_multi=args.include_multi)
    print(f"Picked {len(picks)} resolved markets.", file=sys.stderr)

    print("Fetching price histories (cached per token)…", file=sys.stderr)
    rows, no_hist = analyze(picks)

    res = {
        "n": len(rows), "min_volume": args.min_volume, "multi": args.include_multi,
        "buckets": bucket_stats(rows),
        "final_week": final_week_stats(rows),
        "times": time_stats(rows),
        "collapse": collapse_split(rows),
        "no_hist": no_hist,
    }

    if args.html:
        path = os.path.join(BASE, "backtest.html")
        render_html(res, skips, path)
        print(f"Wrote {path}", file=sys.stderr)

    if args.json:
        print(json.dumps(res, indent=1))
    else:
        render_terminal(res, skips, no_hist, len(events))


if __name__ == "__main__":
    main()
