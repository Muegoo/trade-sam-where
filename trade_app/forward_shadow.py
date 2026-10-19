"""Session-only, non-executing live forward observation and shadow replay."""

import copy
import hashlib
import json
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .datasets import DatasetError, load_dataset
from .event_filter import event_blackout
from .broker import BrokerError
from .replay import ReplayBroker, run_replay
from .risk import preview

MAX_DECISIONS = 200
MAX_QUOTES = 50_000
MAX_SESSION_BYTES = 16 * 1024 * 1024


def _ceil_second(value):
    return int(math.ceil(value))


def _source_time(tick):
    value = tick.get("time_msc")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return int(value // 1000)
    value = tick.get("time")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return int(value)
    return None


def _instrument_fingerprint(info, currency):
    names = ("currency_profit", "point", "trade_tick_size", "trade_contract_size",
             "volume_min", "volume_max", "volume_step", "trade_stops_level",
             "trade_calc_mode", "trade_exemode")
    return hashlib.sha256(json.dumps({"currency": currency,
        **{name: info.get(name) for name in names}}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ForwardShadow:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="forward-shadow")
        self._generation = 0
        self._config = None
        self._context = None
        self._status = "idle"
        self._capture = False
        self._busy = False
        self._resolving = False
        self._caps_pending = False
        self._bars = {"M15": [], "H1": []}
        self._bar_times = {"M15": set(), "H1": set()}
        self._quotes = []
        self._quote_source_times = []
        self._decisions = []
        self._evidence = []
        self._last_quote_time = None
        self._last_decision_bar = None
        self._watermark = None
        self._watermark_frame = None
        self._skipped_bars = 0
        self._dropped_observations = 0
        self._error = None

    @property
    def active(self):
        with self._lock:
            return self._status == "running" or self._capture or self._busy

    def status(self):
        with self._lock:
            config = self._config or {}
            return {"status": self._status, "active": self._status == "running",
                    "capturing_quotes": self._capture, "scope_hash": config.get("scope_hash"),
                    "mode": config.get("mode"), "symbol": config.get("symbol"),
                    "strategy": config.get("strategy"), "decisions": len(self._decisions),
                    "quotes": len(self._quotes), "unresolved": sum(row.get("status") == "unresolved" for row in self._decisions),
                    "skipped_bars": self._skipped_bars, "dropped_observations": self._dropped_observations,
                    "error": self._error, "dataset_hash": None}

    def start(self, config, context, evidence_dataset=None):
        with self._lock:
            if self._status == "running" or self._busy or any(row.get("status") == "unresolved" for row in self._decisions):
                raise ValueError("A forward run is already active or has unresolved virtual decisions")
            if not isinstance(context, dict) or not context.get("connected"):
                raise ValueError("A connected account snapshot is required")
            account, info = context.get("account", {}), context.get("info", {})
            mode, symbol = context.get("mode"), context.get("symbol")
            if mode not in ("mt5", "simulation") or not account.get("is_demo", mode == "simulation"):
                raise ValueError("Forward shadow requires a connected Demo or simulation account")
            currency = account.get("currency")
            if not isinstance(currency, str) or info.get("currency_profit", currency) != currency:
                raise ValueError("Unsupported profit currency conversion")
            frame = "H1" if config["strategy"] == "ai" else "M15"
            bars = context.get("bars", {}).get("bars", []) if isinstance(context.get("bars"), dict) else context.get("bars", [])
            h1 = context.get("h1", [])
            selected = h1 if frame == "H1" else bars
            if len(selected) == 0:
                raise ValueError("Closed-bar warmup data is unavailable")
            observed_at = int(context.get("observed_at", self._clock()))
            normalized = {"point": info.get("point"), "tick_size": info.get("trade_tick_size", info.get("point")),
                "contract_size": info.get("trade_contract_size", info.get("contract_size", 100 if mode == "simulation" else None)),
                "volume_min": info.get("volume_min"), "volume_max": info.get("volume_max"),
                "volume_step": info.get("volume_step"), "stops_level": info.get("trade_stops_level", info.get("stops_level", 0)),
                "currency": currency, "symbol": symbol, "margin_per_lot": config["margin_per_lot"]}
            base_evidence = []
            if evidence_dataset is not None:
                data = evidence_dataset.to_dict()
                if data["instrument"]["symbol"] != symbol:
                    raise ValueError("Evidence dataset symbol does not match the live instrument")
                base_evidence = copy.deepcopy(data["evidence"])
            seed = {"version": 1, "source": "forward_shadow", "timezone": "UTC",
                "instrument": normalized, "costs": config["costs"],
                "quotes": [{"time": observed_at, "bid": 1, "ask": 1.00001}],
                "bars": {"M15": [], "H1": []}, "evidence": base_evidence}
            try:
                validated = load_dataset(seed)
            except (DatasetError, TypeError, ValueError):
                raise ValueError("Forward instrument, margin, or explicit cost settings are invalid") from None
            self._generation += 1
            self._config = dict(config) | {"scope_hash": hashlib.sha256(str(context.get("scope", "")).encode()).hexdigest(),
                                           "mode": mode, "symbol": symbol, "currency": currency,
                                           "instrument": normalized, "frame": frame,
                                           "settings": copy.deepcopy(config["settings"]),
                                           "settings_fingerprint": hashlib.sha256(json.dumps(config["settings"], sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
                                           "instrument_fingerprint": _instrument_fingerprint(info, currency),
                                           "source": f"{mode}_forward_shadow"}
            self._context = self._light_context(context)
            self._status, self._capture, self._busy, self._resolving, self._caps_pending = "running", True, False, False, False
            self._bars, self._bar_times = {"M15": [], "H1": []}, {"M15": set(), "H1": set()}
            for name, seconds, rows in (("M15", 900, bars), ("H1", 3600, h1)):
                for row in rows[-(600 if name == "M15" else 1600):]:
                    opened = int(row["time"])
                    if opened + seconds > observed_at:
                        continue
                    self._bars[name].append({"time": opened, "available_at": observed_at,
                        "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"]})
                    self._bar_times[name].add(opened)
            self._quotes, self._quote_source_times, self._decisions = [], [], []
            self._evidence = base_evidence
            self._last_quote_time = None
            self._watermark_frame = frame
            self._watermark = int(selected[-1]["time"])
            self._last_decision_bar = self._watermark
            self._skipped_bars = self._dropped_observations = 0
            self._error = None
            return {"status": self._status, "watermark": {"frame": frame, "bar_time": self._watermark},
                    "scope_hash": self._config["scope_hash"], "instrument_hash": validated.dataset_hash}

    def stop(self):
        with self._lock:
            if self._status != "running":
                raise ValueError("No active forward run")
            self._generation += 1
            self._status = "stopped"
            self._config["api_key"] = None
            self._capture = any(row.get("status") == "unresolved" for row in self._decisions)
            for row in self._decisions:
                if row.get("status") == "pending_advice":
                    row.update(status="unknown", reason="Stopped while advice was in flight")
            if not self._capture:
                self._capture = False
            return self.status()

    def _stop_locked(self, reason):
        self._generation += 1
        self._status, self._capture, self._error = "stopped", False, reason
        if self._config is not None:
            self._config["api_key"] = None
        for row in self._decisions:
            if row.get("status") in ("unresolved", "pending_advice"):
                row.update(status="unknown", reason=reason)

    def _cap_stop_locked(self):
        self._generation += 1
        self._status, self._capture = "limit_reached", False
        self._error = "Forward session record cap reached; new decisions stopped"
        if self._config is not None:
            self._config["api_key"] = None
        for row in self._decisions:
            if row.get("status") in ("unresolved", "pending_advice"):
                row.update(status="unknown", reason=self._error)

    def observe(self, context):
        if not isinstance(context, dict):
            return
        candidate = None
        resolve_outcomes = False
        check_caps = False
        with self._lock:
            if not self._capture or self._config is None:
                return
            if (not context.get("connected") or context.get("scope") is None or
                    hashlib.sha256(str(context.get("scope", "")).encode()).hexdigest() != self._config["scope_hash"] or
                    context.get("symbol") != self._config["symbol"] or context.get("mode") != self._config["mode"]):
                self._stop_locked("Live scope changed or connection was lost; restart manually")
                return
            if _instrument_fingerprint(context.get("info", {}), context.get("account", {}).get("currency")) != self._config["instrument_fingerprint"]:
                self._stop_locked("Live instrument specification changed; restart forward shadow manually")
                return
            settings = context.get("settings")
            if not isinstance(settings, dict) or hashlib.sha256(json.dumps(settings, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode()).hexdigest() != self._config["settings_fingerprint"]:
                self._stop_locked("Live strategy settings changed; restart forward shadow manually")
                return
            self._context = self._light_context(context)
            observed_at = _ceil_second(context.get("observed_at", self._clock()))
            tick = context.get("tick", {})
            source_time = _source_time(tick)
            bid, ask = tick.get("bid"), tick.get("ask")
            if (source_time is not None and 0 <= observed_at - source_time <= 180 and
                    isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and 0 < bid < ask):
                if self._last_quote_time is None or observed_at > self._last_quote_time:
                    if len(self._quotes) >= MAX_QUOTES:
                        self._cap_stop_locked()
                        return
                    self._quotes.append({"time": observed_at, "bid": bid, "ask": ask, "symbol": self._config["symbol"]})
                    self._quote_source_times.append(source_time)
                    self._last_quote_time = observed_at
                else:
                    self._dropped_observations += 1
            self._capture_bars(context, observed_at)
            if not self._resolving:
                self._resolving = True
                resolve_outcomes = True
            if not self._caps_pending:
                self._caps_pending = True
                check_caps = True
            if self._status == "running" and len(self._decisions) >= self._config["max_decisions"]:
                self._status = "decision_limit"
                self._config["api_key"] = None
                self._capture = any(row.get("status") in ("pending_advice", "unresolved")
                                    for row in self._decisions)
            if self._status == "running":
                selected = context.get("bars", []) if self._watermark_frame == "M15" else context.get("h1", [])
                if selected:
                    newest = int(selected[-1]["time"])
                    if newest > self._last_decision_bar:
                        if newest - self._last_decision_bar > (3600 if self._watermark_frame == "H1" else 900):
                            self._skipped_bars += 1
                        self._last_decision_bar = newest
                        signal = next((row for row in context.get("signals", [])
                                       if row.get("key") == self._config["strategy"] and row.get("time") == newest), None)
                        if self._busy:
                            self._skipped_bars += 1
                        elif signal is not None:
                            candidate = (dict(signal), self._light_context(context), self._generation)
                            self._busy = True
                            self._decisions.append({"id": uuid.uuid4().hex, "bar_time": newest,
                                "context_as_of": observed_at, "status": "pending_advice", "side": signal.get("signal")})
        if candidate is not None:
            self._executor.submit(self._process_candidate, *candidate)
        if resolve_outcomes:
            self._executor.submit(self._resolve_outcomes)
        if check_caps:
            self._executor.submit(self._check_caps)

    @staticmethod
    def _light_context(context):
        return {key: context.get(key) for key in
                ("connected", "scope", "symbol", "mode", "observed_at", "account", "info", "tick", "bars", "h1", "signals")}

    def _capture_bars(self, context, observed_at):
        for frame, seconds, source in (("M15", 900, context.get("bars", [])),
                                       ("H1", 3600, context.get("h1", []))):
            if len(source) == 0:
                continue
            bar = source[-1]
            opened = int(bar["time"])
            if opened + seconds > observed_at or opened in self._bar_times[frame]:
                continue
            if self._bars[frame] and opened <= self._bars[frame][-1]["time"]:
                continue
            self._bars[frame].append({"time": opened, "available_at": observed_at,
                "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"]})
            self._bar_times[frame].add(opened)

    def _dataset_payload_locked(self):
        if not self._quotes:
            return None
        payload = {"version": 1, "source": self._config["source"], "timezone": "UTC",
            "instrument": self._config["instrument"], "costs": self._config["costs"],
            "quotes": list(self._quotes),
            "bars": {key: list(value) for key, value in self._bars.items()}, "evidence": list(self._evidence)}
        return payload

    def _dataset_locked(self):
        payload = self._dataset_payload_locked()
        return load_dataset(payload) if payload else None

    def _dataset_hash_locked(self):
        try:
            dataset = self._dataset_locked()
            return dataset.dataset_hash if dataset else None
        except (DatasetError, ValueError, TypeError):
            return None

    def export_dataset(self):
        with self._lock:
            payload = self._dataset_payload_locked()
        if payload is None:
            raise ValueError("No forward quotes have been captured yet")
        return load_dataset(payload).to_dict()

    def export_result(self):
        with self._lock:
            payload = self._dataset_payload_locked()
            config = dict(self._config or {})
            report = {"scope_hash": config.get("scope_hash"), "symbol": config.get("symbol"),
                "mode": config.get("mode"), "skipped_bars": self._skipped_bars,
                "dropped_observations": self._dropped_observations,
                "quote_provenance": [{"available_at": quote["time"], "source_tick_time": self._quote_source_times[index]}
                    for index, quote in enumerate(self._quotes)],
                "decisions": copy.deepcopy(self._decisions),
                "availability": "quotes and bars are timestamped at first observation; quote source tick time is retained separately",
                "historical_specification": "current instrument specification only; historical settings are not verified",
                "actual_fills": False}
            status = self._status
        if payload is None:
            raise ValueError("No forward quotes have been captured yet")
        dataset = load_dataset(payload)
        return {"status": status, "dataset_hash": dataset.dataset_hash,
                "source": config["source"], "report": report}

    def _process_candidate(self, signal, context, generation):
        from .advisor import AdvisorError, generate_advice

        with self._lock:
            record = next((row for row in reversed(self._decisions) if row["bar_time"] == signal.get("time")
                           and row["status"] == "pending_advice"), None)
            if record is None:
                self._busy = False
                return
            decision_id = record["id"]
            config = copy.deepcopy(self._config)
            dataset = None
        context_as_of = _ceil_second(context["observed_at"])
        side = signal.get("signal")
        result = {"id": decision_id, "bar_time": signal.get("time"), "context_as_of": context_as_of,
                  "side": side, "signal": signal, "status": "rejected", "executed_orders": 0,
                  "actual_fills": False}
        try:
            with self._lock:
                dataset_payload = self._dataset_payload_locked()
            dataset = load_dataset(dataset_payload) if dataset_payload else None
            if dataset is None:
                result.update(status="no_data", reason="No valid observed quote is available")
                return
            tick = context["tick"]
            source_time = _source_time(tick)
            if side not in ("BUY", "SELL"):
                result.update(status="wait", reason=signal.get("reason", "No rule signal"))
            elif source_time is None or not 0 <= context_as_of - source_time <= 180:
                result["reason"] = "Source tick is stale or unavailable"
            else:
                gate = event_blackout(dataset.to_dict()["evidence"], config["symbol"], context_as_of,
                    config["calendar_before_minutes"], config["calendar_after_minutes"])
                if gate["blocked"]:
                    result.update(reason="; ".join(gate["reasons"]), calendar=gate)
                else:
                    advice = None
                    if config["provider"] != "disabled":
                        advice = generate_advice(dataset, context_as_of, provider=config["provider"],
                            model=config.get("model"), api_key=config.get("api_key"), challenger=True,
                            calendar_before_minutes=config["calendar_before_minutes"],
                            calendar_after_minutes=config["calendar_after_minutes"],
                            client=config.get("client"), settings=config["settings"])
                    ready_at = _ceil_second(self._clock())
                    decision_at = max(context_as_of, ready_at)
                    result.update(advice_ready_at=ready_at, decision_at=decision_at,
                                  ready_delay_seconds=max(0, ready_at-context_as_of))
                    analyst = advice.get("analyst_proposal") if advice else None
                    challenger = advice.get("challenger") if advice else None
                    if advice and (not isinstance(analyst, dict) or analyst.get("action") != side or
                                   not isinstance(challenger, dict) or challenger.get("approve") is not True):
                        result["reason"] = "Advisor side agreement or challenger approval failed"
                    elif decision_at >= context_as_of + 180:
                        result["reason"] = "Signal expired before advice was ready"
                        result["status"] = "expired"
                    else:
                        with self._lock:
                            latest = copy.deepcopy(self._context)
                        latest_time = _source_time(latest.get("tick", {})) if latest else None
                        now = decision_at
                        if (not latest or latest.get("scope") != context.get("scope") or
                                latest_time is None or not 0 <= now-latest_time <= 180):
                            result["reason"] = "Scope changed or latest source tick is stale"
                        else:
                            proposal = {"side": side, "kind": "MARKET", "entry": None,
                                        "sl": signal.get("sl"), "tp": signal.get("tp"), "lot": None,
                                        "strategy": config["strategy"]}
                            risk_info = latest["info"]
                            risk_snapshot = {"scope": latest["scope"], "account": {"is_demo": True, "trade_allowed": True,
                                "margin_mode": 2, "equity": config["initial_balance"],
                                "margin_free": config["initial_balance"], "currency": config["currency"]},
                                "info": {"point": risk_info["point"],
                                    "trade_tick_size": risk_info.get("trade_tick_size", risk_info["point"]),
                                    "trade_stops_level": risk_info.get("trade_stops_level", risk_info.get("stops_level", 0)),
                                    "volume_min": risk_info["volume_min"], "volume_max": risk_info["volume_max"],
                                    "volume_step": risk_info["volume_step"]}, "tick": latest["tick"],
                                "positions": [], "orders": [], "deals": []}
                            checked = preview(proposal, risk_snapshot, config["settings"],
                                              ReplayBroker(config["instrument"]), now=now)
                            if not checked["allowed"]:
                                result["reason"] = "; ".join(checked["errors"])
                            else:
                                result.update(status="unresolved", reason="Accepted for virtual forward replay",
                                    lot=checked["lot"], initial_risk_cash=checked["loss"],
                                    sl=proposal["sl"], tp=proposal["tp"], latency_seconds=config["latency_seconds"],
                                    horizon_seconds=config["horizon_seconds"],
                                    decision_at=decision_at, replay_proposal=proposal)
                    if advice:
                        result["provider_status"] = advice.get("provider_status")
                        result["advice_hash"] = hashlib.sha256(json.dumps(
                            {"analyst": analyst, "challenger": challenger}, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        except (AdvisorError, DatasetError, ValueError, TypeError, KeyError, OverflowError) as exc:
            result.update(status="rejected", reason="Advisor, dataset, or risk validation failed",
                          error_category=type(exc).__name__)
        finally:
            with self._lock:
                if generation == self._generation:
                    current = next((row for row in self._decisions if row["id"] == decision_id), None)
                    if current is not None:
                        current.update(result)
                self._busy = False
                if self._status in ("stopped", "decision_limit", "limit_reached"):
                    self._capture = any(row.get("status") in ("pending_advice", "unresolved")
                                        for row in self._decisions)
                if not self._caps_pending:
                    self._caps_pending = True
                    self._executor.submit(self._check_caps)

    def _resolve_outcomes(self):
        with self._lock:
            decisions = [copy.deepcopy(row) for row in self._decisions if row.get("status") == "unresolved"]
            quotes = list(self._quotes)
            config = {key: self._config[key] for key in ("source", "instrument", "costs", "settings", "initial_balance")}
            evidence = list(self._evidence)
        updates = []
        for decision in decisions:
            target = decision["decision_at"] + decision["horizon_seconds"]
            index = next((i for i, quote in enumerate(quotes)
                          if quote["time"] >= target), None)
            if index is None:
                continue
            horizon_quote = quotes[index]
            if horizon_quote["time"] - target > 180:
                updates.append((decision["id"], {"status": "data_unavailable", "reason": "Horizon quote arrived more than 180 seconds late"}))
                continue
            eligible = decision["decision_at"] + decision["latency_seconds"]
            left = next((i for i, q in enumerate(quotes) if q["time"] > eligible), None)
            selected = quotes[left:index + 1] if left is not None else []
            gaps = [right["time"] - first["time"] for first, right in zip(selected, selected[1:])]
            if len(selected) < 2 or selected[0]["time"] - eligible > 60 or any(gap > 60 for gap in gaps):
                updates.append((decision["id"], {"status": "data_unavailable", "reason": "Quote coverage gap exceeded 60 seconds"}))
                continue
            payload = {"version": 1, "source": config["source"], "timezone": "UTC",
                "instrument": config["instrument"], "costs": config["costs"],
                "quotes": selected, "bars": {"M15": [], "H1": []}, "evidence": evidence}
            try:
                dataset = load_dataset(payload)
                proposal = dict(decision["replay_proposal"], decision_at=decision["decision_at"],
                                lot=decision["lot"])
                replay = run_replay(dataset, [proposal], settings=config["settings"],
                    initial_balance=config["initial_balance"],
                    latency_seconds=decision["latency_seconds"], close_at_end=True)
                if replay["rejected"] or not replay["trades"]:
                    updates.append((decision["id"], {"status": "data_unavailable", "reason": "Virtual replay could not resolve a closed trade"}))
                    continue
                net = sum(trade["net_pnl"] for trade in replay["trades"])
                updates.append((decision["id"], {"status": "completed", "net_pnl": net,
                    "net_r": (net / decision["initial_risk_cash"] if decision["initial_risk_cash"] else None),
                    "horizon_quote_time": horizon_quote["time"],
                    "close_reason": replay["trades"][-1].get("reason", "closed by virtual replay"), "actual_fills": False}))
            except (DatasetError, ValueError, TypeError):
                updates.append((decision["id"], {"status": "data_unavailable", "reason": "Virtual replay data validation failed"}))
        resubmit = False
        with self._lock:
            for identity, update in updates:
                current = next((row for row in self._decisions if row["id"] == identity), None)
                if current is not None and current.get("status") == "unresolved":
                    current.update(update)
            if self._status in ("stopped", "decision_limit", "limit_reached"):
                self._capture = any(row.get("status") in ("pending_advice", "unresolved")
                                    for row in self._decisions)
            self._resolving = False
            if any(row.get("status") == "unresolved" and
                   any(quote["time"] >= row["decision_at"] + row["horizon_seconds"] for quote in self._quotes)
                   for row in self._decisions):
                self._resolving = True
                resubmit = True
        if resubmit:
            self._executor.submit(self._resolve_outcomes)

    def _check_caps(self):
        try:
            with self._lock:
                body = {"quotes": list(self._quotes), "quote_source_times": list(self._quote_source_times),
                        "bars": {key: list(value) for key, value in self._bars.items()},
                        "decisions": list(self._decisions)}
            if len(body["quotes"]) > MAX_QUOTES or len(body["decisions"]) > MAX_DECISIONS:
                raise ValueError
            if len(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()) > MAX_SESSION_BYTES:
                raise ValueError
        except (ValueError, TypeError, RuntimeError):
            with self._lock:
                self._cap_stop_locked()
        finally:
            with self._lock:
                self._caps_pending = False

    def close(self):
        with self._lock:
            if self._status == "running":
                self._generation += 1
                self._status = "stopped"
                self._capture = False
                self._config["api_key"] = None
                for row in self._decisions:
                    if row.get("status") in ("pending_advice", "unresolved"):
                        row.update(status="unknown", reason="Application shutdown")
        self._executor.shutdown(wait=True, cancel_futures=True)
