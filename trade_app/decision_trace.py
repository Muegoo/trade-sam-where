"""Append-only, tamper-evident replay trace construction and verification."""

import hashlib
import json
from bisect import bisect_right


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _source_snapshot(data, decision_at, quote_times, bar_times):
    quote_end = bisect_right(quote_times, decision_at)
    latest_quote = data["quotes"][quote_end - 1] if quote_end else None
    bars = {}
    for frame, rows in data["bars"].items():
        times = bar_times[frame]
        end = bisect_right(times, decision_at)
        while end and rows[end - 1]["available_at"] > decision_at:
            end -= 1
        bars[frame] = rows[max(0, end - 3):end]
    evidence = [item for item in data["evidence"]
                if item["published_at"] <= decision_at and item["available_at"] <= decision_at]
    summary = {"as_of": decision_at, "instrument": data["instrument"], "latest_quote": latest_quote,
               "quote_age_seconds": decision_at - latest_quote["time"] if latest_quote else None,
               "bar_tail": bars, "available_evidence_ids": [item["id"] for item in evidence]}
    return {"summary_hash": _hash(summary), **summary}


def build_replay_trace(dataset, result):
    """Build a deterministic hash chain from a completed replay result."""
    data = dataset.to_dict()
    if result.get("dataset_hash") != dataset.dataset_hash:
        raise ValueError("replay result dataset_hash does not match dataset")
    provenance = {key: result.get(key) for key in
                  ("dataset_hash", "config_hash", "proposal_hash", "run_hash")}
    provenance["version"] = 1
    for key in ("dataset_hash", "config_hash"):
        if not isinstance(provenance[key], str) or len(provenance[key]) != 64 or any(c not in "0123456789abcdef" for c in provenance[key]):
            raise ValueError(f"replay result {key} must be a SHA-256 hex digest")
    for key in ("proposal_hash", "run_hash"):
        value = provenance[key]
        if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
            raise ValueError(f"replay result {key} must be a SHA-256 hex digest when present")
    anchor = _hash(provenance)
    rows = []
    serial = 0

    def add(when, kind, details):
        nonlocal serial
        serial += 1
        priority = {"proposal_accepted": 0, "proposal_rejected": 0, "order_accepted": 0,
                    "fill_revalidation": 1, "order_filled": 2, "position_entry_deal": 3,
                    "risk_breach": 4, "swap_accrual": 5, "position_exit_deal": 6,
                    "position_closed": 7, "order_cancelled": 8,
                    "order_pending_or_unknown": 8}.get(kind, 5)
        rows.append((when if isinstance(when, int) and not isinstance(when, bool) else 0,
                     priority, serial, {"time": when, "kind": kind, **details}))

    snapshots = {}
    quote_times = [row["time"] for row in data["quotes"]]
    bar_times = {frame: [row["time"] + data["bar_seconds"][frame] for row in rows]
                 for frame, rows in data["bars"].items()}
    orders_by_proposal = {(o.get("decision_at"), o.get("strategy"), o.get("side")): o
                          for o in result.get("orders", [])}

    def source_at(decision_at):
        if decision_at not in snapshots:
            snapshots[decision_at] = _source_snapshot(data, decision_at, quote_times, bar_times)
        return snapshots[decision_at]

    for accepted in result.get("accepted", []):
        proposal = accepted.get("proposal", {})
        decision_at = proposal.get("decision_at")
        order = orders_by_proposal.get((decision_at, proposal.get("strategy"), proposal.get("side")))
        accepted_snapshot = source_at(decision_at) if isinstance(decision_at, int) and not isinstance(decision_at, bool) else None
        add(accepted.get("accepted_at"), "proposal_accepted", {
            "proposal_index": accepted.get("index"), "strategy": proposal.get("strategy"),
            "decision_at": decision_at, "source_snapshot": accepted_snapshot,
            "evidence_ids": proposal.get("evidence_ids", []),
            "evidence_reference_status": "supplied_refs" if "evidence_ids" in proposal else "available_evidence_only",
            "risk_snapshot": accepted.get("risk_snapshot"), "risk": accepted.get("risk"),
            "order_ticket": order.get("ticket") if order else None,
            "position_id": accepted.get("position_id") if order and order.get("status") == "filled" and order.get("kind") == "MARKET" else None})
    for rejected in result.get("rejected", []):
        proposal = rejected.get("proposal", {})
        decision_at = proposal.get("decision_at")
        rejected_snapshot = source_at(decision_at) if isinstance(decision_at, int) and not isinstance(decision_at, bool) else None
        evaluated_at = rejected.get("evaluated_at", decision_at)
        event_time = evaluated_at
        if rejected.get("stage") == "data" and isinstance(decision_at, int) and isinstance(evaluated_at, int):
            event_time = max(decision_at, evaluated_at)
        add(event_time, "proposal_rejected", {"proposal_index": rejected.get("index"),
            "strategy": proposal.get("strategy"), "decision_at": decision_at,
            "evaluated_at": evaluated_at,
            "stage": rejected.get("stage", "unknown"), "reasons": rejected.get("errors", []),
            "risk_snapshot": rejected.get("risk_snapshot"),
            "source_snapshot": rejected_snapshot,
            "evidence_ids": proposal.get("evidence_ids", []),
            "evidence_reference_status": "supplied_refs" if "evidence_ids" in proposal else "available_evidence_only"})
    for order in result.get("orders", []):
        add(order.get("accepted_at"), "order_accepted", {"order_ticket": order.get("ticket"),
            "strategy": order.get("strategy"), "decision_at": order.get("decision_at"),
            "side": order.get("side"), "kind": order.get("kind")})
    for deal in result.get("deals", []):
        kind = "swap_accrual" if deal.get("cost_only") else (
            "position_entry_deal" if deal.get("entry") == 0 else
            "position_exit_deal" if deal.get("entry") == 1 else "deal")
        add(deal.get("time"), kind, {"deal_id": deal.get("ticket"), "order_ticket": deal.get("order"),
            "position_id": deal.get("position_id"), "strategy": deal.get("strategy"),
            "entry": deal.get("entry"), "price": deal.get("price"), "volume": deal.get("volume"),
            "profit": deal.get("profit"), "commission": deal.get("commission"),
            "swap": deal.get("swap"), "fee": deal.get("fee"), "cost_only": deal.get("cost_only", False)})
    for trade in result.get("trades", []):
        add(trade.get("exit_time"), "position_closed", {"position_id": trade.get("position_id"),
            "order_ticket": trade.get("order_ticket"), "strategy": trade.get("strategy"),
            "reason": trade.get("reason"), "net_pnl": trade.get("net_pnl")})
    end_time = (result.get("equity_curve") or [{}])[-1].get("time")
    entry_times = {}
    for deal in result.get("deals", []):
        if deal.get("entry") == 0 and not deal.get("cost_only") and deal.get("volume", 0) > 0:
            ticket = deal.get("order")
            entry_times[ticket] = min(entry_times.get(ticket, deal.get("time")), deal.get("time"))
    for order in result.get("orders", []):
        status = order.get("status", "unknown")
        fill_time = entry_times.get(order.get("ticket"), end_time)
        if "fill_revalidation" in order:
            add(fill_time, "fill_revalidation", {"order_ticket": order.get("ticket"),
                "position_id": order.get("position_id"), "strategy": order.get("strategy"),
                "diagnostic": order.get("fill_revalidation"),
                "risk_snapshot": order.get("fill_risk_snapshot")})
        if status == "filled":
            add(fill_time, "order_filled", {
                "order_ticket": order.get("ticket"), "position_id": order.get("position_id"),
                "strategy": order.get("strategy"), "status": "filled"})
        elif status == "cancelled_end":
            add(end_time, "order_cancelled", {"order_ticket": order.get("ticket"),
                "strategy": order.get("strategy"), "status": "cancelled_end"})
        else:
            add(end_time, "order_pending_or_unknown", {"order_ticket": order.get("ticket"),
                "strategy": order.get("strategy"), "status": status})
    for breach in result.get("risk_breaches", []):
        ticket = breach.get("order_ticket")
        order = next((item for item in result.get("orders", []) if item.get("ticket") == ticket), {})
        add(breach.get("evaluated_at", entry_times.get(ticket, end_time)), "risk_breach", {"order_ticket": ticket,
            "strategy": order.get("strategy"), "stage": breach.get("stage", "unknown"),
            "reasons": breach.get("errors", []),
            "risk_snapshot": breach.get("risk_snapshot", order.get("fill_risk_snapshot"))})
    rows.sort(key=lambda pair: (pair[0], pair[1], pair[2]))
    events = []
    previous = anchor
    for sequence, (_, _, _, body) in enumerate(rows, 1):
        event = {"sequence": sequence, "prev_hash": previous, **body}
        event["hash"] = _hash(event)
        events.append(event)
        previous = event["hash"]
    return {"version": 1, **provenance, "anchor_hash": anchor, "events": events,
            "trace_hash": previous}


def verify_trace(trace):
    """Check provenance anchoring and every event in a trace hash chain."""
    try:
        if not isinstance(trace, dict) or trace.get("version") != 1 or isinstance(trace.get("version"), bool):
            return False
        if set(trace) != {"version", "dataset_hash", "config_hash", "proposal_hash", "run_hash", "anchor_hash", "events", "trace_hash"}:
            return False
        provenance = {key: trace.get(key) for key in
                      ("dataset_hash", "config_hash", "proposal_hash", "run_hash")}
        provenance["version"] = 1
        for key in ("dataset_hash", "config_hash"):
            if not isinstance(provenance[key], str) or len(provenance[key]) != 64 or any(c not in "0123456789abcdef" for c in provenance[key]):
                return False
        for key in ("proposal_hash", "run_hash"):
            value = provenance[key]
            if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
                return False
        anchor = _hash(provenance)
        if trace.get("anchor_hash") != anchor or not isinstance(trace.get("events"), list):
            return False
        previous = anchor
        for index, event in enumerate(trace["events"], 1):
            if not isinstance(event, dict) or event.get("sequence") != index or isinstance(event.get("sequence"), bool):
                return False
            if event.get("prev_hash") != previous or not isinstance(event.get("hash"), str):
                return False
            body = dict(event)
            digest = body.pop("hash")
            if _hash(body) != digest:
                return False
            previous = digest
        return trace.get("trace_hash") == previous
    except (TypeError, ValueError, OverflowError):
        return False
