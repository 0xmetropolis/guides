# ML Signal Payloads

When generating, consuming, or testing ML model signals, follow this shape.

## Payload Example

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

## Fields

- `target_pct`: predicted price move magnitude (e.g. 0.05 = 5%).
- `short_prob` / `neutral_prob` / `long_prob`: directional probabilities. Must sum to 1.
- `probabilities`: `[short, neutral, long]`. Must match the individual fields.
- `decision`: `"long"`, `"short"`, or `"neutral"`.
- `signal`: `1` = long, `-1` = short, `0` = neutral. Must match `decision`.
- `threshold`: probability cutoffs that produced the decision.
- `model_type`: classifier class name.
- `timestamp`: ISO 8601 with microseconds.
- `trading_pair`: exchange-formatted pair (e.g. `DOGE-USD`).
