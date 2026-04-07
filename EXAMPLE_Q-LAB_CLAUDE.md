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

<!-- Add new strategy types below -->
