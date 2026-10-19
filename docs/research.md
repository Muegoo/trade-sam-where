# Offline research CLI

`python3 -m trade_app.research` validates archived datasets, exports causal snapshots, replays explicit proposals, and checks advisory proposals in shadow mode. It reads local JSON only. Every input remains unchanged, JSON output rejects non-finite numbers, and file outputs are created exclusively so an existing file is never overwritten. Inputs must be regular UTF-8 JSON files no larger than 100 MiB. Duplicate object keys are rejected.

## Commands

Validate a dataset and print a compact manifest:

```sh
python3 -m trade_app.research validate --dataset archive.json
```

Export the public point-in-time snapshot to standard output, or to a new file:

```sh
python3 -m trade_app.research snapshot --dataset archive.json --as-of 1700000000
python3 -m trade_app.research snapshot --dataset archive.json --as-of 1700000000 --output snapshot.json
```

Replay proposals with optional risk settings, starting balance, latency, and calendar blackout:

```sh
python3 -m trade_app.research replay --dataset archive.json --proposals proposals.json --settings settings.json --balance 10000 --latency 2 --calendar-before 15 --calendar-after 10 --output replay.json --trace-output trace.json
```

Replay runs execute only against the supplied historical quotes. `--leave-open` preserves positions and pending orders at the end of the archive. `--calendar-before` and `--calendar-after` default to zero and accept integers from 0 to 1440 minutes. A nonzero window rejects a proposal when a matching known high-impact calendar event covers its decision timestamp.

Validate advisory proposals against one public point-in-time snapshot without executing orders or calling a provider:

```sh
python3 -m trade_app.research shadow --dataset archive.json --proposals shadow-proposals.json --as-of 1700000000 --calendar-before 15 --calendar-after 10 --output shadow.json
```

Shadow responses report `provider_status: not_configured`, `mode: shadow`, and `executed_orders: 0`. `WAIT` and `NO_TRADE` remain non-action statuses. A calendar blackout changes a valid `BUY` or `SELL` proposal to `NO_TRADE` with its event reasons.

Run the built-in scanners against archived bars with chronological walk-forward folds:

```sh
python3 -m trade_app.research evaluate --dataset archive.json --strategies smc,ema,ai --train-bars 600 --validation-bars 250 --test-bars 100 --purge-bars 1 --embargo-bars 1 --balance 10000 --latency 2 --calendar-before 15 --calendar-after 10 --settings settings.json --output evaluation.json
```

Evaluation uses M15 bars for `smc`, `supply`, `ema`, `bollinger`, and `asian`; `ai` uses H1 bars. The first training window begins at index zero. Each later window advances by `test-bars`; its test range does not overlap the prior fold's test range. Purge separates train and validation, and embargo separates validation and test. Decisions use only closed bars whose `available_at` has passed. The AI baseline reuses `gold_demo_alert.feature_rows` and `classify`, fits `StandardScaler` plus `LogisticRegression(max_iter=1000)` on training labels only, and applies the existing 20 selected signals / 55% validation gate before allowing test proposals. A fold without enough training classes or validation signals reports its model status and produces no AI proposals.

Each fold starts at the supplied balance and replays archived quotes from the first test decision, up to but excluding the earlier of the next bar's decision time and one timeframe after the final test decision. The final fold uses the one-timeframe cutoff too, so later unrelated quotes cannot extend its holding period. If no eligible quote exists, the fold reports `data_unavailable`. The evaluator rejects runs requiring more than 200 folds per strategy instead of truncating them. The report lists fold-level trades and drawdowns; it does not combine fold equity curves into a continuous portfolio drawdown or rank strategies. Transaction costs and execution rules come from the dataset and the existing replay engine. Insufficient bars return an explicit `insufficient_data` status with required and available counts. Results are historical simulations and make no profitability claim.

## Dataset input

The dataset is a version 1 JSON object in UTC. The loader requires `source`, `instrument`, `costs`, and a non-empty `quotes` array; `bars` and `evidence` may be empty. No prices or transaction costs are supplied by the CLI.

| Field | Required contents |
|---|---|
| `instrument` | `symbol`, `currency`, positive `point`, `tick_size`, `contract_size`, `volume_min`, `volume_max`, `volume_step`, nonnegative integer `stops_level`, and positive fixed `margin_per_lot` |
| `costs` | `commission_per_lot_side`, `fee_per_lot_side`, and `slippage_points` (nonnegative); signed `swap_long_per_lot_day` and `swap_short_per_lot_day`; `rollover_hour_utc` (0–23), unique `rollover_weekdays` (Monday=0 through Sunday=6), and optional `triple_swap_weekday` using the same numbering |
| `quotes[]` | Strictly increasing positive epoch `time`, positive `bid` and `ask` with `ask > bid`; an optional `symbol` must equal the instrument symbol |
| `bars.M15[]`, `bars.H1[]` | Optional OHLC records with `time`, `available_at`, and positive `open`, `high`, `low`, `close`; availability must be at or after bar close and monotonic |
| `evidence[]` | Optional unique `id`, `kind` (`news`, `macro`, or `calendar`), `source`, matching `symbol`, `published_at`, `available_at`, `fetched_at`, and scalar `value`; calendar records also require `event_at`, and `impact` may be `low`, `medium`, or `high` |

Costs are explicit inputs. Missing costs fail validation; the CLI does not infer commissions, fees, swaps, margin, or slippage. Snapshots and event gates use `published_at` and source-verified `available_at` no later than the as-of time. `fetched_at` records archive provenance and cannot establish when information became available to a trader.

## Proposal inputs

Replay accepts an array in the schema consumed by `run_replay`: each row has positive integer `decision_at`, `side` (`BUY` or `SELL`), `kind` (`MARKET`, `LIMIT`, or `STOP`), positive `sl` and `tp`, and optional `entry`, `lot`, and `strategy`. Strategy defaults to `manual`; an explicit entry and lot may be null. Replay waits for the next eligible quote after decision time and configured latency. Calendar-excluded rows remain in `rejected` with stage `calendar`, event IDs, reasons, and the next allowed timestamp.

Shadow requires `action` (`BUY`, `SELL`, `WAIT`, or `NO_TRADE`), nonempty `strategy` (up to 100 characters), positive `created_at` no later than `as-of`, `expires_at` later than both, the exact current `snapshot_hash`, unique `evidence_ids` from that snapshot, and nonempty `reason` (up to 20,000 characters). `kind` defaults to `MARKET`. `BUY` and `SELL` require positive finite `entry`, `sl`, and `tp` in valid directional order, plus a quote no older than 180 seconds. Evidence references may be empty for technical-only proposals. Shadow rejects unknown fields, stale hashes, unavailable evidence, invalid geometry, expired or future proposals, and malformed rows individually.

## Trace and model limits

Replay can write a separate version 1 trace with a deterministic SHA-256 hash chain anchored to dataset, execution configuration, proposal, and run hashes. Each event carries its sequence and previous hash. The verifier detects modifications to the anchored provenance and events; the chain is not a signature and does not establish who created the artifact. Trace snapshots are compact summaries with a `summary_hash`, recent completed bar tails, the latest known quote, and available evidence IDs. Proposal-supplied evidence references are labeled separately; the trace does not claim a replay consumed evidence the proposal did not name.

Replay assumes linear P&L using `contract_size` in the account currency and fixed `margin_per_lot`. It uses epoch-second quote resolution, fixed dataset transaction-cost inputs, adverse configured slippage for market and stop execution, and no-worse limit entries and take-profit exits. It cannot model sub-second ticks, broker rejections, or pending-order server expiry without recorded events. Results are historical simulations and make no profitability claim. The CLI does not place live orders or contact MetaTrader, external APIs, calendars, or model providers.

## Local research API and advisor

The local FastAPI app exposes a session-only research workspace. Every `/api/research` route, including GET routes and downloads, requires the current `X-Trade-Token`; the app's localhost host and same-origin checks also apply. Upload JSON datasets with `POST /api/research/datasets`, list manifests and sanitized job summaries at `GET /api/research/status`, and request causal snapshots from `GET /api/research/datasets/{dataset_hash}/snapshot?as_of=...`. Download an imported dataset from `GET /api/research/datasets/{dataset_hash}/download`. Submit `replay`, `evaluate`, `compare`, `shadow`, or `advisor` jobs to `POST /api/research/jobs`; poll `GET /api/research/jobs/{id}`, then download completed output from `/result` and replay traces from `/trace`. Replay and shadow proposals are supplied as a JSON array string in `proposals_json`; kind-specific settings belong in the allowlisted `options` object. The dataset manifest includes the first and last quote timestamps.

The Research page provides dataset import and download, replay/evaluation/shadow/advisor jobs, paired A/B comparison, MT5 historical collection, and forward shadow. The comparison runs one strategy across shared chronological folds: A uses rule candidates, B adds the calendar gate, C requires analyst agreement, and D also requires challenger approval. Select one strategy and a maximum of 1–20 LLM decisions (default 10); a disabled provider leaves C/D unavailable. Missing metrics and unavailable folds remain unavailable and are not imputed as zero. Fold drawdown is reported separately and is not a continuous portfolio drawdown.

Historical collection uses `POST /api/research/collect` with UTC epoch-second `start_at` and `end_at`, the full explicit `costs` object, `margin_per_lot`, and optional `gap_threshold_seconds` (default 60). The range must end by the current time and span no more than 31 days. Collection is available only when MT5 Demo history is connected; it creates a `collect` job, imports the resulting dataset into the current session, and returns a report containing sampling counts, quote gaps, bar gaps, and current-specification/cost/availability assumptions. The collector cannot verify historical instrument specifications or original bar publication times. These collection fields are independent from the shared Risk and Replay forms.

Forward shadow status is available at `GET /api/research/forward`. Start it with `POST /api/research/forward/start` using one `strategy`, explicit `costs`, `margin_per_lot`, and `initial_balance`, plus optional latency, horizon (300–86,400 seconds), calendar windows, selected-dataset `evidence_dataset_hash`, provider/model/API key, `max_decisions` (1–20), and `challenger`. Stop new decisions with `POST /api/research/forward/stop`; export the report and captured dataset from `GET /api/research/forward/result` and `/api/research/forward/dataset`. A new run waits for a new closed bar after its start watermark. Results contain independent virtual decisions, not broker fills; after stop, already accepted decisions may continue resolving to their horizon. Unknown or unavailable outcomes have no P&L value. Simulation mode is supported and explicitly labeled; MT5 mode requires a validated demo instrument.

The Research page keeps datasets, jobs, and forward observations in process memory for the current session. Download artifacts needed after an app restart. Advisor, comparison, and optional forward-provider calls remain disabled by default and use only a provider, model, and API key entered manually. Advisor and comparison keys are sent with the job request; a forward key remains temporarily in server process memory during that run for subsequent bar evaluations, then is cleared when the run stops or ends. The browser clears API key fields immediately after serialization; credentials are not stored in browser storage, job metadata, or results. Provider failures fail closed. No research path submits orders or changes account risk settings.

Imports and finished jobs live in process memory and disappear when the app restarts. The workspace accepts at most three unique datasets, each upload is limited to 8 MiB, and it retains at most five finished jobs. It runs one job at a time, caps each result/trace at 10 MiB, and caps retained outputs at 50 MiB. Download outputs you need to keep. The API never writes research artifacts to arbitrary paths or to the trading database.

Advisor calls are optional and disabled unless the request includes a provider, a manually supplied model name, and a manually supplied API key; these keys are transient to the job request. Forward shadow also requires manual provider configuration, and retains its key temporarily in server process memory for later bar evaluations until stopped or ended. Supported adapters use fixed OpenAI-compatible HTTPS endpoints for [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) and [Gemini's OpenAI compatibility endpoint](https://ai.google.dev/gemini-api/docs/openai); the app does not read credentials from environment variables, fetch news or calendars, retry failures, or invent model pricing. Comparison uses one analyst/challenger decision budget (default 10, maximum 20); forward shadow allows at most two provider calls per new bar. Provider failures and invalid or truncated responses fail closed. All valid advice passes the same shadow validation used by the CLI. Tests use mocked HTTP responses and make no paid provider calls.

Point-in-time snapshots constrain the evidence provided to the model, but they cannot remove a model's latent knowledge from training. Historical advisor analysis therefore does not establish that an LLM evaluation is fully free of lookahead. Advisor output is research-only and does not send orders or change risk settings; no live profitability or execution claim is made.
