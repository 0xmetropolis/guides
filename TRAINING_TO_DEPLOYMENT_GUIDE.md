# From Research to Live Trading: Model Training & Deployment Guide

This guide walks through the complete workflow for training and optimizing a strategy in **quants-lab**, then deploying it as a live bot managed by the **hummingbot-api**.

---

## System Overview

The stack has four components that work together:

| Component | Role |
|-----------|------|
| **quants-lab** | Research: data collection, feature engineering, backtesting, optimization, autonomous orchestration |
| **hummingbot-api** | Control plane: REST API that manages Docker containers, credentials, controller configs, and bot lifecycle |
| **hummingbot** | Execution engine: runs inside Docker containers, executes strategies on exchanges |
| **dashboard** | Web UI: Streamlit app at port 8501, wraps the hummingbot-api for point-and-click operations |

```
quants-lab (research + signals)
    │
    │  REST API calls (BackendAPIClient)
    ▼
hummingbot-api (FastAPI + PostgreSQL + EMQX/MQTT)
    │                          │
    │  Docker API              │  MQTT
    ▼                          ▼
hummingbot containers      Bot state / commands
(one per bot instance)

dashboard (Streamlit) ──► hummingbot-api
```

---

## Prerequisites

Before starting, make sure you have:

- Python environment set up with `quants-lab` installed (see its README)
- Access to the hummingbot-api (local or remote: `https://api.metallorum.duckdns.org/`)
- Exchange API keys added under `Accounts` in the dashboard or via `/accounts` endpoint
- MongoDB running locally or accessible (for quants-lab features/signals storage)
- Docker daemon running on the machine where hummingbot-api is deployed

---

## Workflow Overview

```
Step 1: Collect historical data
Step 2: Engineer features & generate signals   [optional for signal-based strategies]
Step 3: Optimize strategy hyperparameters
Step 4: Analyze optimization results
Step 5: Create controller config
Step 6: Validate with a single backtest
Step 7: Deploy to a live bot
Step 8: Monitor and manage the bot lifecycle
```

---

## Step 1: Collect Historical Data

All optimization and backtesting requires local OHLCV candle data. Use the candle downloader task or research notebook.

### Option A — Run the downloader task via the orchestrator

```yaml
# In quants-lab/config/my_pipeline.yml
tasks:
  download_candles:
    enabled: true
    task_class: app.tasks.data_collection.candles_downloader_task.CandlesDownloaderTask
    schedule:
      type: frequency
      frequency_hours: 6.0
      timezone: UTC
    config:
      connector_name: "kucoin_perpetual"   # always use kucoin as candles connector
      quote_asset: "USDT"
      intervals: ["1m", "3m", "1h"]
      days_data_retention: 60
```

Then run the orchestrator:

```bash
cd quants-lab
python -m core.tasks.orchestrator --config config/my_pipeline.yml
```

### Option B — Use the research notebook

Open and run `research_notebooks/data_collection/download_candles_all_pairs.ipynb`. It downloads 1m candles for all USDT-perpetual pairs and writes them to the local cache (`data/candles/`).

### Verify data is present

```python
from core.data_paths import DataPaths
import pandas as pd

paths = DataPaths()
candles = pd.read_parquet(paths.get_candles_path("kucoin_perpetual", "BTC-USDT", "1m"))
print(candles.tail())
```

---

## Step 2: Engineer Features & Generate Signals

> **Skip this step** if you are optimizing a standalone indicator-based controller (like `macd_bb_v1` or `bollinger_v1`) that does not rely on pre-computed signals. Go directly to Step 3.

This step is required for signal-driven deployment pipelines (like the trend-follower grid).

Open and run `research_notebooks/feature_engineering/trend_follower_grid.ipynb`. It:

1. Loads candles from the local cache
2. Computes an `ema_trend` feature for each trading pair (a float in [-1, +1] representing trend strength and direction)
3. Writes `Feature` and `Signal` documents to MongoDB

**Signal schema:**

| Field | Description |
|-------|-------------|
| `signal_name` | e.g., `"ema_trend"` |
| `trading_pair` | e.g., `"BTC-USDT"` |
| `category` | `"tf"` (trend-following), `"mr"` (mean-reversion), `"pt"` (pairs-trading) |
| `value` | Float in `[-1.0, +1.0]`. Values > 0.7 → strong long. Values < -0.7 → strong short. |

You can query signals in your own notebooks:

```python
from core.features.storage import FeatureStorage
from core.database_manager import DatabaseManager

db = DatabaseManager()
storage = FeatureStorage(db)

signals = await storage.get_latest_signals(
    signal_name="ema_trend",
    category="tf",
    min_value=0.7    # only strong long signals
)
```

---

## Step 3: Optimize Strategy Hyperparameters

This is the core "training" step. The `StrategyOptimizer` uses Optuna (TPE sampler) to find the controller config that maximizes a performance objective (default: Sharpe ratio) over historical data.

### 3a. Choose the controller you want to optimize

Available controllers by type:

**Directional Trading** (signal → position with triple-barrier exits)
- `macd_bb_v1` — MACD + Bollinger Bands
- `bollinger_v1`, `bollinger_v2` — pure Bollinger
- `supertrend_v1` — Supertrend indicator
- `dman_v3` — Dynamic market-adaptive
- `bollingrid` — Bollinger + grid hybrid

**Market Making** (spread-based)
- `pmm_simple` — Pure market making
- `pmm_dynamic` — Volatility-adjusted spreads
- `dman_maker_v2` — Dynamic market maker

**Generic** (custom logic)
- `grid_strike` — Price-range grid trading
- `stat_arb` — Statistical arbitrage
- `xemm_multiple_levels` — Cross-exchange market making

### 3b. Write a config generator

Create a subclass of `BaseStrategyConfigGenerator` that defines the hyperparameter search space using Optuna's `trial` API:

```python
# quants-lab/app/tasks/backtesting/my_macd_optimization_task.py
from core.backtesting.optimizer import BaseStrategyConfigGenerator, StrategyOptimizer
from hummingbot.controllers.directional_trading.macd_bb_v1 import MACDBBControllerConfig

class MACDBBConfigGenerator(BaseStrategyConfigGenerator):
    def generate_config(self, trial) -> MACDBBControllerConfig:
        return MACDBBControllerConfig(
            controller_name="macd_bb_v1",
            connector_name="kucoin_perpetual",
            trading_pair=self.trading_pair,
            interval=trial.suggest_categorical("interval", ["3m", "5m", "15m"]),
            bb_length=trial.suggest_int("bb_length", 50, 200, step=10),
            bb_std=trial.suggest_float("bb_std", 1.5, 3.0, step=0.25),
            bb_long_threshold=trial.suggest_float("bb_long_threshold", 0.0, 0.35, step=0.05),
            bb_short_threshold=trial.suggest_float("bb_short_threshold", 0.65, 1.0, step=0.05),
            macd_fast=trial.suggest_int("macd_fast", 8, 21, step=1),
            macd_slow=trial.suggest_int("macd_slow", 21, 55, step=2),
            macd_signal=trial.suggest_int("macd_signal", 5, 15, step=1),
            total_amount_quote=500,
            max_executors_per_side=1,
            leverage=10,
            triple_barrier_config=TripleBarrierConfig(
                stop_loss=trial.suggest_float("stop_loss", 0.005, 0.03, step=0.005),
                take_profit=trial.suggest_float("take_profit", 0.005, 0.03, step=0.005),
                time_limit=60 * 60 * 24,  # 1 day
                open_order_type=OrderType.MARKET,
            ),
        )
```

### 3c. Run the optimizer

```python
import asyncio
from core.backtesting.optimizer import StrategyOptimizer

optimizer = StrategyOptimizer(
    config_generator=MACDBBConfigGenerator(trading_pair="BTC-USDT"),
    connector_name="kucoin_perpetual",
    trading_pair="BTC-USDT",
    start_date="2024-01-01",
    end_date="2024-06-30",
    n_trials=200,           # number of hyperparameter combinations to try
    n_jobs=4,               # parallel backtests
    study_name="macd_bb_btcusdt_v1",
    storage="sqlite:///optuna_studies.db",   # persistent study storage
)

asyncio.run(optimizer.optimize())
```

Or wrap it in a task YAML (see `config/template_1_candles_optimization.yml` for a full example) and run it through the orchestrator.

### 3d. What "training" actually does

Each Optuna trial:

1. Generates a controller config from the search space
2. Runs `BacktestingEngine.run_backtesting()` on your chosen date range
3. Returns an objective value (Sharpe ratio by default, but you can override to use net PnL, max drawdown, etc.)
4. Optuna's TPE sampler uses the result to guide the next trial toward better regions of the parameter space

This is Bayesian optimization, not neural-network training. The "model" is the parameter set that maximizes your objective on historical data.

---

## Step 4: Analyze Optimization Results

Use the analysis notebook to inspect your study:

```
research_notebooks/optimization_analysis/analyze_optimization_results.ipynb
```

It provides:

- Objective value distribution across all trials
- Parameter importance plots (which hyperparameters matter most)
- Best trial config
- Equity curve for the best configuration

**Retrieve best parameters programmatically:**

```python
import optuna

study = optuna.load_study(
    study_name="macd_bb_btcusdt_v1",
    storage="sqlite:///optuna_studies.db"
)

best_trial = study.best_trial
print(f"Best Sharpe: {best_trial.value:.4f}")
print(f"Best params: {best_trial.params}")
```

**Critical checks before deploying:**

- [ ] Sharpe ratio > 1.0 on the training period
- [ ] Walk-forward test: re-run the best config on a held-out date range it has never seen
- [ ] The best config is not over-fitted (check that top-10 trials have similar performance)
- [ ] Max drawdown is within acceptable bounds for your risk appetite
- [ ] The number of trades is large enough (> 50) to be statistically significant

---

## Step 5: Create the Controller Config

Translate the best parameters into a controller config YAML. This YAML is what the hummingbot-api stores and the hummingbot engine reads.

### Option A — Create via the Dashboard (recommended for simple configs)

1. Open the dashboard at `http://localhost:8501` (or your hosted URL)
2. Go to **Config → [Strategy Name]** (e.g., MACD BB v1)
3. Fill in the parameters from your optimization results
4. Click **"Backtest"** to verify performance inline
5. Click **"Save Config"** — enter a descriptive name (e.g., `macd_bb_btcusdt_long_v1`)

The config is saved to `bots/conf/controllers/macd_bb_btcusdt_long_v1.yml` on the API server.

### Option B — Create via API (for automated pipelines)

```python
import httpx
import yaml

controller_config = {
    "id": "macd_bb_btcusdt_long_v1",
    "controller_name": "macd_bb_v1",
    "controller_type": "directional_trading",
    "connector_name": "kucoin_perpetual",
    "trading_pair": "BTC-USDT",
    "interval": "5m",
    "bb_length": 120,
    "bb_std": 2.0,
    "bb_long_threshold": 0.1,
    "bb_short_threshold": 0.9,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "total_amount_quote": "500",
    "max_executors_per_side": 1,
    "leverage": 10,
    "position_mode": "HEDGE",
    "triple_barrier_config": {
        "stop_loss": "0.015",
        "take_profit": "0.015",
        "time_limit": 86400,
        "open_order_type": 1,          # MARKET = 1
        "take_profit_order_type": 2,   # LIMIT = 2
        "stop_loss_order_type": 1,
    }
}

response = httpx.post(
    "https://api.metallorum.duckdns.org/controllers/configs/macd_bb_btcusdt_long_v1",
    json=controller_config,
    auth=("username", "password"),
)
response.raise_for_status()
print("Config saved:", response.json())
```

### Option C — Use the quants-lab BackendAPIClient (for notebooks/pipelines)

```python
from core.services.backend_api_client import BackendAPIClient

client = BackendAPIClient(
    host="https://api.metallorum.duckdns.org",
    username="username",
    password="password",
)

await client.save_controller_config(config_dict=controller_config)
```

### GridStrike config example

For the trend-follower pipeline, the `grid_strike` controller is used. The key fields are price levels derived from the `ema_trend` feature:

```yaml
id: tf_btcusdt_long_20250317_1200
controller_name: grid_strike
controller_type: generic
connector_name: kucoin_perpetual
trading_pair: BTC-USDT
side: 1                        # 1=long, 2=short
position_mode: HEDGE
leverage: 20
start_price: "42000.0"         # grid lower bound
end_price: "45000.0"           # grid upper bound
limit_price: "41000.0"         # stop loss level (below start for long)
total_amount_quote: "400"
max_open_orders: 2
max_orders_per_batch: 1
min_order_amount_quote: "6"
min_spread_between_orders: "0.002"
activation_bounds: "0.004"
order_frequency: 5
keep_position: false
triple_barrier_config:
  open_order_type: 3           # LIMIT_MAKER
  take_profit: "0.0016"
  take_profit_order_type: 3
```

---

## Step 6: Validate with a Single Backtest

Before deploying, run a final validation backtest through the API. This confirms the config is valid and gives you one last performance check.

### Via Dashboard

In any strategy Config page, after loading your saved config, click **Backtest**. The dashboard calls `POST /backtesting/run-backtesting` and shows results inline.

### Via API

```python
import httpx

payload = {
    "start_time": "2024-07-01 00:00:00",
    "end_time": "2024-12-31 00:00:00",
    "backtesting_resolution": "1m",
    "trade_cost": 0.0006,
    "config": {
        "controller_name": "macd_bb_v1",
        "connector_name": "kucoin_perpetual",
        "trading_pair": "BTC-USDT",
        # ... all params
    }
}

response = httpx.post(
    "https://api.metallorum.duckdns.org/backtesting/run-backtesting",
    json=payload,
    auth=("username", "password"),
    timeout=120,
)
result = response.json()
print(f"Sharpe: {result['results']['sharpe_ratio']:.4f}")
print(f"Net PnL: {result['results']['net_pnl_quote']:.2f}")
print(f"Max Drawdown: {result['results']['max_drawdown_usd']:.2f}")
```

---

## Step 7: Deploy to a Live Bot

Once you're satisfied with the config, deploy it. A single bot instance can run multiple controllers simultaneously (useful for deploying a portfolio of strategies in one container).

### 7a. Prerequisites

- Confirm the controller config YAML exists on the API server (`GET /controllers/configs/`)
- Confirm exchange credentials are configured (`GET /accounts/`)
- Check available Docker images (`GET /docker/images`)

### Option A — Deploy via Dashboard (recommended for manual deploys)

1. Go to **Orchestration → Deploy Bot**
2. Set **Instance Name** (e.g., `macd-bb-portfolio-v1`)
3. Select **Credentials Profile** (e.g., `master_account`)
4. Select **Docker Image** (e.g., `hummingbot/hummingbot:latest`)
5. Check the boxes next to the controller configs you want to include
6. Optionally set drawdown limits
7. Click **Deploy Bot**

### Option B — Deploy via API

```python
import httpx

deployment = {
    "instance_name": "macd-bb-portfolio-v1",
    "credentials_profile": "master_account",
    "controllers_config": [
        "macd_bb_btcusdt_long_v1",
        "macd_bb_ethusdt_long_v1",
    ],
    "image": "hummingbot/hummingbot:latest",
    "max_global_drawdown_quote": 1000.0,
    "max_controller_drawdown_quote": 200.0,
}

response = httpx.post(
    "https://api.metallorum.duckdns.org/bot-orchestration/deploy-v2-controllers",
    json=deployment,
    auth=("username", "password"),
)
response.raise_for_status()
print("Bot deployed:", response.json())
```

### Option C — Deploy via quants-lab BackendAPIClient (for automated pipelines)

```python
await client.deploy_v2_controllers(
    instance_name="macd-bb-portfolio-v1",
    credentials_profile="master_account",
    controllers_config=["macd_bb_btcusdt_long_v1", "macd_bb_ethusdt_long_v1"],
    image="hummingbot/hummingbot:latest",
    max_global_drawdown_quote=1000.0,
    max_controller_drawdown_quote=200.0,
)
```

### What happens under the hood

1. The API generates a script config YAML (`conf/scripts/{instance_name}-{timestamp}.yml`):
   ```yaml
   script_file_name: v2_with_controllers.py
   controllers_config:
     - macd_bb_btcusdt_long_v1.yml
     - macd_bb_ethusdt_long_v1.yml
   max_global_drawdown_quote: 1000.0
   max_controller_drawdown_quote: 200.0
   ```
2. A Docker container is created from the chosen image, with the bot's directory mounted as a volume.
3. The hummingbot engine starts inside the container, reads the script config, loads all controller configs, and begins trading.
4. The bot connects to the EMQX MQTT broker. The API discovers it and begins tracking its state.
5. A `BotRun` record is written to PostgreSQL with metadata about this deployment.

---

## Step 8: Monitor and Manage the Bot

### Check bot status

```python
# All active bots
response = httpx.get(
    "https://api.metallorum.duckdns.org/bot-orchestration/status",
    auth=("username", "password"),
)
bots = response.json()
for bot_name, status in bots.items():
    print(f"{bot_name}: {status['status']}")
```

### Dashboard monitoring

- **Orchestration → Instances** — shows all Docker containers and their states
- **Performance → Bot Performance** — P&L breakdown per controller, per bot, with equity curves

### Update a running bot's controller config (live update)

You can update a controller's parameters without restarting the bot:

```python
response = httpx.post(
    "https://api.metallorum.duckdns.org/controllers/bots/macd-bb-portfolio-v1/macd_bb_btcusdt_long_v1/config",
    json={"take_profit": "0.02", "stop_loss": "0.01"},  # partial update
    auth=("username", "password"),
)
```

### Stop and archive a bot

When a bot has run its course (or you want to replace it with a new version):

```python
response = httpx.post(
    "https://api.metallorum.duckdns.org/bot-orchestration/stop-and-archive-bot/macd-bb-portfolio-v1",
    auth=("username", "password"),
)
```

This gracefully stops all controllers, archives the bot's data to S3, and removes the Docker container.

---

## Automated Pipeline: The Trend-Follower Grid

The production pipeline in `research_notebooks/bot_orchestration/tf_pipeline.ipynb` demonstrates the full automated workflow. It runs on a schedule (every 30 minutes via the task orchestrator) and does everything automatically:

1. **Signal ingestion** — reads `ema_trend` signals from MongoDB; selects top-N long (> 0.7) and short (< -0.7) pairs
2. **Bot inventory** — queries the API for all running `trend_follower_grid*` bots
3. **Stop decisions** — stops controllers that have been running long enough AND whose signal has flipped or P&L is below threshold
4. **Archiving** — archives bots where all controllers are stopped
5. **New deployments** — for each new strong signal with no existing position, creates a `grid_strike` config and deploys a new bot
6. **Notification** — sends a Telegram summary of all actions taken

To build your own automated pipeline, use `DeploymentBaseTask` as a base:

```python
# quants-lab/app/tasks/deployment/my_strategy_deployment.py
from app.tasks.deployment.deployment_base_task import DeploymentBaseTask

class MyStrategyDeploymentTask(DeploymentBaseTask):
    async def get_candidates(self):
        # Return list of controller configs to potentially deploy
        signals = await self.storage.get_latest_signals(signal_name="my_signal", min_value=0.7)
        return [self.signal_to_config(s) for s in signals]

    async def should_stop_controller(self, bot_name, controller_name, status):
        # Return True if this controller should be stopped
        pnl = status.get("net_pnl_quote", 0)
        return pnl < -self.max_controller_drawdown
```

---

## Naming Conventions

Consistent naming makes it easier to track which configs belong to which study and when they were deployed.

| Object | Convention | Example |
|--------|-----------|---------|
| Optuna study | `{strategy}_{pair}_{version}` | `macd_bb_btcusdt_v1` |
| Controller config | `{strategy}_{pair}_{direction}_{date}` | `macd_bb_btcusdt_long_20250317` |
| Bot instance | `{strategy}-{portfolio-id}` | `macd-bb-portfolio-v1` |
| Docker image tag | `hummingbot/hummingbot:{version}` | `hummingbot/hummingbot:latest` |

---

## Quick Reference

### Controller config endpoints

| Action | Method | Endpoint |
|--------|--------|----------|
| List all configs | GET | `/controllers/configs/` |
| Get a config | GET | `/controllers/configs/{name}` |
| Create/update config | POST | `/controllers/configs/{name}` |
| Delete config | DELETE | `/controllers/configs/{name}` |
| Get config template | GET | `/controllers/{type}/{name}/config/template` |
| Validate config | POST | `/controllers/{type}/{name}/config/validate` |

### Bot lifecycle endpoints

| Action | Method | Endpoint |
|--------|--------|----------|
| Deploy bot | POST | `/bot-orchestration/deploy-v2-controllers` |
| All bots status | GET | `/bot-orchestration/status` |
| Single bot status | GET | `/bot-orchestration/{bot_name}/status` |
| Bot history | GET | `/bot-orchestration/{bot_name}/history` |
| Start bot | POST | `/bot-orchestration/start-bot` |
| Stop bot | POST | `/bot-orchestration/stop-bot` |
| Stop and archive | POST | `/bot-orchestration/stop-and-archive-bot/{bot_name}` |
| Bot run history | GET | `/bot-orchestration/bot-runs` |

### Key quants-lab paths

| Path | Description |
|------|-------------|
| `config/*.yml` | Task pipeline configs |
| `research_notebooks/data_collection/` | Candle download notebooks |
| `research_notebooks/feature_engineering/` | Feature computation notebooks |
| `research_notebooks/bot_orchestration/tf_pipeline.ipynb` | Main automated orchestration notebook |
| `research_notebooks/optimization_analysis/` | Study analysis notebooks |
| `core/backtesting/optimizer.py` | StrategyOptimizer class |
| `core/features/storage.py` | MongoDB feature/signal storage |
| `core/services/backend_api_client.py` | HTTP client for hummingbot-api |
| `app/tasks/deployment/deployment_base_task.py` | Base class for autonomous deployment tasks |

---

## Common Issues & Tips

**Backtest has too few trades**
- Widen the indicator thresholds (e.g., lower `bb_long_threshold`) or use a shorter interval
- Use a longer date range

**Optimization runs very slowly**
- Increase `n_jobs` (parallel trials) — up to the number of available CPU cores
- Use a shorter date range for the initial search, then refine on a longer range
- Pre-filter trading pairs to only the most liquid ones

**Bot doesn't appear after deploy**
- Check the MQTT broker is reachable from the bot container (`GET /bot-orchestration/mqtt`)
- Inspect the Docker container logs: `GET /docker/containers/{bot_name}/logs`
- Verify the credentials profile has valid API keys for the target exchange

**Controller config validation fails**
- Use `POST /controllers/{type}/{name}/config/validate` to get a detailed error message
- Check that all required fields are present and that numeric fields are strings where Decimal is expected

**Live update not reflected**
- There is a short polling delay. The bot controller reads its config every few seconds.
- If the bot appears stuck, check its status via `GET /bot-orchestration/{bot_name}/status`
