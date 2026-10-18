import math
import time
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from .analytics import BANGKOK
from .broker import BrokerError


def day_net(deals, now=None):
    today = datetime.fromtimestamp(now or time.time(), BANGKOK).date()
    return sum(sum(d.get(k, 0) for k in ("profit", "commission", "swap", "fee"))
               for d in deals if d["type"] in (0, 1) and datetime.fromtimestamp(d["time"], BANGKOK).date() == today)


def floor_lot(value, step):
    return float((Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR) * Decimal(str(step)))


def preview(order, snap, settings, broker):
    a, info, tick = snap["account"], snap["info"], snap["tick"]
    errors = []
    side, kind = order["side"], order["kind"]
    buy = side == "BUY"
    entry = tick["ask"] if buy else tick["bid"]
    if kind != "MARKET":
        entry = order.get("entry") or 0
    sl, tp = order["sl"], order["tp"]
    if not all(math.isfinite(v) and v > 0 for v in (entry, sl, tp)):
        raise BrokerError("ราคา Entry, SL, TP ต้องเป็นตัวเลขบวก")
    spread = (tick["ask"] - tick["bid"]) / info["point"]
    if settings["kill_switch"]:
        errors.append("Kill Switch เปิดอยู่")
    if not a["is_demo"]:
        errors.append("เวอร์ชันนี้ส่งคำสั่งได้เฉพาะบัญชี Demo")
    if not a["trade_allowed"]:
        errors.append("MT5 ไม่อนุญาต Algo Trading / Python Trading")
    if time.time() - tick["time"] > 180 or tick["bid"] <= 0 or tick["ask"] <= tick["bid"]:
        errors.append("ราคาเก่าหรือไม่ถูกต้อง; รอ tick ล่าสุด")
    if a["equity"] <= 0:
        errors.append("Equity ต้องมากกว่า 0")
    if spread > settings["max_spread"] + 1e-8:
        errors.append("Spread เกินค่าสูงสุด")
    count = len(snap["positions"]) + len(snap["orders"])
    if count >= settings["max_orders"]:
        errors.append("จำนวน Position + Pending ทั้งบัญชีเต็มแล้ว")
    if a.get("margin_mode") in (0, 1) and any(p["symbol"] == broker.symbol for p in snap["positions"] + snap["orders"]):
        errors.append("บัญชี Netting มี exposure ในสัญลักษณ์นี้แล้ว; จัดการ Position / Pending ใน MT5 ก่อนเปิดเพิ่ม")
    if not (sl < entry < tp if buy else tp < entry < sl):
        errors.append("SL / TP อยู่ผิดด้านของ Entry")
    distance = info["trade_stops_level"] * info["point"]
    reference = (tick["bid"] if buy else tick["ask"]) if kind == "MARKET" else entry
    if (reference-sl < distance or tp-reference < distance) if buy else (sl-reference < distance or reference-tp < distance):
        errors.append("SL / TP ใกล้ราคาเกิน stop level ของ Broker")
    if kind == "LIMIT" and not (entry < tick["ask"] if buy else entry > tick["bid"]):
        errors.append("Buy Limit ต้องต่ำกว่า Ask; Sell Limit ต้องสูงกว่า Bid")
    if kind == "STOP" and not (entry > tick["ask"] if buy else entry < tick["bid"]):
        errors.append("Buy Stop ต้องสูงกว่า Ask; Sell Stop ต้องต่ำกว่า Bid")
    if kind != "MARKET" and abs(entry-(tick["ask"] if buy else tick["bid"])) < distance:
        errors.append("Pending Entry ใกล้ตลาดเกิน stop level")
    tick_size = info.get("trade_tick_size") or info["point"]
    for key, value in (("Entry", entry), ("SL", sl), ("TP", tp)):
        if abs(value/tick_size-round(value/tick_size)) > 1e-6:
            errors.append(f"{key} ต้องตรง tick size {tick_size}")
    unit = info["volume_min"]
    loss_unit = abs(broker.profit(side, broker.symbol, unit, entry, sl))
    if loss_unit <= 0:
        raise BrokerError("ระยะ SL ต้องมากกว่า 0")
    budget = max(0, a["equity"]) * settings["risk_percent"] / 100
    max_volume = min(settings["max_lot"], info["volume_max"])
    suggested = floor_lot(min(budget/loss_unit*unit, max_volume), info["volume_step"])
    lot = order.get("lot") if order.get("lot") is not None else suggested
    if not math.isfinite(lot) or lot <= 0:
        raise BrokerError("Lot ที่คำนวณได้ต่ำกว่าขั้นต่ำ; ปรับ SL หรือค่าความเสี่ยง")
    if lot < unit or lot > max_volume + 1e-9:
        errors.append(f"Lot ต้องอยู่ระหว่าง {unit} ถึง {max_volume}")
    if abs(lot/info["volume_step"] - round(lot/info["volume_step"])) > 1e-6:
        errors.append(f"Lot ต้องเพิ่มทีละ {info['volume_step']}")
    loss = loss_unit * lot / unit
    reward = broker.profit(side, broker.symbol, lot, entry, tp)
    margin = broker.margin(side, broker.symbol, lot, entry)
    if loss > budget + 1e-8:
        errors.append("Risk ต่อไม้เกินค่าที่กำหนด")
    if margin > a["margin_free"]:
        errors.append("Free Margin ไม่เพียงพอ")
    realized = day_net(snap["deals"])
    daily_used = max(0, -realized) + sum(max(0, -p.get("profit", 0)) for p in snap["positions"])
    # Reserve downside to all existing SLs; an unprotected exposure blocks new orders.
    exposure = 0.0
    for p in snap["positions"] + snap["orders"]:
        if not p.get("sl"):
            errors.append(f"#{p['ticket']} ไม่มี SL; ต้องกำหนด SL ก่อนเพิ่มความเสี่ยง")
            continue
        pside = "BUY" if p["type"] in (0, 2, 4, 6) else "SELL"
        baseline = p.get("price_current", p["price_open"])
        exposure += max(0, -broker.profit(pside, p["symbol"], p["volume"], baseline, p["sl"]))
    if daily_used >= settings["daily_loss"]:
        errors.append("ถึงขีดจำกัดขาดทุนรายวันแล้ว")
    if daily_used + exposure + loss > settings["daily_loss"] + 1e-8:
        errors.append("ความเสี่ยงรวมของออเดอร์เดิมและออเดอร์ใหม่เกินงบขาดทุนรายวัน")
    return dict(order) | {"entry": entry, "lot": lot, "suggested_lot": suggested,
                          "loss": loss, "reward": reward, "rr": reward/loss,
                          "risk_percent": 100*loss/a["equity"] if a["equity"] > 0 else 0,
                          "margin": margin, "spread": spread, "daily_used": daily_used,
                          "reserved_risk": exposure, "allowed": not errors, "errors": errors,
                          "currency": a["currency"], "scope": snap["scope"]}
