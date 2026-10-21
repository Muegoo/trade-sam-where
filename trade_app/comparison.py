"""Paired walk-forward comparison of rule and advisory decision variants."""

import bisect
import hashlib
import json
import math
import random
from pathlib import Path

from .datasets import load_dataset
from .event_filter import event_blackout
from .evaluation import evaluate_dataset
from .replay import run_replay

MAX_LLM_DECISIONS = 20
VARIANTS = ("A_rules", "B_calendar", "C_analyst_agreement", "D_challenger_approved")


class ComparisonBudgetError(ValueError):
    def __init__(self, required, limit):
        self.required = required
        self.limit = limit
        super().__init__(f"comparison requires {required} LLM decisions; limit is {limit}")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _quote_window(data, fold):
    quotes = data["quotes"]
    times = [row["time"] for row in quotes]
    window = fold["quote_window"]
    return quotes[bisect.bisect_left(times, window["start_inclusive"]):
                  bisect.bisect_left(times, window["end_exclusive"])]


def _calendar(data, proposal, before, after):
    return event_blackout(data["evidence"], data["instrument"]["symbol"],
                          proposal["decision_at"], before, after)


def _metric(replay):
    metrics = replay["metrics"]
    trades = replay["trades"]
    return {"net_pnl": metrics["net_pnl"], "closed_trades": len(trades),
            "expectancy_per_closed_trade": (sum(row["net_pnl"] for row in trades) / len(trades) if trades else None),
            "max_equity_drawdown": metrics["max_equity_drawdown"]}


def _confidence_interval(values, seed):
    if len(values) < 2:
        return {"status": "unavailable", "n": len(values), "mean": None, "ci95": None}
    rng = random.Random(int(seed[:16], 16))
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(5000))
    return {"status": "available", "n": len(values), "mean": sum(values) / len(values),
            "ci95": [means[124], means[4874]],
            "method": "paired fold bootstrap, 5000 resamples with replacement"}


def compare_strategy(dataset, strategy, settings=None, train_bars=600, validation_bars=250,
                     test_bars=100, purge_bars=1, embargo_bars=1,
                     initial_balance=10000.0, latency_seconds=0,
                     calendar_before_minutes=0, calendar_after_minutes=0,
                     provider="disabled", model=None, api_key=None,
                     max_llm_decisions=10, client=None, require_quote_coverage=False):
    """Compare four variants using identical causal folds and archived quotes."""
    from .advisor import AdvisorError, generate_advice

    if not isinstance(strategy, str):
        raise ValueError("strategy must be a supported strategy name")
    if isinstance(max_llm_decisions, bool) or not isinstance(max_llm_decisions, int) or not 1 <= max_llm_decisions <= MAX_LLM_DECISIONS:
        raise ValueError("max_llm_decisions must be an integer in 1..20")
    if provider not in ("disabled", "openai", "gemini"):
        raise ValueError("provider is unsupported")
    if model is not None and (not isinstance(model, str) or not model or len(model) > 200):
        raise ValueError("model is invalid")
    if api_key is not None and (not isinstance(api_key, str) or not api_key or len(api_key) > 4096):
        raise ValueError("api_key is invalid")
    for value, name in ((calendar_before_minutes, "calendar_before_minutes"),
                        (calendar_after_minutes, "calendar_after_minutes")):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1440:
            raise ValueError(f"{name} must be an integer in 0..1440")
    configured = provider != "disabled" and bool(model) and bool(api_key)

    # Freeze rule candidates without a calendar filter; B/C/D apply the requested gate below.
    evaluation = evaluate_dataset(dataset, strategies=[strategy], settings=settings,
        train_bars=train_bars, validation_bars=validation_bars, test_bars=test_bars,
        purge_bars=purge_bars, embargo_bars=embargo_bars, initial_balance=initial_balance,
        latency_seconds=latency_seconds, calendar_before_minutes=0, calendar_after_minutes=0,
        require_quote_coverage=require_quote_coverage)
    data = dataset.to_dict()
    output = evaluation["strategies"][strategy]
    plans, preflight = [], []
    for fold in output.get("folds", []):
        quotes = _quote_window(data, fold)
        record = {"fold": fold["fold"], "status": fold["status"], "proposals": fold["proposals"],
                  "quotes": quotes, "variants": {name: [] for name in VARIANTS}, "decisions": []}
        if fold["status"] != "completed" or not quotes:
            record["status"] = "data_unavailable"
            record["unavailable_reason"] = "evaluation fold has no replayable quote window"
            plans.append(record)
            continue
        eligible = []
        replay = fold.get("replay") or {}
        verdicts = {}
        for row in replay.get("accepted", []) + replay.get("rejected", []):
            risk = row.get("risk") if isinstance(row, dict) else None
            lot = risk.get("lot") if isinstance(risk, dict) else None
            if isinstance(lot, (int, float)) and not isinstance(lot, bool) and math.isfinite(lot) and lot > 0:
                verdicts[row.get("index")] = lot
        for proposal_index, candidate in enumerate(fold["proposals"]):
            lot = verdicts.get(proposal_index)
            if lot is None:
                record.setdefault("common_exclusions", []).append({"candidate_index": proposal_index,
                    "reason": "baseline A replay has no positive sizing verdict"})
                continue
            candidate = dict(candidate, lot=lot)
            next_quote = next((quote for quote in quotes
                               if quote["time"] > candidate["decision_at"] + latency_seconds), None)
            gate = _calendar(data, candidate, calendar_before_minutes, calendar_after_minutes)
            decision = {"candidate_index": proposal_index, "decision_at": candidate["decision_at"],
                        "side": candidate["side"], "calendar": gate,
                        "quote_available": next_quote is not None, "analyst_action": None,
                        "challenger_approve": None, "accepted": {name: False for name in VARIANTS},
                        "veto_reasons": {}, "decision_hash": _hash(candidate),
                        "advice_hash": None, "lot": lot,
                        "sizing_reference": "A_rules baseline at acceptance"}
            decision["accepted"]["A_rules"] = True
            decision["accepted"]["B_calendar"] = not gate["blocked"]
            record["decisions"].append(decision)
            record["variants"]["A_rules"].append(candidate)
            if not gate["blocked"]:
                record["variants"]["B_calendar"].append(candidate)
                causal_bars = [bar for bar in data["bars"]["M15"] if bar["available_at"] <= candidate["decision_at"]]
                prior_quotes = [quote for quote in data["quotes"]
                                if quote["time"] <= candidate["decision_at"]]
                quote_age = candidate["decision_at"] - prior_quotes[-1]["time"] if prior_quotes else None
                if next_quote is not None and len(causal_bars) >= 80 and quote_age is not None and 0 <= quote_age <= 180:
                    eligible.append((candidate, decision))
                else:
                    reason = "no_quote_after_decision" if next_quote is None else "advisor_snapshot_unavailable"
                    decision["veto_reasons"]["C_analyst_agreement"] = reason
                    decision["veto_reasons"]["D_challenger_approved"] = reason
            else:
                reason = "; ".join(gate["reasons"]) or "calendar blackout"
                decision["veto_reasons"]["C_analyst_agreement"] = reason
                decision["veto_reasons"]["D_challenger_approved"] = reason
        record["eligible_decisions"] = len(eligible)
        preflight.extend((record, candidate, decision) for candidate, decision in eligible)
        plans.append(record)

    if len(preflight) > max_llm_decisions:
        raise ComparisonBudgetError(len(preflight), max_llm_decisions)
    config = {"strategy": strategy, "settings": evaluation["parameters"]["settings"],
              "train_bars": train_bars, "validation_bars": validation_bars,
              "test_bars": test_bars, "purge_bars": purge_bars, "embargo_bars": embargo_bars,
              "initial_balance": initial_balance, "latency_seconds": latency_seconds,
              "calendar_before_minutes": calendar_before_minutes,
              "calendar_after_minutes": calendar_after_minutes, "provider": provider,
              "model": model, "challenger": True, "max_llm_decisions": max_llm_decisions,
              "require_quote_coverage": require_quote_coverage}
    config_hash = _hash(config)

    provider_calls = 0
    usable_advice_count = 0
    if configured:
        for record, candidate, decision in preflight:
            try:
                advice = generate_advice(dataset, candidate["decision_at"], provider=provider,
                    model=model, api_key=api_key, challenger=True,
                    calendar_before_minutes=calendar_before_minutes,
                    calendar_after_minutes=calendar_after_minutes, client=client,
                    settings=evaluation["parameters"]["settings"])
                usage_rows = advice.get("token_usage") if isinstance(advice, dict) else None
                provider_calls += len(usage_rows) if isinstance(usage_rows, list) else 0
            except AdvisorError:
                raise
            analyst = advice.get("analyst_proposal")
            challenge = advice.get("challenger")
            if isinstance(analyst, dict) and isinstance(challenge, dict):
                usable_advice_count += 1
            decision["analyst_action"] = analyst.get("action") if isinstance(analyst, dict) else None
            decision["challenger_approve"] = challenge.get("approve") if isinstance(challenge, dict) else None
            decision["advice_hash"] = _hash({"analyst_proposal": analyst, "challenger": challenge})
            agrees = decision["analyst_action"] == candidate["side"]
            if agrees:
                record["variants"]["C_analyst_agreement"].append(candidate)
                decision["accepted"]["C_analyst_agreement"] = True
            else:
                decision["veto_reasons"]["C_analyst_agreement"] = "analyst side did not agree with frozen rule candidate"
            if agrees and decision["challenger_approve"] is True:
                record["variants"]["D_challenger_approved"].append(candidate)
                decision["accepted"]["D_challenger_approved"] = True
            elif agrees:
                decision["veto_reasons"]["D_challenger_approved"] = (
                    challenge.get("reason", "challenger did not approve") if isinstance(challenge, dict)
                    else advice.get("provider_status", "challenger unavailable"))
            else:
                decision["veto_reasons"]["D_challenger_approved"] = "analyst side did not agree with frozen rule candidate"
    else:
        for record, _, decision in preflight:
            decision["veto_reasons"]["C_analyst_agreement"] = "provider_not_configured"
            decision["veto_reasons"]["D_challenger_approved"] = "provider_not_configured"

    per_fold = []
    slice_source = {key: data[key] for key in
                    ("version", "source", "timezone", "instrument", "costs", "evidence")}
    for record in plans:
        fold_result = {"fold": record["fold"], "status": record["status"],
                       "eligible_decisions": record.get("eligible_decisions", 0),
                       "decisions": record["decisions"], "variants": {}}
        if record["status"] == "data_unavailable":
            fold_result["unavailable_reason"] = record["unavailable_reason"]
            fold_result["variants"] = {name: None for name in VARIANTS}
            per_fold.append(fold_result)
            continue
        slice_payload = dict(slice_source, quotes=record["quotes"], bars={"M15": [], "H1": []})
        fold_dataset = load_dataset(slice_payload)
        for name in VARIANTS:
            if name in ("C_analyst_agreement", "D_challenger_approved") and (not configured or usable_advice_count == 0):
                fold_result["variants"][name] = None
                continue
            replay = run_replay(fold_dataset, record["variants"][name],
                                settings=evaluation["parameters"]["settings"],
                                initial_balance=initial_balance, latency_seconds=latency_seconds,
                                close_at_end=True)
            fold_result["variants"][name] = _metric(replay)
        per_fold.append(fold_result)

    summaries = {}
    for name in VARIANTS:
        values = [fold["variants"].get(name) for fold in per_fold
                  if fold["status"] == "completed" and fold["variants"].get(name) is not None]
        if name in ("C_analyst_agreement", "D_challenger_approved") and (not configured or usable_advice_count == 0):
            summaries[name] = {"status": "unavailable" if not configured else "no_data",
                "reason": "provider_not_configured" if not configured else "no_advice_executed", "metrics": None}
        elif not values:
            status = "no_candidates" if not preflight else "no_data"
            summaries[name] = {"status": status, "metrics": None}
        else:
            trades = sum(row["closed_trades"] for row in values)
            summaries[name] = {"status": "completed", "metrics": {
                "net_pnl_sum": sum(row["net_pnl"] for row in values), "closed_trades": trades,
                "expectancy_per_closed_trade": (sum(row["net_pnl"] for row in values) / trades if trades else None),
                "worst_fold_drawdown": max(row["max_equity_drawdown"] for row in values),
                "valid_folds": len(values)}}

    paired = {}
    base_by_fold = {row["fold"]: row["variants"]["A_rules"]["net_pnl"] for row in per_fold
                    if row["status"] == "completed" and row["variants"].get("A_rules") is not None}
    for name in VARIANTS[1:]:
        deltas = [row["variants"][name]["net_pnl"] - base_by_fold[row["fold"]]
                  for row in per_fold if row["fold"] in base_by_fold and row["variants"].get(name) is not None]
        paired[name] = _confidence_interval(deltas, _hash({"config_hash": config_hash,
                                                             "dataset_hash": dataset.dataset_hash,
                                                             "variant": name}))

    decision_hashes = [decision["decision_hash"] for fold in per_fold for decision in fold["decisions"]]
    advice_hashes = [decision["advice_hash"] for fold in per_fold for decision in fold["decisions"]
                     if decision["advice_hash"]]
    all_proposals = [candidate for fold in output.get("folds", []) for candidate in fold["proposals"]]
    calendar_known = any(row["kind"] == "calendar" and row["symbol"] == data["instrument"]["symbol"]
                         for row in data["evidence"])
    return {"status": evaluation["status"], "dataset_hash": dataset.dataset_hash,
            "evaluation_run_hash": evaluation["run_hash"], "evaluation_source_hash": evaluation["source_hash"],
            "advisor_source_hash": hashlib.sha256(Path(__file__).with_name("advisor.py").read_bytes()).hexdigest(),
            "comparison_source_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "replay_source_hash": hashlib.sha256(Path(__file__).with_name("replay.py").read_bytes()).hexdigest(),
            "event_filter_source_hash": hashlib.sha256(Path(__file__).with_name("event_filter.py").read_bytes()).hexdigest(),
            "advisor_prompt_hash": hashlib.sha256(__import__("trade_app.advisor", fromlist=["SYSTEM_PROMPT"]).SYSTEM_PROMPT.encode()).hexdigest(),
            "proposal_hash": _hash(all_proposals), "configuration_hash": config_hash,
            "decision_hashes": decision_hashes, "advice_hashes": advice_hashes,
            "parameters": config, "calendar": {"known_calendar_evidence": calendar_known,
                "before_minutes": calendar_before_minutes, "after_minutes": calendar_after_minutes,
                "zero_window_note": "calendar gate disabled because both windows are zero"
                    if calendar_before_minutes == 0 and calendar_after_minutes == 0 else None},
            "provider_status": "configured" if configured else "not_configured",
            "llm_decisions": usable_advice_count,
            "provider_calls": provider_calls,
            "folds": per_fold, "variants": summaries, "paired_fold_pnl_delta_vs_A": paired,
            "quote_coverage": {"required": require_quote_coverage,
                "planned_folds": output.get("planned_folds", 0),
                "evaluated_folds": output.get("complete_folds", 0),
                "excluded_folds": output.get("coverage_exclusions", [])},
            "paired_fold_policy": "data_unavailable folds are excluded; folds are never imputed as zero",
            "winner": None}
