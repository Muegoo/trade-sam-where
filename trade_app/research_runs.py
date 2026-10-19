"""Shared in-process replay and shadow research operations."""

import hashlib
import json
import math

from .datasets import MAX_TIMESTAMP
from .event_filter import event_blackout
from .replay import run_replay, validate_proposals


class ResearchRunError(ValueError):
    pass


def canonical_hash(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _calendar_gate(data, proposal, before, after):
    if not isinstance(proposal, dict):
        return {"blocked": False, "status": "invalid_proposal", "events": [], "event_ids": [],
                "reasons": [], "next_allowed_at": None}
    decision_at = proposal.get("decision_at")
    if isinstance(decision_at, bool) or not isinstance(decision_at, int) or decision_at < 1 or decision_at > MAX_TIMESTAMP:
        return {"blocked": False, "status": "invalid_proposal", "events": [], "event_ids": [],
                "reasons": [], "next_allowed_at": None}
    evidence = [item for item in data["evidence"] if item["published_at"] <= decision_at
                and item["available_at"] <= decision_at]
    return event_blackout(evidence, data["instrument"]["symbol"], decision_at, before, after)


def _shadow_row(raw, index, snapshot, before, after):
    errors = []
    path = f"proposals[{index}]"
    if not isinstance(raw, dict):
        return {"index": index, "status": "invalid", "errors": [f"{path} must be an object"]}
    allowed = {"action", "strategy", "created_at", "expires_at", "snapshot_hash", "evidence_ids",
               "reason", "kind", "entry", "sl", "tp"}
    extra = sorted(set(raw) - allowed)
    if extra:
        errors.append(f"{path} has unsupported field(s): {', '.join(extra)}")
    action = raw.get("action")
    if action not in ("BUY", "SELL", "WAIT", "NO_TRADE"):
        errors.append(f"{path}.action must be BUY, SELL, WAIT, or NO_TRADE")
    strategy = raw.get("strategy")
    if not isinstance(strategy, str) or not strategy.strip() or len(strategy) > 100:
        errors.append(f"{path}.strategy must be non-empty text of at most 100 characters")
    created, expires = raw.get("created_at"), raw.get("expires_at")
    if isinstance(created, bool) or not isinstance(created, int) or created < 1 or created > snapshot["as_of"]:
        errors.append(f"{path}.created_at must be a positive epoch at or before as-of")
    if isinstance(expires, bool) or not isinstance(expires, int) or expires > MAX_TIMESTAMP or expires <= (created if isinstance(created, int) and not isinstance(created, bool) else 0) or expires <= snapshot["as_of"]:
        errors.append(f"{path}.expires_at must be after created_at and as-of")
    if raw.get("snapshot_hash") != snapshot["snapshot_hash"]:
        errors.append(f"{path}.snapshot_hash does not match current snapshot")
    refs = raw.get("evidence_ids")
    if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs) or len(refs) != len(set(refs)):
        errors.append(f"{path}.evidence_ids must be an array of unique strings")
        refs = []
    else:
        known = {item["id"] for item in snapshot["evidence"]}
        if any(item not in known for item in refs):
            errors.append(f"{path}.evidence_ids contains unknown or unavailable evidence")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 20000:
        errors.append(f"{path}.reason must be non-empty text of at most 20000 characters")
    kind = raw.get("kind", "MARKET")
    if kind not in ("MARKET", "LIMIT", "STOP"):
        errors.append(f"{path}.kind must be MARKET, LIMIT, or STOP")
    prices = {}
    if action in ("BUY", "SELL"):
        for name in ("entry", "sl", "tp"):
            value = raw.get(name)
            try:
                finite = math.isfinite(value)
            except (OverflowError, TypeError):
                finite = False
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite or value <= 0:
                errors.append(f"{path}.{name} must be a positive finite number")
            else:
                prices[name] = value
        if len(prices) == 3 and not (prices["sl"] < prices["entry"] < prices["tp"] if action == "BUY"
                                     else prices["tp"] < prices["entry"] < prices["sl"]):
            errors.append(f"{path} has invalid {action} entry/SL/TP geometry")
        if snapshot.get("latest_quote") is None or snapshot.get("quote_age_seconds") is None or snapshot["quote_age_seconds"] > 180:
            errors.append(f"{path} has no current quote or quote is older than 180 seconds")
    elif any(name in raw for name in ("entry", "sl", "tp")):
        errors.append(f"{path} must not provide prices for WAIT or NO_TRADE")
    if errors:
        return {"index": index, "status": "invalid", "action": action, "errors": errors}
    row = {"index": index, "action": action, "strategy": strategy, "created_at": created,
           "expires_at": expires, "snapshot_hash": raw["snapshot_hash"], "evidence_ids": refs,
           "reason": reason, "kind": kind}
    row.update(prices)
    blackout = event_blackout(snapshot["evidence"], snapshot["instrument"]["symbol"], snapshot["as_of"], before, after)
    row["calendar"] = blackout
    if action in ("BUY", "SELL") and blackout["blocked"]:
        row["status"] = "NO_TRADE"
        row["no_trade_reasons"] = blackout["reasons"]
    else:
        row["status"] = action
    return row


def _minutes(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1440:
        raise ResearchRunError(f"{name} must be an integer in 0..1440")
    return value


def run_shadow(dataset, proposals, as_of, before_minutes=0, after_minutes=0):
    if not hasattr(dataset, "snapshot") or not hasattr(dataset, "dataset_hash"):
        raise TypeError("dataset must be a loaded Dataset")
    if isinstance(proposals, (str, bytes)) or not isinstance(proposals, (list, tuple)):
        raise ResearchRunError("proposals must be an array")
    before = _minutes(before_minutes, "before_minutes")
    after = _minutes(after_minutes, "after_minutes")
    snapshot = dataset.snapshot(as_of)
    rows = [_shadow_row(row, index, snapshot, before, after) for index, row in enumerate(proposals)]
    return {"mode": "shadow", "provider_status": "not_configured", "executed_orders": 0,
            "dataset_hash": dataset.dataset_hash, "snapshot_hash": snapshot["snapshot_hash"],
            "as_of": as_of, "instrument": snapshot["instrument"],
            "accepted": [row for row in rows if row["status"] in ("BUY", "SELL", "WAIT", "NO_TRADE")],
            "rejected": [row for row in rows if row["status"] == "invalid"],
            "counts": {"invalid": sum(row["status"] == "invalid" for row in rows),
                       "no_trade": sum(row["status"] in ("WAIT", "NO_TRADE") for row in rows)}}


def run_replay_experiment(dataset, proposals, settings=None, initial_balance=10000.0,
                          latency_seconds=0, close_at_end=True, calendar_before_minutes=0,
                          calendar_after_minutes=0):
    if not hasattr(dataset, "to_dict") or not hasattr(dataset, "dataset_hash"):
        raise TypeError("dataset must be a loaded Dataset")
    before = _minutes(calendar_before_minutes, "calendar_before_minutes")
    after = _minutes(calendar_after_minutes, "calendar_after_minutes")
    if isinstance(latency_seconds, bool) or not isinstance(latency_seconds, int) or latency_seconds < 0:
        raise ResearchRunError("latency_seconds must be a non-negative integer")
    try:
        if not math.isfinite(initial_balance) or initial_balance <= 0:
            raise ResearchRunError("initial_balance must be a positive finite number")
    except (TypeError, OverflowError):
        raise ResearchRunError("initial_balance must be a positive finite number") from None
    if settings is not None and not isinstance(settings, dict):
        raise ResearchRunError("settings must be an object")
    data = dataset.to_dict()
    normalized = validate_proposals(proposals)
    normalized_by_index = {row["index"]: row for row in normalized}
    executable, executable_indices, calendar_rejected, duplicate_rejected = [], [], [], []
    for index, raw_proposal in enumerate(proposals):
        proposal_row = normalized_by_index[index]
        proposal = proposal_row["proposal"]
        gate = _calendar_gate(data, proposal, before, after)
        if gate["blocked"]:
            calendar_rejected.append({"index": index, "proposal": proposal, "stage": "calendar",
                "errors": gate["reasons"], "event_ids": gate["event_ids"],
                "next_allowed_at": gate["next_allowed_at"]})
        elif proposal_row["duplicate"]:
            duplicate_rejected.append({"index": index, "proposal": proposal,
                "stage": "duplicate", "errors": ["duplicate strategy and decision_at"]})
        else:
            executable.append(proposal)
            executable_indices.append(index)
    result = run_replay(dataset, executable, settings=settings, initial_balance=initial_balance,
                        latency_seconds=latency_seconds, close_at_end=close_at_end)
    for row in result["accepted"] + result["rejected"]:
        local_index = row.get("index")
        if isinstance(local_index, int) and 0 <= local_index < len(executable_indices):
            row["index"] = executable_indices[local_index]
    for row in result["risk_breaches"]:
        local_index = row.get("index")
        if isinstance(local_index, int) and 0 <= local_index < len(executable_indices):
            row["index"] = executable_indices[local_index]
    result["rejected"].extend(calendar_rejected)
    result["rejected"].extend(duplicate_rejected)
    result["duplicate_count"] += len(duplicate_rejected)
    result["rejected"].sort(key=lambda row: row.get("index", -1))
    result["execution_config_hash"] = result["config_hash"]
    result["calendar_filter"] = {"before_minutes": before, "after_minutes": after}
    result["config_hash"] = canonical_hash({"execution_config_hash": result["execution_config_hash"],
                                            "calendar_filter": result["calendar_filter"]})
    result["proposal_hash"] = canonical_hash(proposals)
    result["run_hash"] = canonical_hash({"dataset_hash": dataset.dataset_hash,
                                         "proposal_hash": result["proposal_hash"],
                                         "config_hash": result["config_hash"]})
    return result
