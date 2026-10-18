from datetime import datetime, timezone
import math
import time
from .analytics import MAGICS, APP_MANUAL_MAGIC

UTC = timezone.utc


class BrokerError(RuntimeError):
    pass


class BrokerRejected(BrokerError):
    """Failure before order_send; no execution can have occurred."""


class MT5Broker:
    mode = "mt5"

    def __init__(self, symbol):
        import MetaTrader5
        self.mt5 = MetaTrader5
        self.symbol = symbol

    def snapshot(self):
        m = self.mt5
        if not m.initialize(timeout=10000):
            raise BrokerError(f"เชื่อม MT5 ไม่สำเร็จ: {m.last_error()}")
        account, terminal = m.account_info(), m.terminal_info()
        if account is None or terminal is None or not terminal.connected:
            raise BrokerError("กรุณาเปิด MT5 และล็อกอินบัญชี Demo")
        if not m.symbol_select(self.symbol, True):
            raise BrokerError(f"ไม่พบ {self.symbol}; ตรวจชื่อใน Market Watch")
        info, tick = m.symbol_info(self.symbol), m.symbol_info_tick(self.symbol)
        positions, orders = m.positions_get(), m.orders_get()
        deals = m.history_deals_get(datetime(2000, 1, 1, tzinfo=UTC), datetime.now(UTC))
        bars = m.copy_rates_from_pos(self.symbol, m.TIMEFRAME_M15, 0, 600)
        h1 = m.copy_rates_from_pos(self.symbol, m.TIMEFRAME_H1, 1, 1600)
        if any(x is None for x in (info, tick, positions, orders, deals, bars, h1)):
            raise BrokerError(f"ข้อมูล MT5 ไม่ครบ: {m.last_error()}")
        a = account._asdict()
        a["is_demo"] = account.trade_mode == m.ACCOUNT_TRADE_MODE_DEMO
        a["trade_allowed"] = bool(a.get("trade_allowed") and a.get("trade_expert") and terminal.trade_allowed and not terminal.tradeapi_disabled)
        return {"account": a, "info": info._asdict(), "tick": tick._asdict(),
                "positions": [p._asdict() for p in positions], "orders": [o._asdict() for o in orders],
                "deals": [d._asdict() for d in deals],
                "bars": [{k: (int(b[k]) if k in ("time", "tick_volume", "spread", "real_volume") else float(b[k])) for k in bars.dtype.names} for b in bars],
                "h1": h1, "scope": f"mt5:{account.server}:{account.login}"}

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
        if info is None or tick is None or time.time()-tick.time > 180:
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
