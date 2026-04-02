# Signal to Execution: How Hummingbot Executes Positions

This document traces the full lifecycle of a trading signal — from the moment it is published on the MQTT broker to a closed position with PnL stored in the database. It is intended as a reference for the team to understand the execution machinery, debug live trades, and know what to look for in logs.

The stack involves four moving parts:

- **ML model** (Docker container): computes a prediction and publishes a signal to EMQX
- **EMQX broker**: routes the MQTT message to any subscribed bot
- **Hummingbot bot** (Docker container): runs the `ai_livestream` controller, which receives the signal and manages executors
- **PostgreSQL** (via hummingbot-api): stores executor history and PnL

```mermaid
sequenceDiagram
    participant M as ML Model
    participant Q as EMQX Broker
    participant C as ai_livestream Controller
    participant E as PositionExecutor
    participant X as Exchange (e.g. Hyperliquid)
    participant D as PostgreSQL

    M->>Q: MQTT publish<br/>hbot/predictions/doge_usd/ML_SIGNALS
    Q->>C: deliver message (QoS 1)
    C->>C: _handle_ml_signal()<br/>apply thresholds → signal ∈ {-1, 0, 1}
    C->>E: CreateExecutorAction<br/>PositionExecutorConfig
    E->>X: place_open_order() → MARKET order
    X-->>E: OrderFilled event
    loop Every tick
        E->>E: control_barriers()<br/>check TP / SL / time limit
    end
    E->>X: place_close_order() → MARKET order
    X-->>E: OrderFilled event
    E->>D: executor record + PnL written
```
