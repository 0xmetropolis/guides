# Backtesting

When generating backtests for trading strategies, follow these rules.

## General

- Trading pair and exchange: always ask. No defaults.
- Default candle granularity: 1h. Expose as a parameter.
- When producing Python notebooks, put all tunable parameters as labeled variables at the top of the first cell.
- If a parameter is ambiguous or missing, ask before assuming.

## Directional Trading

### Triple Barrier Defaults

- **Take profit**: `take_profit_multiplier * target_pct`. Default multiplier = `1.0` (TP equals the predicted move).
- **Stop loss**: Fixed at 3%. Not scaled by `target_pct`.
- **Time limit**: 24h.

### Price Levels

```
LONG:
  tp_price = entry_price * (1 + take_profit_multiplier * target_pct)
  sl triggers when pnl_pct <= -stop_loss

SHORT:
  tp_price = entry_price * (1 - take_profit_multiplier * target_pct)
  sl triggers when pnl_pct <= -stop_loss
```

### Entry Threshold

No fixed default. Treat entry probability threshold as a parameter to optimize over (e.g. sweep `long_prob` threshold from 0.5 to 0.9).

## Data

- Default lookback: 180 days. Expose as a parameter.
- Read candles from the local cache (`core/data_paths.py`, `app/data/`) before downloading. Refresh only if stale.
- Train/test split: chronological, 80/20. No shuffling. No leakage across the boundary.
- Always set `random_state=42` for reproducibility.

## Labeling (`target_pct`)

- `target_pct` is the **signed forward return** over a fixed horizon (default 24 candles).
- Three classes: `short` / `neutral` / `long`. Neutral band is `|return| < 0.5 * stop_loss` unless told otherwise.
- Never use future information in features. Ask if a feature window is ambiguous.

## Backtest Assumptions

- Fees: 4 bps per side. Slippage: 2 bps per side. Funding: ignore unless asked.
- Position sizing: fixed notional. Default `total_amount_quote = 100`.
- Leverage: 1. Cooldown: 600s.
- Respect Hyperliquid's 10 USD min notional after quantization.

## Optimization

- Use Optuna. Default 200 trials.
- Objective: `net_pnl / max_drawdown` (Calmar-like). Override only on request.
- Reject any trial with: fewer than 30 trades, max drawdown > 25%, or negative net PnL.

## Acceptance Bar

A model is **not** worth deploying unless it clears all of these on the holdout:
- Sharpe ≥ 1.0
- Max drawdown ≤ 20%
- ≥ 30 trades
- Net PnL > 0

Report these four numbers explicitly when summarizing a backtest.

<!-- Add new strategy types below -->
