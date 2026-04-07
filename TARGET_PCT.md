## `target_pct` — Complete Findings

### What it is

A number representing the predicted percentage of price movement (e.g., `0.05` = 5%). It arrives in Hummingbot as part of a payload. The payload also has `short_prob`, `neutral_prob` and `long_prob` to show the direction of the predicted movement (up or down). 

### Where it enters the bot

**`controllers/directional_trading/ai_livestream.py:136`**
```python
triple_barrier_config=self.config.triple_barrier_config.new_instance_with_adjusted_volatility(
    volatility_factor=self.processed_data["features"].get("target_pct", 0.01)),
```
It's pulled from the ML signal's features and passed as `volatility_factor`. Defaults to `0.01` if not provided.

### How it scales the barriers

**`hummingbot/strategy_v2/executors/position_executor/data_types.py:28-45`**
```python
def new_instance_with_adjusted_volatility(self, volatility_factor: float) -> TripleBarrierConfig:
    return TripleBarrierConfig(
        stop_loss=self.stop_loss * Decimal(volatility_factor) if self.stop_loss is not None else None,
        take_profit=self.take_profit * Decimal(volatility_factor) if self.take_profit is not None else None,
        ...
    )
```
Straight multiplication. The config's base `take_profit` and `stop_loss` values are multiplied by `target_pct` to produce the final distance fractions.

### How the position executor applies those distances

**`hummingbot/strategy_v2/executors/position_executor/position_executor.py:285-292`** (take profit):
```python
if self.config.side == TradeType.BUY:
    take_profit_price = self.entry_price * (1 + self.config.triple_barrier_config.take_profit)
else:
    take_profit_price = self.entry_price * (1 - self.config.triple_barrier_config.take_profit)
```

**`position_executor.py:522-524`** (stop loss):
```python
if self.config.triple_barrier_config.stop_loss:
    if self.net_pnl_pct <= -self.config.triple_barrier_config.stop_loss:
        self.place_close_order_and_cancel_open_orders(close_type=CloseType.STOP_LOSS)
```

The take profit is applied directionally (above entry for longs, below for shorts). The stop loss triggers when PnL drops below the negative of the threshold.

### End-to-end example

Config: `take_profit=0.02`, `stop_loss=0.03`. ML model sends `target_pct=0.05`. Entry price is $100, going long.

**Step 1 — Scaling:**
- Final TP = 0.02 × 0.05 = 0.001
- Final SL = 0.03 × 0.05 = 0.0015

**Step 2 — Price levels:**
- TP price = $100 × (1 + 0.001) = $100.10 (+0.1% profit)
- SL triggers when PnL <= -0.15% (around $99.85)

### The oddity

Remember the predicted move was 5%, but the way the configured `tp=0.02` and `sl=0.03` are used, the actual barriers end up at 0.1% TP and 0.15% SL. That means only a tiny fraction of the predicted move was captured. This means the config values must be sized with this multiplication in mind, or the strategy is intentionally designed to scalp small pieces of larger predicted moves.

### Conclusion

Take-profit calculation can stay the same, but the parameter needs to be increased from 2% to closer to 100%.

Stop-loss should **not** mean "how much, in relation to the predicted move, am I willing to lose." Instead, it should mean "how much can I lose on this trade to stay within my general risk parameters?"

```python
TP = config.take_profit × target_pct     # (unchanged, but we increase the value)
SL = config.stop_loss                    # (fixed, e.g. 0.03 = 3%)
```