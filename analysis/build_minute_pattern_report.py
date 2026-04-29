"""
Minute-of-hour pattern report — does anything special happen at :44/:45 of each
hour on the perp recorders? Tests against last full weekend (2026-04-25/26)
since the actual crash weekend (04-18/19) was lost to OOM.

Generates two HTML files:
  - minute_of_hour_pattern_2026-04-25_26.html         (all weekend hours)
  - minute_of_hour_pattern_2026-04-25_26_filtered.html (CME open/close hours
    excluded — 17:00 ET maintenance hour and 18:00 ET Sunday reopen hour)

Usage:
    uv run python build_minute_pattern_report.py
"""

from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go

# Recorder collection window (recorder/window.py): Fri 16:00 ET → Sun 19:00 ET
# (Sunday hour < 19, exclusive). Files are ET-named per the README convention.
WINDOW_START = "2026-04-24 16:00:00"
WINDOW_END = "2026-04-26 19:00:00"
DATA_GLOBS = [
    # Friday 16:00–23:00 ET
    "/Users/marcolavagnino/metal/hl-trades-recorder/data/**/2026-04-24T1[6-9]*.parquet",
    "/Users/marcolavagnino/metal/hl-trades-recorder/data/**/2026-04-24T2[0-3]*.parquet",
    # All of Saturday
    "/Users/marcolavagnino/metal/hl-trades-recorder/data/**/2026-04-25T*.parquet",
    # Sunday 00:00–18:00 ET
    "/Users/marcolavagnino/metal/hl-trades-recorder/data/**/2026-04-26T0*.parquet",
    "/Users/marcolavagnino/metal/hl-trades-recorder/data/**/2026-04-26T1[0-8]*.parquet",
]
OUT_DIR = Path(__file__).parent

VENUE_COLORS = {
    "hyperliquid": "#7c3aed",
    "extended": "#0ea5e9",
    "binance": "#f59e0b",
}

# Minutes-of-hour where H3 (avg / p95 trade size) shows visible spikes that
# warrant a "what trades caused this?" diagnostic underneath. Add minutes here
# to render an extra outlier table per minute.
INSPECT_MINUTES = [27, 56]

# CME WTI/Brent transitions inside our window (Fri 16:00 → Sun 19:00 ET):
#   Fri 17:00 ET → market close.   Sun 18:00 ET → market reopen.
# Sat is closed all day; Fri 18 & Sun 17 are quiet pre/post-CME hours — those
# are still "pure weekend perp behaviour" and should NOT be filtered.
MODES = [
    {
        "label": "all",
        "exclude_day_hours": set(),
        "out": OUT_DIR / "minute_of_hour_pattern_2026-04-25_26.html",
        "title_suffix": "(all weekend hours)",
    },
    {
        "label": "filtered",
        # (day_name, hour_et) pairs — only the actual CME transition hours.
        "exclude_day_hours": {("Friday", 17), ("Sunday", 18)},
        "out": OUT_DIR / "minute_of_hour_pattern_2026-04-25_26_filtered.html",
        "title_suffix": "(CME transition hours excluded — Fri 17 ET close & Sun 18 ET reopen)",
    },
]


def build(exclude_day_hours: set[tuple[str, int]], out_path: Path, title_suffix: str):
    # Restrict to the recorder's intended collection window (Fri 16:00 ET →
    # Sun 19:00 ET, exclusive). Bounds applied in SQL on the ET timestamp so
    # this works regardless of file-name conventions.
    where_parts = [
        f"((to_timestamp(timestamp/1000.0) AT TIME ZONE 'America/New_York')) >= TIMESTAMP '{WINDOW_START}'",
        f"((to_timestamp(timestamp/1000.0) AT TIME ZONE 'America/New_York')) <  TIMESTAMP '{WINDOW_END}'",
    ]
    if exclude_day_hours:
        ts_et_expr = "(to_timestamp(timestamp/1000.0) AT TIME ZONE 'America/New_York')"
        excl_tuples = ", ".join(
            f"('{day}', {hour})" for day, hour in sorted(exclude_day_hours)
        )
        where_parts.append(
            f"(dayname({ts_et_expr}), EXTRACT(HOUR FROM {ts_et_expr})::INT) "
            f"NOT IN ({excl_tuples})"
        )
    excl_clause = "WHERE " + " AND ".join(where_parts)

    globs_sql = "[" + ", ".join(f"'{g}'" for g in DATA_GLOBS) + "]"

    con = duckdb.connect()
    con.execute(
        f"""
        CREATE OR REPLACE VIEW trades AS
        SELECT
          venue,
          asset_symbol,
          side,
          price,
          size,
          notional_usd,
          mark_price,
          oracle_price,
          to_timestamp(timestamp/1000.0) AS ts_utc,
          (to_timestamp(timestamp/1000.0) AT TIME ZONE 'America/New_York') AS ts_et,
          EXTRACT(MINUTE FROM ((to_timestamp(timestamp/1000.0) AT TIME ZONE 'America/New_York')))::INT AS minute_of_hour,
          EXTRACT(HOUR FROM ((to_timestamp(timestamp/1000.0) AT TIME ZONE 'America/New_York')))::INT AS hour_et,
          EXTRACT(DAY FROM ((to_timestamp(timestamp/1000.0) AT TIME ZONE 'America/New_York')))::INT AS day_et,
          timestamp AS ts_ms
        FROM read_parquet({globs_sql}, union_by_name=true)
        {excl_clause}
        """
    )

    summary = con.execute(
        """SELECT venue, asset_symbol, COUNT(*) AS rows,
                  MIN(ts_et) AS first_trade_et, MAX(ts_et) AS last_trade_et
           FROM trades GROUP BY venue, asset_symbol ORDER BY venue, asset_symbol"""
    ).df()

    agg = con.execute(
        """SELECT venue,
                  minute_of_hour,
                  COUNT(*) AS trades,
                  SUM(notional_usd) AS notional,
                  AVG(notional_usd) AS avg_notional,
                  QUANTILE_CONT(notional_usd, 0.95) AS p95_notional,
                  SUM(CASE WHEN side='buy' THEN notional_usd ELSE 0 END) AS buy_notional,
                  SUM(CASE WHEN side='sell' THEN notional_usd ELSE 0 END) AS sell_notional,
                  AVG(CASE WHEN mark_price IS NOT NULL AND mark_price > 0
                      THEN ABS(price - mark_price) END) AS avg_mark_gap,
                  QUANTILE_CONT(CASE WHEN mark_price IS NOT NULL AND mark_price > 0
                      THEN ABS(price - mark_price) END, 0.5) AS median_mark_gap
           FROM trades
           GROUP BY venue, minute_of_hour
           ORDER BY venue, minute_of_hour"""
    ).df()
    agg["imbalance_pct"] = (
        100.0 * (agg["buy_notional"] - agg["sell_notional"]) / (agg["buy_notional"] + agg["sell_notional"])
    )

    flush_minutes = [4, 9, 14, 19, 24, 29, 34, 39, 44, 49, 54, 59]
    flush_cum = con.execute(
        """WITH ordered AS (
             SELECT ts_et,
                    EXTRACT(MINUTE FROM ts_et)::INT AS m,
                    ROW_NUMBER() OVER (ORDER BY ts_ms) AS cum_rows
             FROM trades WHERE venue='hyperliquid'
           ),
           flush_pts AS (
             SELECT date_trunc('hour', ts_et) AS hour_et, m AS minute,
                    MAX(cum_rows) AS rows_at_flush
             FROM ordered
             WHERE m IN (4, 9, 14, 19, 24, 29, 34, 39, 44, 49, 54, 59)
             GROUP BY 1, 2
           )
           SELECT * FROM flush_pts ORDER BY hour_et, minute"""
    ).df()
    flush_cum["t"] = flush_cum["hour_et"] + (flush_cum["minute"].astype("timedelta64[m]"))

    def line_per_venue(metric, ylabel, title, hover_fmt=":,.0f"):
        fig = go.Figure()
        for venue in ["hyperliquid", "extended", "binance"]:
            d = agg[agg["venue"] == venue].sort_values("minute_of_hour")
            fig.add_trace(
                go.Scatter(
                    x=d["minute_of_hour"], y=d[metric],
                    mode="lines+markers", name=venue,
                    line=dict(color=VENUE_COLORS[venue], width=2),
                    marker=dict(size=5),
                    hovertemplate="minute %{x:02d}<br>" + ylabel + f": %{{y{hover_fmt}}}<extra>{venue}</extra>",
                )
            )
        fig.add_vrect(x0=43.5, x1=46.5, fillcolor="red", opacity=0.08, line_width=0,
                      annotation_text=":44–:46", annotation_position="top left")
        fig.update_layout(
            title=title, xaxis_title="Minute of hour (ET)", yaxis_title=ylabel,
            hovermode="x unified", template="plotly_white", height=380,
            margin=dict(l=50, r=20, t=50, b=40),
            xaxis=dict(tickmode="linear", tick0=0, dtick=5, range=[-0.5, 59.5]),
        )
        return fig

    def heatmap_for(venue):
        d = con.execute(
            f"""SELECT dayname(ts_et) AS day_name,
                       hour_et,
                       minute_of_hour,
                       COUNT(*) AS trades
                FROM trades WHERE venue='{venue}'
                GROUP BY 1, 2, 3"""
        ).df()
        # Sort key: collection-window order Friday → Saturday → Sunday
        day_order = {"Friday": 0, "Saturday": 1, "Sunday": 2}
        d["sort_key"] = d["day_name"].map(day_order).fillna(99) * 100 + d["hour_et"]
        d["label"] = d["day_name"].str.slice(0, 3) + " " + d["hour_et"].apply(lambda h: f"{h:02d}:00")
        d = d.sort_values("sort_key")
        ordered_labels = d.drop_duplicates(subset=["label"]).sort_values("sort_key")["label"].tolist()
        pivot = d.pivot_table(index="label", columns="minute_of_hour", values="trades", fill_value=0)
        pivot = pivot.reindex(ordered_labels)
        fig = go.Figure(
            go.Heatmap(
                z=pivot.values, x=pivot.columns, y=pivot.index,
                colorscale="Viridis", colorbar=dict(title="trades"),
                hovertemplate="%{y} :%{x:02d}<br>%{z} trades<extra></extra>",
            )
        )
        fig.add_vline(x=44, line=dict(color="red", width=1, dash="dot"))
        fig.add_vline(x=45, line=dict(color="red", width=1, dash="dot"))
        excl_note = ""
        if exclude_day_hours:
            excl_note = " — " + ", ".join(
                f"{d[:3]} {h:02d} ET excluded" for d, h in sorted(exclude_day_hours)
            )
        fig.update_layout(
            title=f"{venue} · trades per (day-hour-ET × minute-of-hour) — red lines mark :44, :45{excl_note}",
            xaxis_title="minute of hour", yaxis_title="day · hour (ET)",
            height=600, template="plotly_white",
            xaxis=dict(tickmode="linear", tick0=0, dtick=5),
            yaxis=dict(autorange="reversed"),  # Sat 00 at top, Sun 23 at bottom
            margin=dict(l=80, r=20, t=50, b=40),
        )
        return fig

    def flush_staircase():
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=flush_cum["t"], y=flush_cum["rows_at_flush"],
                mode="lines+markers",
                line=dict(color="#7c3aed", width=1.5),
                marker=dict(
                    size=6,
                    color=flush_cum["minute"].apply(lambda m: "red" if m == 44 else "#7c3aed"),
                ),
                text=flush_cum["minute"].apply(lambda m: f":{m:02d}"),
                hovertemplate="%{x|%a %H:%M ET}<br>cum rows: %{y:,}<br>flush: %{text}<extra></extra>",
                name="HL cumulative rows at each flush",
            )
        )
        fig.add_hline(
            y=136164, line=dict(color="red", width=1.5, dash="dash"),
            annotation_text="136k rows = OOM line on 04-18 (256 MB cap)",
            annotation_position="top left",
        )
        fig.update_layout(
            title="Hyperliquid cumulative rows over the weekend, sampled at every flush boundary "
                  "(:04, :09, …, :59). The :44 boundaries are highlighted in red — "
                  "if OOM was triggered by total file size, the crash lands wherever the line "
                  "crosses the dashed threshold, regardless of the hour.",
            xaxis_title="Time (ET)", yaxis_title="Cumulative HL rows since first trade",
            template="plotly_white", height=420, margin=dict(l=60, r=20, t=80, b=40),
        )
        return fig

    fig_trades = line_per_venue("trades", "Trades", "Hypothesis 1: Trade-count burst by minute-of-hour")
    fig_notional = line_per_venue("notional", "Notional (USD)", "Hypothesis 2: Notional volume by minute-of-hour")
    fig_avg = line_per_venue("avg_notional", "Avg trade $", "Hypothesis 3a: Average trade size by minute-of-hour")
    fig_p95 = line_per_venue("p95_notional", "p95 trade $", "Hypothesis 3b: p95 trade size (whales) by minute-of-hour")
    fig_imb = line_per_venue("imbalance_pct", "Imbalance %", "Hypothesis 4: Buy/sell imbalance by minute-of-hour", hover_fmt=":+.1f")
    fig_gap = line_per_venue("median_mark_gap", "Median |price−mark|", "Hypothesis 5: Mark-trade gap by minute-of-hour", hover_fmt=":,.4f")

    heatmaps = {v: heatmap_for(v) for v in ["hyperliquid", "extended", "binance"]}
    fig_flush = flush_staircase()

    # Diagnostic outlier tables — for each minute-of-hour the user flags as anomalous,
    # show its top trades (across all venues) and a comparison of that minute's avg /
    # p95 vs neighbouring minutes. This explains the H3 spikes inline.
    def outlier_box(minute):
        stats_df = con.execute(
            f"""
            SELECT minute_of_hour AS m, COUNT(*) AS n,
                   ROUND(AVG(notional_usd)) AS avg_n,
                   ROUND(QUANTILE_CONT(notional_usd, 0.95)) AS p95
            FROM trades
            WHERE minute_of_hour BETWEEN {minute - 2} AND {minute + 2}
            GROUP BY minute_of_hour ORDER BY minute_of_hour
            """
        ).df()
        # Top 3 per venue at this minute (ROW_NUMBER over the view triggers a
        # DuckDB bind bug with our TIMESTAMPTZ-derived columns; do it in pandas).
        all_at_min = con.execute(
            f"""
            SELECT ts_et, venue, asset_symbol, side, price,
                   ROUND(notional_usd) AS notional, notional_usd AS _raw_notional
            FROM trades
            WHERE minute_of_hour = {minute}
            ORDER BY notional_usd DESC
            """
        ).df()
        if all_at_min.empty:
            top_df = all_at_min
        else:
            top_df = (
                all_at_min.sort_values("_raw_notional", ascending=False)
                .groupby("venue", group_keys=False)
                .head(3)
                .sort_values(["venue", "_raw_notional"], ascending=[True, False])
                .drop(columns=["_raw_notional"])
            )
        if top_df.empty:
            return ""

        stats_rows = "".join(
            f"<tr{' class=hot' if int(r.m) == minute else ''}>"
            f"<td>:{int(r.m):02d}</td><td>{int(r.n):,}</td>"
            f"<td>${int(r.avg_n):,}</td><td>${int(r.p95):,}</td></tr>"
            for r in stats_df.itertuples()
        )
        top_rows = "".join(
            f"<tr><td>{pd.Timestamp(r.ts_et).strftime('%a %m-%d %H:%M:%S.%f')[:-3]}</td>"
            f"<td>{r.venue}</td><td>{r.asset_symbol}</td>"
            f"<td><span class='pill {r.side}'>{r.side}</span></td>"
            f"<td style='text-align:right'>{r.price:.3f}</td>"
            f"<td style='text-align:right'>${int(r.notional):,}</td></tr>"
            for r in top_df.itertuples()
        )
        return f"""
<div class="outlier-box">
  <h4>Minute :{minute:02d} — what's driving the spike?</h4>
  <div class="outlier-grid">
    <div>
      <div class="outlier-label">Avg + p95 vs ±2-min neighbours</div>
      <table class="mini">
        <thead><tr><th>Min</th><th>Trades</th><th>Avg $</th><th>p95 $</th></tr></thead>
        <tbody>{stats_rows}</tbody>
      </table>
    </div>
    <div>
      <div class="outlier-label">Top 3 trades per venue at :{minute:02d} (any day)</div>
      <table class="mini">
        <thead><tr><th>Time (ET)</th><th>Venue</th><th>Asset</th><th>Side</th>
          <th style='text-align:right'>Price</th><th style='text-align:right'>Notional</th></tr></thead>
        <tbody>{top_rows}</tbody>
      </table>
    </div>
  </div>
</div>
"""

    inspect_html = "".join(outlier_box(m) for m in INSPECT_MINUTES)

    def to_div(fig):
        return fig.to_html(include_plotlyjs=False, full_html=False, div_id=None)

    summary_rows = "".join(
        f"<tr><td>{row['venue']}</td><td>{row['asset_symbol']}</td>"
        f"<td style='text-align:right'>{row['rows']:,}</td>"
        f"<td>{row['first_trade_et']}</td><td>{row['last_trade_et']}</td></tr>"
        for _, row in summary.iterrows()
    )

    filter_banner = ""
    if exclude_day_hours:
        excl_human = ", ".join(
            f"<code>{d} {h:02d}:00 ET</code>" for d, h in sorted(exclude_day_hours)
        )
        filter_banner = (
            f'<div class="box warn"><strong>Filtered view.</strong> Trades from {excl_human} '
            f'are excluded — these are the only CME transition hours inside the recorder\'s '
            f'collection window (<strong>Fri 17:00 ET close</strong> and <strong>Sun 18:00 ET reopen</strong>). '
            f'All other weekend hours (including Sat 17–18 ET, when CME is fully closed) are kept.</div>'
        )

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Minute-of-hour pattern report — 2026-04-25/26 {title_suffix}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1.5em; color: #111; line-height: 1.55; }}
  h1 {{ margin-bottom: 0.2em; }}
  h2 {{ margin-top: 2em; border-bottom: 1px solid #e5e5e5; padding-bottom: 0.3em; }}
  h3 {{ margin-top: 1.5em; }}
  .meta {{ color: #666; font-size: 0.92em; margin-bottom: 1.5em; }}
  .box {{ background: #f8f9fb; border-left: 4px solid #7c3aed; padding: 0.9em 1.2em; margin: 1.2em 0; border-radius: 4px; }}
  .box.warn {{ border-left-color: #f59e0b; background: #fff8eb; }}
  .box.kill {{ border-left-color: #ef4444; background: #fef2f2; }}
  table.summary {{ border-collapse: collapse; margin: 1em 0; font-size: 0.92em; }}
  table.summary th, table.summary td {{ border: 1px solid #ddd; padding: 0.4em 0.8em; }}
  table.summary th {{ background: #f3f4f6; }}
  .hyp {{ font-weight: 600; color: #7c3aed; }}
  .outlier-box {{ background: #fff8eb; border-left: 4px solid #f59e0b; border-radius: 4px;
                  padding: 0.9em 1.2em; margin: 1em 0 1.5em; }}
  .outlier-box h4 {{ margin: 0 0 0.6em; font-size: 0.98em; color: #92400e; }}
  .outlier-grid {{ display: grid; grid-template-columns: 240px 1fr; gap: 1.5em; }}
  .outlier-label {{ font-size: 0.78em; color: #666; text-transform: uppercase;
                    letter-spacing: 0.06em; margin-bottom: 0.3em; }}
  table.mini {{ border-collapse: collapse; width: 100%; font-size: 0.82em; }}
  table.mini th, table.mini td {{ border: 1px solid #e5e7eb; padding: 0.25em 0.6em; text-align: left; }}
  table.mini th {{ background: #fef3c7; font-weight: 600; }}
  table.mini tr.hot td {{ background: #fef3c7; font-weight: 600; }}
  .pill {{ display: inline-block; padding: 0px 6px; border-radius: 999px; font-size: 0.78em; font-weight: 600; }}
  .pill.buy {{ background: #d1fae5; color: #065f46; }}
  .pill.sell {{ background: #fee2e2; color: #991b1b; }}
  code {{ background: #f3f4f6; padding: 0.1em 0.4em; border-radius: 3px; font-size: 0.92em; }}
</style>
</head><body>

<h1>Does anything special happen at the 45th minute? <span style="font-size:0.6em;color:#666;font-weight:normal">{title_suffix}</span></h1>
<div class="meta">
  Hypothesis test using the last full collection window (Sat 2026-04-25 → Sun 2026-04-26 ET).
  The actual crash weekend (04-18/19) is not available — both HL parquet files were
  corrupted by the OOM and we have no replacement. This is a proxy.
</div>

{filter_banner}

<div class="box">
  <strong>Background.</strong> The HL recorder crashed mid-Saturday at <code>09:44 ET</code>
  and again mid-Sunday at <code>10:45 ET</code>. The proximate cause was an OOM kill — every
  flush re-loaded the entire daily Parquet file into memory, hitting the 256 MB container
  cap. Fixed in commit <code>08a8a7d</code>. The question this report tries to answer:
  <strong>was the :44/:45 timing meaningful, or just the minute where the cumulative
  buffer happened to cross threshold?</strong>
</div>

<div class="box warn">
  <strong>Caveats up front.</strong>
  <ul>
    <li><strong>n = 1 weekend.</strong> Patterns visible here might not generalise.</li>
    <li>The crash itself was driven by <em>cumulative</em> file size, not <em>per-minute</em>
      activity. A market event at :45 only matters if it spikes activity enough to push
      a borderline-OOM container over the edge — and the postmortem shows the staircase
      was already 95.9% of cap before any final spike.</li>
    <li>The 5-minute flush schedule lands on <code>:04, :09, …, :44, :49, :54, :59</code>.
      The 9th flush of the day is <code>:44</code>. Coincidence is plausible.</li>
  </ul>
</div>

<h3>Data coverage</h3>
<table class="summary">
  <tr><th>venue</th><th>symbol</th><th>rows</th><th>first trade (ET)</th><th>last trade (ET)</th></tr>
  {summary_rows}
</table>

<h2>Section 1 · Per-venue minute-of-hour patterns</h2>
<p>Each chart aggregates all trades from the weekend by their minute-of-hour (0–59 ET).
The shaded red band marks <code>:44–:46</code>. If the <span class="hyp">:45 hypothesis</span>
holds, we expect a visible spike (or trough, for some metrics) inside the band that does
not appear elsewhere on the curve.</p>

<h3>H1 · Trade-count burst</h3>
{to_div(fig_trades)}

<h3>H2 · Notional volume</h3>
{to_div(fig_notional)}

<h3>H3 · Trade size (avg + p95)</h3>
{to_div(fig_avg)}
{to_div(fig_p95)}
{inspect_html}

<h3>H4 · Buy/sell imbalance</h3>
{to_div(fig_imb)}

<h3>H5 · Trade-vs-mark gap (proxy for stale-oracle moments)</h3>
{to_div(fig_gap)}

<h2>Section 2 · Day×hour × minute heatmaps</h2>
<p>If <code>:45</code> is special, every row of the heatmap should show a brighter column
at minute 44–45. A vertical red dashed line marks <code>:44</code> and <code>:45</code>
on each heatmap. Rows are labelled by day name and ET hour.</p>

<h3>Hyperliquid (the venue that actually crashed)</h3>
{to_div(heatmaps["hyperliquid"])}

<h3>Extended</h3>
{to_div(heatmaps["extended"])}

<h3>Binance</h3>
{to_div(heatmaps["binance"])}

<h2>Section 3 · Null-hypothesis test — the flush staircase</h2>
<p>Reconstructs the cumulative HL row count sampled at every <code>:04, :09, …, :59</code>
flush boundary across the weekend. The dashed line is the OOM threshold from the postmortem
(136,164 rows triggered the kill on 04-18). <span class="hyp">If the OOM is purely a function
of cumulative size, the crash will land on whichever <code>:04+5k</code> minute the line
crosses the threshold — <code>:44</code> wasn't selected by anything in the market, it was
selected by the flush schedule.</span></p>
{to_div(fig_flush)}

<h2>How to read the result</h2>
<div class="box">
  <strong>Reading guide.</strong>
  <ul>
    <li><strong>If H1–H5 charts are flat across all minutes</strong> (no visible spike at
      :44–:46) and the staircase chart shows the threshold getting crossed at the next
      :04+5k flush after the cumulative count exceeds it: the <span class="hyp">:45
      hypothesis is unsupported</span>. The original crashes are explained entirely by the
      cumulative-size bug + the 5-min flush cadence.</li>
    <li><strong>If H1–H2 spike at :44–:46 on Hyperliquid only</strong> (and not on
      Extended/Binance): a real HL-specific event at :45 is plausible — likely tied to
      Hyperliquid's hourly funding cycle (funding pays at :00, repositioning may bunch
      pre-funding at :45–:59).</li>
    <li><strong>If H5 (mark-trade gap) spikes at :45 on HL only</strong>: oracle update
      cadence may pause around :45, leaving trade price free to drift.</li>
    <li><strong>If patterns appear on all three venues</strong>: market-wide schedule
      (e.g., MM bots running cron, news-feed cycles, exchange-side snapshot intervals).</li>
  </ul>
</div>

<div class="box kill">
  <strong>What this report cannot tell us.</strong> n = 1 weekend. Even a clean spike at
  :45 is suggestive, not confirmatory. The right next step if a pattern appears is to
  re-run the same aggregation across the next 4–5 weekends as data accumulates.
</div>

</body></html>
"""
    out_path.write_text(html)
    print(f"wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    for mode in MODES:
        build(mode["exclude_day_hours"], mode["out"], mode["title_suffix"])
