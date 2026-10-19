"""Session-local research workspace HTTP API."""

import json
import math
import threading
import time
import uuid
import copy
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response

from .advisor import AdvisorError, generate_advice
from .collector import collect_mt5_dataset
from .comparison import ComparisonBudgetError, compare_strategy
from .datasets import DatasetError, load_dataset
from .decision_trace import build_replay_trace
from .research_runs import run_replay_experiment, run_shadow

MAX_DATASET_BYTES = 8 * 1024 * 1024
MAX_PROPOSALS_BYTES = 2 * 1024 * 1024
MAX_JOB_RESULT_BYTES = 10 * 1024 * 1024
MAX_RETAINED_RESULT_BYTES = 50 * 1024 * 1024


def _bad(status, detail):
    raise HTTPException(status_code=status, detail=detail)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON object key")
        out[key] = value
    return out


def _constant(_):
    raise ValueError("non-finite JSON number")


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        _bad(422, "Invalid JSON")


def _finite_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _int(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or maximum is not None and value > maximum:
        _bad(422, f"Invalid {name}")
    return value


def _number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _bad(422, f"Invalid {name}")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        _bad(422, f"Invalid {name}")
    return value


def _advisor_configuration(body):
    provider = body.get("provider", "disabled")
    model, api_key = body.get("model"), body.get("api_key")
    if provider not in ("disabled", "openai", "gemini"):
        _bad(422, "Invalid advisor configuration")
    if provider == "disabled":
        if model is not None or api_key is not None:
            _bad(422, "Invalid advisor configuration")
        return provider, None, None
    for value, maximum in ((model, 100), (api_key, 4096)):
        if (not isinstance(value, str) or not value or len(value) > maximum or
                value != value.strip() or
                any(ord(char) < 32 or ord(char) == 127 for char in value)):
            _bad(422, "Invalid advisor configuration")
    return provider, model, api_key


def _explicit_costs(value):
    fields = {"commission_per_lot_side", "fee_per_lot_side", "slippage_points",
              "swap_long_per_lot_day", "swap_short_per_lot_day", "rollover_hour_utc",
              "rollover_weekdays", "triple_swap_weekday"}
    if not isinstance(value, dict) or set(value) != fields:
        _bad(422, "All explicit dataset cost fields are required")
    sample = {"version": 1, "source": "validation", "timezone": "UTC",
        "instrument": {"symbol": "TEST", "currency": "USD", "point": .01,
            "tick_size": .01, "contract_size": 1, "volume_min": .01,
            "volume_max": 1, "volume_step": .01, "stops_level": 0,
            "margin_per_lot": 1}, "costs": value,
        "quotes": [{"time": 1, "bid": 1, "ask": 1.1}],
        "bars": {"M15": [], "H1": []}, "evidence": []}
    try:
        return load_dataset(sample).to_dict()["costs"]
    except (DatasetError, TypeError, ValueError):
        _bad(422, "Invalid explicit cost settings")


class ResearchWorkspace:
    def __init__(self, settings_provider, memory_provider=None, token_provider=None,
                 history_provider=None, collection_status_provider=None,
                 monitor_blocked_provider=None, forward=None, forward_context_provider=None,
                 broker_lock=None):
        self._settings_provider = settings_provider
        self._memory_provider = memory_provider or (lambda: None)
        self._token_provider = token_provider or (lambda: None)
        self._history_provider = history_provider
        self._collection_status_provider = collection_status_provider or (lambda: {"available": False, "reason": "MT5 demo history is unavailable"})
        self._monitor_blocked_provider = monitor_blocked_provider or (lambda: False)
        self._forward = forward
        self._forward_context_provider = forward_context_provider or (lambda: None)
        self._broker_lock = broker_lock or threading.RLock()
        self._lock = threading.RLock()
        self._datasets = {}
        self._jobs = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research")
        self._closed = False
        self._reserved_datasets = 0

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for job in self._jobs.values():
                if job["status"] == "queued":
                    job["status"] = "failed"
                    job["error"] = "Research workspace closed"
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self._forward is not None:
            self._forward.close()

    def _trim_finished(self):
        finished = sorted((item for item in self._jobs.values()
                           if item["status"] in ("completed", "failed")),
                          key=lambda item: item["created_at"])
        while len(finished) > 5:
            oldest = finished.pop(0)
            self._jobs.pop(oldest["id"], None)

    def _dataset(self, digest):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            _bad(404, "Dataset not found")
        item = self._datasets.get(digest)
        if item is None:
            _bad(404, "Dataset not found")
        return item["dataset"]

    def _manifest(self, dataset):
        data = dataset.to_dict()
        quotes = data["quotes"]
        return {"dataset_hash": dataset.dataset_hash, "source": data["source"],
                "instrument": data["instrument"],
                "counts": {"quotes": len(quotes), "bars": {k: len(v) for k, v in data["bars"].items()},
                           "evidence": len(data["evidence"])},
                "first_quote_time": quotes[0]["time"], "last_quote_time": quotes[-1]["time"]}

    async def _body(self, request, cap):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            _bad(415, "Content-Type must be application/json")
        raw, size = bytearray(), 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > cap:
                _bad(413, "Request body is too large")
            raw.extend(chunk)
        return _json(bytes(raw))

    def _authorize(self, request):
        token = self._token_provider()
        if not isinstance(token, str) or request.headers.get("x-trade-token") != token:
            _bad(403, "Invalid session token")

    def _summary(self, job):
        return {key: job[key] for key in ("id", "status", "kind", "dataset_hash", "created_at", "updated_at", "error") if key in job}

    def _job(self, identifier):
        if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            _bad(404, "Job not found")
        job = self._jobs.get(identifier)
        if job is None:
            _bad(404, "Job not found")
        return job

    def _submit(self, kind, digest, dataset, work):
        with self._lock:
            if self._closed:
                _bad(503, "Research workspace is closed")
            active = any(item["status"] in ("queued", "running") for item in self._jobs.values())
            if active:
                _bad(409, "A research job is already running")
            if kind == "collect":
                if len(self._datasets) + self._reserved_datasets >= 3:
                    _bad(409, "Dataset limit reached; restart the session to clear imports")
                self._reserved_datasets += 1
            finished = [item for item in self._jobs.values() if item["status"] in ("completed", "failed")]
            if len(finished) >= 5:
                oldest = min(finished, key=lambda item: item["created_at"])
                del self._jobs[oldest["id"]]
            identifier = uuid.uuid4().hex
            now = int(time.time())
            record = {"id": identifier, "status": "queued", "kind": kind, "dataset_hash": digest,
                      "created_at": now, "updated_at": now, "error": None, "result": None,
                      "trace": None, "result_size": 0}
            self._jobs[identifier] = record

            def execute():
                with self._lock:
                    if self._closed or record["status"] != "queued":
                        return
                    record["status"] = "running"
                    record["updated_at"] = int(time.time())
                reservation_released = False
                try:
                    result, trace = work(dataset)
                    encoded = _finite_json(result)
                    trace_encoded = _finite_json(trace) if trace is not None else None
                    if len(encoded) > MAX_JOB_RESULT_BYTES or trace_encoded is not None and len(trace_encoded) > MAX_JOB_RESULT_BYTES:
                        raise ValueError("result exceeds limit")
                    size = len(encoded) + (len(trace_encoded) if trace_encoded else 0)
                    collected = None
                    collected_size = 0
                    if record["kind"] == "collect":
                        collected = load_dataset(result["dataset"])
                        if collected.dataset_hash != result.get("dataset_hash"):
                            raise ValueError("collector hash mismatch")
                        collected_size = len(_finite_json(collected.to_dict()))
                    with self._lock:
                        retained = sum(row.get("result_size", 0) for row in self._jobs.values()
                                       if row["status"] == "completed")
                        if retained + size > MAX_RETAINED_RESULT_BYTES:
                            raise ValueError("retained result limit")
                        if collected is not None and collected.dataset_hash not in self._datasets:
                            self._datasets[collected.dataset_hash] = {"dataset": collected, "size": collected_size}
                        if record["kind"] == "collect":
                            self._reserved_datasets = max(0, self._reserved_datasets - 1)
                            reservation_released = True
                            record["dataset_hash"] = collected.dataset_hash
                        record.update(status="completed", result=result, trace=trace,
                                      result_size=size, updated_at=int(time.time()))
                        self._trim_finished()
                except ComparisonBudgetError as exc:
                    with self._lock:
                        if record["kind"] == "collect" and not reservation_released:
                            self._reserved_datasets = max(0, self._reserved_datasets - 1)
                            reservation_released = True
                        record.update(status="failed",
                            error=f"LLM decision budget exceeded: required {exc.required}, limit {exc.limit}",
                            updated_at=int(time.time()))
                        self._trim_finished()
                except Exception:
                    with self._lock:
                        if record["kind"] == "collect" and not reservation_released:
                            self._reserved_datasets = max(0, self._reserved_datasets - 1)
                            reservation_released = True
                        record.update(status="failed", error="Research job failed", updated_at=int(time.time()))
                        self._trim_finished()

            self._executor.submit(execute)
            return {"id": identifier, "status": "queued", "kind": kind, "dataset_hash": digest}

    def install(self, app: FastAPI):
        router = APIRouter()

        @router.post("/api/research/datasets", status_code=201)
        async def upload_dataset(request: Request):
            self._authorize(request)
            value = await self._body(request, MAX_DATASET_BYTES)
            if not isinstance(value, dict):
                _bad(422, "Dataset must be a JSON object")
            try:
                dataset = load_dataset(value)
            except (DatasetError, TypeError, ValueError):
                _bad(422, "Dataset validation failed")
            with self._lock:
                if dataset.dataset_hash not in self._datasets and len(self._datasets) + self._reserved_datasets >= 3:
                    _bad(409, "Dataset limit reached; restart the session to clear imports")
                self._datasets.setdefault(dataset.dataset_hash, {"dataset": dataset, "size": len(_finite_json(value))})
                return self._manifest(dataset)

        @router.get("/api/research/status")
        def status(request: Request):
            self._authorize(request)
            with self._lock:
                return {"datasets": [self._manifest(item["dataset"]) for item in self._datasets.values()],
                        "jobs": [self._summary(item) for item in self._jobs.values()],
                        "provider_status": "not_configured",
                        "limits": {"datasets": 3, "dataset_bytes": MAX_DATASET_BYTES,
                                   "finished_jobs": 5, "running_jobs": 1,
                                   "job_result_bytes": MAX_JOB_RESULT_BYTES,
                                   "retained_result_bytes": MAX_RETAINED_RESULT_BYTES},
                        "session_only": True,
                        "collection": self._collection_status_provider(),
                        "forward": self._forward.status() if self._forward is not None else {"status": "unavailable"}}

        @router.get("/api/research/datasets/{digest}/snapshot")
        def snapshot(request: Request, digest: str, as_of: int):
            self._authorize(request)
            _int(as_of, "as_of", 1, 253402275599)
            with self._lock:
                dataset = self._dataset(digest)
            try:
                return dataset.snapshot(as_of)
            except (TypeError, ValueError):
                _bad(422, "Invalid snapshot time")

        @router.get("/api/research/datasets/{digest}/download")
        def download_dataset(request: Request, digest: str):
            self._authorize(request)
            with self._lock:
                dataset = self._dataset(digest)
                body = _finite_json(dataset.to_dict())
            return Response(body, media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="dataset-{digest}.json"'})

        @router.post("/api/research/collect", status_code=202)
        async def collect(request: Request):
            self._authorize(request)
            body = await self._body(request, 128 * 1024)
            allowed = {"start_at", "end_at", "costs", "margin_per_lot", "gap_threshold_seconds"}
            if not isinstance(body, dict) or set(body) - allowed or not {"start_at", "end_at", "costs", "margin_per_lot"} <= set(body):
                _bad(422, "Invalid collection request")
            if self._history_provider is None or not self._collection_status_provider().get("available"):
                _bad(409, self._collection_status_provider().get("reason", "MT5 demo history is unavailable"))
            if self._monitor_blocked_provider() or self._forward is not None and self._forward.active:
                _bad(409, "Stop Monitor and forward shadow before collecting MT5 history")
            start_at = _int(body["start_at"], "start_at", 1, 253402275599)
            end_at = _int(body["end_at"], "end_at", 1, 253402275599)
            now = int(time.time())
            if start_at >= end_at or end_at > now or end_at - start_at > 31 * 86400:
                _bad(422, "Collection range must be increasing, end by now, and span at most 31 days")
            costs = _explicit_costs(body["costs"])
            margin = _number(body["margin_per_lot"], "margin_per_lot", 0)
            if margin <= 0:
                _bad(422, "Invalid margin_per_lot")
            threshold = _int(body.get("gap_threshold_seconds", 60), "gap_threshold_seconds", 1, 86400)
            with self._lock:
                if self._monitor_blocked_provider() or self._forward is not None and self._forward.active:
                    _bad(409, "Stop Monitor and forward shadow before collecting MT5 history")
                if len(self._datasets) + self._reserved_datasets >= 3:
                    _bad(409, "Dataset limit reached; restart the session to clear imports")

            def work(_dataset):
                raw = self._history_provider(datetime.fromtimestamp(start_at, timezone.utc),
                                             datetime.fromtimestamp(end_at, timezone.utc))
                return collect_mt5_dataset(raw, costs, margin, gap_threshold_seconds=threshold), None

            with self._broker_lock:
                if self._monitor_blocked_provider() or self._forward is not None and self._forward.active:
                    _bad(409, "Stop Monitor and forward shadow before collecting MT5 history")
                return self._submit("collect", None, None, work)

        @router.get("/api/research/forward")
        def forward_status(request: Request):
            self._authorize(request)
            if self._forward is None:
                return {"status": "unavailable"}
            return self._forward.status()

        async def start_forward(request: Request):
            self._authorize(request)
            body = await self._body(request, 128 * 1024)
            allowed = {"strategy", "costs", "margin_per_lot", "initial_balance", "latency_seconds",
                "horizon_seconds", "calendar_before_minutes", "calendar_after_minutes",
                "evidence_dataset_hash", "provider", "model", "api_key", "max_decisions", "challenger"}
            required = {"strategy", "costs", "margin_per_lot", "initial_balance"}
            if not isinstance(body, dict) or set(body) - allowed or not required <= set(body):
                _bad(422, "Invalid forward start request")
            if self._forward is None:
                _bad(409, "Forward shadow is unavailable")
            strategy = body["strategy"]
            if strategy not in ("smc", "supply", "ema", "bollinger", "asian", "ai"):
                _bad(422, "Invalid forward strategy")
            provider, model, api_key = _advisor_configuration(body)
            if api_key and api_key in (model or ""):
                _bad(422, "Invalid advisor configuration")
            settings = copy.deepcopy(self._settings_provider())
            if strategy not in settings.get("enabled_strategies", []):
                _bad(422, "Forward strategy must be enabled in current settings")
            costs = _explicit_costs(body["costs"])
            margin = _number(body["margin_per_lot"], "margin_per_lot", 0)
            initial_balance = _number(body["initial_balance"], "initial_balance", 0)
            if margin <= 0 or initial_balance <= 0:
                _bad(422, "Invalid forward capital or margin")
            latency = _int(body.get("latency_seconds", 0), "latency_seconds")
            horizon = _int(body.get("horizon_seconds", 900), "horizon_seconds", 300, 86400)
            before = _int(body.get("calendar_before_minutes", 0), "calendar_before_minutes", 0, 1440)
            after = _int(body.get("calendar_after_minutes", 0), "calendar_after_minutes", 0, 1440)
            max_decisions = _int(body.get("max_decisions", 20), "max_decisions", 1, 20)
            challenger = body.get("challenger", True)
            if not isinstance(challenger, bool) or challenger is not True and provider != "disabled":
                _bad(422, "Forward shadow requires challenger approval when an advisor is configured")
            evidence_hash = body.get("evidence_dataset_hash")
            with self._lock:
                evidence_dataset = self._dataset(evidence_hash) if evidence_hash is not None else None
                active_collection = any(job["kind"] == "collect" and job["status"] in ("queued", "running")
                                        for job in self._jobs.values())
            if active_collection:
                _bad(409, "Wait for MT5 history collection to finish before starting forward shadow")
            config = {"strategy": strategy, "costs": costs, "margin_per_lot": margin,
                "initial_balance": initial_balance, "latency_seconds": latency,
                "horizon_seconds": horizon, "calendar_before_minutes": before,
                "calendar_after_minutes": after, "provider": provider, "model": model,
                "api_key": api_key, "max_decisions": max_decisions,
                "settings": settings, "challenger": challenger}
            with self._broker_lock:
                if self._monitor_blocked_provider():
                    _bad(409, "Stop Monitor and expiry-close session before starting forward shadow")
                if any(job["kind"] == "collect" and job["status"] in ("queued", "running")
                       for job in self._jobs.values()):
                    _bad(409, "Wait for MT5 history collection to finish before starting forward shadow")
                context = self._forward_context_provider()
                if not isinstance(context, dict):
                    _bad(409, "Connected account context is unavailable")
                if context.get("mode") == "mt5" and context.get("linear_model_valid") is not True:
                    _bad(409, "MT5 instrument is not validated for the linear research P&L model")
                try:
                    return self._forward.start(config, context, evidence_dataset)
                except (ValueError, DatasetError, TypeError):
                    _bad(409, "Forward shadow could not start with the current account and instrument")

        @router.post("/api/research/forward", status_code=201)
        async def start_forward_alias(request: Request):
            return await start_forward(request)

        @router.post("/api/research/forward/start", status_code=201)
        async def start_forward_route(request: Request):
            return await start_forward(request)

        @router.post("/api/research/forward/stop")
        def stop_forward(request: Request):
            self._authorize(request)
            if self._forward is None:
                _bad(409, "Forward shadow is unavailable")
            try:
                return self._forward.stop()
            except ValueError:
                _bad(409, "No active forward run")

        @router.get("/api/research/forward/result")
        def forward_result(request: Request):
            self._authorize(request)
            if self._forward is None:
                _bad(409, "Forward shadow is unavailable")
            try:
                body = _finite_json(self._forward.export_result())
            except ValueError as exc:
                _bad(409, str(exc))
            return Response(body, media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="forward-shadow-result.json"'})

        @router.get("/api/research/forward/dataset")
        def forward_dataset(request: Request):
            self._authorize(request)
            if self._forward is None:
                _bad(409, "Forward shadow is unavailable")
            try:
                body = _finite_json(self._forward.export_dataset())
            except ValueError as exc:
                _bad(409, str(exc))
            return Response(body, media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="forward-shadow-dataset.json"'})

        @router.post("/api/research/jobs", status_code=202)
        async def submit(request: Request):
            self._authorize(request)
            body = await self._body(request, MAX_PROPOSALS_BYTES * 2 + 65536)
            if not isinstance(body, dict):
                _bad(422, "Invalid job request")
            kind, digest = body.get("kind"), body.get("dataset_hash")
            if kind not in ("replay", "evaluate", "shadow", "advisor", "compare"):
                _bad(422, "Invalid job kind")
            common = {"dataset_hash", "kind", "options"}
            if kind in ("replay", "shadow"):
                allowed_fields = common | {"proposals_json"}
                if "proposals_json" not in body:
                    _bad(422, "Invalid job request")
            elif kind == "evaluate":
                allowed_fields = common
            else:
                allowed_fields = common | {"provider", "model", "api_key"}
            if set(body) - allowed_fields or not {"dataset_hash", "kind"} <= set(body):
                _bad(422, "Invalid job request")
            advisor_config = _advisor_configuration(body) if kind in ("advisor", "compare") else None
            if advisor_config and advisor_config[2] and advisor_config[2] in (advisor_config[1] or ""):
                _bad(422, "Invalid advisor configuration")
            with self._lock:
                dataset = self._dataset(digest)
                settings = copy.deepcopy(self._settings_provider())
                memory = copy.deepcopy(self._memory_provider())
            options = body.get("options", {})
            if not isinstance(options, dict):
                _bad(422, "Invalid options")
            proposal_json = body.get("proposals_json")
            proposals = None
            if kind in ("replay", "shadow"):
                if not isinstance(proposal_json, str) or len(proposal_json.encode("utf-8")) > MAX_PROPOSALS_BYTES:
                    _bad(422, "proposals_json is required and must be at most 2 MiB")
                proposals = _json(proposal_json.encode("utf-8"))
                if not isinstance(proposals, list):
                    _bad(422, "proposals_json must contain an array")
            if kind == "replay":
                allowed = {"settings", "initial_balance", "latency_seconds", "close_at_end", "calendar_before_minutes", "calendar_after_minutes"}
                if set(options) - allowed:
                    _bad(422, "Unsupported replay option")
                initial_balance = _number(options.get("initial_balance", 10000.0), "initial_balance", 0)
                if initial_balance <= 0:
                    _bad(422, "Invalid initial_balance")
                config = {"settings": options.get("settings", settings),
                          "initial_balance": initial_balance,
                          "latency_seconds": _int(options.get("latency_seconds", 0), "latency_seconds"),
                          "close_at_end": options.get("close_at_end", True),
                          "calendar_before_minutes": _int(options.get("calendar_before_minutes", 0), "calendar_before_minutes", 0, 1440),
                          "calendar_after_minutes": _int(options.get("calendar_after_minutes", 0), "calendar_after_minutes", 0, 1440)}
                if not isinstance(config["close_at_end"], bool) or not isinstance(config["settings"], dict):
                    _bad(422, "Invalid replay options")
                def work(ds):
                    result = run_replay_experiment(ds, proposals, **config)
                    return result, build_replay_trace(ds, result)
            elif kind == "evaluate":
                allowed = {"settings", "strategies", "train_bars", "validation_bars", "test_bars", "purge_bars", "embargo_bars", "initial_balance", "latency_seconds", "calendar_before_minutes", "calendar_after_minutes"}
                if set(options) - allowed:
                    _bad(422, "Unsupported evaluation option")
                config = {"settings": options.get("settings", settings),
                          "strategies": options.get("strategies"),
                          "train_bars": _int(options.get("train_bars", 600), "train_bars", 80),
                          "validation_bars": _int(options.get("validation_bars", 250), "validation_bars", 20),
                          "test_bars": _int(options.get("test_bars", 100), "test_bars", 1),
                          "purge_bars": _int(options.get("purge_bars", 1), "purge_bars", 1),
                          "embargo_bars": _int(options.get("embargo_bars", 1), "embargo_bars", 1),
                          "initial_balance": _number(options.get("initial_balance", 10000), "initial_balance", 0),
                          "latency_seconds": _int(options.get("latency_seconds", 0), "latency_seconds"),
                          "calendar_before_minutes": _int(options.get("calendar_before_minutes", 0), "calendar_before_minutes", 0, 1440),
                          "calendar_after_minutes": _int(options.get("calendar_after_minutes", 0), "calendar_after_minutes", 0, 1440)}
                if not isinstance(config["settings"], dict):
                    _bad(422, "Invalid evaluation settings")
                if config["initial_balance"] <= 0:
                    _bad(422, "Invalid initial_balance")
                if config["strategies"] is not None and (not isinstance(config["strategies"], list) or any(not isinstance(x, str) for x in config["strategies"])):
                    _bad(422, "Invalid strategies")
                def work(ds):
                    from .evaluation import evaluate_dataset
                    return evaluate_dataset(ds, **config), None
            elif kind == "shadow":
                if set(options) - {"as_of", "calendar_before_minutes", "calendar_after_minutes"}:
                    _bad(422, "Unsupported shadow option")
                as_of = _int(options.get("as_of"), "as_of", 1, 253402275599)
                before = _int(options.get("calendar_before_minutes", 0), "calendar_before_minutes", 0, 1440)
                after = _int(options.get("calendar_after_minutes", 0), "calendar_after_minutes", 0, 1440)
                def work(ds):
                    return run_shadow(ds, proposals, as_of, before, after), None
            else:
                provider, model, api_key = advisor_config
                if kind == "advisor":
                    if set(options) - {"as_of", "timeout_seconds", "max_output_tokens", "challenger", "calendar_before_minutes", "calendar_after_minutes"}:
                        _bad(422, "Unsupported advisor option")
                    timeout = _number(options.get("timeout_seconds", 20), "timeout_seconds", 1, 30)
                    token_cap = _int(options.get("max_output_tokens", 1200), "max_output_tokens", 128, 2048)
                    challenger = options.get("challenger", True)
                    if not isinstance(challenger, bool):
                        _bad(422, "Invalid challenger option")
                    before = _int(options.get("calendar_before_minutes", 0), "calendar_before_minutes", 0, 1440)
                    after = _int(options.get("calendar_after_minutes", 0), "calendar_after_minutes", 0, 1440)
                    as_of = _int(options.get("as_of"), "as_of", 1, 253402275599)
                    def work(ds):
                        return generate_advice(ds, as_of, provider, model, api_key, challenger, timeout,
                            token_cap, before, after, memory), None
                else:
                    allowed = {"strategy", "settings", "train_bars", "validation_bars", "test_bars",
                        "purge_bars", "embargo_bars", "initial_balance", "latency_seconds",
                        "calendar_before_minutes", "calendar_after_minutes", "max_llm_decisions"}
                    if set(options) - allowed:
                        _bad(422, "Unsupported comparison option")
                    strategy = options.get("strategy")
                    if not isinstance(strategy, str) or strategy not in ("smc", "supply", "ema", "bollinger", "asian", "ai"):
                        _bad(422, "Invalid comparison strategy")
                    config = {"strategy": strategy, "settings": options.get("settings", settings),
                        "train_bars": _int(options.get("train_bars", 600), "train_bars", 80),
                        "validation_bars": _int(options.get("validation_bars", 250), "validation_bars", 20),
                        "test_bars": _int(options.get("test_bars", 100), "test_bars", 1),
                        "purge_bars": _int(options.get("purge_bars", 1), "purge_bars", 1),
                        "embargo_bars": _int(options.get("embargo_bars", 1), "embargo_bars", 1),
                        "initial_balance": _number(options.get("initial_balance", 10000), "initial_balance", 0),
                        "latency_seconds": _int(options.get("latency_seconds", 0), "latency_seconds"),
                        "calendar_before_minutes": _int(options.get("calendar_before_minutes", 0), "calendar_before_minutes", 0, 1440),
                        "calendar_after_minutes": _int(options.get("calendar_after_minutes", 0), "calendar_after_minutes", 0, 1440),
                        "max_llm_decisions": _int(options.get("max_llm_decisions", 10), "max_llm_decisions", 1, 20),
                        "provider": provider, "model": model, "api_key": api_key}
                    if not isinstance(config["settings"], dict) or config["initial_balance"] <= 0:
                        _bad(422, "Invalid comparison settings")
                    def work(ds):
                        return compare_strategy(ds, **config), None
            return self._submit(kind, digest, dataset, work)

        @router.get("/api/research/jobs/{identifier}")
        def get_job(request: Request, identifier: str):
            self._authorize(request)
            with self._lock:
                return self._summary(self._job(identifier))

        @router.get("/api/research/jobs/{identifier}/result")
        def get_result(request: Request, identifier: str):
            self._authorize(request)
            with self._lock:
                job = self._job(identifier)
                if job["status"] != "completed":
                    _bad(409, "Research result is not ready")
                body = _finite_json(job["result"])
            return Response(body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="research-{identifier}.json"'})

        @router.get("/api/research/jobs/{identifier}/trace")
        def get_trace(request: Request, identifier: str):
            self._authorize(request)
            with self._lock:
                job = self._job(identifier)
                if job["kind"] != "replay" or job["status"] != "completed" or job["trace"] is None:
                    _bad(409, "Replay trace is not available")
                body = _finite_json(job["trace"])
            return Response(body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="trace-{identifier}.json"'})

        app.include_router(router)
        app.state.research = self
        return self


def install_research(app: FastAPI, settings_provider, memory_provider=None, token_provider=None,
                     history_provider=None, collection_status_provider=None,
                     monitor_blocked_provider=None, forward=None, forward_context_provider=None,
                     broker_lock=None):
    return ResearchWorkspace(settings_provider, memory_provider, token_provider,
        history_provider, collection_status_provider, monitor_blocked_provider,
        forward, forward_context_provider, broker_lock).install(app)
