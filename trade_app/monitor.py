"""Demo execution loop. Called under the same lock as manual orders and closing."""
import hashlib
import math
import time
from .broker import BrokerError, BrokerRejected
from .analytics import APP_MAGICS, MAGICS, NAMES
from .risk import preview


class Monitor:
    def __init__(self, store, broker):
        self.store, self.broker = store, broker
        self.enabled = False
        self.scope = None
        self.expires = 0
        self.message = "Monitor ปิดอยู่"
        self.floors = {}
        self.previous_positions = set()
        self.quote = None
        self.quote_changed = time.monotonic()
        saved = store.monitor_session()
        self.close_on_expiry = bool(saved.get("close_on_expiry") and saved.get("symbol") == broker.symbol)
        self.max_positions = store.settings()["max_orders"]
        self.close_retry_at = {}
        self.strategy_status = {}
        if self.close_on_expiry:
            self.scope, self.expires = saved["scope"], saved["expires"]
            self.message = "Auto เปิดไม้ใหม่ปิดอยู่ · กำหนดปิด Position เมื่อหมดเวลายังคงอยู่"

    def persist_session(self):
        self.store.save_monitor_session({"scope": self.scope, "symbol": self.broker.symbol,
            "expires": self.expires, "close_on_expiry": self.close_on_expiry})

    def event(self, message, **extra):
        self.message = message
        self.store.monitor_event(self.scope or "", int(time.time()), {"message": message, **extra})

    def status(self, scope=None):
        return {"enabled": self.enabled, "scope": self.scope, "expires": self.expires,
                "message": self.message, "events": self.store.monitor_events(scope or self.scope or ""),
                "single_position": self.max_positions == 1, "max_positions": self.max_positions,
                "close_on_expiry": self.close_on_expiry, "per_strategy_limit": 1,
                "strategy_status": list(self.strategy_status.values())}

    def suppress(self, signals):
        for signal in signals:
            self.floors[signal["key"]] = max(self.floors.get(signal["key"], 0), signal.get("time", 0))

    def suppress_positions(self, positions, signals):
        magics = {p["magic"] for p in positions}
        self.suppress([s for s in signals if MAGICS.get(s["key"]) in magics])

    def diagnose(self, snap, signals, settings):
        self.strategy_status = {}
        for signal in signals:
            key = signal["key"]
            exposures = [p for p in snap["positions"]+snap["orders"] if p["symbol"] == self.broker.symbol and p.get("magic") == MAGICS.get(key)]
            sample_ids = {d["position_id"] for d in snap["deals"] if d.get("symbol") == self.broker.symbol and d.get("magic") == MAGICS.get(key) and d.get("entry") in (0, 2) and d.get("type") in (0, 1)}
            sample_ids.update(p.get("identifier", p["ticket"]) for p in snap["positions"] if p["symbol"] == self.broker.symbol and p.get("magic") == MAGICS.get(key))
            samples = len(sample_ids)
            status, reason = "READY", "มีสัญญาณใหม่ รอตรวจ Risk"
            if key not in settings["enabled_strategies"] or not signal.get("enabled"):
                status, reason = "DISABLED", "ยังไม่ได้เปิดใช้กลยุทธ์"
            elif exposures:
                status, reason = "HOLDING", "มี Position/Pending ของกลยุทธ์นี้แล้ว (สูงสุด 1 ไม้)"
            elif signal["signal"] == "WAIT":
                status, reason = "WAIT", signal.get("reason", "ยังไม่เข้าเงื่อนไข")
            elif not self.enabled:
                status, reason = "PAUSED", "Monitor ยังไม่เปิด หรือ Session สิ้นสุดแล้ว"
            elif signal.get("time", 0) <= self.floors.get(key, 0):
                status, reason = "WAIT_NEW_BAR", "แท่งนี้เกิดก่อนเริ่ม Monitor หรือประเมินแล้ว รอแท่งใหม่"
            self.strategy_status[key] = {"key": key, "name": NAMES.get(key, key), "status": status,
                "reason": reason, "signal": signal["signal"], "time": signal.get("time"),
                "open_count": len(exposures), "samples": samples}

    def block_ready(self, reason):
        for row in self.strategy_status.values():
            if row["status"] == "READY":
                row.update(status="BLOCKED", reason=reason)

    def start(self, snap, signals, settings, hours=8, close_on_expiry=True, expires_at=None):
        if not snap["account"]["is_demo"]:
            raise BrokerError("Monitor ส่งคำสั่งได้เฉพาะ Demo")
        if settings["kill_switch"] or not snap["account"]["trade_allowed"]:
            raise BrokerError("ปิด Kill Switch และเปิด Algo / Python Trading ก่อนเริ่ม Monitor")
        if not settings["enabled_strategies"]:
            raise BrokerError("เลือกกลยุทธ์อย่างน้อยหนึ่งรายการใน Risk Management")
        if self.store.unresolved_auto(snap["scope"]):
            raise BrokerError("มี Auto request ที่ยังไม่ทราบผล ต้องตรวจ MT5 และแก้สถานะคำสั่งก่อนเริ่มใหม่")
        if expires_at is not None and not time.time() < expires_at <= time.time()+24*3600:
            raise BrokerError("กำหนดสิ้นสุด Session ต้องอยู่ใน 24 ชั่วโมงข้างหน้า")
        self.scope = snap["scope"]
        self.floors = {}
        self.suppress(signals)  # Starting / resuming never replays an existing signal.
        self.previous_positions = {p["ticket"]:p for p in snap["positions"] if p["symbol"] == self.broker.symbol}
        self.quote = None
        self.quote_changed = time.monotonic()
        self.expires = int(expires_at if expires_at is not None else time.time() + hours * 3600)
        self.close_on_expiry = close_on_expiry
        self.max_positions = settings["max_orders"]
        self.persist_session()
        self.enabled = True
        self.diagnose(snap, signals, settings)
        self.event("เริ่ม Auto Monitor · รอสัญญาณจากแท่งใหม่", strategies=settings["enabled_strategies"], hours=hours)

    def stop(self, reason="ผู้ใช้หยุด Auto Monitor"):
        self.enabled = False
        self.event(reason)

    def connection_error(self):
        if self.enabled:
            self.message = "พักส่งคำสั่ง: ข้อมูลขาดการเชื่อมต่อ"

    def step(self, snap, signals, settings):
        self.max_positions = settings["max_orders"]
        self.diagnose(snap, signals, settings)
        if self.close_on_expiry and time.time() >= self.expires:
            self.enabled = False
            if snap["scope"] != self.scope or not snap["account"]["is_demo"]:
                self.message = "รอปิดเมื่อหมดเวลา: ต้องเชื่อมบัญชี Demo เดิม"
                return False
            return self.expire_positions(snap)
        if not self.enabled:
            return False
        if snap["scope"] != self.scope or not snap["account"]["is_demo"]:
            self.stop("หยุด Monitor: บัญชีเปลี่ยนหรือไม่ใช่ Demo")
            return False
        if settings["kill_switch"] or time.time() >= self.expires:
            self.stop("หยุด Monitor: Kill Switch เปิดอยู่หรือครบเวลา Session")
            return False
        current = {p["ticket"]:p for p in snap["positions"] if p["symbol"] == self.broker.symbol}
        removed = set(self.previous_positions) - set(current)
        if removed:
            self.suppress_positions([self.previous_positions[t] for t in removed], signals)
            self.diagnose(snap, signals, settings)
        self.previous_positions = current
        quote = tuple(snap["tick"].get(k) for k in ("time_msc", "time", "bid", "ask"))
        if quote != self.quote:
            self.quote, self.quote_changed = quote, time.monotonic()
        if time.monotonic() - self.quote_changed > 180:
            self.message = "พักส่งคำสั่ง: ไม่มี tick ใหม่เกิน 180 วินาที"
            self.block_ready(self.message)
            return False
        count = len(snap["positions"]) + len(snap["orders"])
        netting_exposed = snap["account"].get("margin_mode") in (0, 1) and (
            current or any(o["symbol"] == self.broker.symbol for o in snap["orders"]))
        if count >= settings["max_orders"] or netting_exposed:
            self.suppress(signals)
            self.message = "รอ Position เดิมปิด: บัญชี Netting" if netting_exposed else f"ครบเพดาน {settings['max_orders']} ไม้แล้ว · รอออเดอร์ปิดและสัญญาณแท่งใหม่"
            self.block_ready(self.message)
            return False
        candidates = [s for s in signals if self.strategy_status[s["key"]]["status"] == "READY"]
        # Hedging accounts evaluate each strategy independently, including opposite sides.
        if snap["account"].get("margin_mode", 2) != 2 and len({s["signal"] for s in candidates}) > 1:
            self.message = "รอ: กลยุทธ์ให้สัญญาณ Buy และ Sell ขัดกัน"
            self.block_ready(self.message)
            return False
        candidates.sort(key=lambda s:self.strategy_status[s["key"]]["samples"])
        self.message = "รอสัญญาณเข้าเงื่อนไข · ดูเหตุผลแยกตามกลยุทธ์" if not candidates else "กำลังตรวจสัญญาณรายกลยุทธ์"
        for signal in candidates:
            row = self.strategy_status[signal["key"]]
            digest = hashlib.sha256(f"{self.scope}:{self.broker.symbol}:{signal['key']}:{signal['time']}".encode()).hexdigest()
            request_id = "auto-" + digest
            if self.store.request(self.scope, request_id):
                row.update(status="EVALUATED", reason="คำสั่งของแท่งนี้ถูกประเมิน/ส่งไปแล้ว ไม่ส่งซ้ำ")
                continue
            try:
                proposal = self.proposal(signal, snap, settings)
                if not proposal["allowed"]:
                    self.message = "พักส่งคำสั่ง: " + "; ".join(proposal["errors"])
                    row.update(status="RISK_BLOCKED", reason="; ".join(proposal["errors"]))
                    continue
                # A durable claim BEFORE broker I/O protects against restart / lost responses.
                self.store.save_request(self.scope, request_id, {"pending": True, "signal": signal["key"]})
                self.event("กำลังส่ง Auto Demo", strategy=signal["key"], side=proposal["side"],
                           lot=proposal["lot"], entry=proposal["entry"], sl=proposal["sl"], tp=proposal["tp"])
                result = self.broker.send(proposal, snap)
                self.store.save_request(self.scope, request_id, result)
                self.floors[signal["key"]] = signal["time"]  # Once per strategy/candle.
                row.update(status="SENT" if result["ok"] else "REJECTED", reason=result.get("message", ""))
                self.event("ส่ง Auto Demo แล้ว" if result["ok"] else "Broker ปฏิเสธ Auto Demo",
                           strategy=signal["key"], result=result)
                return bool(result["ok"])
            except BrokerRejected as exc:
                self.store.save_request(self.scope, request_id, {"ok": False, "message": str(exc)})
                self.floors[signal["key"]] = signal["time"]
                self.event(f"Broker ปฏิเสธ Auto Demo ก่อนส่ง: {exc}", strategy=signal["key"])
                row.update(status="REJECTED", reason=str(exc))
                return False
            except Exception as exc:
                row.update(status="BLOCKED", reason=str(exc))
                if self.store.request(self.scope, request_id):
                    self.stop(f"หยุด Monitor: ผลส่งคำสั่งไม่แน่นอน ตรวจ MT5 / Journal ก่อนเริ่มใหม่ ({exc})")
                    return False
                self.message = f"พักส่งคำสั่ง: {exc}"
        return False

    def expire_positions(self, snap):
        """Close app exposures at the deadline, including after process restart.

        Unknown outcomes are not retried; partial fills are followed using the
        remaining volume in the next snapshot. Explicit rejections retry later.
        """
        positions = [p for p in snap["positions"] if p["symbol"] == self.broker.symbol and p["magic"] in APP_MAGICS]
        orders = [o for o in snap["orders"] if o["symbol"] == self.broker.symbol and o["magic"] in APP_MAGICS]
        if not positions and not orders:
            self.close_on_expiry = False
            self.persist_session()
            self.event("จบ Session · ปิด Position/Pending ของแอปครบแล้ว")
            return False
        self.message = "ครบเวลา Session · กำลังส่งปิด Position ของแอป"
        if not snap["account"]["trade_allowed"]:
            self.message = "ครบเวลาแล้ว รอปิด: MT5 ยังไม่อนุญาต Python Trading"
            return False
        changed = False
        # Cancel pending entries before closing positions, so they cannot refill exposure.
        for order in orders:
            try:
                self.broker.cancel(order["ticket"])
                self.event("ยกเลิก Pending เมื่อหมดเวลา", ticket=order["ticket"])
                changed = True
            except Exception as exc:
                self.message = f"รอยกเลิก Pending เมื่อหมดเวลา: {exc}"
                return changed
        for position in positions:
            digest = hashlib.sha256(f"{self.scope}:{self.expires}:{position['ticket']}:{position['volume']}".encode()).hexdigest()
            key = "expiry-" + digest
            existing = self.store.request(self.scope, key)
            if existing and (existing.get("pending") or existing.get("ok")):
                self.message = f"รอตรวจผลปิด #{position['ticket']} ใน MT5 · ไม่ส่งซ้ำเมื่อผลไม่แน่นอน"
                continue
            if time.monotonic() < self.close_retry_at.get(key, 0):
                continue
            self.close_retry_at[key] = time.monotonic()+15
            self.store.save_request(self.scope, key, {"pending": True})
            try:
                result = self.broker.close_position(position, snap)
                self.store.save_request(self.scope, key, result)
                self.event("ส่งปิดเมื่อหมดเวลาแล้ว" if result["ok"] else "รอปิดเมื่อหมดเวลา: Broker ปฏิเสธ", ticket=position["ticket"], result=result)
                changed = changed or bool(result["ok"])
            except BrokerRejected as exc:
                self.store.save_request(self.scope, key, {"ok": False, "message": str(exc)})
                self.message = f"รอส่งปิด #{position['ticket']} อีกครั้ง: {exc}"
            except Exception as exc:
                self.message = f"ผลปิด #{position['ticket']} ไม่แน่นอน ตรวจ MT5 ก่อนส่งซ้ำ ({exc})"
        return changed

    def proposal(self, signal, snap, settings):
        period = 3600 if signal["key"] == "ai" else 900
        bars = snap["h1"] if signal["key"] == "ai" else snap["bars"][:-1]
        if not len(bars) or int(bars[-1]["time"]) != signal["time"]:
            raise BrokerError("สัญญาณไม่ใช่แท่งปิดล่าสุด")
        if not period <= snap["tick"]["time"] - signal["time"] <= period * 2:
            raise BrokerError("แท่งสัญญาณเก่าเกินไปหรือข้อมูลยังตามราคาไม่ทัน")
        buy = signal["signal"] == "BUY"
        entry = snap["tick"]["ask"] if buy else snap["tick"]["bid"]
        original_risk = abs(signal["entry"] - signal["sl"])
        if not original_risk or abs(entry - signal["entry"]) > original_risk * .5:
            raise BrokerError("ราคาไกลจากสัญญาณเกินครึ่งระยะ SL; รอสัญญาณใหม่")
        size = snap["info"].get("trade_tick_size") or snap["info"]["point"]
        def outward(value, up):
            return round((math.ceil(value/size-1e-8) if up else math.floor(value/size+1e-8))*size, 8)
        sl = outward(signal["sl"], not buy)
        if not (sl < entry if buy else sl > entry):
            raise BrokerError("ราคาผ่าน SL ของสัญญาณแล้ว")
        tp = outward(entry + (1 if buy else -1)*abs(entry-sl)*settings["reward_ratio"], buy)
        return preview({"side": signal["signal"], "kind": "MARKET", "entry": None, "sl": sl,
                        "tp": tp, "lot": None, "strategy": signal["key"]}, snap, settings, self.broker)
