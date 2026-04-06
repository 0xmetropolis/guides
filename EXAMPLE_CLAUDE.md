# Backtest Parameter Reference

This file provides context for generating backtests for trading strategies.
If any parameter is ambiguous or missing for a specific backtest request, ask the user before assuming.

---

## General Rules

- When producing Python notebooks, define all tunable parameters as clearly labeled variables at the top of the first cell so they are easy to find and edit.
- Default candle granularity: **1h**. Always expose as a parameter.
- Trading pair and exchange must be specified per-backtest; there are no defaults.

---

## Signal Payload Shape

Every signal arrives as a JSON object with at minimum these fields:

```json
{
  "id": 1743580800123,
  "trading_pair": "DOGE-USD",
  "probabilities": [0.12, 0.18, 0.70],
  "timestamp": "2026-04-02T10:00:00.123456",
  "target_pct": 0.018700,
  "short_prob": 0.12,
  "neutral_prob": 0.18,
  "long_prob": 0.70,
  "decision": "long",
  "signal": 1,
  "threshold": { "short": 0.5, "long": 0.5 },
  "model_type": "RandomForestClassifier"
}
```

Key fields:

| Field | Type | Meaning |
|---|---|---|
| `target_pct` | float | Predicted price move magnitude (e.g. 0.05 = 5%) |
| `short_prob` | float | Probability of downward move |
| `neutral_prob` | float | Probability of no significant move |
| `long_prob` | float | Probability of upward move |
| `decision` | string | `"long"`, `"short"`, or `"neutral"` |
| `signal` | int | 1 = long, -1 = short, 0 = neutral |
| `threshold` | object | Probability thresholds that produced the decision |

---

## Strategy Types

### Directional Trading

#### Triple Barrier Parameters

| Parameter | Default | Notes |
|---|---|---|
| Take profit multiplier | `1.0` | Final TP = `take_profit_multiplier * target_pct`. At 1.0, TP distance equals the predicted move. |
| Stop loss | `0.03` (3%) | Fixed percentage. **Not** scaled by `target_pct`. |
| Time limit | `24h` | Maximum duration before a position is closed regardless of PnL. |
| Position size | 10% of portfolio balance | Fraction of available balance allocated per trade. |

**How TP and SL are applied:**

```
LONG:
  tp_price = entry_price * (1 + take_profit_multiplier * target_pct)
  sl_triggers when pnl_pct <= -stop_loss

SHORT:
  tp_price = entry_price * (1 - take_profit_multiplier * target_pct)
  sl_triggers when pnl_pct <= -stop_loss
```

#### Entry Threshold

There is no fixed default. Backtests should treat the entry probability threshold as a parameter to optimize over (e.g. sweep `long_prob` threshold from 0.5 to 0.9).

<!-- Add new strategy types below this line -->
