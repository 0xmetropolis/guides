"""
Address activity report — every Hyperliquid trade involving one address across
all available HL data (Apr 2026 onward). All times in US/Eastern.

Includes a FIFO P&L estimate per asset (realized + mark-to-market unrealized).

The address surfaced from build_news_event_report.py: at Sat 11:50:54 ET this
taker swept resting offers across BOTH Brent and WTI on Hyperliquid in a single
millisecond — about $457k of Brent and ~$420k of WTI — minutes before Trump's
"we have all the cards" Iran cancellation news, which lifted oil ~2% on the day.

Usage:
    uv run python build_address_activity_report.py
"""

import sys
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Pass the target address as the first CLI arg; defaults to the cb71 taker.
ADDRESS = (sys.argv[1] if len(sys.argv) > 1 else "0xcb71e31d4b3538483fc0a07de20b8eb8f66c7853").lower()
HL_DATA_ROOT = "/Users/marcolavagnino/metal/hl-trades-recorder/data/hyperliquid"
OUT = Path(__file__).parent / f"address_activity_{ADDRESS[:6]}_{ADDRESS[-4:]}.html"

# When an asset has more than this many trades, switch the price-marker chart
# to 5-min density bins (one marker per (bin × side × role)) so the HTML stays
# small enough to render in a browser.
DENSE_THRESHOLD = 3000

# Cap the inline trade-list table at this many rows (top-N by notional desc).
TRADE_TABLE_CAP = 300

# Per-address narrative shown in the top "Why this address" box.
ADDRESS_NARRATIVES = {
    "0xcb71e31d4b3538483fc0a07de20b8eb8f66c7853": (
        "In the news-attribution report's E1 (Sat noon rally), the largest pre-news trade "
        "on the chart is a <strong>$455k Brent buy at $100.77</strong> with this address as "
        "<strong>taker</strong>, landing <code>2026-04-25 11:50:54 ET</code> — minutes "
        "before the Trump-cancels-Iran-envoys news anchor. The same millisecond swept "
        "multiple offers on <code>xyz:BRENTOIL</code>; ~50 seconds later a similar sweep "
        "hit <code>xyz:CL</code> (WTI). Brent and WTI both rallied ~1.5–2% in the next "
        "two hours."
    ),
    "0xa2ce501d9c0c5e23d34272f84402cfb7835b3126": (
        "Counterparty on the suspicious E1 trade. While the cb71 taker was sweeping the "
        "book at <code>2026-04-25 11:50:54 ET</code> on Brent, this address was the "
        "<strong>maker</strong> sitting on the offer at $100.77 — about $455k of resting "
        "sell liquidity that got eaten in a single millisecond minutes before the rally. "
        "Across the recorder history this address shows up 100% as maker (no taker activity), "
        "consistent with an automated market-maker / passive liquidity provider rather "
        "than a directional trader."
    ),
    "0xce975678a14f17a15c946b95704744cd7c677e78": (
        "Surfaced from the minute-:07 spike investigation. In the very first minute of the "
        "2026-04-24/26 collection window (<code>Fri 16:07:07–:24 ET</code>), this address "
        "appears as <strong>both maker and taker</strong> across <strong>both Brent and "
        "WTI</strong> in a 17-second burst — 8 of the top 10 HL trades at minute :07 across "
        "the entire weekend involve it. The dual-sided, two-asset, sub-second cadence is "
        "the signature of an automated <strong>market maker / cross-venue arbitrageur</strong>, "
        "not a directional trader."
    ),
}

# Glob patterns covering the full HL recorder history. Crafted to exclude the
# only known corrupt file at 2026-04-27T11 (the in-progress current-hour file
# at the time of the snapshot).
DATA_GLOBS = [
    f"{HL_DATA_ROOT}/**/2026-04-1*T*.parquet",          # Apr 10–19
    f"{HL_DATA_ROOT}/**/2026-04-2[0-6]T*.parquet",       # Apr 20–26
    f"{HL_DATA_ROOT}/**/2026-04-27T0*.parquet",          # Apr 27 hours 00–09
    f"{HL_DATA_ROOT}/**/2026-04-27T10*.parquet",         # Apr 27 hour 10
]

# Recorder-window news anchors (still useful to mark even when viewing full history)
NEWS_ANCHORS = [
    ("2026-04-25 12:00:00", "Trump cancels Iran envoys"),
    ("2026-04-26 11:00:00", "Iran-Oman Hormuz talks"),
    ("2026-04-26 17:00:00", "\"Hormuz won't reopen\" + CME reopen"),
]


def fifo_pnl(trades_df):
    """FIFO single-bucket PnL across one asset's chronological trades.

    Inputs (per row): effective_side ('buy'/'sell'), price, size, notional_usd, ts_et.
    Returns a DataFrame with running pos, avg_entry, realized_to_date,
    pnl_delta_realized, mtm_pnl (realized + unrealized at current price).
    """
    pos = 0.0
    avg = 0.0
    realized = 0.0
    out = []
    for _, t in trades_df.iterrows():
        size = float(t["size"])
        price = float(t["price"])
        signed = size if t["effective_side"] == "buy" else -size

        delta = 0.0
        if pos == 0.0:
            pos = signed
            avg = price
        elif (pos > 0) == (signed > 0):
            new_pos = pos + signed
            avg = (avg * pos + price * signed) / new_pos
            pos = new_pos
        else:
            close_size = min(abs(signed), abs(pos))
            sign = 1 if pos > 0 else -1
            delta = (price - avg) * close_size * sign
            realized += delta
            new_pos = pos + signed
            if abs(new_pos) < 1e-9:
                pos, avg = 0.0, 0.0
            elif (pos > 0) != (new_pos > 0):
                pos, avg = new_pos, price
            else:
                pos = new_pos

        unrealized = (price - avg) * pos if pos != 0.0 else 0.0
        out.append({
            "ts_et": t["ts_et"],
            "side": t["effective_side"],
            "price": price,
            "size": size,
            "notional": float(t["notional_usd"]),
            "pos_after": pos,
            "avg_after": avg,
            "realized_to_date": realized,
            "pnl_delta_realized": delta,
            "mtm_pnl": realized + unrealized,
        })
    return pd.DataFrame(out)


def main():
    globs_sql = "[" + ", ".join(f"'{g}'" for g in DATA_GLOBS) + "]"
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE OR REPLACE VIEW trades AS
        SELECT
          venue, asset_symbol, side, price, size, notional_usd, mark_price,
          maker_user_address, taker_user_address,
          (to_timestamp(timestamp/1000.0) AT TIME ZONE 'America/New_York') AS ts_et,
          timestamp AS ts_ms
        FROM read_parquet({globs_sql}, union_by_name=true)
        WHERE venue = 'hyperliquid'
        """
    )

    addr_sql = f"'{ADDRESS}'"
    df = con.execute(
        f"""
        SELECT
          ts_et, ts_ms, asset_symbol, side AS taker_side,
          price, size, notional_usd, mark_price,
          maker_user_address, taker_user_address,
          CASE WHEN taker_user_address = {addr_sql} THEN 'taker' ELSE 'maker' END AS role,
          CASE
            WHEN taker_user_address = {addr_sql} THEN side
            WHEN side = 'buy' THEN 'sell' ELSE 'buy'
          END AS effective_side,
          CASE WHEN taker_user_address = {addr_sql}
               THEN maker_user_address ELSE taker_user_address END AS counterparty
        FROM trades
        WHERE taker_user_address = {addr_sql} OR maker_user_address = {addr_sql}
        ORDER BY ts_ms
        """
    ).df()

    # Per-asset role/direction summary (raw bought/sold notionals)
    summary = con.execute(
        f"""
        WITH a AS (
          SELECT asset_symbol,
                 CASE WHEN taker_user_address = {addr_sql} THEN 'taker' ELSE 'maker' END AS role,
                 CASE
                   WHEN taker_user_address = {addr_sql} THEN side
                   WHEN side='buy' THEN 'sell' ELSE 'buy'
                 END AS eff,
                 notional_usd
          FROM trades
          WHERE taker_user_address = {addr_sql} OR maker_user_address = {addr_sql}
        )
        SELECT asset_symbol, role,
               COUNT(*) AS n,
               SUM(CASE WHEN eff='buy' THEN notional_usd ELSE 0 END) AS bought,
               SUM(CASE WHEN eff='sell' THEN notional_usd ELSE 0 END) AS sold,
               SUM(CASE WHEN eff='buy' THEN notional_usd ELSE -notional_usd END) AS net_buy
        FROM a GROUP BY 1,2 ORDER BY 1,2
        """
    ).df()

    # Last mark / last trade price per asset (for end-state MTM)
    last_state = con.execute(
        """
        SELECT
          asset_symbol,
          last(mark_price ORDER BY ts_ms) AS mark_price,
          last(price ORDER BY ts_ms) AS price,
          MAX(ts_et) AS ts_et
        FROM trades
        GROUP BY asset_symbol
        """
    ).df()
    last_mark_by_asset = {
        r.asset_symbol: (r.mark_price if pd.notna(r.mark_price) and r.mark_price > 0 else r.price)
        for r in last_state.itertuples()
    }
    last_state_ts = last_state["ts_et"].max()

    # Background prices (5-min) per asset for the chart backdrop
    assets = sorted(df["asset_symbol"].unique().tolist())
    bg_prices = {}
    for asset in assets:
        bg_prices[asset] = con.execute(
            """
            SELECT time_bucket(INTERVAL 5 MINUTE, ts_et) AS bin,
                   last(price ORDER BY ts_ms) AS c
            FROM trades
            WHERE asset_symbol = ?
              AND ts_et >= (
                SELECT MIN(ts_et) - INTERVAL 1 HOUR FROM trades
                WHERE asset_symbol = ?
                  AND (taker_user_address = ? OR maker_user_address = ?)
              )
              AND ts_et <= (
                SELECT MAX(ts_et) + INTERVAL 1 HOUR FROM trades
                WHERE asset_symbol = ?
                  AND (taker_user_address = ? OR maker_user_address = ?)
              )
            GROUP BY bin ORDER BY bin
            """,
            [asset, asset, ADDRESS, ADDRESS, asset, ADDRESS, ADDRESS],
        ).df()

    # ---- Compute PnL per asset ----
    pnl_by_asset = {}
    final_pos = {}
    for asset in assets:
        sub = df[df["asset_symbol"] == asset].copy()
        pnl_df = fifo_pnl(sub)
        pnl_by_asset[asset] = pnl_df
        if pnl_df.empty:
            continue
        last_row = pnl_df.iloc[-1]
        last_mark = last_mark_by_asset.get(asset, last_row["price"])
        unreal = (last_mark - last_row["avg_after"]) * last_row["pos_after"] if last_row["pos_after"] != 0 else 0.0
        final_pos[asset] = {
            "realized": last_row["realized_to_date"],
            "pos_size": last_row["pos_after"],
            "avg_entry": last_row["avg_after"] if last_row["pos_after"] != 0 else 0.0,
            "last_mark": last_mark,
            "unrealized": unreal,
            "total_pnl": last_row["realized_to_date"] + unreal,
        }

    total_realized = sum(f["realized"] for f in final_pos.values())
    total_unrealized = sum(f["unrealized"] for f in final_pos.values())
    total_pnl = total_realized + total_unrealized

    # ---- Charts: per asset, 3 stacked rows (price+dots, position size, cum PnL) ----
    figs = []
    for asset in assets:
        addr_trades = df[df["asset_symbol"] == asset]
        bg = bg_prices[asset]
        pnl_df = pnl_by_asset[asset]

        is_dense = len(addr_trades) > DENSE_THRESHOLD
        row1_title = (
            f"{asset} — HL 5-min close (grey); each marker = ONE 5-MIN BIN of this address's "
            f"trades (vol-weighted price, size = $notional). circle=taker, diamond=maker. "
            f"[{len(addr_trades):,} trades aggregated]"
            if is_dense else
            f"{asset} — HL 5-min close (grey) with this address's trades dotted; "
            f"colour = effective side, circle = taker, diamond = maker"
        )
        fig = make_subplots(
            rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.05,
            row_heights=[0.55, 0.22, 0.23],
            subplot_titles=(
                row1_title,
                "Net position over time (signed contracts)",
                "Cumulative PnL — realized (purple) and mark-to-market at trade price (orange)",
            ),
        )

        fig.add_trace(
            go.Scatter(
                x=bg["bin"], y=bg["c"], mode="lines",
                line=dict(color="#9ca3af", width=1.3),
                name="HL close", showlegend=False,
                hovertemplate="%{x|%a %b %d %H:%M ET}<br>$%{y:.3f}<extra></extra>",
            ),
            row=1, col=1,
        )

        dense_mode = len(addr_trades) > DENSE_THRESHOLD
        if dense_mode:
            # Aggregate into 5-min bins per (side, role). One marker per bin,
            # sized by total notional in the bin, y = volume-weighted avg price.
            tmp = addr_trades.copy()
            tmp["bin"] = tmp["ts_et"].dt.floor("5min")
            tmp["pn"] = tmp["price"] * tmp["notional_usd"]
            agg = (
                tmp.groupby(["bin", "effective_side", "role"], as_index=False)
                .agg(
                    n=("price", "size"),
                    notional=("notional_usd", "sum"),
                    pn=("pn", "sum"),
                )
            )
            agg["price"] = agg["pn"] / agg["notional"]
            for side, color in [("buy", "#10b981"), ("sell", "#ef4444")]:
                for role, sym in [("taker", "circle"), ("maker", "diamond")]:
                    d = agg[(agg["effective_side"] == side) & (agg["role"] == role)]
                    if d.empty:
                        continue
                    fig.add_trace(
                        go.Scatter(
                            x=d["bin"], y=d["price"], mode="markers",
                            name=f"{side} {role} ({int(d['n'].sum())})",
                            marker=dict(
                                size=d["notional"].apply(
                                    lambda v: max(4, min(34, (v / 50_000) ** 0.55 * 6))
                                ),
                                color=color, opacity=0.45,
                                line=dict(color="white", width=0.5),
                                symbol=sym,
                            ),
                            customdata=list(zip(d["n"], d["notional"])),
                            hovertemplate=(
                                "%{x|%a %b %d %H:%M ET} bin<br>"
                                f"{role} {side}<br>"
                                "%{customdata[0]:,} trades, $%{customdata[1]:,.0f}<br>"
                                "vw price %{y:.3f}<extra></extra>"
                            ),
                        ),
                        row=1, col=1,
                    )
        else:
            for side, color in [("buy", "#10b981"), ("sell", "#ef4444")]:
                d = addr_trades[addr_trades["effective_side"] == side]
                if d.empty:
                    continue
                fig.add_trace(
                    go.Scatter(
                        x=d["ts_et"], y=d["price"], mode="markers",
                        name=f"{side} ({len(d)})",
                        marker=dict(
                            size=d["notional_usd"].apply(lambda v: max(5, min(28, (v / 5000) ** 0.65 * 4))),
                            color=color, opacity=0.55,
                            line=dict(color="white", width=1),
                            symbol=d["role"].map({"taker": "circle", "maker": "diamond"}),
                        ),
                        text=[
                            f"{r.role} {r.effective_side} ${r.notional_usd:,.0f}<br>"
                            f"counterparty {r.counterparty[:6]}…{r.counterparty[-4:] if r.counterparty else ''}"
                            for r in d.itertuples()
                        ],
                        hovertemplate="%{x|%a %b %d %H:%M:%S ET}<br>%{text}<br>price %{y:.3f}<extra></extra>",
                    ),
                    row=1, col=1,
                )

        # Downsample position/PnL series for dense addresses to keep file small.
        # Take last value per 1-min bucket (preserves shape; line traces don't
        # need every individual trade to convey position trajectory).
        if is_dense and not pnl_df.empty:
            ds = pnl_df.copy()
            ds = ds.set_index("ts_et")
            ds = ds.resample("1min").last().dropna(how="all").reset_index()
            pnl_plot = ds
        else:
            pnl_plot = pnl_df

        # Position size
        fig.add_trace(
            go.Scatter(
                x=pnl_plot["ts_et"], y=pnl_plot["pos_after"],
                mode="lines", line=dict(color="#0ea5e9", width=2),
                name="position", showlegend=False,
                hovertemplate="%{x|%a %b %d %H:%M:%S ET}<br>pos %{y:,.2f}<extra></extra>",
            ),
            row=2, col=1,
        )
        fig.add_hline(y=0, line=dict(color="#999", width=1, dash="dot"), row=2, col=1)

        # Cumulative PnL (realized + mtm)
        fig.add_trace(
            go.Scatter(
                x=pnl_plot["ts_et"], y=pnl_plot["realized_to_date"],
                mode="lines", line=dict(color="#7c3aed", width=2),
                name="realized PnL ($)",
                hovertemplate="%{x|%a %b %d %H:%M:%S ET}<br>realized $%{y:,.0f}<extra></extra>",
            ),
            row=3, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=pnl_plot["ts_et"], y=pnl_plot["mtm_pnl"],
                mode="lines", line=dict(color="#f59e0b", width=1.5, dash="dot"),
                name="mark-to-market PnL ($)",
                hovertemplate="%{x|%a %b %d %H:%M:%S ET}<br>MTM $%{y:,.0f}<extra></extra>",
            ),
            row=3, col=1,
        )

        # If position is still open after the last trade, extend the MTM line by
        # marking the held position against subsequent 5-min closes. Without this,
        # one-shot fillers (passive MMs that get hit and walk away) show a flat-zero
        # MTM line because their last trade price equals their avg entry.
        if not pnl_df.empty:
            last_t = pnl_df.iloc[-1]["ts_et"]
            last_pos = pnl_df.iloc[-1]["pos_after"]
            last_avg = pnl_df.iloc[-1]["avg_after"]
            last_realized = pnl_df.iloc[-1]["realized_to_date"]
            if last_pos != 0 and last_avg != 0:
                bg_after = bg[bg["bin"] > last_t].copy()
                if not bg_after.empty:
                    bg_after["mtm"] = last_realized + (bg_after["c"] - last_avg) * last_pos
                    # Connect the extension to the last trade-time point
                    ext_x = pd.concat([pd.Series([last_t]), bg_after["bin"]], ignore_index=True)
                    ext_y = pd.concat([pd.Series([last_realized]), bg_after["mtm"]], ignore_index=True)
                    fig.add_trace(
                        go.Scatter(
                            x=ext_x, y=ext_y,
                            mode="lines", line=dict(color="#f59e0b", width=1.5),
                            name="MTM (open position vs market) ($)",
                            hovertemplate="%{x|%a %b %d %H:%M ET}<br>MTM $%{y:,.0f}<extra></extra>",
                        ),
                        row=3, col=1,
                    )

        fig.add_hline(y=0, line=dict(color="#999", width=1, dash="dot"), row=3, col=1)

        for ts, label in NEWS_ANCHORS:
            fig.add_vline(x=ts, line=dict(color="red", width=1, dash="dash"), row=1, col=1)
            fig.add_annotation(
                x=ts, y=1, yref="y domain", row=1, col=1,
                text=label, showarrow=False, yshift=12,
                font=dict(color="red", size=9), textangle=-15,
            )

        fig.update_layout(
            title=f"{asset}",
            template="plotly_white", height=720,
            margin=dict(l=60, r=20, t=70, b=30),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="closest",
        )
        fig.update_yaxes(title_text="price", row=1, col=1)
        fig.update_yaxes(title_text="contracts", row=2, col=1)
        fig.update_yaxes(title_text="$", row=3, col=1)
        figs.append((asset, fig))

    # ---- Trade table (cap to top-N by notional to keep file size sane) ----
    table_was_capped = len(df) > TRADE_TABLE_CAP
    df_table = (
        df.nlargest(TRADE_TABLE_CAP, "notional_usd").copy()
        if table_was_capped else df.copy()
    )
    df_table["ts_et"] = df_table["ts_et"].dt.strftime("%Y-%m-%d %H:%M:%S.%f").str.slice(0, -3)
    df_table["notional_usd"] = df_table["notional_usd"].apply(lambda v: f"${v:,.0f}")
    df_table["price"] = df_table["price"].apply(lambda v: f"{v:.3f}")
    df_table["counterparty"] = df_table["counterparty"].apply(
        lambda a: f"{a[:6]}…{a[-4:]}" if a else "—"
    )
    df_table = df_table[[
        "ts_et", "asset_symbol", "role", "effective_side",
        "price", "notional_usd", "counterparty",
    ]].rename(columns={
        "ts_et": "Time (ET)",
        "asset_symbol": "Asset",
        "role": "Role",
        "effective_side": "Side (addr POV)",
        "price": "Price",
        "notional_usd": "Notional",
        "counterparty": "Counterparty",
    })
    table_html = df_table.to_html(index=False, classes="trades-table", border=0, escape=False)
    table_caption = (
        f"<p class='meta'>Showing the top <strong>{TRADE_TABLE_CAP:,}</strong> trades by notional "
        f"(of <strong>{len(df):,}</strong> total). Sorted by notional descending.</p>"
        if table_was_capped else ""
    )

    # ---- Header stats ----
    total_n = len(df)
    total_notional = df["notional_usd"].sum()
    n_taker = (df["role"] == "taker").sum()
    n_maker = (df["role"] == "maker").sum()
    first_ts = df["ts_et"].min().strftime("%a %Y-%m-%d %H:%M:%S ET")
    last_ts = df["ts_et"].max().strftime("%a %Y-%m-%d %H:%M:%S ET")
    last_state_ts_str = pd.Timestamp(last_state_ts).strftime("%a %Y-%m-%d %H:%M:%S ET")

    pnl_class = "stat pnl-pos" if total_pnl >= 0 else "stat pnl-neg"

    summary_rows = "".join(
        f"<tr><td>{r.asset_symbol}</td><td>{r.role}</td>"
        f"<td style='text-align:right'>{int(r.n)}</td>"
        f"<td style='text-align:right'>${r.bought:,.0f}</td>"
        f"<td style='text-align:right'>${r.sold:,.0f}</td>"
        f"<td style='text-align:right'><b>${r.net_buy:+,.0f}</b></td></tr>"
        for r in summary.itertuples()
    )

    pnl_rows = "".join(
        f"<tr><td>{asset}</td>"
        f"<td style='text-align:right'>${f['realized']:+,.0f}</td>"
        f"<td style='text-align:right'>{f['pos_size']:+,.2f}</td>"
        f"<td style='text-align:right'>{('$' + format(f['avg_entry'], ',.3f')) if f['pos_size'] != 0 else '—'}</td>"
        f"<td style='text-align:right'>${f['last_mark']:,.3f}</td>"
        f"<td style='text-align:right'>${f['unrealized']:+,.0f}</td>"
        f"<td style='text-align:right'><b>${f['total_pnl']:+,.0f}</b></td></tr>"
        for asset, f in final_pos.items()
    )

    chart_blocks = "\n".join(
        f"<h3>{asset}</h3>\n" + fig.to_html(include_plotlyjs=False, full_html=False)
        for asset, fig in figs
    )

    narrative_html = ADDRESS_NARRATIVES.get(
        ADDRESS,
        f"Address pulled from the recorder. No specific narrative attached for "
        f"<code>{ADDRESS}</code> — see the activity panels and PnL summary below.",
    )

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Address activity — {ADDRESS}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1180px; margin: 2em auto; padding: 0 1.5em; color: #111; line-height: 1.6; }}
  h1 {{ margin-bottom: 0.2em; font-size: 1.55em; }}
  h2 {{ margin-top: 2.5em; border-bottom: 1px solid #ddd; padding-bottom: 0.3em; }}
  h3 {{ margin-top: 1.5em; }}
  .meta {{ color: #666; font-size: 0.92em; }}
  code {{ background: #f3f4f6; padding: 0.1em 0.4em; border-radius: 3px; font-size: 0.9em; word-break: break-all; }}
  .box {{ background: #f8f9fb; border-left: 4px solid #7c3aed; padding: 1em 1.3em; margin: 1.2em 0; border-radius: 4px; }}
  .box.warn {{ border-left-color: #f59e0b; background: #fff8eb; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1em; margin: 1.2em 0; }}
  .stat {{ background: #f8f9fb; border-radius: 6px; padding: 0.8em 1em; border-left: 3px solid #7c3aed; }}
  .stat .num {{ font-size: 1.4em; font-weight: 600; color: #111; }}
  .stat .lbl {{ font-size: 0.78em; color: #666; text-transform: uppercase; letter-spacing: 0.06em; }}
  .stat.pnl-pos {{ border-left-color: #10b981; background: #ecfdf5; }}
  .stat.pnl-pos .num {{ color: #065f46; }}
  .stat.pnl-neg {{ border-left-color: #ef4444; background: #fef2f2; }}
  .stat.pnl-neg .num {{ color: #991b1b; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.86em; margin: 1em 0; }}
  table th, table td {{ border: 1px solid #ddd; padding: 0.4em 0.7em; text-align: left; }}
  table th {{ background: #f3f4f6; position: sticky; top: 0; }}
  table.trades-table tbody tr:hover {{ background: #fafbfc; }}
  .table-wrap {{ max-height: 540px; overflow-y: auto; border: 1px solid #ddd; border-radius: 4px; }}
</style>
</head><body>

<h1>Address activity — <code>{ADDRESS}</code></h1>
<div class="meta">
  All Hyperliquid trades for this address across the entire available HL recorder
  history. All timestamps below are in <strong>US/Eastern</strong>.
</div>

<div class="box">
  <strong>Why this address.</strong> {narrative_html}
</div>

<div class="stats">
  <div class="stat"><div class="num">{total_n:,}</div><div class="lbl">total trades</div></div>
  <div class="stat"><div class="num">${total_notional/1e6:.2f}M</div><div class="lbl">total notional</div></div>
  <div class="stat"><div class="num">{n_taker:,} / {n_maker:,}</div><div class="lbl">taker / maker</div></div>
  <div class="{pnl_class}"><div class="num">${total_pnl:+,.0f}</div><div class="lbl">total PnL (realized + MTM)</div></div>
</div>

<div class="meta">
  First trade: <code>{first_ts}</code> &nbsp;·&nbsp; Last trade: <code>{last_ts}</code><br>
  Last data point in the recorder: <code>{last_state_ts_str}</code> (used as the mark
  for end-state mark-to-market).
</div>

<h2>PnL summary</h2>
<p>Per-asset FIFO single-bucket model: realized PnL is locked in at every direction-flip
or position-close; unrealized is the open position marked to the latest mark price (or
last trade price if mark is missing). Total = realized + unrealized.</p>
<table>
  <thead><tr>
    <th>Asset</th>
    <th style="text-align:right">Realized</th>
    <th style="text-align:right">Final position</th>
    <th style="text-align:right">Avg entry</th>
    <th style="text-align:right">Last mark</th>
    <th style="text-align:right">Unrealized</th>
    <th style="text-align:right">Total</th>
  </tr></thead>
  <tbody>{pnl_rows}</tbody>
  <tfoot>
    <tr style="font-weight:600;background:#f3f4f6">
      <td>All</td>
      <td style="text-align:right">${total_realized:+,.0f}</td>
      <td colspan="3"></td>
      <td style="text-align:right">${total_unrealized:+,.0f}</td>
      <td style="text-align:right">${total_pnl:+,.0f}</td>
    </tr>
  </tfoot>
</table>

<h2>Per-asset role &amp; direction</h2>
<table>
  <thead><tr><th>Asset</th><th>Role</th><th style="text-align:right">Trades</th>
  <th style="text-align:right">Bought $</th><th style="text-align:right">Sold $</th>
  <th style="text-align:right">Net buy $</th></tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>Activity over time, per asset</h2>
<p>Top: HL 5-min close (grey) with this address's trades dotted (size ∝ notional,
green = buy / red = sell, circle = taker / diamond = maker). Middle: net position size
(positive = long, negative = short). Bottom: cumulative PnL — purple solid is realized,
orange dotted is mark-to-market (realized + unrealized at the current trade price).
Red dashed lines mark the news anchors from the news-attribution report.</p>

{chart_blocks}

<h2>Largest trades</h2>
{table_caption}
<div class="table-wrap">
{table_html}
</div>

<h2>Limitations</h2>
<div class="box warn">
  <ul>
    <li>Hyperliquid only — Extended &amp; Binance don't expose maker/taker addresses,
      so this address's activity (if any) on those venues is invisible.</li>
    <li>Data range = whatever the recorder has captured. The recorder's first hour with
      data is around mid-April 2026; activity from earlier is not in the file.</li>
    <li>FIFO single-bucket is a simplification. It treats partial closes as drawing from
      a single average entry, not from individual lots. For most short-horizon trading
      this matches the funded-PnL a centralized perp clears, but it can diverge from
      strict tax-lot FIFO if the address opens and closes positions in tightly nested
      sequences.</li>
    <li>Funding/fees are <em>not</em> included. Hyperliquid charges hourly funding on
      open positions (paid both ways) and small taker/maker fees. This report's PnL is
      gross — net PnL after fees and funding will be a few hundred to a few thousand
      dollars different over a multi-day horizon.</li>
    <li>Mark-to-market uses the latest <code>mark_price</code> in the recorder. If the
      recorder stopped before the address closed its position, the unrealized line
      reflects whatever the open position would be worth at that snapshot — actual
      eventual P&amp;L will differ.</li>
  </ul>
</div>

</body></html>
"""
    OUT.write_text(html)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
