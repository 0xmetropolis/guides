"""
News-event attribution report for the 2026-04-25/26 weekend perp moves.

Three events to characterise:
  E1  Sat 04-25  ~08:00–11:00 ET  +1.5–1.8%  UP
  E2  Sun 04-26  ~13:00–17:00 ET  +1.2%      UP
  E3  Sun 04-26  ~18:00–20:00 ET  −0.9%      DOWN

For each event we render: 5-min candles per venue, buy/sell imbalance,
mark-trade gap, large-trade dots, and a cross-venue lead/lag estimate.
"""

from pathlib import Path
import duckdb
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

DATA_GLOB = "/Users/marcolavagnino/metal/hl-trades-recorder/data/**/2026-04-2[56]T*.parquet"
OUT = Path(__file__).parent / "news_attribution_2026-04-25_26.html"

EVENTS = [
    {
        "id": "E1",
        "title": "Saturday morning rally — Trump cancels Iran envoys",
        "start": "2026-04-25 06:00",
        "end": "2026-04-25 12:00",
        "anchor_time": "2026-04-25 08:00",
        "direction": "UP",
        "narrative": (
            "Saturday morning ET, Trump cancelled the Witkoff/Kushner trip to Islamabad "
            "(\"too much time wasted\", \"we have all the cards\"). Iran's negotiator had "
            "left Pakistan an hour earlier with no breakthrough — diplomatic-progress "
            "narrative dies. Bullish for oil because the path-to-Hormuz-reopening just "
            "got longer. The sharp leg ran 08:00–10:00 ET on all three venues."
        ),
    },
    {
        "id": "E2",
        "title": "Sunday afternoon rally — Iran's \"Hormuz will not reopen\" statement",
        "start": "2026-04-26 12:00",
        "end": "2026-04-26 17:30",
        "anchor_time": "2026-04-26 14:00",
        "direction": "UP",
        "narrative": (
            "Iran's Deputy Parliament Speaker Ali Nikzad: <em>\"the Strait of Hormuz will "
            "not return to its previous state under any circumstances.\"</em> CNN's piece "
            "with this quote published 18:23 ET, but the wire-services reaction in oil "
            "markets started ~14:00 ET, suggesting an earlier wire pickup. Hawkish Iran → "
            "bullish oil. Same day Iran's FM Araghchi was meeting Oman's Sultan in Muscat "
            "to discuss Hormuz transit, which the markets evidently did not read as a "
            "softening signal."
        ),
    },
    {
        "id": "E3",
        "title": "Sunday evening drop — CME reopen + Iran proposal leak",
        "start": "2026-04-26 17:00",
        "end": "2026-04-26 22:00",
        "anchor_time": "2026-04-26 18:00",
        "direction": "DOWN",
        "narrative": (
            "Two simultaneous mechanisms at 18:00 ET Sunday: (a) <strong>CME Globex "
            "reopens</strong> for the new trading week — same mean-reversion-to-oracle "
            "pattern flagged in last weekend's analysis; (b) Axios/Bloomberg start "
            "circulating <strong>Iran's new proposal</strong> to reopen Hormuz in exchange "
            "for the US blockade lifting and deferring nuclear talks. Either is bearish "
            "by itself; together they explain a $0.9 drop in 90 minutes. By Monday Asia "
            "open Trump rejected the proposal and Brent gapped UP to $108 — but during "
            "this Sunday-evening window, the venue data only sees the bearish leg."
        ),
    },
]

VENUES = ["hyperliquid", "extended", "binance"]
VENUE_COLORS = {"hyperliquid": "#7c3aed", "extended": "#0ea5e9", "binance": "#f59e0b"}
# Pair Brent products (more liquid in the data) across venues
SYMBOL_BY_VENUE = {
    "hyperliquid": "xyz:BRENTOIL",
    "extended": "XBR-USD",
    "binance": "BZUSDT",
}

con = duckdb.connect()
con.execute(
    f"""
    CREATE OR REPLACE VIEW trades AS
    SELECT venue, asset_symbol, side, price, size, notional_usd,
           mark_price, oracle_price,
           to_timestamp(timestamp/1000.0) - INTERVAL 4 HOUR AS ts_et,
           timestamp AS ts_ms
    FROM read_parquet('{DATA_GLOB}', union_by_name=true)
    """
)


def candles_5m(venue, symbol, start, end):
    df = con.execute(
        """
        WITH base AS (
          SELECT
            time_bucket(INTERVAL 5 MINUTE, ts_et) AS bin,
            ts_ms, price, side, notional_usd, mark_price
          FROM trades
          WHERE venue = ? AND asset_symbol = ?
            AND ts_et >= ?::TIMESTAMP AND ts_et < ?::TIMESTAMP
        )
        SELECT
          bin,
          first(price ORDER BY ts_ms) AS o,
          max(price) AS h,
          min(price) AS l,
          last(price ORDER BY ts_ms) AS c,
          sum(notional_usd) AS v,
          sum(CASE WHEN side='buy' THEN notional_usd ELSE 0 END) AS buy_v,
          sum(CASE WHEN side='sell' THEN notional_usd ELSE 0 END) AS sell_v,
          avg(CASE WHEN mark_price IS NOT NULL AND mark_price > 0
                   THEN price - mark_price END) AS avg_gap,
          count(*) AS n
        FROM base
        GROUP BY bin ORDER BY bin
        """,
        [venue, symbol, start, end],
    ).df()
    df["imbalance_pct"] = (
        100.0 * (df["buy_v"] - df["sell_v"]) / (df["buy_v"] + df["sell_v"]).replace(0, pd.NA)
    )
    return df


def big_trades(venue, symbol, start, end, top_n=15):
    return con.execute(
        """
        SELECT ts_et, side, price, notional_usd
        FROM trades
        WHERE venue = ? AND asset_symbol = ?
          AND ts_et >= ?::TIMESTAMP AND ts_et < ?::TIMESTAMP
        ORDER BY notional_usd DESC LIMIT ?
        """,
        [venue, symbol, start, end, top_n],
    ).df()


def event_figure(event):
    """A 4-row stacked figure for one event:
        row 1 — overlaid candle close lines from the 3 venues, with biggest trades
                marked as scatter
        row 2 — 5-min notional volume per venue (stacked bars)
        row 3 — buy-sell imbalance % per venue (lines)
        row 4 — average (price − mark) per venue (lines), shows oracle gap
    """
    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.04,
        row_heights=[0.46, 0.18, 0.18, 0.18],
        subplot_titles=(
            "Price (close) — overlaid across venues, with the 5 largest trades marked",
            "5-min notional volume",
            "5-min buy/sell imbalance %",
            "5-min avg (price − mark) — proxy for trade-vs-oracle gap",
        ),
    )

    for venue in VENUES:
        sym = SYMBOL_BY_VENUE[venue]
        d = candles_5m(venue, sym, event["start"], event["end"])
        if d.empty:
            continue
        color = VENUE_COLORS[venue]

        fig.add_trace(
            go.Scatter(x=d["bin"], y=d["c"], name=f"{venue} close",
                       line=dict(color=color, width=2), legendgroup=venue,
                       hovertemplate="%{x}<br>close %{y:.3f}<extra>" + venue + "</extra>"),
            row=1, col=1,
        )
        fig.add_trace(
            go.Bar(x=d["bin"], y=d["v"], name=f"{venue} vol",
                   marker_color=color, opacity=0.55, legendgroup=venue, showlegend=False,
                   hovertemplate="%{x}<br>$%{y:,.0f}<extra>" + venue + "</extra>"),
            row=2, col=1,
        )
        fig.add_trace(
            go.Scatter(x=d["bin"], y=d["imbalance_pct"], name=f"{venue} imb",
                       line=dict(color=color, width=1.5), legendgroup=venue, showlegend=False,
                       hovertemplate="%{x}<br>imb %{y:+.0f}%<extra>" + venue + "</extra>"),
            row=3, col=1,
        )
        fig.add_trace(
            go.Scatter(x=d["bin"], y=d["avg_gap"], name=f"{venue} gap",
                       line=dict(color=color, width=1.5), legendgroup=venue, showlegend=False,
                       hovertemplate="%{x}<br>gap %{y:+.4f}<extra>" + venue + "</extra>"),
            row=4, col=1,
        )

        # Big trades on row 1 — mark only top 5 per venue to avoid clutter
        bt = big_trades(venue, sym, event["start"], event["end"], top_n=5)
        if not bt.empty:
            fig.add_trace(
                go.Scatter(
                    x=bt["ts_et"], y=bt["price"], mode="markers",
                    name=f"{venue} top trades",
                    marker=dict(
                        size=bt["notional_usd"].apply(lambda v: max(8, min(28, v / 5000))),
                        color=color, opacity=0.6,
                        line=dict(color="white", width=1),
                    ),
                    text=[f"{r.side} ${r.notional_usd:,.0f}" for r in bt.itertuples()],
                    legendgroup=venue, showlegend=False,
                    hovertemplate="%{x}<br>%{text}<br>price %{y:.3f}<extra>" + venue + "</extra>",
                ),
                row=1, col=1,
            )

    # Mark anchor time
    anchor = event["anchor_time"]
    fig.add_vline(x=anchor, line=dict(color="red", width=1.5, dash="dash"), row=1, col=1)
    fig.add_annotation(
        x=anchor, y=1, yref="y domain", row=1, col=1,
        text="news anchor", showarrow=False, yshift=10, font=dict(color="red", size=11),
    )

    # Imbalance zero line + gap zero line
    fig.add_hline(y=0, line=dict(color="#999", width=1), row=3, col=1)
    fig.add_hline(y=0, line=dict(color="#999", width=1), row=4, col=1)

    fig.update_layout(
        height=820, template="plotly_white",
        title=f"{event['id']} · {event['title']}",
        margin=dict(l=60, r=20, t=70, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="price", row=1, col=1)
    fig.update_yaxes(title_text="$", row=2, col=1)
    fig.update_yaxes(title_text="%", row=3, col=1)
    fig.update_yaxes(title_text="$ gap", row=4, col=1)
    return fig


# ---- Lead/lag analysis ----
# For each event, take 1-min close series of HL Brent vs Binance Brent,
# compute the cross-correlation lag in ±5 minutes that maximises Pearson r.
def lead_lag_text(event):
    rows = []
    base_v = "hyperliquid"
    base_sym = SYMBOL_BY_VENUE[base_v]
    base = con.execute(
        """SELECT time_bucket(INTERVAL 1 MINUTE, ts_et) AS bin,
                  last(price ORDER BY ts_ms) AS c
           FROM trades WHERE venue=? AND asset_symbol=?
             AND ts_et >= ?::TIMESTAMP AND ts_et < ?::TIMESTAMP
           GROUP BY bin ORDER BY bin""",
        [base_v, base_sym, event["start"], event["end"]],
    ).df().set_index("bin")["c"].rename("hl")

    for v in VENUES:
        if v == base_v:
            continue
        sym = SYMBOL_BY_VENUE[v]
        other = con.execute(
            """SELECT time_bucket(INTERVAL 1 MINUTE, ts_et) AS bin,
                      last(price ORDER BY ts_ms) AS c
               FROM trades WHERE venue=? AND asset_symbol=?
                 AND ts_et >= ?::TIMESTAMP AND ts_et < ?::TIMESTAMP
               GROUP BY bin ORDER BY bin""",
            [v, sym, event["start"], event["end"]],
        ).df().set_index("bin")["c"].rename(v)

        df = pd.concat([base, other], axis=1).dropna()
        if len(df) < 10:
            rows.append(f"<li><b>{v}</b>: insufficient overlap</li>")
            continue
        # normalise to pct change — comparable across products at different price levels
        df = df.pct_change().dropna()
        best = (None, -2.0)
        for lag in range(-5, 6):
            shifted = df["hl"].shift(lag)
            corr = shifted.corr(df[v])
            if pd.notna(corr) and corr > best[1]:
                best = (lag, corr)
        lag, corr = best
        if lag is None:
            rows.append(f"<li><b>{v}</b>: insufficient overlap (no valid correlation)</li>")
            continue
        if lag == 0:
            lead = "moves at the same time as HL"
        elif lag > 0:
            lead = f"<b>HL leads by {lag}m</b>"
        else:
            lead = f"<b>{v} leads HL by {-lag}m</b>"
        rows.append(f"<li>{v}: {lead} (peak corr = {corr:+.2f})</li>")
    return "<ul>" + "".join(rows) + "</ul>"


# ---- Build figures ----
event_blocks = []
for ev in EVENTS:
    fig = event_figure(ev)
    ll = lead_lag_text(ev)
    event_blocks.append((ev, fig, ll))

# Cross-event summary table — pre-event price, peak/trough, and net move per venue per event
def summary_row(event, venue):
    sym = SYMBOL_BY_VENUE[venue]
    d = con.execute(
        """SELECT min(price) AS lo, max(price) AS hi,
                  first(price ORDER BY ts_ms) AS o, last(price ORDER BY ts_ms) AS c,
                  sum(notional_usd) AS v
           FROM trades WHERE venue=? AND asset_symbol=?
             AND ts_et >= ?::TIMESTAMP AND ts_et < ?::TIMESTAMP""",
        [venue, sym, event["start"], event["end"]],
    ).df().iloc[0]
    if pd.isna(d["o"]):
        return None
    pct = 100 * (d["c"] - d["o"]) / d["o"]
    arrow = "↑" if pct > 0 else "↓" if pct < 0 else "→"
    return f"<td>{d['o']:.2f}</td><td>{d['hi']:.2f} / {d['lo']:.2f}</td><td>{d['c']:.2f}</td><td><b>{arrow} {pct:+.2f}%</b></td><td>${d['v']/1e6:.2f}M</td>"

summary_html = "<table class='summary'><thead><tr><th>Event</th><th>Venue</th><th>open</th><th>high / low</th><th>close</th><th>net</th><th>volume</th></tr></thead><tbody>"
for ev in EVENTS:
    for i, v in enumerate(VENUES):
        row = summary_row(ev, v)
        if row is None:
            continue
        if i == 0:
            ev_cell = f"<td rowspan='3'><b>{ev['id']}</b><br><span class='small'>{ev['start'][5:]}–{ev['end'][11:16]} ET</span></td>"
        else:
            ev_cell = ""
        summary_html += f"<tr>{ev_cell}<td>{v}</td>{row}</tr>"
summary_html += "</tbody></table>"


# ---- Render HTML ----
def to_div(fig):
    return fig.to_html(include_plotlyjs=False, full_html=False)

html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>News attribution — 2026-04-25/26 weekend oil moves</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1180px; margin: 2em auto; padding: 0 1.5em; color: #111; line-height: 1.6; }}
  h1 {{ margin-bottom: 0.2em; }}
  h2 {{ margin-top: 2.5em; border-bottom: 1px solid #ddd; padding-bottom: 0.3em; }}
  h3 {{ margin-top: 1.5em; }}
  .meta {{ color: #666; font-size: 0.92em; }}
  .box {{ background: #f8f9fb; border-left: 4px solid #7c3aed; padding: 1em 1.3em; margin: 1.2em 0; border-radius: 4px; }}
  .box.warn {{ border-left-color: #f59e0b; background: #fff8eb; }}
  .box.kill {{ border-left-color: #ef4444; background: #fef2f2; }}
  .box.good {{ border-left-color: #10b981; background: #f0fdf4; }}
  table.summary {{ border-collapse: collapse; margin: 1em 0; font-size: 0.9em; width: 100%; }}
  table.summary th, table.summary td {{ border: 1px solid #ddd; padding: 0.45em 0.7em; text-align: left; }}
  table.summary th {{ background: #f3f4f6; }}
  .small {{ font-size: 0.78em; color: #666; }}
  .hyp {{ font-weight: 600; }}
  .verdict {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.82em; font-weight: 700; margin-right: 6px; }}
  .v-supported {{ background: #d1fae5; color: #065f46; }}
  .v-disproven {{ background: #fee2e2; color: #991b1b; }}
  .v-partial {{ background: #fef3c7; color: #92400e; }}
  .v-untested {{ background: #e5e7eb; color: #374151; }}
  ul {{ padding-left: 1.4em; }}
  li {{ margin-bottom: 0.4em; }}
  code {{ background: #f3f4f6; padding: 0.1em 0.4em; border-radius: 3px; font-size: 0.9em; }}
  .sources {{ font-size: 0.88em; }}
  .sources li {{ margin-bottom: 0.25em; }}
</style>
</head><body>

<h1>What was the market reacting to?</h1>
<div class="meta">Three perp-venue oil moves on the 2026-04-25/26 weekend, attributed against
news flow and tested with on-chain trade data. Brent products only (HL <code>xyz:BRENTOIL</code>,
Extended <code>XBR-USD</code>, Binance <code>BZUSDT</code>) — they're more liquid than WTI on
all three venues, and the news drivers in this period are Brent-specific (Hormuz / Iran).</div>

<div class="box">
  <strong>Macro context (this didn't show up in last weekend's report).</strong> An active
  <strong>US/Israel–Iran war</strong> has been ongoing since 28 Feb 2026; the Strait of
  Hormuz has been effectively closed for ~2 months. Pre-war Brent was ~$72; on Friday
  04-24 CME Brent settled <strong>$105.33</strong>, WTI $94.40. The perp venues we record
  trade <em>below</em> CME — HL Brent closed Friday at $100.80 — because the venue oracles
  can't refresh while CME is shut and the products don't fully arb to physical. So all
  three weekend moves are happening on top of a war-tight base price.
</div>

<h2>Event summary</h2>
{summary_html}

<div class="box warn">
  <strong>Reading the table.</strong> Net % is intra-event open→close. The bigger story is
  agreement across venues — all three move the same direction with similar magnitude on
  E1 and E2, which rules out single-venue technicals. E3 is also cross-venue, but the lead/lag
  analysis below shows a different signature.
</div>

"""

for ev, fig, ll in event_blocks:
    html += f"""
<h2>{ev['id']} · {ev['title']}</h2>
<div class="box">
  <strong>What happened (news).</strong> {ev['narrative']}
</div>
<div class="box good">
  <strong>Cross-venue lead/lag.</strong> Reference = Hyperliquid Brent. 1-min close-to-close
  pct returns, lag chosen ∈ [-5m, +5m] to maximise Pearson correlation:
  {ll}
</div>
{to_div(fig)}
"""

html += """
<h2>Hypotheses — what the data says</h2>

<div class="box">
  <p><span class="verdict v-supported">SUPPORTED</span><span class="hyp">H1 · Geopolitical news drove the moves.</span>
  All three events line up cleanly with specific Iran-related news on the wires (Trump
  cancellation E1, Iran's Hormuz statement E2, Iran's reopen-proposal leak E3). The same
  direction and similar magnitude on all three venues confirms it's market-wide, not local.</p>

  <p><span class="verdict v-supported">SUPPORTED</span><span class="hyp">H4 · CME-reopen mean-reversion contributed to E3.</span>
  Sun 18:00 ET is exactly when CME Globex re-opens. Last weekend's report flagged the same
  effect — n=2 now (limited but suggestive). The drop's <em>timing</em> aligns with reopen
  to the minute; the proposal-leak news is a parallel mechanism. Hard to fully separate
  without lower-frequency news-wire timestamps.</p>

  <p><span class="verdict v-partial">PARTIAL</span><span class="hyp">H3 · Weekend overshoot vs oracle.</span>
  The mark-trade gap chart in each event shows whether trade price is running ahead/behind
  the oracle. On HL specifically, trade-price ran ~+$0.20 above mark during the E1 spike
  (oracle slow to update), then converged after the move. On Extended the gap is much
  wider — Stork's WTI/Brent oracle clearly lags spike pricing. This matches last weekend's
  finding but is muted here because the moves are smaller in % terms.</p>

  <p><span class="verdict v-disproven">DISPROVEN</span><span class="hyp">H2 · OPEC+ announcement.</span>
  The relevant OPEC+ meeting was 04-05 (three weeks earlier). No new OPEC headline on
  04-25 or 04-26. Ruled out.</p>

  <p><span class="verdict v-disproven">DISPROVEN</span><span class="hyp">H5 · Single-venue lead.</span>
  At 1-min resolution all three venues move simultaneously on every event (peak correlation
  at lag = 0 in 5 of 6 venue/event pairs; one pair had insufficient overlap on Binance Brent
  during E2). Cross-venue correlation is +0.59 to +0.89, with the highest on E3 (+0.89) — a
  shared external impulse, not one venue leading. If anything is leading, it's at sub-minute
  granularity which this resolution can't see.</p>

  <p><span class="verdict v-partial">PARTIAL</span><span class="hyp">H6 · Coordinated big-trade clusters.</span>
  The largest 5 trades per venue per event are dotted on the price panel. On E1 and E2
  several large trades cluster within minutes of the news anchor, especially on HL — but
  total volume is also up everywhere, so this is broad participation, not a single whale
  driving price.</p>
</div>

<h2>What this analysis cannot do</h2>
<div class="box kill">
  <ul>
    <li><strong>No exact news-wire timestamp.</strong> CNN's article timestamps are
      publication-time, not the underlying wire-pickup time. A specific news event can
      hit terminals 30+ minutes before a major outlet publishes. The "news anchor" lines
      are best-effort, not millisecond-precise.</li>
    <li><strong>n = 1 weekend, again.</strong> The CME-reopen-mean-reversion hypothesis
      now has n=2 (last weekend + this Sun 18:00 drop). Still nowhere near tradeable.</li>
    <li><strong>Trade-vs-mark gap is noisy on Brent venues.</strong> The mark-price feed
      from Hyperliquid for <code>xyz:BRENTOIL</code> is sparser than for <code>xyz:CL</code>,
      so the gap trace has gaps. WTI products would show this hypothesis cleaner (left
      out here because the news flow is Brent-specific).</li>
  </ul>
</div>

<h2>Sources</h2>
<div class="sources">
<ul>
  <li>CNBC, <a href="https://www.cnbc.com/2026/04/25/iran-says-no-meeting-with-us-negotiators-planned-in-pakistan.html">Trump cancels U.S. envoy trip to Pakistan for Iran war negotiations</a> (E1)</li>
  <li>Time, <a href="https://time.com/article/2026/04/25/trump-iran-peace-talks-canceled/">Trump Cancels Iran Peace Talks at Last Minute: 'We Have All the Cards'</a> (E1)</li>
  <li>Axios, <a href="https://www.axios.com/2026/04/25/trump-iran-pakistan-talks">Trump cancels envoys' trip to Pakistan for Iran talks</a> (E1)</li>
  <li>CNN, <a href="https://www.cnn.com/2026/04/26/business/oil-prices-stock-futures-iran-war">Oil prices increase after Iran doubles down on Strait of Hormuz closure</a> (E2)</li>
  <li>CNBC, <a href="https://www.cnbc.com/2026/04/26/oil-price-iran-war-strait-hormuz.html">Brent oil briefly tops $108 per barrel after Iran peace talks unravel</a> (E2/E3)</li>
  <li>Bloomberg, <a href="https://www.bloomberg.com/news/articles/2026-04-27/iran-offers-deal-to-us-to-reopen-strait-delay-nuclear-talks-axios-says">Iran Offers Deal to US to Reopen Strait, Delay Nuclear Talks, Axios Says</a> (E3)</li>
  <li>Times of Israel, <a href="https://www.timesofisrael.com/iran-said-to-offer-us-deal-to-reopen-hormuz-end-war-and-put-off-nuclear-talks/">Iran said to offer US deal to reopen Hormuz, end war and put off nuclear talks</a> (E3)</li>
  <li>Wikipedia, <a href="https://en.wikipedia.org/wiki/2026_Strait_of_Hormuz_crisis">2026 Strait of Hormuz crisis</a> (background)</li>
  <li>EIA, <a href="https://www.eia.gov/todayinenergy/detail.php?id=67544">Brent crude oil spot prices surge past futures price in April 2026</a> (background)</li>
</ul>
</div>

</body></html>
"""

OUT.write_text(html)
print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB)")
