# Architecture and verification

Trade Sam Where is a local prototype demonstrating a complete UI-to-API workflow with persistence, synthetic data and explicit execution controls.

## Main modules

| Module | Responsibility |
|---|---|
| `trade_app/server.py` | API schemas, local request checks, application lifecycle and orchestration |
| `trade_app/broker.py` | Paper account and optional MT5 adapter |
| `trade_app/risk.py` | Order geometry, lot sizing and risk gates |
| `trade_app/monitor.py` | Server-side monitoring and close/expiry behavior |
| `trade_app/store.py` | SQLite state and request persistence |
| `trade_app/analytics.py` | Journal aggregation and account metrics |
| `trade_app/static/` | Responsive UI and canvas charts |

## Evidence

The initial portfolio audit on 19 October 2026 ran 49 Python tests and the JavaScript level-calculation checks successfully. The publication adds a regression check that the historical analysis module imports with MetaTrader5 unavailable. Current results are reported by CI.

The browser walkthrough covers navigation, monitor start/stop, order preview/send, cancellation, close confirmation, journal filters/export, settings persistence, kill switch and mobile overflow. It uses an isolated synthetic account; screenshots do not show a connected broker account.

## Limits

Paper execution is simplified and is not an exchange/broker simulator. The historical classifier's directional validation is not a backtest including all execution costs. MT5 integration, OS-specific installation and broker execution must be checked separately from paper-mode tests. Public-hosting authentication and multi-user isolation are outside this local prototype.
