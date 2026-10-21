"""Bounded, operator-started MT5 Demo session with durable local statistics."""
import argparse
import json
import os
from pathlib import Path
import socket
import threading
import time

import httpx
import uvicorn

from .analytics import APP_MAGICS, BANGKOK
from .server import create_app
from .session_stats import SessionRecorder


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def run(plan_path):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    output = plan_path.parent
    deadline = plan["expires_at"]
    if not time.time() < deadline <= time.time() + 86400:
        raise ValueError("Session deadline must be in the next 24 hours")
    marker = output / "started.json"
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"pid": os.getpid(), "started_at": int(time.time())}, stream)
    # Fail before app startup if another server already owns this address.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", plan["port"]))
    app = create_app("mt5", plan["symbol"], plan["db_path"],
                     mt5_time_offset_seconds=plan.get("mt5_time_offset_seconds", 0))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=plan["port"], log_level="warning"))
    worker = threading.Thread(target=server.run, daemon=False)
    worker.start()
    if os.name == "nt":
        import ctypes
        # Keep this authorized session awake without changing persistent power settings.
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    client = httpx.Client(base_url=f"http://127.0.0.1:{plan['port']}", timeout=45)
    scope = plan["expected_scope"]
    armed = False
    jobs = {}
    day = plan["date"]
    recorder = None
    last_dataset_export = 0

    def request(method, route, body=None):
        response = client.request(method, route, **({"json": body} if body is not None else {}))
        response.raise_for_status()
        return response

    def state():
        return request("GET", "/api/state").json()

    def save(current, phase):
        nonlocal last_dataset_export
        current = {key: value for key, value in current.items() if key != "token"}
        write_json(output / "latest-state.json", current)
        monitor = current.get("monitor", {})
        write_json(output / "status.json", {
            "phase": phase, "pid": os.getpid(), "heartbeat": int(time.time()),
            "expires_at": deadline, "max_trades": plan["max_trades"],
            "trades_used": monitor.get("trades_used", 0), "connected": current.get("connected"),
            "monitor_enabled": monitor.get("enabled"), "close_on_expiry": monitor.get("close_on_expiry"),
            "message": monitor.get("message"), "research_jobs": jobs})
        record = {"time": int(time.time()), "phase": phase, "connected": current.get("connected"),
                  "tick": current.get("tick"), "account": current.get("account"),
                  "positions": current.get("positions", []), "orders": current.get("orders", []),
                  "monitor": monitor, "signals": current.get("signals", [])}
        with (output / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        session = recorder.capture(current, int(time.time())) if recorder else None
        if recorder and time.time() - last_dataset_export >= 300:
            recorder.export_dataset()
            last_dataset_export = time.time()
        if not current.get("connected") or current.get("scope") != scope:
            path = output / "summary.json"
            previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            write_json(path, previous | {"phase": phase, "connected": False, "updated_at": int(time.time()),
                "error": current.get("error", "Expected account unavailable")})
        if current.get("connected") and current.get("scope") == scope:
            response = request("GET", f"/api/journal.csv?start={day}&end={day}")
            temporary = output / "journal.csv.tmp"
            temporary.write_bytes(response.content)
            temporary.replace(output / "journal.csv")
            rows = [row for row in current.get("journal", [])
                    if row.get("local_time", "").startswith(day)]
            app_rows = [row for row in rows if row.get("magic") in APP_MAGICS]
            opened = {row["position_id"] for row in app_rows
                      if row.get("entry") in (0, 2) and row["time"] >= plan["session_requested_at"]}
            write_json(output / "summary.json", {
                "date": day, "timezone": str(BANGKOK), "phase": phase,
                "updated_at": int(time.time()), "session_start": plan["session_requested_at"],
                "deadline": deadline, "session_entries": len(opened),
                "trades_used": monitor.get("trades_used", 0), "max_trades": plan["max_trades"],
                "day_net": current.get("day_net"),
                "app_day_deal_net": round(sum(row.get("net", 0) for row in app_rows), 2),
                "open_app_positions": [row for row in current.get("positions", [])
                                       if row.get("magic") in APP_MAGICS and row.get("symbol") == plan["symbol"]],
                "analytics": current.get("analytics", {}),
                "connected": True, **(session or {}),
                "cost_assumptions": plan["research_assumptions"],
                "note": "Broker Demo results. Research replay is hypothetical; unavailable results remain unavailable."})

    def archive_jobs():
        for name, job in list(jobs.items()):
            if job.get("status") in ("completed", "failed"):
                continue
            result = request("GET", f"/api/research/jobs/{job['id']}").json()
            jobs[name] = result
            if result["status"] == "completed":
                payload = request("GET", f"/api/research/jobs/{job['id']}/result").json()
                write_json(output / f"research-{name}.json", payload)
                if name == "collection" and recorder:
                    recorder.seed_dataset(payload["dataset"])
                    recorder.export_dataset()

    try:
        until = time.monotonic() + 90
        while time.monotonic() < until:
            if not worker.is_alive():
                raise RuntimeError("Dashboard server failed to start")
            try:
                current = state()
                if current.get("connected"):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("MT5 connection did not become ready")
        client.headers["X-Trade-Token"] = current["token"]
        if current.get("scope") != scope or not current["account"]["is_demo"]:
            raise RuntimeError("Expected Demo account is not connected")
        if current["positions"] or current["orders"]:
            raise RuntimeError("Start requires no existing exposures")
        if current["settings"]["kill_switch"]:
            if not plan.get("reset_kill_switch", False):
                raise RuntimeError("The current kill switch is enabled")
            request("POST", "/api/kill", {"active": False})
            current = state()
        write_json(output / "baseline.json", {key: value for key, value in current.items() if key != "token"})
        recorder = SessionRecorder(output, plan, current)
        # Collection requires the execution monitor to be stopped; archive before arming.
        end_at = int(time.time()) - 2
        jobs["collection"] = request("POST", "/api/research/collect", {
            "start_at": end_at - plan.get("collection_hours", 4) * 3600, "end_at": end_at,
            "costs": plan["costs"], "margin_per_lot": plan["margin_per_lot"]}).json()
        until = time.monotonic() + 180
        while time.monotonic() < until:
            archive_jobs()
            if jobs["collection"]["status"] in ("completed", "failed"):
                break
            save(state(), "collecting_history")
            time.sleep(2)
        if jobs["collection"]["status"] not in ("completed", "failed"):
            raise RuntimeError("History collection timed out; execution was not started")
        # Research failures never loosen the execution risk settings.
        if jobs["collection"]["status"] == "completed":
            digest = jobs["collection"]["dataset_hash"]
            jobs["comparison"] = request("POST", "/api/research/jobs", {
                "kind": "compare", "dataset_hash": digest, "provider": "disabled",
                "options": {"strategy": "ema", "train_bars": 200, "validation_bars": 100,
                            "test_bars": 4, "initial_balance": current["account"]["balance"],
                            "latency_seconds": 1, "max_llm_decisions": 20,
                            "require_quote_coverage": True}}).json()
        request("POST", "/api/monitor/start", {
            "scope": scope, "expires_at": deadline, "close_on_expiry": True,
            "max_trades": plan["max_trades"]})
        armed = True
        write_json(output / "armed.json", {"armed_at": int(time.time()), "expires_at": deadline,
                                           "max_trades": plan["max_trades"]})
        while time.time() < deadline:
            try:
                archive_jobs()
                save(state(), "running")
            except Exception as exc:
                write_json(output / "last-error.json", {"time": int(time.time()), "error": str(exc)})
            time.sleep(min(15, max(0, deadline - time.time())))
        halted = False
        while True:
            try:
                if not halted:
                    request("POST", "/api/monitor/stop")
                    request("POST", "/api/kill", {"active": True})
                    halted = True
                current = state()
                save(current, "closing_at_deadline")
                if (current.get("connected") and current.get("scope") == scope
                        and not current["monitor"]["close_on_expiry"]):
                    break
            except Exception as exc:
                write_json(output / "last-error.json", {"time": int(time.time()), "error": str(exc)})
            # Keep the server available if the broker is unavailable at midnight.
            time.sleep(3)
        archive_jobs()
        dataset = recorder.export_dataset()
        if dataset and plan.get("evaluate_session", False):
            try:
                manifest = request("POST", "/api/research/datasets", dataset).json()
                jobs["session-evaluation"] = request("POST", "/api/research/jobs", {
                    "kind": "evaluate", "dataset_hash": manifest["dataset_hash"], "options": {
                        "strategies": ["smc", "supply", "ema", "bollinger", "asian"],
                        "train_bars": 200, "validation_bars": 100, "test_bars": 4,
                        "initial_balance": recorder.baseline["account"]["balance"],
                        "settings": recorder.baseline["settings"],
                        "latency_seconds": 1, "require_quote_coverage": True}}).json()
                until = time.monotonic() + 120
                while time.monotonic() < until:
                    archive_jobs()
                    if jobs["session-evaluation"]["status"] in ("completed", "failed"):
                        break
                    time.sleep(1)
            except Exception as exc:
                write_json(output / "research-final-error.json", {"time": int(time.time()), "error": str(exc)})
        save(state(), "completed")
        write_json(output / "completed.json", {"completed_at": int(time.time()), "expires_at": deadline})
    except BaseException as exc:
        write_json(output / "fatal-error.json", {"time": int(time.time()), "error": str(exc), "armed": armed})
        if armed:
            # Keep durable expiry management alive even if the statistics controller fails.
            try:
                request("POST", "/api/monitor/stop")
            except Exception:
                pass
            worker.join()
        raise
    finally:
        server.should_exit = True
        worker.join(timeout=30)
        client.close()
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    run(parser.parse_args().plan.resolve())


if __name__ == "__main__":
    main()
