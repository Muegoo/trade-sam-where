import argparse
import copy
import csv
import io
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analytics import APP_MAGICS, analytics, journal
from .broker import BrokerError, MT5Broker, SimulationBroker
from .risk import day_net, preview
from .store import Store
from .strategies import scan
from .monitor import Monitor
from .research_api import install_research
from .forward_shadow import ForwardShadow

ROOT = Path(__file__).resolve().parent


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    side: Literal["BUY", "SELL"]
    kind: Literal["MARKET", "LIMIT", "STOP"] = "MARKET"
    entry: float | None = Field(default=None, gt=0)
    sl: float = Field(gt=0)
    tp: float = Field(gt=0)
    lot: float | None = Field(default=None, gt=0)
    strategy: Literal["manual", "smc", "supply", "ema", "bollinger", "asian", "ai"] = "manual"


class SendOrder(Order):
    request_id: str = Field(min_length=16, max_length=100)
    scope: str = Field(min_length=1, max_length=150)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    max_lot: float = Field(gt=0, le=100)
    risk_percent: float = Field(gt=0, le=10)
    daily_loss: float = Field(gt=0)
    max_orders: int = Field(ge=1, le=100)
    max_spread: float = Field(gt=0)
    daily_target: float = Field(gt=0)
    magic_names: dict[str, str] = Field(default_factory=dict)
    playbook_threshold: float = Field(ge=.6, le=.95)
    reward_ratio: float = Field(gt=0, le=20)
    asian_start: int = Field(ge=0, le=22)
    asian_end: int = Field(ge=1, le=23)
    enabled_strategies: list[Literal["smc", "supply", "ema", "bollinger", "asian", "ai"]]

    @model_validator(mode="after")
    def validate_hours(self):
        if self.asian_end <= self.asian_start:
            raise ValueError("เวลาสิ้นสุด Asian Range ต้องมากกว่าเวลาเริ่ม")
        if any(not k.isdigit() or not v.strip() or len(v) > 80 for k, v in self.magic_names.items()):
            raise ValueError("Magic ต้องเป็นจำนวนเต็มและชื่อ EA ต้องยาว 1–80 ตัวอักษร")
        return self


class Kill(BaseModel):
    active: bool


class MonitorStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1, max_length=150)
    hours: int = Field(default=8, ge=1, le=24)
    close_on_expiry: bool = True
    expires_at: int | None = Field(default=None, gt=0)


class ClosePosition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    scope: str = Field(min_length=1, max_length=150)
    request_id: str = Field(min_length=16, max_length=100)
    identifier: int = Field(gt=0)
    volume: float = Field(gt=0)
    side: Literal[0, 1]


def create_app(mode="simulation", symbol="XAUUSD", db_path=None):
    store = Store(db_path or ROOT.parent / "data" / "trade.db")
    broker = SimulationBroker(symbol, store) if mode == "simulation" else MT5Broker(symbol)
    monitor = Monitor(store, broker)
    lock = threading.RLock()
    token = secrets.token_urlsafe(32)
    state = {"connected": False, "mode": mode, "symbol": symbol, "error": "กำลังเชื่อมต่อ"}
    model_cache = {"bar": None, "result": {}}
    signals_cache = {"bar": None, "settings": None, "result": []}
    forward_shadow = ForwardShadow()
    forward_cache = {"value": None}
    stop = threading.Event()

    def update(run_monitor=False):
        with lock:
            try:
                snap = broker.snapshot()
                settings = store.settings()
                rows = journal(snap["deals"], settings["magic_names"])
                store.sample(snap["scope"], int(time.time()), snap["account"])
                curve = store.curve(snap["scope"])
                results = analytics(rows, snap["positions"], curve)
                h1 = snap["h1"]
                bar = (snap["scope"], int(h1[-1]["time"])) if len(h1) else None
                if bar != model_cache["bar"]:
                    try:
                        from gold_demo_alert import analyze
                        model_cache["result"] = analyze(h1)
                    except (ValueError, ImportError) as exc:
                        model_cache["result"] = {"error": str(exc), "validated": False}
                    model_cache["bar"] = bar
                closed = snap["bars"][:-1]
                signature = snap["scope"] + str({k: settings[k] for k in ("enabled_strategies", "reward_ratio", "asian_start", "asian_end", "playbook_threshold")})
                if closed and (signals_cache["bar"] != closed[-1]["time"] or signals_cache["settings"] != signature):
                    # AI model uses closed H1; other scanners use closed M15.
                    signals = scan(closed, settings, model_cache["result"])
                    signals[-1] = scan([dict((k, float(b[k]) if k != "time" else int(b[k])) for k in ("time", "open", "high", "low", "close")) for b in h1], settings, model_cache["result"])[-1]
                    signals_cache.update(bar=closed[-1]["time"], settings=signature, result=signals)
                if run_monitor and monitor.step(snap, signals_cache["result"], settings):
                    snap = broker.snapshot()
                    rows = journal(snap["deals"], settings["magic_names"])
                    store.sample(snap["scope"], int(time.time()), snap["account"])
                    curve = store.curve(snap["scope"])
                    results = analytics(rows, snap["positions"], curve)
                state.clear()
                state.update(connected=True, mode=mode, symbol=symbol, updated=int(time.time()),
                             scope=snap["scope"], account=snap["account"], info=snap["info"], tick=snap["tick"],
                             bars=snap["bars"], positions=snap["positions"], orders=snap["orders"],
                             journal=rows, curve=curve, analytics=results, signals=signals_cache["result"],
                             settings=settings, day_net=day_net(snap["deals"]),
                             spread=(snap["tick"]["ask"]-snap["tick"]["bid"])/snap["info"]["point"])
                def closed_rows(rows):
                    result = []
                    for row in rows:
                        opened = int(row["time"])
                        if opened + 3600 <= time.time():
                            result.append({"time": opened, **{name: float(row[name])
                                for name in ("open", "high", "low", "close")}})
                    return result
                forward_cache["value"] = {"connected": True, "mode": mode, "symbol": symbol,
                    "scope": snap["scope"], "observed_at": time.time(), "account": copy.deepcopy(snap["account"]),
                    "info": copy.deepcopy(snap["info"]), "tick": copy.deepcopy(snap["tick"]),
                    "bars": copy.deepcopy(closed), "h1": closed_rows(h1),
                    "signals": copy.deepcopy(signals_cache["result"]), "settings": copy.deepcopy(settings)}
            except Exception as exc:
                monitor.connection_error()
                forward_cache["value"] = None
                state.clear()
                state.update(connected=False, mode=mode, symbol=symbol, error=str(exc), updated=int(time.time()), settings=store.settings())

    def poll():
        while not stop.is_set():
            update(run_monitor=True)
            with lock:
                observation = copy.deepcopy(forward_cache.get("value"))
                if observation is None:
                    observation = {"connected": False, "mode": mode, "symbol": symbol,
                                   "observed_at": time.time()}
            forward_shadow.observe(observation)
            stop.wait(3)

    @asynccontextmanager
    async def lifespan(app):
        worker = threading.Thread(target=poll, daemon=True)
        worker.start()
        yield
        stop.set()
        worker.join(timeout=10)
        app.state.research.close()
        with lock:
            broker.close()
            store.db.close()

    app = FastAPI(title="Trade Sam Where", lifespan=lifespan)
    app.state.store, app.state.broker, app.state.refresh = store, broker, update
    app.state.monitor = monitor

    def research_settings():
        with lock:
            return dict(store.settings())

    def research_memory():
        with lock:
            account = state.get("account", {})
            if not state.get("connected") or mode != "simulation" and not account.get("is_demo"):
                return []
            source = "simulation" if mode == "simulation" else "broker_demo"
            rows = state.get("analytics", {}).get("closed_trades", [])
            return [{"symbol": row.get("symbol"), "closed_at": row.get("time"),
                     "net": row.get("net"), "source": source, "completed": True,
                     "risk_cash": row.get("risk_cash")} for row in rows]

    def collection_status():
        with lock:
            if mode != "mt5":
                return {"available": False, "reason": "Historical MT5 collection requires MT5 mode"}
            account = state.get("account", {})
            if not state.get("connected") or not account.get("is_demo"):
                return {"available": False, "reason": "Connect a demo account in MT5 first"}
            return {"available": True, "reason": None}

    def research_history(start, end):
        with lock:
            if monitor.enabled or monitor.close_on_expiry or forward_shadow.active:
                raise BrokerError("Stop Monitor and forward shadow before collecting MT5 history")
            if mode != "mt5" or not state.get("connected") or not state.get("account", {}).get("is_demo"):
                raise BrokerError("Historical MT5 collection requires a connected demo account")
            return broker.research_history(start, end)

    def forward_context():
        with lock:
            context = copy.deepcopy(forward_cache.get("value"))
            if context is None:
                return None
            context["linear_model_valid"] = (True if mode == "simulation" else
                broker.validate_research_instrument(context["info"], context["tick"],
                                                    context["account"].get("currency")))
            return context

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("localhost", "127.0.0.1", "testserver"):
            return Response("Localhost only", status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-trade-token") != token:
                return Response("Invalid session token", status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != f"http://{request.headers['host']}":
                return Response("Invalid origin", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    install_research(app, research_settings, research_memory, lambda: token,
        history_provider=research_history, collection_status_provider=collection_status,
        monitor_blocked_provider=lambda: monitor.enabled or monitor.close_on_expiry,
        forward=forward_shadow, forward_context_provider=forward_context, broker_lock=lock)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/state")
    def get_state():
        with lock:
            return dict(state) | {"token": token, "settings": store.settings(), "monitor": monitor.status(state.get("scope"))}

    @app.post("/api/monitor/start")
    def monitor_start(body: MonitorStart):
        with lock:
            try:
                update()
                if not state.get("connected"):
                    raise BrokerError(state.get("error", "ยังไม่เชื่อมต่อ"))
                snap = broker.snapshot()
                if snap["scope"] != body.scope or state.get("scope") != snap["scope"]:
                    raise BrokerError("บัญชีเปลี่ยน กรุณาโหลดข้อมูลใหม่")
                if monitor.enabled:
                    raise BrokerError("Monitor เปิดอยู่แล้ว หยุดก่อนเริ่ม Session ใหม่")
                monitor.start(snap, state["signals"], store.settings(), body.hours, body.close_on_expiry, body.expires_at)
                return monitor.status()
            except BrokerError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.post("/api/monitor/stop")
    def monitor_stop():
        with lock:
            monitor.stop()
            return monitor.status()

    @app.put("/api/settings")
    def save_settings(settings: Settings):
        with lock:
            value = settings.model_dump() | {"kill_switch": store.settings()["kill_switch"]}
            store.save_settings(value)
            return value

    @app.post("/api/kill")
    def kill(body: Kill):
        with lock:
            settings = store.settings() | {"kill_switch": body.active}
            store.save_settings(settings)  # Persist the halt BEFORE contacting MT5.
            if body.active and monitor.enabled:
                monitor.stop("หยุด Monitor ด้วย Kill Switch")
            errors, cancelled = [], []
            if body.active:
                try:
                    snap = broker.snapshot()
                    if snap["account"]["is_demo"]:
                        for order in snap["orders"]:
                            if order["magic"] in APP_MAGICS:
                                try:
                                    broker.cancel(order["ticket"])
                                    cancelled.append(order["ticket"])
                                except BrokerError as exc:
                                    errors.append(str(exc))
                except Exception as exc:
                    errors.append(str(exc))
            return {"active": body.active, "cancelled": cancelled, "errors": errors}

    @app.post("/api/preview")
    def order_preview(body: Order):
        with lock:
            try:
                return preview(body.model_dump(), broker.snapshot(), store.settings(), broker)
            except BrokerError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.post("/api/orders")
    def send_order(body: SendOrder):
        with lock:
            existing = store.request(body.scope, body.request_id)
            if existing:
                if existing.get("pending"):
                    raise HTTPException(409, "ไม่ทราบผลคำสั่งเดิม ตรวจ Journal / MT5 ก่อนสร้างคำสั่งใหม่")
                return existing
            try:
                snap = broker.snapshot()
                if snap["scope"] != body.scope:
                    raise BrokerError("บัญชีเปลี่ยนหลัง preview; กรุณาคำนวณใหม่")
                proposal = preview(body.model_dump(exclude={"request_id", "scope"}), snap, store.settings(), broker)
                if not proposal["allowed"]:
                    raise BrokerError("; ".join(proposal["errors"]))
                store.save_request(body.scope, body.request_id, {"pending": True})
                result = broker.send(proposal, snap)
                store.save_request(body.scope, body.request_id, result)
                update()
                return result
            except BrokerError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/orders/{ticket}")
    def cancel_order(ticket: int, scope: str):
        with lock:
            try:
                snap = broker.snapshot()
                if snap["scope"] != scope or not snap["account"]["is_demo"]:
                    raise BrokerError("บัญชีไม่ตรงหรือไม่ใช่ Demo")
                if not any(o["ticket"] == ticket for o in snap["orders"]):
                    raise BrokerError("ไม่พบ pending order")
                broker.cancel(ticket)
                update()
                return {"ok": True}
            except BrokerError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.post("/api/positions/{ticket}/close")
    def close_position(ticket: int, body: ClosePosition):
        with lock:
            key = "close-" + body.request_id
            existing = store.request(body.scope, key)
            if existing:
                if existing.get("pending"):
                    raise HTTPException(409, "ผลปิดคำสั่งเดิมไม่แน่นอน ตรวจ MT5 ก่อนลองใหม่")
                return existing
            try:
                snap = broker.snapshot()
                if snap["scope"] != body.scope or not snap["account"]["is_demo"]:
                    raise BrokerError("บัญชีเปลี่ยนหรือไม่ใช่ Demo")
                if not snap["account"]["trade_allowed"]:
                    raise BrokerError("MT5 ไม่อนุญาต Algo / Python Trading")
                position = next((p for p in snap["positions"] if p["ticket"] == ticket), None)
                if position is None:
                    raise BrokerError("ไม่พบ Position อาจปิดไปแล้ว กรุณาโหลดใหม่")
                if (position.get("identifier", ticket) != body.identifier or position["type"] != body.side
                        or abs(position["volume"]-body.volume) > 1e-8):
                    raise BrokerError("Position เปลี่ยนจากที่แสดง กรุณาโหลดและตรวจใหม่")
                if monitor.enabled and monitor.scope == snap["scope"] and position["symbol"] == symbol:
                    monitor.suppress_positions([position], state.get("signals", []))
                store.save_request(body.scope, key, {"pending": True})
                result = broker.close_position(position, snap)
                store.save_request(body.scope, key, result)
                update()
                if monitor.enabled and monitor.scope == snap["scope"] and position["symbol"] == symbol:
                    monitor.suppress_positions([position], state.get("signals", []))
                    monitor.message = "ปิด Position ตามคำขอแล้ว · รอสัญญาณแท่งใหม่"
                return result
            except BrokerError as exc:
                raise HTTPException(409, str(exc)) from exc

    def filtered(start, end, strategy, source, hour_start, hour_end):
        with lock:
            if not state.get("connected"):
                raise HTTPException(409, "ยังไม่เชื่อมต่อข้อมูล")
            rows = list(state.get("journal", []))
        if start:
            rows = [r for r in rows if r["local_time"][:10] >= start]
        if end:
            rows = [r for r in rows if r["local_time"][:10] <= end]
        if strategy:
            rows = [r for r in rows if r["strategy"] == strategy]
        if source:
            rows = [r for r in rows if r["source"] == source]
        if hour_start:
            rows = [r for r in rows if r["local_time"][11:16] >= hour_start]
        if hour_end:
            rows = [r for r in rows if r["local_time"][11:16] <= hour_end]
        return rows

    @app.get("/api/journal")
    def get_journal(start: str = "", end: str = "", strategy: str = "", source: str = "", hour_start: str = "", hour_end: str = ""):
        return filtered(start, end, strategy, source, hour_start, hour_end)

    @app.get("/api/journal.csv")
    def export(start: str = "", end: str = "", strategy: str = "", source: str = "", hour_start: str = "", hour_end: str = ""):
        rows = filtered(start, end, strategy, source, hour_start, hour_end)
        stream = io.StringIO()
        fields = ["ticket", "position_id", "local_time", "symbol", "side", "entry_label", "volume", "price", "magic", "strategy", "source", "profit", "commission", "swap", "fee", "net", "comment"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Protect spreadsheet consumers against formula injection in broker comments.
            writer.writerow({k: "'"+v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in row.items()})
        return Response("\ufeff"+stream.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="trading-journal.csv"'})

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


def main():
    parser = argparse.ArgumentParser(description="Local Trade Dashboard and MT5 Demo Terminal")
    parser.add_argument("--mode", choices=["simulation", "mt5"], default="simulation")
    parser.add_argument("--symbol", default=os.getenv("MT5_GOLD_SYMBOL", "XAUUSD"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.mode, args.symbol), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
