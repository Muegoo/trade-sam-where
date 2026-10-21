import hashlib
import json
import math
import copy
from datetime import date, datetime, time, timedelta, timezone

from .broker import BrokerError
from .datasets import MAX_TIMESTAMP
from .risk import preview
from .store import DEFAULTS


UTC = timezone.utc


def _num(value, path, positive=False, allow_none=False):
    if allow_none and value is None:
        return None
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
        raise ValueError(f"{path} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{path} must be positive")
    return float(value)


def _int(value, path, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{path} must be an integer >= {minimum}")
    return value


def _timestamp(value, path):
    value = _int(value, path, 1)
    if value > MAX_TIMESTAMP:
        raise ValueError(f"{path} must be <= {MAX_TIMESTAMP} (UTC year 9999)")
    return value


def _finite(value, path):
    try:
        if not math.isfinite(value):
            raise ValueError(f"{path} must remain finite")
    except (OverflowError, TypeError) as exc:
        raise ValueError(f"{path} must remain finite") from exc
    return value


def _canonical_hash(value):
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


class ReplayBroker:
    def __init__(self, instrument):
        self.instrument = instrument
        self.symbol = instrument["symbol"]

    def profit(self, side, symbol, lot, entry, exit_price):
        direction = 1 if side == "BUY" else -1
        return _finite(direction * (exit_price-entry) * lot * self.instrument["contract_size"], "profit")

    def margin(self, side, symbol, lot, entry):
        return _finite(self.instrument["margin_per_lot"] * lot, "margin")


def validate_proposals(proposals):
    """Validate and normalize replay proposals without executing a replay."""
    if not isinstance(proposals, (list, tuple)):
        raise ValueError("proposals must be an array")
    if len(proposals) > 100_000:
        raise ValueError("proposals exceeds 100000 records")
    clean = []
    seen = set()
    for index, raw in enumerate(proposals):
        path = f"proposals[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be an object")
        try:
            raw_encoded = json.dumps(raw, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path} must contain JSON-safe finite values: {exc}") from exc
        if len(raw_encoded.encode()) > 65536:
            raise ValueError(f"{path} exceeds 65536 bytes")
        decision = _timestamp(raw.get("decision_at"), path + ".decision_at")
        side, kind = raw.get("side"), raw.get("kind")
        if side not in ("BUY", "SELL") or kind not in ("MARKET", "LIMIT", "STOP"):
            raise ValueError(f"{path} has unsupported side or kind")
        strategy = raw.get("strategy", "manual")
        if not isinstance(strategy, str) or not strategy.strip():
            raise ValueError(f"{path}.strategy must be non-empty text")
        duplicate_key = (strategy, decision)
        if duplicate_key in seen:
            clean.append({"index": index, "proposal": raw, "duplicate": True})
            continue
        seen.add(duplicate_key)
        item = dict(raw)
        item["decision_at"] = decision
        item["sl"] = _num(raw.get("sl"), path + ".sl", positive=True)
        item["tp"] = _num(raw.get("tp"), path + ".tp", positive=True)
        item["entry"] = _num(raw.get("entry"), path + ".entry", positive=True, allow_none=True)
        item["lot"] = _num(raw.get("lot"), path + ".lot", positive=True, allow_none=True)
        item["strategy"] = strategy
        try:
            encoded = json.dumps(item, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path} must contain JSON-safe finite values: {exc}") from exc
        if len(encoded.encode()) > 65536:
            raise ValueError(f"{path} exceeds 65536 bytes")
        clean.append({"index": index, "proposal": item, "duplicate": False})
    clean.sort(key=lambda x: (x["proposal"]["decision_at"], x["index"]))
    return clean


def run_replay(dataset, proposals, settings=None, initial_balance=10000.0,
               latency_seconds=0, close_at_end=True):
    if not hasattr(dataset, "to_dict") or not hasattr(dataset, "dataset_hash"):
        raise TypeError("dataset must be a loaded Dataset")
    data = dataset.to_dict()
    initial_balance = _num(initial_balance, "initial_balance", positive=True)
    latency_seconds = _int(latency_seconds, "latency_seconds")
    if not isinstance(close_at_end, bool):
        raise ValueError("close_at_end must be boolean")
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    cfg = dict(DEFAULTS) | (settings or {})
    for key in ("max_lot", "risk_percent", "daily_loss", "max_orders", "max_spread"):
        _num(cfg.get(key), f"settings.{key}", positive=True)
    _int(cfg["max_orders"], "settings.max_orders", 1)
    if not isinstance(cfg.get("kill_switch"), bool):
        raise ValueError("settings.kill_switch must be boolean")
    clean = validate_proposals(proposals)
    proposal_queue = [entry for entry in clean if not entry["duplicate"]]
    proposal_cursor = 0
    quotes = data["quotes"]
    instrument, costs = data["instrument"], data["costs"]
    broker = ReplayBroker(instrument)
    balance = initial_balance
    positions, pending = [], []
    orders, trades, deals = [], [], []
    accepted, rejected, risk_breaches, duplicate_count = [], [], [], 0
    ticket = 1
    equity_curve = []
    prev_quote_time = None
    slippage = _finite(costs["slippage_points"] * instrument["point"], "slippage")
    if slippage < 0:
        raise ValueError("slippage must be non-negative")
    rollover_entries = 0

    def mark_value(q):
        return _finite(sum(broker.profit(p["side"], instrument["symbol"], p["volume"], p["entry"],
                                         q["bid"] if p["side"] == "BUY" else q["ask"]) for p in positions), "floating PnL")

    def risk_snapshot(q, now, execution_side=None, execution_price=None):
        floating = mark_value(q)
        margin_used = _finite(sum(broker.margin(p["side"], instrument["symbol"], p["volume"], p["entry"]) for p in positions), "used margin")
        margin_used = _finite(margin_used + sum(broker.margin(p["proposal"]["side"], instrument["symbol"], p["proposal"]["lot"],
                                                               p["proposal"]["entry"]) for p in pending), "used margin")
        tick = {"time": q["time"], "bid": q["bid"], "ask": q["ask"]}
        if execution_side == "BUY": tick["ask"] = execution_price
        if execution_side == "SELL": tick["bid"] = execution_price
        return {"account": {"is_demo": True, "trade_allowed": True, "margin_mode": 2,
                             "equity": balance + floating, "margin_free": balance + floating - margin_used,
                             "currency": instrument["currency"]},
                "info": {"point": instrument["point"], "trade_tick_size": instrument["tick_size"],
                         "trade_stops_level": instrument["stops_level"], "volume_min": instrument["volume_min"],
                         "volume_max": instrument["volume_max"], "volume_step": instrument["volume_step"]},
                "tick": tick, "quoted_tick": {"time": q["time"], "bid": q["bid"], "ask": q["ask"]},
                "positions": [p["risk"] for p in positions], "orders": [p["risk"] for p in pending],
                "deals": deals, "scope": f"replay:{dataset.dataset_hash}"}

    def deal(side, entry_flag, when, pos, price, profit=0.0, commission=0.0, swap=0.0, fee=0.0,
             volume=None, extra=None):
        nonlocal ticket, balance
        values = [profit, commission, swap, fee, price, pos["volume"] if volume is None else volume]
        for name, value in zip(("profit", "commission", "swap", "fee", "price", "volume"), values):
            _finite(value, f"deal.{name}")
        new_balance = _finite(balance + profit + commission + swap + fee, "balance")
        row = {"ticket": ticket, "order": pos["order_ticket"], "position_id": pos["ticket"], "time": when,
               "type": 0 if side == "BUY" else 1, "entry": entry_flag,
               "volume": pos["volume"] if volume is None else volume, "price": price,
               "profit": profit, "commission": commission, "swap": swap, "fee": fee, "symbol": instrument["symbol"],
               "magic": 0, "strategy": pos["strategy"]}
        if extra: row.update(extra)
        ticket += 1
        balance = new_balance
        deals.append(row)
        return row

    def executable_price(side, kind, proposal, q):
        quoted = q["ask"] if side == "BUY" else q["bid"]
        if kind == "LIMIT":
            price = min(quoted, proposal["entry"]) if side == "BUY" else max(quoted, proposal["entry"])
        else:
            price = quoted + slippage if side == "BUY" else quoted - slippage
        price = _finite(price, "executable entry")
        if price <= 0:
            raise ValueError("executable entry must be positive")
        return quoted, price

    def fill(proposal, q, execution_kind, order_ticket, risk_cash=0.0):
        nonlocal ticket
        side = proposal["side"]
        quoted_side_price, price = executable_price(side, execution_kind, proposal, q)
        raw_spread = _finite((q["ask"] - q["bid"]) / instrument["point"], "quoted spread")
        executed_slippage = price - quoted_side_price if side == "BUY" else quoted_side_price - price
        pos = {"ticket": ticket, "side": side, "volume": proposal["lot"], "entry": price,
               "sl": proposal["sl"], "tp": proposal["tp"], "strategy": proposal["strategy"],
               "order_ticket": order_ticket, "opened_at": q["time"], "swap": 0.0, "risk_cash": risk_cash,
               "entry_evidence": {"quoted_bid": q["bid"], "quoted_ask": q["ask"],
                                  "quoted_side_price": quoted_side_price, "quoted_spread": raw_spread,
                                  "executed_price": price, "slippage": executed_slippage}}
        ticket += 1
        risk_pos = {"ticket": pos["ticket"], "type": 0 if side == "BUY" else 1, "symbol": instrument["symbol"],
                    "price_open": price, "price_current": q["bid"] if side == "BUY" else q["ask"],
                    "volume": pos["volume"], "sl": pos["sl"], "profit": 0}
        pos["risk"] = risk_pos
        positions.append(pos)
        commission = -costs["commission_per_lot_side"] * pos["volume"]
        fee = -costs["fee_per_lot_side"] * pos["volume"]
        deal(side, 0, q["time"], pos, price, commission=commission, fee=fee,
             extra=pos["entry_evidence"])
        return pos

    def close_position(pos, q, exit_reason, forced=False):
        side = pos["side"]
        mark = q["bid"] if side == "BUY" else q["ask"]
        if exit_reason == "TP":
            price = max(mark, pos["tp"]) if side == "BUY" else min(mark, pos["tp"])
        elif exit_reason == "SL":
            price = mark - slippage if side == "BUY" else mark + slippage
        else:
            price = mark - slippage if side == "BUY" else mark + slippage
        profit = broker.profit(side, instrument["symbol"], pos["volume"], pos["entry"], price)
        commission = -costs["commission_per_lot_side"] * pos["volume"]
        fee = -costs["fee_per_lot_side"] * pos["volume"]
        deal(side, 1, q["time"], pos, price, profit=profit, commission=commission, fee=fee)
        positions.remove(pos)
        trades.append({"position_id": pos["ticket"], "strategy": pos["strategy"], "side": side,
                       "order_ticket": pos["order_ticket"], "entry_evidence": pos["entry_evidence"],
                       "entry_time": pos["opened_at"], "exit_time": q["time"], "entry": pos["entry"],
                       "exit": price, "volume": pos["volume"], "gross_pnl": profit,
                       "net_pnl": profit + pos["swap"] + 2 * (commission + fee),
                       "risk_cash": pos["risk_cash"], "reason": exit_reason, "forced_exit": forced, "swap": pos["swap"]})

    def exit_reason_at(pos, q):
        mark = q["bid"] if pos["side"] == "BUY" else q["ask"]
        if (pos["side"] == "BUY" and mark <= pos["sl"]) or (pos["side"] == "SELL" and mark >= pos["sl"]):
            return "SL"
        if (pos["side"] == "BUY" and mark >= pos["tp"]) or (pos["side"] == "SELL" and mark <= pos["tp"]):
            return "TP"
        return None

    for qi, q in enumerate(quotes):
        now = q["time"]
        for p in positions:
            mark = q["bid"] if p["side"] == "BUY" else q["ask"]
            p["risk"]["price_current"] = mark
            p["risk"]["profit"] = broker.profit(p["side"], instrument["symbol"], p["volume"], p["entry"], mark)
        if prev_quote_time is not None and positions:
            start = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=prev_quote_time)
            end = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=now)
            rollover_day = start.date()
            while rollover_day <= end.date():
                if rollover_day.weekday() in costs["rollover_weekdays"]:
                    rollover = datetime.combine(rollover_day, time(costs["rollover_hour_utc"]), UTC)
                    if start < rollover <= end:
                        multiplier = 3 if rollover.weekday() == costs["triple_swap_weekday"] else 1
                        stamp = int(rollover.timestamp())
                        for p in positions:
                            rollover_entries += 1
                            if rollover_entries > 100_000:
                                raise ValueError("replay exceeds 100000 generated rollover entries")
                            amount = _finite((costs["swap_long_per_lot_day"] if p["side"] == "BUY" else costs["swap_short_per_lot_day"]) * p["volume"] * multiplier, "swap")
                            p["swap"] = _finite(p["swap"] + amount, "position swap")
                            deal(p["side"], 0, stamp, p, p["entry"], swap=amount, volume=0,
                                 extra={"cost_only": True, "accrual_volume": p["volume"]})
                if rollover_day == end.date():
                    break
                rollover_day += timedelta(days=1)
        prev_quote_time = now
        for p in list(positions):
            reason = exit_reason_at(p, q)
            if reason: close_position(p, q, reason)
        for p in list(pending):
            order = p["proposal"]
            mark = q["ask"] if order["side"] == "BUY" else q["bid"]
            triggered = (order["kind"] == "LIMIT" and (mark <= order["entry"] if order["side"] == "BUY" else mark >= order["entry"])) or \
                        (order["kind"] == "STOP" and (mark >= order["entry"] if order["side"] == "BUY" else mark <= order["entry"]))
            if not triggered: continue
            pending.remove(p)
            check = {"side": order["side"], "kind": "MARKET", "entry": None, "sl": order["sl"], "tp": order["tp"], "lot": order["lot"]}
            quoted_price, actual_price = executable_price(order["side"], order["kind"], order, q)
            snap = risk_snapshot(q, now, order["side"], actual_price)
            try:
                verdict = preview(check, snap, cfg, broker, now=now)
                json.dumps(verdict, allow_nan=False)
                diagnostic = copy.deepcopy(verdict)
                errors = verdict["errors"]
                breached = not verdict["allowed"]
            except (BrokerError, KeyError, OverflowError, TypeError, ValueError) as exc:
                diagnostic = {"allowed": False, "errors": [str(exc)]}
                errors, breached = diagnostic["errors"], True
            if breached:
                risk_breaches.append({"index": p["index"], "order_ticket": p["risk"]["ticket"],
                                      "stage": "fill", "evaluated_at": now,
                                      "risk_snapshot": copy.deepcopy(snap),
                                      "errors": list(errors), "risk": diagnostic})
            order_row = orders[p["order_index"]]
            order_row["fill_revalidation"] = diagnostic
            order_row["fill_risk_snapshot"] = copy.deepcopy(snap)
            pos = fill(order, q, order["kind"], p["risk"]["ticket"], p["risk_cash"])
            order_row["actual_fill"] = copy.deepcopy(pos["entry_evidence"])
            orders[p["order_index"]]["status"] = "filled"
            orders[p["order_index"]]["position_id"] = pos["ticket"]
            reason = exit_reason_at(pos, q)
            if reason:
                close_position(pos, q, reason)
        while proposal_cursor < len(proposal_queue):
            entry = proposal_queue[proposal_cursor]
            order = entry["proposal"]
            if now <= order["decision_at"] or now < order["decision_at"] + latency_seconds:
                break
            proposal_cursor += 1
            entry["processed"] = True
            executable = None
            if order["kind"] == "MARKET":
                quoted_price, executable = executable_price(order["side"], "MARKET", order, q)
                snap = risk_snapshot(q, now, order["side"], executable)
                risk_order = {k: order.get(k) for k in ("side", "sl", "tp", "lot", "strategy")}
                risk_order.update({"kind": "MARKET", "entry": None})
            else:
                snap = risk_snapshot(q, now)
                risk_order = {k: order.get(k) for k in ("side", "kind", "entry", "sl", "tp", "lot", "strategy")}
            try:
                verdict = preview(risk_order, snap, cfg, broker, now=now)
                json.dumps(verdict, allow_nan=False)
            except (BrokerError, KeyError, OverflowError, TypeError, ValueError) as exc:
                rejected.append({"index": entry["index"], "proposal": order, "stage": "acceptance",
                                 "evaluated_at": now, "risk_snapshot": copy.deepcopy(snap),
                                 "errors": [str(exc)]})
                continue
            verdict["quoted_spread"] = _finite((q["ask"]-q["bid"])/instrument["point"], "quoted spread")
            if not verdict["allowed"]:
                rejected.append({"index": entry["index"], "proposal": order, "stage": "acceptance",
                                 "evaluated_at": now, "risk_snapshot": copy.deepcopy(snap),
                                 "risk": verdict, "errors": verdict["errors"]})
                continue
            accepted_row = {"index": entry["index"], "proposal": copy.deepcopy(order), "risk": copy.deepcopy(verdict),
                            "risk_snapshot": copy.deepcopy(snap), "accepted_at": now}
            accepted.append(accepted_row)
            order_row = {"ticket": ticket, "strategy": order["strategy"], "side": order["side"], "kind": order["kind"],
                         "decision_at": order["decision_at"], "accepted_at": now, "entry": verdict["entry"],
                         "sl": order["sl"], "tp": order["tp"], "volume": verdict["lot"], "status": "accepted",
                         "quoted_bid": q["bid"], "quoted_ask": q["ask"],
                         "quoted_spread": verdict["quoted_spread"],
                         "executable_price": executable if executable is not None else verdict["entry"]}
            ticket += 1; order_idx = len(orders); orders.append(order_row)
            if order["kind"] == "MARKET":
                pos = fill(order | {"lot": verdict["lot"]}, q, "MARKET", order_row["ticket"], verdict["loss"])
                accepted_row["position_id"] = pos["ticket"]
                order_row["status"] = "filled"; order_row["position_id"] = pos["ticket"]
                order_row["actual_fill"] = copy.deepcopy(pos["entry_evidence"])
            else:
                pending_row = {"ticket": order_row["ticket"], "type": 2 if order["side"] == "BUY" else 3,
                               "symbol": instrument["symbol"], "price_open": order["entry"], "price_current": order["entry"],
                               "volume": verdict["lot"], "sl": order["sl"], "profit": 0}
                pending.append({"proposal": order | {"lot": verdict["lot"]}, "risk": pending_row,
                                "risk_cash": verdict["loss"], "index": entry["index"], "order_index": order_idx})
        equity_curve.append({"time": now, "balance": balance, "equity": balance + mark_value(q)})
    # All proposals without an eligible later quote are rejected explicitly.
    for entry in clean:
        if entry["duplicate"]:
            duplicate_count += 1
            rejected.append({"index": entry["index"], "proposal": entry["proposal"], "stage": "duplicate", "errors": ["duplicate strategy and decision_at"]})
        elif not entry.get("processed"):
            rejected.append({"index": entry["index"], "proposal": entry["proposal"], "stage": "data",
                             "decision_at": entry["proposal"]["decision_at"],
                             "evaluated_at": quotes[-1]["time"] if quotes else None,
                             "errors": ["no eligible quote strictly after decision_at and latency"]})
    if close_at_end and quotes:
        q = quotes[-1]
        for p in list(positions): close_position(p, q, "forced_end", forced=True)
        for p in pending:
            orders[p["order_index"]]["status"] = "cancelled_end"
        pending.clear()
    if close_at_end and quotes and equity_curve:
        equity_curve[-1] = {"time": quotes[-1]["time"], "balance": balance, "equity": balance}
    nets = [t["net_pnl"] for t in trades]
    wins = [n for n in nets if n > 0]; losses = [-n for n in nets if n < 0]
    peak = initial_balance; max_dd = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        if peak > 0: max_dd = max(max_dd, (peak-point["equity"])/peak)
    total_volume = sum(d["volume"] for d in deals if d["entry"] == 0 and not d.get("cost_only"))
    gross_wins = sum(wins); gross_losses = sum(losses)
    replay_config = {"settings": cfg, "initial_balance": initial_balance,
                     "latency_seconds": latency_seconds, "close_at_end": close_at_end}
    result = {"dataset_hash": dataset.dataset_hash, "config_hash": _canonical_hash(replay_config),
            "assumptions": {"pnl": "linear contract_size in account currency", "margin": "fixed margin_per_lot",
                            "execution": "next eligible quote; market/stop adverse slippage; quote-resolution stops use observed gap price",
                            "limits": "limit entries and TP exits use no-worse prices", "unknown_costs": "not inferred",
                            "quotes": "epoch-second timestamps; no full tick or sub-second fidelity",
                            "instrument_cost_model": "instrument, costs, slippage, and margin are fixed from the dataset",
                            "pending_orders": "broker-owned after acceptance; trigger diagnostics do not veto fills; absent server rejection or expiry events are not inferred",
                            "evidence": "published_at and source-verified available_at gate point-in-time use; fetched_at is archive provenance and importer cannot prove source timing"},
            "accepted": accepted, "rejected": rejected, "risk_breaches": risk_breaches,
            "risk_breach_count": len(risk_breaches), "duplicate_count": duplicate_count,
            "orders": orders, "trades": trades, "deals": deals,
            "positions_open": [{k: v for k, v in p.items() if k != "risk"} for p in positions],
            "pending_open": [{k: v for k, v in p["proposal"].items() if k != "_risk_cash"} |
                             {"ticket": p["risk"]["ticket"]} for p in pending],
            "equity_curve": equity_curve,
            "metrics": {"closed_trades": len(trades), "win_rate": len(wins)/len(trades) if trades else 0.0,
                        "profit_factor": gross_wins/gross_losses if gross_losses else (None if not gross_wins else None),
                        "net_expectancy_r": (sum(t["net_pnl"] / t["risk_cash"] for t in trades if t["risk_cash"] > 0) /
                                             sum(1 for t in trades if t["risk_cash"] > 0)) if any(t["risk_cash"] > 0 for t in trades) else 0.0,
                        "net_pnl": balance-initial_balance, "max_equity_drawdown": max_dd,
                        "turnover": total_volume, "turnover_unit": "lots",
                        "open_exposure": _finite(sum(p["volume"]*p["entry"]*instrument["contract_size"] for p in positions), "open exposure")}}
    try:
        json.dumps(result, allow_nan=False)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"replay result is not finite JSON: {exc}") from exc
    return result
