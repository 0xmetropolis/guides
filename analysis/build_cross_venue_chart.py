"""Render a self-contained interactive HTML chart comparing per-second prices
across Hyperliquid, Extended, and Binance for WTI and Brent.

Each underlying gets its own subplot. Each line is the per-second VWAP of
trade prints from one venue, so all three venues are on apples-to-apples
event-time. Uses Plotly's WebGL renderer (`scattergl`) so a 24h window of
1s points per venue (~86k × 6 series ≈ 500k pts) stays interactive.

Usage:
    uv run python build_cross_venue_chart.py [--hours 48] [--out chart.html]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from zoneinfo import ZoneInfo

ET = ZoneInfo("US/Eastern")

# Data lives in the sibling hl-trades-recorder repo. Hard-coded so this script
# can run from anywhere, matching the convention of other guides/analysis/* tools.
DATA_ROOT = "/Users/marcolavagnino/metal/hl-trades-recorder/data"

# (label, [(venue, asset_symbol, glob, color)])
PAIRS = [
    ("WTI Crude", [
        ("Hyperliquid", "xyz:CL",  f"{DATA_ROOT}/hyperliquid/xyz:CL/*.parquet",  "#1f77b4"),
        ("Extended",    "WTI-USD", f"{DATA_ROOT}/extended/WTI-USD/*.parquet",    "#ff7f0e"),
        ("Binance",     "CLUSDT",  f"{DATA_ROOT}/binance/CLUSDT/*.parquet",      "#2ca02c"),
    ]),
    ("Brent Crude", [
        ("Hyperliquid", "xyz:BRENTOIL", f"{DATA_ROOT}/hyperliquid/xyz:BRENTOIL/*.parquet", "#1f77b4"),
        ("Extended",    "XBR-USD",      f"{DATA_ROOT}/extended/XBR-USD/*.parquet",         "#ff7f0e"),
        ("Binance",     "BZUSDT",       f"{DATA_ROOT}/binance/BZUSDT/*.parquet",           "#2ca02c"),
    ]),
]


def load_series(con, glob: str, symbol: str, since_ms: int, until_ms: int, skip_files: set[str]):
    """Per-second VWAP for one (venue, symbol) within [since_ms, until_ms).

    `skip_files` lists files to ignore (typically the in-progress current
    hour, whose parquet footer hasn't been written yet). Any file whose
    footer can't be read (e.g., legacy SIGKILL crash from earlier outages)
    is silently dropped — better to render a chart with one missing hour
    than to fail the whole job."""
    import glob as _glob
    import pyarrow.parquet as pq
    files = []
    for f in _glob.glob(glob):
        if f in skip_files:
            continue
        try:
            pq.ParquetFile(f).metadata  # touches the footer
        except Exception:
            continue
        files.append(f)
    if not files:
        import pandas as pd
        return pd.DataFrame(columns=["sec", "vwap", "volume"])
    q = """
        WITH raw AS (
            SELECT timestamp, price, size
            FROM read_parquet(?)
            WHERE asset_symbol = ?
              AND timestamp >= ?
              AND timestamp < ?
              AND price > 0 AND size > 0
        )
        SELECT
            CAST(timestamp / 1000 AS BIGINT) AS sec,
            SUM(price * size) / SUM(size) AS vwap,
            SUM(size) AS volume
        FROM raw
        GROUP BY sec
        ORDER BY sec
    """
    return con.execute(q, [files, symbol, since_ms, until_ms]).fetchdf()


def find_current_hour_files() -> set[str]:
    """Files whose footer is probably not yet written (the open hour)."""
    import glob as _glob
    from zoneinfo import ZoneInfo
    et_now = datetime.now(ZoneInfo("US/Eastern"))
    tag = et_now.strftime("%Y-%m-%dT%H")
    out = set()
    for g in [
        "data/hyperliquid/*/", "data/extended/*/", "data/binance/*/",
    ]:
        for d in _glob.glob(g):
            f = f"{d}{tag}.parquet"
            if _glob.os.path.exists(f):
                out.add(f)
    return out


def _resolve_weekend_window(weekend: str | None):
    """Map "latest" or YYYY-MM-DD (the Friday) to the
    Fri 16:00 ET → Sun 19:00 ET window used by recorder/window.py.
    Returns (start_utc, end_utc).
    """
    from zoneinfo import ZoneInfo
    et = ZoneInfo("US/Eastern")
    if weekend == "latest" or weekend is None:
        # Walk back from today (ET) to the most recent Friday.
        now_et = datetime.now(et)
        # Mon=0 ... Sun=6; we want the Friday whose Sun-19:00 has already passed
        # if today is past Sunday 19:00 ET, otherwise the upcoming/current cycle's Fri.
        # Simpler: most recent Friday whose 16:00 ET <= now.
        fri_offset = (now_et.weekday() - 4) % 7
        fri_date = (now_et - timedelta(days=fri_offset)).date()
    else:
        fri_date = datetime.strptime(weekend, "%Y-%m-%d").date()
    start = datetime.combine(fri_date, datetime.min.time(), tzinfo=et).replace(hour=16)
    end   = start + timedelta(hours=51)  # Fri 16:00 + 51h = Sun 19:00
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int,
                    help="Plot last N hours up to now. Mutually exclusive with --weekend.")
    ap.add_argument("--weekend", default="latest",
                    help="Plot exactly the recorder's collection window "
                         "(Fri 16:00 → Sun 19:00 ET). Pass 'latest' (default) "
                         "or a specific Friday date as YYYY-MM-DD.")
    ap.add_argument("--out", default="cross_venue_chart.html",
                    help="Output HTML file.")
    args = ap.parse_args()

    if args.hours is not None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=args.hours)
        window_label = f"last {args.hours}h"
    else:
        start, end = _resolve_weekend_window(args.weekend)
        s_et = start.astimezone(ET)
        e_et = end.astimezone(ET)
        window_label = (f"recorder window (Fri 16:00 → Sun 19:00 ET) "
                        f"= {s_et:%Y-%m-%d %H:%M} → {e_et:%Y-%m-%d %H:%M} ET")
    since_ms = int(start.timestamp() * 1000)
    until_ms = int(end.timestamp() * 1000)
    skip_files = find_current_hour_files()
    print(f"Loading data: {window_label}")
    print(f"Skipping {len(skip_files)} in-progress hour files: {sorted(skip_files)}\n")

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=[label for label, _ in PAIRS],
        vertical_spacing=0.10,
        shared_xaxes=False,
    )

    for row_idx, (label, venues) in enumerate(PAIRS, start=1):
        print(f"=== {label} ===")
        for venue, symbol, glob, color in venues:
            df = load_series(con, glob, symbol, since_ms, until_ms, skip_files)
            if df.empty:
                print(f"  {venue:<12} ({symbol}): no data in window")
                continue
            print(f"  {venue:<12} ({symbol}): {len(df):>6,} seconds, "
                  f"px range {df.vwap.min():.3f}–{df.vwap.max():.3f}")
            # Convert UTC seconds → ET wallclock, then drop tzinfo so Plotly
            # renders the values as-is (Plotly always treats axis data as
            # naive). DST transitions are handled by tz_convert before strip.
            ts_et = (pd.to_datetime(df.sec, unit="s", utc=True)
                       .dt.tz_convert(ET).dt.tz_localize(None))
            fig.add_trace(
                go.Scattergl(
                    x=ts_et,
                    y=df.vwap,
                    mode="lines",
                    name=f"{venue} ({symbol})",
                    line=dict(color=color, width=1),
                    legendgroup=venue,
                    showlegend=(row_idx == 1),  # one legend entry per venue
                    hovertemplate=(f"<b>{venue}</b><br>"
                                   "%{x|%Y-%m-%d %H:%M:%S} ET<br>"
                                   "VWAP: %{y:.4f}<extra></extra>"),
                ),
                row=row_idx, col=1,
            )
        print()

    fig.update_layout(
        title=(f"Cross-venue per-second price — {window_label} "
               f"(generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})"),
        template="plotly_white",
        height=900,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.04, xanchor="right", x=1),
        margin=dict(l=60, r=30, t=80, b=40),
    )
    for row_idx in (1, 2):
        fig.update_yaxes(title_text="USD", row=row_idx, col=1)
    fig.update_xaxes(title_text="US/Eastern", row=2, col=1)

    fig.write_html(args.out, include_plotlyjs="cdn", full_html=True,
                   config={"scrollZoom": True, "displaylogo": False})
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
