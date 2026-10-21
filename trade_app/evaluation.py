"""Offline, causal walk-forward evaluation of the built-in strategy scanners."""

import bisect
import copy
import hashlib
import json
import math
from pathlib import Path

from .datasets import MAX_TIMESTAMP, load_dataset
from .event_filter import event_blackout
from .replay import run_replay
from .store import DEFAULTS


STRATEGIES = ("smc", "supply", "ema", "bollinger", "asian", "ai")
FRAME_BY_STRATEGY = {key: "H1" if key == "ai" else "M15" for key in STRATEGIES}
MAX_FOLDS = 200
MAX_TEST_DECISIONS = 5000
MAX_BAR_WINDOW = {"M15": 600, "H1": 1600}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _integer(value, name, minimum, maximum=100000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _finite(value, name, positive=False):
    try:
        valid = math.isfinite(value)
    except (TypeError, OverflowError):
        valid = False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not valid:
        raise ValueError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


def _settings(settings, strategies):
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    unknown = set(settings or {}) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
    cfg = copy.deepcopy(DEFAULTS | (settings or {}))
    enabled = cfg.get("enabled_strategies")
    if (not isinstance(enabled, list) or any(x not in STRATEGIES for x in enabled)
            or len(set(enabled)) != len(enabled)):
        raise ValueError("settings.enabled_strategies must contain unique supported strategies")
    cfg["enabled_strategies"] = list(strategies)
    for key in ("max_lot", "risk_percent", "daily_loss", "max_spread", "daily_target",
                "reward_ratio"):
        _finite(cfg.get(key), f"settings.{key}", positive=True)
    _integer(cfg.get("max_orders"), "settings.max_orders", 1)
    if not isinstance(cfg.get("kill_switch"), bool):
        raise ValueError("settings.kill_switch must be boolean")
    for key in ("asian_start", "asian_end"):
        _integer(cfg.get(key), f"settings.{key}", 0, 23)
    threshold = _finite(cfg.get("playbook_threshold"), "settings.playbook_threshold")
    if not .6 <= threshold <= .95:
        raise ValueError("settings.playbook_threshold must be in 0.6..0.95")
    return cfg


def _decision(bar, seconds):
    return max(bar["time"] + seconds, bar["available_at"])


def _folds(count, train_bars, validation_bars, test_bars, purge_bars, embargo_bars):
    required = train_bars + purge_bars + validation_bars + embargo_bars + test_bars
    complete = 0 if count < required else 1 + (count - required) // test_bars
    if complete > MAX_FOLDS:
        raise ValueError(f"evaluation requires {complete} folds; limit is {MAX_FOLDS}")
    out = []
    start = 0
    while len(out) < MAX_FOLDS:
        train_end = start + train_bars
        validation_start = train_end + purge_bars
        validation_end = validation_start + validation_bars
        test_start = validation_end + embargo_bars
        test_end = test_start + test_bars
        if test_end > count:
            break
        out.append({"train": [start, train_end], "purge": [train_end, validation_start],
                    "validation": [validation_start, validation_end],
                    "embargo": [validation_end, test_start], "test": [test_start, test_end]})
        start += test_bars
    return out


def quote_coverage(quotes, times, start, end, max_gap=60):
    rows = quotes[bisect.bisect_left(times, start):bisect.bisect_left(times, end)]
    gaps = ([rows[0]["time"] - start, end - rows[-1]["time"]] +
            [right["time"] - left["time"] for left, right in zip(rows, rows[1:])]) if rows else []
    return {"covered": bool(rows) and max(gaps) <= max_gap,
            "quote_count": len(rows), "maximum_gap_seconds": max(gaps) if gaps else None,
            "allowed_gap_seconds": max_gap, "start": start, "end": end}


def _training_digest(rows, labels, strategy, bounds):
    return _hash({"strategy": strategy, "features": rows, "labels": labels,
                  "training_indices": bounds["train"],
                  "feature_version": "gold_demo_alert.feature_rows.v1",
                  "model": "StandardScaler+LogisticRegression(max_iter=1000)"})


def _metrics(predictions):
    labeled = [row for row in predictions if row["actual_up"] is not None]
    if not labeled:
        return {"labeled_predictions": 0, "directional_accuracy": None, "brier_score": None}
    return {"labeled_predictions": len(labeled),
            "directional_accuracy": sum((row["up_probability"] >= .5) == row["actual_up"]
                                         for row in labeled) / len(labeled),
            "brier_score": sum((row["up_probability"] - int(row["actual_up"])) ** 2
                               for row in labeled) / len(labeled)}


def _slice_dataset(data, quotes):
    payload = {key: copy.deepcopy(data[key]) for key in
               ("version", "source", "timezone", "instrument", "costs", "evidence")}
    payload["quotes"] = quotes
    payload["bars"] = {"M15": [], "H1": []}
    return load_dataset(payload)


def _model_for_fold(bars, decisions, frame_seconds, bounds, strategy, validation_start_at,
                    first_test_at):
    from gold_demo_alert import classify, feature_rows
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    train_start, train_end = bounds["train"]
    val_start, val_end = bounds["validation"]
    history_start = max(0, train_start - MAX_BAR_WINDOW["H1"])
    history = bars[history_start:bounds["validation"][1]]
    row_offset = history_start
    features = feature_rows(history)
    feature_by_index = {row_offset + index: values for index, values in features}
    x, y, indexes, label_availability = [], [], [], []
    for index in range(train_start, train_end):
        target = index + 1
        values = feature_by_index.get(index)
        if values is None or target >= train_end or decisions[target] > validation_start_at:
            continue
        x.append(values)
        y.append(int(bars[target]["close"] > bars[index]["close"]))
        indexes.append(index)
        label_availability.append(decisions[target])
    provenance = {"train_indices": [train_start, train_end], "fit_feature_indices": indexes,
                  "label_max_available_at": max(label_availability) if label_availability else None,
                  "validation_start_at": validation_start_at, "sample_count": len(x)}
    fingerprint = _training_digest(x, y, strategy, bounds)
    if len(x) < 20 or len(set(y)) < 2:
        return {"status": "unavailable", "reason": "insufficient_training_rows_or_single_class",
                "provenance": provenance, "fingerprint": fingerprint}, None, classify
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    model.fit(x, y)
    validation_x, validation_y = [], []
    for index in range(val_start, val_end):
        target = index + 1
        values = feature_by_index.get(index)
        if values is None or target >= val_end or decisions[target] > first_test_at:
            continue
        validation_x.append(values)
        validation_y.append(int(bars[target]["close"] > bars[index]["close"]))
    probabilities = model.predict_proba(validation_x)[:, 1].tolist() if validation_x else []
    selected = [i for i, probability in enumerate(probabilities) if classify(float(probability)) != "WAIT"]
    accuracy = (sum((classify(float(probabilities[i])) == "WATCH_BUY") == bool(validation_y[i])
                    for i in selected) / len(selected)) if selected else 0.0
    validated = len(selected) >= 20 and accuracy >= .55
    status = "validated" if validated else "validation_failed"
    provenance.update({"validation_indices": [val_start, val_end], "validation_feature_indices":
                       [index for index in range(val_start, val_end) if index in feature_by_index and
                        index + 1 < val_end and decisions[index + 1] <= first_test_at],
                       "validation_label_max_available_at": max((decisions[index + 1] for index in range(val_start, val_end)
                                                                  if index + 1 < val_end and decisions[index + 1] <= first_test_at), default=None),
                       "validation_start_at": decisions[val_start], "validation_known_by": first_test_at})
    return {"status": status, "reason": None if validated else "validation_gate_not_met",
            "validation_selected": len(selected), "validation_accuracy": accuracy,
            "validated": validated, "provenance": provenance, "fingerprint": fingerprint}, model, classify


def evaluate_dataset(dataset, strategies=None, settings=None, train_bars=600,
                     validation_bars=250, test_bars=100, purge_bars=1,
                     embargo_bars=1, initial_balance=10000.0, latency_seconds=0,
                     calendar_before_minutes=0, calendar_after_minutes=0, require_quote_coverage=False):
    """Evaluate causal, closed-bar strategy decisions against archived quotes."""
    if not hasattr(dataset, "to_dict") or not hasattr(dataset, "dataset_hash"):
        raise TypeError("dataset must be a loaded Dataset")
    train_bars = _integer(train_bars, "train_bars", 80)
    validation_bars = _integer(validation_bars, "validation_bars", 20)
    test_bars = _integer(test_bars, "test_bars", 1)
    purge_bars = _integer(purge_bars, "purge_bars", 1)
    embargo_bars = _integer(embargo_bars, "embargo_bars", 1)
    latency_seconds = _integer(latency_seconds, "latency_seconds", 0)
    calendar_before_minutes = _integer(calendar_before_minutes, "calendar_before_minutes", 0, 1440)
    calendar_after_minutes = _integer(calendar_after_minutes, "calendar_after_minutes", 0, 1440)
    initial_balance = _finite(initial_balance, "initial_balance", positive=True)
    if type(require_quote_coverage) is not bool:
        raise ValueError("require_quote_coverage must be boolean")
    if strategies is None:
        strategies = list(STRATEGIES)
    if (not isinstance(strategies, (list, tuple)) or not strategies or
            any(not isinstance(item, str) or item not in STRATEGIES for item in strategies) or
            len(set(strategies)) != len(strategies)):
        raise ValueError("strategies must be a non-empty unique subset of supported strategies")
    cfg = _settings(settings, strategies)
    data = dataset.to_dict()
    config = {"strategies": list(strategies), "settings": cfg, "train_bars": train_bars,
              "validation_bars": validation_bars, "test_bars": test_bars,
              "purge_bars": purge_bars, "embargo_bars": embargo_bars,
              "initial_balance": initial_balance, "latency_seconds": latency_seconds,
              "calendar_before_minutes": calendar_before_minutes,
              "require_quote_coverage": require_quote_coverage,
              "calendar_after_minutes": calendar_after_minutes,
              "fold_policy": "fixed-width rolling windows; test blocks non-overlapping",
              "model": "StandardScaler+LogisticRegression(max_iter=1000); train-only fit"}
    config_hash = _hash(config)
    import trade_app.strategies as strategy_module
    import gold_demo_alert
    source_hash = _hash({"evaluation": _file_hash(__file__),
                         "strategies": _file_hash(strategy_module.__file__),
                         "gold_features": _file_hash(gold_demo_alert.__file__)})
    outputs = {}
    quotes = data["quotes"]
    quote_times = [q["time"] for q in quotes]
    evidence_times = [e["available_at"] for e in data["evidence"]]
    evidence_order = sorted(data["evidence"], key=lambda e: e["available_at"])
    evidence_times = [e["available_at"] for e in evidence_order]
    total_test_decisions = 0
    for strategy in strategies:
        frame = FRAME_BY_STRATEGY[strategy]
        bars = data["bars"][frame]
        seconds = data["bar_seconds"][frame]
        folds = _folds(len(bars), train_bars, validation_bars, test_bars, purge_bars, embargo_bars)
        total_test_decisions += len(folds) * test_bars
        if len(folds) > MAX_FOLDS:
            raise ValueError("evaluation exceeds 200 folds per strategy")
        if total_test_decisions > MAX_TEST_DECISIONS:
            raise ValueError("evaluation exceeds 5000 test decisions")
        if not folds:
            required = train_bars + purge_bars + validation_bars + embargo_bars + test_bars
            outputs[strategy] = {"timeframe": frame, "status": "insufficient_data",
                                 "bars_required": required, "bars_available": len(bars),
                                 "complete_folds": 0, "incomplete_tail_bars": len(bars)}
            continue
        decisions = [_decision(bar, seconds) for bar in bars]
        result_folds = []
        coverage_exclusions = []
        for fold_index, bounds in enumerate(folds):
            test_start, test_end = bounds["test"]
            first_test_at = decisions[test_start]
            last_test_at = decisions[test_end - 1]
            next_test_at = decisions[test_end] if test_end < len(bars) else None
            quote_window_end = min(next_test_at if next_test_at is not None else MAX_TIMESTAMP,
                                   min(MAX_TIMESTAMP, last_test_at + seconds))
            coverage = quote_coverage(quotes, quote_times, first_test_at, quote_window_end)
            if require_quote_coverage and not coverage["covered"]:
                coverage_exclusions.append({"fold": fold_index, "reason": "quote_coverage_incomplete", **coverage})
                continue
            if strategy == "ai":
                model_info, model, classify = _model_for_fold(
                    bars, decisions, seconds, bounds, strategy,
                    decisions[bounds["validation"][0]], first_test_at)
            else:
                model_info, model, classify = None, None, None
            predictions, proposals, exclusions = [], [], []
            for index in range(test_start, test_end):
                decision_at = decisions[index]
                prefix_start = max(0, index - MAX_BAR_WINDOW[frame] + 1)
                prefix = bars[prefix_start:index + 1]
                # The scanner sees only bars actually closed and available at this decision.
                prefix = [bar for bar in prefix if bar["time"] + seconds <= decision_at and bar["available_at"] <= decision_at]
                if not prefix or prefix[-1]["time"] != bars[index]["time"]:
                    exclusions.append({"index": index, "decision_at": decision_at, "reason": "bar_unavailable_at_decision"})
                    continue
                model_result = None
                if strategy == "ai":
                    # Features are computed only through this test bar; the fitted pipeline is frozen.
                    from gold_demo_alert import feature_rows
                    local_features = feature_rows(prefix)
                    current = next((values for local_index, values in local_features if local_index == len(prefix) - 1), None)
                    probability = (float(model.predict_proba([current])[0, 1])
                                   if model is not None and current is not None else None)
                    raw = classify(probability) if probability is not None else "WAIT"
                    model_result = {"up_probability": probability,
                                    "validated": bool(model_info.get("validated") and probability is not None),
                                    "validation_signals": model_info.get("validation_selected", 0),
                                    "validation_accuracy": model_info.get("validation_accuracy", 0.0)}
                    actual = None
                    if probability is not None and index + 1 < test_end and decisions[index + 1] <= (next_test_at or last_test_at + seconds):
                        actual = bool(bars[index + 1]["close"] > bars[index]["close"])
                    predictions.append({"bar_index": index, "bar_time": bars[index]["time"],
                                        "decision_at": decision_at, "up_probability": probability,
                                        "raw_signal": raw, "actual_up": actual,
                                        "label_available_at": decisions[index + 1] if actual is not None else None})
                from .strategies import scan
                row = next((item for item in scan(prefix, cfg, model_result=model_result)
                            if item["key"] == strategy), None)
                if not row or row.get("time") != bars[index]["time"]:
                    exclusions.append({"index": index, "decision_at": decision_at, "reason": "scanner_bar_mismatch"})
                    continue
                signal = row.get("signal")
                if signal not in ("BUY", "SELL"):
                    continue
                entry, stop, target = row.get("entry"), row.get("sl"), row.get("tp")
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in (entry, stop, target)):
                    exclusions.append({"index": index, "decision_at": decision_at, "reason": "invalid_scanner_prices"})
                    continue
                proposal = {"decision_at": decision_at, "side": signal, "kind": "MARKET",
                            "entry": entry, "sl": stop, "tp": target, "strategy": strategy,
                            "bar_index": index, "bar_time": bars[index]["time"]}
                known_end = bisect.bisect_right(evidence_times, decision_at)
                gate = event_blackout(evidence_order[:known_end], data["instrument"]["symbol"],
                                     decision_at, calendar_before_minutes, calendar_after_minutes)
                if gate["blocked"]:
                    exclusions.append({"index": index, "decision_at": decision_at,
                                       "reason": "calendar_blackout", "event_ids": gate["event_ids"],
                                       "reasons": gate["reasons"], "next_allowed_at": gate["next_allowed_at"]})
                    continue
                proposals.append(proposal)
            quote_end_exclusive = quote_window_end
            quote_start = bisect.bisect_left(quote_times, first_test_at)
            quote_end = bisect.bisect_left(quote_times, quote_end_exclusive)
            test_quotes = quotes[quote_start:quote_end]
            replay = None
            status = "completed"
            if not test_quotes or (proposals and not any(q["time"] > p["decision_at"] + latency_seconds
                                                         for q in test_quotes for p in proposals)):
                status = "data_unavailable"
            else:
                replay_ds = _slice_dataset(data, test_quotes)
                replay = run_replay(replay_ds, proposals, settings=cfg, initial_balance=initial_balance,
                                    latency_seconds=latency_seconds, close_at_end=True)
                if proposals and all(item["stage"] == "data" for item in replay["rejected"]):
                    status = "data_unavailable"
            if strategy == "ai":
                prediction_metrics = _metrics(predictions)
            else:
                prediction_metrics = None
            result_folds.append({"fold": fold_index, "indices": bounds,
                                 "times": {name: [decisions[value[0]], decisions[value[1] - 1]]
                                           for name, value in bounds.items()},
                                 "quote_window": {"start_inclusive": first_test_at,
                                                  "end_exclusive": quote_window_end},
                                 "status": status, "training": model_info,
                                 "quote_coverage": coverage,
                                 "predictions": predictions if strategy == "ai" else [],
                                 "prediction_metrics": prediction_metrics,
                                 "proposals": proposals, "exclusions": exclusions,
                                 "replay": replay})
        closed = [trade for fold in result_folds if fold["replay"] for trade in fold["replay"]["trades"]]
        net = sum(fold["replay"]["metrics"]["net_pnl"] for fold in result_folds if fold["replay"])
        pnl = [fold["replay"]["metrics"]["net_pnl"] for fold in result_folds if fold["replay"]]
        outputs[strategy] = {"timeframe": frame, "status": "completed" if result_folds else "insufficient_quote_coverage", "bars_available": len(bars),
                             "planned_folds": len(folds), "coverage_exclusions": coverage_exclusions,
                             "complete_folds": len(result_folds),
                             "incomplete_tail_bars": len(bars) - (folds[-1]["test"][1]),
                             "summary": {"net_pnl_sum": net, "closed_trades": len(closed),
                                         "expectancy_per_closed_trade": (sum(t["net_pnl"] for t in closed) / len(closed)
                                                                         if closed else None),
                                         "fold_net_pnl": pnl,
                                         "worst_fold_drawdown": max((fold["replay"]["metrics"]["max_equity_drawdown"]
                                                                      for fold in result_folds if fold["replay"]), default=None),
                                         "drawdown_policy": "reported per isolated fold; no continuous combined drawdown"},
                             "folds": result_folds}
    result = {"status": "completed", "dataset_hash": dataset.dataset_hash,
              "evaluation_config_hash": config_hash, "source_hash": source_hash,
              "run_hash": _hash({"dataset_hash": dataset.dataset_hash, "config_hash": config_hash,
                                  "source_hash": source_hash}),
              "assumptions": {"bar_time": "open timestamp; decision time is max(close time, available_at)",
                              "folds": "chronological rolling windows; non-overlapping test blocks; purge and embargo kept",
                              "replay": "quotes within test interval only; each fold starts at initial_balance and is force-closed at its final quote",
                              "validation": "AI model fits only train rows; validation gate uses selected directional signals and is frozen for test",
                              "historical_results": "simulation only; no profitability claim or automatic strategy ranking"},
              "parameters": config, "strategies": outputs}
    if all(row["status"] in ("insufficient_data", "insufficient_quote_coverage") for row in outputs.values()):
        result["status"] = "insufficient_quote_coverage" if require_quote_coverage else "insufficient_data"
    _canonical(result)
    return result
