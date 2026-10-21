from datetime import datetime, timezone
import math
import time
from datetime import timedelta
from .analytics import MAGICS, APP_MANUAL_MAGIC

UTC = timezone.utc


class BrokerError(RuntimeError):
    pass


class BrokerRejected(BrokerError):
    """Failure before order_send; no execution can have occurred."""


def validate_linear_research_model(mt5, symbol, info, tick, currency):
    try:
        info = info._asdict() if hasattr(info, "_asdict") else info
        tick = tick._asdict() if hasattr(tick, "_asdict") else tick
        if info.get("currency_profit") != currency:
            raise BrokerError("Unsupported profit currency conversion")
        point, tick_size = float(info["point"]), float(info["trade_tick_size"])
        contract_size, volume_min = float(info["trade_contract_size"]), float(info["volume_min"])
        bid, ask = float(tick["bid"]), float(tick["ask"])
    except BrokerError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise BrokerError("Invalid MT5 instrument specification or quote") from None
    if not all(math.isfinite(value) and value > 0 for value in
               (point, tick_size, contract_size, volume_min, bid, ask)) or ask <= bid:
        raise BrokerError("Invalid MT5 instrument specification")
    for order_type, entry, exit_price in (
            (mt5.ORDER_TYPE_BUY, ask, ask + 10 * tick_size),
            (mt5.ORDER_TYPE_BUY, ask, ask - 10 * tick_size),
            (mt5.ORDER_TYPE_SELL, bid, bid - 10 * tick_size),
            (mt5.ORDER_TYPE_SELL, bid, bid + 10 * tick_size)):
        if exit_price <= 0:
            raise BrokerError("Instrument prices do not support the linear dataset model")
        linear = ((exit_price - entry) if order_type == mt5.ORDER_TYPE_BUY else (entry - exit_price)) * contract_size * volume_min
        try:
            actual = float(mt5.order_calc_profit(order_type, symbol, volume_min, entry, exit_price))
        except (TypeError, ValueError, OverflowError):
            actual = math.nan
        if not math.isfinite(actual) or not math.isclose(actual, linear, rel_tol=1e-6, abs_tol=1e-4):
            raise BrokerError("Instrument P&L is not supported by the linear dataset model")
    return True


class MT5Broker:
    mode = "mt5"

    def __init__(self, symbol, time_offset_seconds=0):
        if type(time_offset_seconds) is not int or abs(time_offset_seconds) > 14 * 3600:
            raise ValueError("MT5 timestamp offset must be integer seconds within +/-14 hours")
        import MetaTrader5
        self.mt5 = MetaTrader5
        self.symbol = symbol
        self.time_offset_seconds = time_offset_seconds
        self._initialized = False
        self._retry_at = 0
        self._retry_delay = 3
        self._connection_error = None

    def connection_failed(self, message):
        # Discard the IPC handle after terminal restart; never retry broker writes.
        self._initialized = False
        try:
            self.mt5.shutdown()
        except Exception:
            pass  # Preserve the original connection error if the stale handle cannot close.
        finally:
            delay = getattr(self, "_retry_delay", 3)
            self._retry_at = time.monotonic() + delay
            self._retry_delay = min(30, delay * 2)
            self._connection_error = message
        raise BrokerError(message)

    def connect(self):
        if getattr(self, "_initialized", False):
            return
        if time.monotonic() < getattr(self, "_retry_at", 0):
            raise BrokerError(self._connection_error + " · รอเชื่อมต่อใหม่")
        if not self.mt5.initialize(timeout=10000):
            self.connection_failed(f"เชื่อม MT5 ไม่สำเร็จ: {self.mt5.last_error()}")
        self._initialized = True

    def utc_record(self, record):
        """Normalize an explicitly configured broker clock; never infer an offset."""
        offset = getattr(self, "time_offset_seconds", 0)
        seconds = {"time", "time_update", "time_setup", "time_done", "time_expiration"}
        millis = {"time_msc", "time_update_msc", "time_setup_msc", "time_done_msc"}
        return {key: value - offset * (1000 if key in millis else 1)
                if key in seconds | millis and value > 0 else value
                for key, value in record.items()}

    def snapshot(self):
        m = self.mt5
        self.connect()
        account, terminal = m.account_info(), m.terminal_info()
        if account is None or terminal is None or not terminal.connected:
            self.connection_failed(f"MT5 ขาดการเชื่อมต่อ: {m.last_error()} · กรุณาเปิด MT5 และล็อกอินบัญชี Demo")
        if not m.symbol_select(self.symbol, True):
            raise BrokerError(f"ไม่พบ {self.symbol}; ตรวจชื่อใน Market Watch")
        info, tick = m.symbol_info(self.symbol), m.symbol_info_tick(self.symbol)
        positions, orders = m.positions_get(), m.orders_get()
        shift = timedelta(seconds=getattr(self, "time_offset_seconds", 0))
        deals = m.history_deals_get(datetime(2000, 1, 1, tzinfo=UTC) + shift, datetime.now(UTC) + shift)
        bars = m.copy_rates_from_pos(self.symbol, m.TIMEFRAME_M15, 0, 600)
        h1 = m.copy_rates_from_pos(self.symbol, m.TIMEFRAME_H1, 1, 1600)
        if any(x is None for x in (info, tick, positions, orders, deals, bars, h1)):
            self.connection_failed(f"ข้อมูล MT5 ไม่ครบ: {m.last_error()}")
        self._retry_delay, self._retry_at, self._connection_error = 3, 0, None
        a = account._asdict()
        a["is_demo"] = account.trade_mode == m.ACCOUNT_TRADE_MODE_DEMO
        a["trade_allowed"] = bool(a.get("trade_allowed") and a.get("trade_expert") and terminal.trade_allowed and not terminal.tradeapi_disabled)
        h1 = h1.copy()
        h1["time"] -= getattr(self, "time_offset_seconds", 0)
        return {"account": a, "info": info._asdict(), "tick": self.utc_record(tick._asdict()),
                "positions": [self.utc_record(p._asdict()) for p in positions],
                "orders": [self.utc_record(o._asdict()) for o in orders],
                "deals": [self.utc_record(d._asdict()) for d in deals],
                "bars": [self.utc_record({k: (int(b[k]) if k in ("time", "tick_volume", "spread", "real_volume") else float(b[k])) for k in bars.dtype.names}) for b in bars],
                "h1": h1, "scope": f"mt5:{account.server}:{account.login}"}

    def validate_research_instrument(self, info, tick, currency):
        return validate_linear_research_model(self.mt5, self.symbol, info, tick, currency)

    def research_history(self, start, end):
        """Read bounded UTC history without initializing or shutting down MT5."""
        m = self.mt5
        if (not isinstance(start, datetime) or not isinstance(end, datetime) or
                start.tzinfo is None or end.tzinfo is None or
                start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0) or
                start.microsecond or end.microsecond or start >= end):
            raise BrokerError("Research range must be whole-second UTC datetimes with start < end")
        now = datetime.now(UTC).replace(microsecond=0)
        if end > now or end - start > timedelta(days=31):
            raise BrokerError("Research range must end by now and span at most 31 days")
        account, terminal = m.account_info(), m.terminal_info()
        if account is None or terminal is None or not terminal.connected:
            raise BrokerError("กรุณาเปิด MT5 และล็อกอินบัญชี Demo")
        if account.trade_mode != m.ACCOUNT_TRADE_MODE_DEMO:
            raise BrokerError("Research history is available only on a demo account")
        if not m.symbol_select(self.symbol, True):
            raise BrokerError(f"ไม่พบ {self.symbol}; ตรวจชื่อใน Market Watch")
        info = m.symbol_info(self.symbol)
        tick = m.symbol_info_tick(self.symbol)
        if info is None or tick is None:
            raise BrokerError(f"ข้อมูล MT5 ไม่ครบ: {m.last_error()}")
        currency = account.currency
        self.validate_research_instrument(info, tick, currency)

        ticks = []
        shift = timedelta(seconds=getattr(self, "time_offset_seconds", 0))
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(hours=1), end)
            batch = m.copy_ticks_range(self.symbol, cursor + shift, chunk_end + shift, m.COPY_TICKS_INFO)
            if batch is None:
                raise BrokerError(f"MT5 tick history unavailable: {m.last_error()}")
            if len(batch) > 500_000 - len(ticks):
                raise BrokerError("MT5 tick history exceeds 500000 records")
            ticks.extend(batch)
            cursor = chunk_end
        if len(ticks) > 500_000:
            raise BrokerError("MT5 tick history exceeds 500000 records")
        rates = {}
        for frame, timeframe, warmup, seconds in (
                ("M15", m.TIMEFRAME_M15, 1200, 900), ("H1", m.TIMEFRAME_H1, 1200, 3600)):
            bar_start = max(datetime(1970, 1, 1, tzinfo=UTC), start - timedelta(seconds=warmup * seconds))
            rows = m.copy_rates_range(self.symbol, timeframe, bar_start + shift, end + shift)
            if rows is None:
                raise BrokerError(f"MT5 {frame} history unavailable: {m.last_error()}")
            if len(rows) > 20_000:
                raise BrokerError(f"MT5 {frame} history exceeds 20000 bars")
            rates[frame] = [self.utc_record({key: (int(row[key]) if key in ("time", "tick_volume", "spread", "real_volume") else float(row[key]))
                             for key in rows.dtype.names}) for row in rows]
        return {"symbol": self.symbol, "currency": currency, "info": info._asdict(),
                "ticks": [self.utc_record({key: int(row[key]) if key in ("time", "time_msc", "flags") else float(row[key])
                           for key in row.dtype.names}) for row in ticks],
                "bars": rates, "requested_range": {"start": int(start.timestamp()), "end": int(end.timestamp())},
                "timestamp_offset_seconds": getattr(self, "time_offset_seconds", 0),
                "fetched_at": int(now.timestamp())}

    def profit(self, side, symbol, lot, entry, exit_price):
        m = self.mt5
        value = m.order_calc_profit(m.ORDER_TYPE_BUY if side == "BUY" else m.ORDER_TYPE_SELL, symbol, lot, entry, exit_price)
        if value is None:
            raise BrokerError(f"คำนวณความเสี่ยงไม่ได้: {m.last_error()}")
        return value

    def margin(self, side, symbol, lot, entry):
        m = self.mt5
        value = m.order_calc_margin(m.ORDER_TYPE_BUY if side == "BUY" else m.ORDER_TYPE_SELL, symbol, lot, entry)
        if value is None:
            raise BrokerError("คำนวณ Margin ไม่สำเร็จ")
        return value

    def send(self, proposal, snapshot):
        m = self.mt5
        side, kind = proposal["side"], proposal["kind"]
        order_type = getattr(m, f"ORDER_TYPE_{side}" + (f"_{kind}" if kind != "MARKET" else ""))
        info = snapshot["info"]
        if kind != "MARKET":
            filling = m.ORDER_FILLING_RETURN
        elif info["filling_mode"] & 1:
            filling = m.ORDER_FILLING_FOK
        elif info["filling_mode"] & 2:
            filling = m.ORDER_FILLING_IOC
        elif info["trade_exemode"] != m.SYMBOL_TRADE_EXECUTION_MARKET:
            filling = m.ORDER_FILLING_RETURN
        else:
            raise BrokerError("ไม่มี filling mode ที่รองรับ")
        request = {"action": m.TRADE_ACTION_DEAL if kind == "MARKET" else m.TRADE_ACTION_PENDING,
                   "symbol": self.symbol, "volume": proposal["lot"], "type": order_type,
                   "price": proposal["entry"], "sl": proposal["sl"], "tp": proposal["tp"],
                   "deviation": 20, "magic": MAGICS.get(proposal["strategy"], APP_MANUAL_MAGIC),
                   "comment": "TradeDesk " + proposal["strategy"],
                   "type_time": m.ORDER_TIME_GTC, "type_filling": filling}
        return self._execute(request)

    def _execute(self, request):
        m = self.mt5
        check = m.order_check(request)
        if check is None or check.retcode != 0:
            raise BrokerRejected(f"Broker ปฏิเสธ order_check: {check.comment if check else m.last_error()}")
        result = m.order_send(request)  # Never retry automatically: outcome may be uncertain.
        if result is None:
            raise BrokerError("ไม่ทราบผลส่งคำสั่ง ตรวจใน MT5 ก่อนส่งใหม่")
        if result.retcode in (10012, 10031):
            raise BrokerError("หมดเวลาหรือขาดการเชื่อมต่อระหว่างส่งคำสั่ง ผลอาจไม่แน่นอน ตรวจ MT5 ก่อนส่งใหม่")
        return {"ok": result.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED, m.TRADE_RETCODE_DONE_PARTIAL),
                "retcode": result.retcode, "message": result.comment, "order": result.order,
                "deal": result.deal, "volume": result.volume, "price": result.price}

    def close_position(self, position, snapshot):
        m = self.mt5
        info, tick = m.symbol_info(position["symbol"]), m.symbol_info_tick(position["symbol"])
        age = time.time() - (tick.time - getattr(self, "time_offset_seconds", 0)) if tick else float("inf")
        if info is None or tick is None or not -5 <= age <= 180:
            raise BrokerRejected("ไม่พบราคาล่าสุดสำหรับปิด Position")
        if info.filling_mode & 1:
            filling = m.ORDER_FILLING_FOK
        elif info.filling_mode & 2:
            filling = m.ORDER_FILLING_IOC
        elif info.trade_exemode != m.SYMBOL_TRADE_EXECUTION_MARKET:
            filling = m.ORDER_FILLING_RETURN
        else:
            raise BrokerRejected("ไม่มี filling mode สำหรับปิด Position")
        buy = position["type"] == m.ORDER_TYPE_BUY
        return self._execute({"action": m.TRADE_ACTION_DEAL, "symbol": position["symbol"],
            "position": position["ticket"], "volume": position["volume"],
            "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY,
            "price": tick.bid if buy else tick.ask, "deviation": 20,
            "magic": position["magic"], "comment": "TradeDesk close",
            "type_time": m.ORDER_TIME_GTC, "type_filling": filling})

    def cancel(self, ticket):
        result = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": ticket})
        if result is None or result.retcode != self.mt5.TRADE_RETCODE_DONE:
            raise BrokerError(f"ยกเลิก pending #{ticket} ไม่สำเร็จ")

    def close(self):
        self.mt5.shutdown()
        self._initialized = False


class SimulationBroker:
    """Explicit paper account, independent of MT5. Persist orders and fills."""
    mode = "simulation"

    def __init__(self, symbol, store):
        self.symbol, self.store = symbol, store
        self.data = store.simulation() or self.seed()
        self.store.save_simulation(self.data)

    def seed(self):
        now = int(time.time())
        deals = []
        for i in range(72):
            opened = now - (73-i) * 8 * 3600
            magic = list(MAGICS.values())[i % 6] if i % 7 else 0
            profit = round(28 + (i % 9)*9 if i % 5 not in (0, 3) else -24 - (i % 7)*8, 2)
            entry = 2600 + math.sin(i)*15
            for exit_deal in (False, True):
                deals.append({"ticket": 1000+i*2+int(exit_deal), "order": 1000+i*2+int(exit_deal),
                    "position_id": 500+i, "time": opened+int(exit_deal)*3600, "symbol": self.symbol,
                    "magic": magic, "type": int(exit_deal), "entry": int(exit_deal),
                    "volume": .02, "price": entry+(profit/2 if exit_deal else 0),
                    "profit": profit if exit_deal else 0, "commission": -.07, "swap": 0, "fee": 0,
                    "comment": "ตัวอย่างจำลอง"})
        return {"deals": deals, "positions": [], "orders": [], "next": 20000}

    def bars(self, period, count):
        end = int(time.time()) // period * period
        def price(t):
            x = t / 900
            return 2640 + 16*math.sin(x/25) + 6*math.sin(x/4) + 2*math.cos(x*1.7)
        return [{"time": t, "open": round(price(t-period), 2), "close": round(price(t), 2),
                 "high": round(max(price(t-period), price(t))+1.4, 2),
                 "low": round(min(price(t-period), price(t))-1.4, 2), "tick_volume": 100+i%250}
                for i, t in enumerate(range(end-(count-1)*period, end+1, period))]

    def record(self, p, price, exiting=False):
        ticket = self.data["next"]
        self.data["next"] += 1
        self.data["deals"].append({"ticket": ticket, "order": ticket, "position_id": p["ticket"],
            "time": int(time.time()), "symbol": self.symbol, "magic": p["magic"],
            "type": 1-p["type"] if exiting else p["type"], "entry": int(exiting),
            "volume": p["volume"], "price": price,
            "profit": self.profit("BUY" if p["type"] == 0 else "SELL", self.symbol, p["volume"], p["price_open"], price) if exiting else 0,
            "commission": round(-p["volume"]*3.5, 2), "swap": 0, "fee": 0, "comment": "Paper trade"})

    def snapshot(self):
        bars = self.bars(900, 600)
        bid = round(bars[-1]["close"] + .12*math.sin(time.time()/15), 2)
        ask = round(bid+.20, 2)
        for o in list(self.data["orders"]):
            buy = o["type"] in (2, 4)
            triggered = (ask <= o["price_open"] if o["type"] == 2 else bid >= o["price_open"] if o["type"] == 3 else ask >= o["price_open"] if o["type"] == 4 else bid <= o["price_open"])
            if triggered:
                self.data["orders"].remove(o)
                p = dict(o) | {"type": 0 if buy else 1}
                self.data["positions"].append(p)
                self.record(p, p["price_open"])
        for p in list(self.data["positions"]):
            mark = bid if p["type"] == 0 else ask
            p["price_current"] = mark
            p["profit"] = self.profit("BUY" if p["type"] == 0 else "SELL", self.symbol, p["volume"], p["price_open"], mark)
            if (p["type"] == 0 and (mark <= p["sl"] or mark >= p["tp"])) or (p["type"] == 1 and (mark >= p["sl"] or mark <= p["tp"])):
                self.record(p, mark, True)
                self.data["positions"].remove(p)
        balance = 10000 + sum(sum(d.get(k, 0) for k in ("profit", "commission", "swap", "fee")) for d in self.data["deals"])
        profit = sum(p["profit"] for p in self.data["positions"])
        self.store.save_simulation(self.data)
        return {"account": {"login": 100001, "server": "Paper Simulation", "currency": "USD", "balance": balance,
                            "equity": balance+profit, "credit": 0, "profit": profit, "margin_free": balance+profit,
                            "is_demo": True, "trade_allowed": True},
                "info": {"point": .01, "digits": 2, "trade_tick_size": .01, "volume_min": .01,
                         "volume_max": 100, "volume_step": .01, "trade_stops_level": 20},
                "tick": {"bid": bid, "ask": ask, "time": int(time.time())},
                "positions": [dict(p) for p in self.data["positions"]],
                "orders": [dict(o) for o in self.data["orders"]], "deals": list(self.data["deals"]),
                "bars": bars, "h1": self.bars(3600, 1601)[:-1], "scope": "simulation:100001"}

    def profit(self, side, symbol, lot, entry, exit_price):
        return (exit_price-entry) * (1 if side == "BUY" else -1) * lot * 100

    def margin(self, side, symbol, lot, entry):
        return lot * entry * 100 / 100

    def send(self, proposal, snapshot):
        ticket = self.data["next"]
        self.data["next"] += 1
        p = {"ticket": ticket, "identifier": ticket, "symbol": self.symbol,
             "type": (0 if proposal["side"] == "BUY" else 1) + {"MARKET": 0, "LIMIT": 2, "STOP": 4}[proposal["kind"]],
             "volume": proposal["lot"], "price_open": proposal["entry"], "sl": proposal["sl"],
             "tp": proposal["tp"], "magic": MAGICS.get(proposal["strategy"], APP_MANUAL_MAGIC), "time": int(time.time()), "profit": 0}
        if proposal["kind"] == "MARKET":
            self.data["positions"].append(p)
            self.record(p, p["price_open"])
        else:
            self.data["orders"].append(p)
        self.store.save_simulation(self.data)
        return {"ok": True, "order": ticket, "message": "บันทึก Paper order แล้ว", "volume": p["volume"], "price": p["price_open"]}

    def cancel(self, ticket):
        self.data["orders"] = [o for o in self.data["orders"] if o["ticket"] != ticket]
        self.store.save_simulation(self.data)

    def close_position(self, position, snapshot):
        live = next((p for p in self.data["positions"] if p["ticket"] == position["ticket"]), None)
        if live is None:
            raise BrokerError("Position ปิดไปแล้ว")
        price = snapshot["tick"]["bid"] if live["type"] == 0 else snapshot["tick"]["ask"]
        self.record(live, price, True)
        self.data["positions"].remove(live)
        self.store.save_simulation(self.data)
        return {"ok": True, "order": position["ticket"], "volume": position["volume"],
                "price": price, "message": "ปิด Paper Position แล้ว"}

    def close(self):
        pass
