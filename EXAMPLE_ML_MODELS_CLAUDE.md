# ML Models

When generating, consuming, or testing ML model signals, follow this guide.

## Repo Layout

- Each model lives in `models/<name>/`. `<name>` is snake_case and equals the key in `services.yml`.
- Allowed files only: `Dockerfile`, `main.py`, `pyproject.toml`, `model.joblib`, `scaler.pkl`. Nothing else.
- Python `>=3.13` in `pyproject.toml`.
- `*.joblib` and `*.pkl` are tracked via Git LFS. Never commit secrets.

## Model Contract

- Expose an `sklearn`-style `predict_proba`. Output order is `[short, neutral, long]`.
- Ship `model.joblib` (required) and `scaler.pkl` (optional).
- Inference candle interval MUST equal training candle interval.
- Features must depend only on the live candle buffer. No lookahead, no external state, no network calls.

## Signal Payload

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

Field rules:

- `target_pct`: predicted price move magnitude (e.g. 0.05 = 5%).
- `probabilities`: `[short, neutral, long]`. Must sum to 1 and match the individual `*_prob` fields.
- `decision`: `"long"` / `"short"` / `"neutral"`. Must match `signal` (`1` / `-1` / `0`).
- `threshold`: probability cutoffs that produced the decision.
- `model_type`: classifier class name.
- `timestamp`: ISO 8601 with microseconds.
- `trading_pair`: exchange-formatted pair (e.g. `DOGE-USD`).

## `services.yml` Defaults

Use these unless explicitly overridden:

- `controller_name: ai_livestream`
- `controller_type: directional_trading`
- `prediction_interval: 300`
- `total_amount_quote: 12` (must clear HL's 10 USD min notional after quantization)
- `leverage: 1`
- `cooldown_time: 600`
- `time_limit: 7200`
- `stop_loss: 0.03` (fixed 3%, per `EXAMPLE_Q-LAB_CLAUDE.md`)
- `take_profit_multiplier: 1.0` (TP = `target_pct`, per `EXAMPLE_Q-LAB_CLAUDE.md`)
- `long_threshold: 0.55`, `short_threshold: 0.45` (symmetric around 0.5 unless justified)
- `max_global_drawdown: 20`, `max_controller_drawdown: 20`
- `credentials_profile: paper_trading` until the model has run profitably on paper for ≥ 1 week
