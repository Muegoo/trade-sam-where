"""Closed-candle scanners. Signals are proposals, never automatic orders."""
import pandas as pd
from .analytics import BANGKOK, NAMES


def scan(bars, settings, model_result=None):
    frame = pd.DataFrame(bars)
    if len(frame) < 80:
        return [{"key": k, "name": n, "signal": "WAIT", "reason": "ต้องมีแท่งปิดอย่างน้อย 80 แท่ง"}
                for k, n in NAMES.items()]
    close, high, low = frame.close, frame.high, frame.low
    fast = close.ewm(span=20, adjust=False).mean()
    slow = close.ewm(span=50, adjust=False).mean()
    atr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1).rolling(14).mean().iloc[-1]
    c, prev = frame.iloc[-1], frame.iloc[-2]
    tolerance = float(atr) * 0.25
    output = []

    def add(key, side, reason, stop=None, extra=None):
        enabled = key in settings["enabled_strategies"]
        side = side if enabled else "WAIT"
        entry = float(c.close)
        sl = float(stop if stop is not None else entry + (-1.5 if side == "BUY" else 1.5) * atr)
        valid = side != "WAIT" and ((sl < entry) if side == "BUY" else (sl > entry))
        if side != "WAIT" and not valid:
            side, reason = "WAIT", "ระยะ Stop Loss ไม่เหมาะสม"
        output.append({"key": key, "name": NAMES[key], "enabled": enabled,
                       "signal": side, "reason": reason if enabled else "ปิดกลยุทธ์นี้อยู่",
                       "time": int(c.time), "entry": entry if side != "WAIT" else None,
                       "sl": sl if side != "WAIT" else None,
                       "tp": entry + (1 if side == "BUY" else -1) * abs(entry-sl) * settings["reward_ratio"] if side != "WAIT" else None,
                       **(extra or {})})

    # Sweep must precede BOS; use pre-sweep structure, avoiding current-bar lookahead.
    structure = frame.iloc[-22:-2]
    ceiling, floor = float(structure.high.max()), float(structure.low.min())
    buy = prev.low < floor and prev.close > floor and c.close > ceiling
    sell = prev.high > ceiling and prev.close < ceiling and c.close < floor
    add("smc", "BUY" if buy else "SELL" if sell else "WAIT",
        "Liquidity sweep แล้วปิดทะลุโครงสร้าง" if buy or sell else "รอ sweep และ BOS ในแท่งถัดไป",
        min(prev.low, c.low) if buy else max(prev.high, c.high))

    # Latest historical displacement candle defines a fresh zone; no future bars used.
    zone = None
    for i in range(len(frame)-3, max(15, len(frame)-40), -1):
        impulse, base = frame.iloc[i], frame.iloc[i-1]
        if abs(impulse.close-impulse.open) >= float(atr) * 1.3:
            direction = "BUY" if impulse.close > impulse.open else "SELL"
            zlow, zhigh = float(base.low), float(base.high)
            after = frame.iloc[i+1:-1]
            broken = (after.close < zlow).any() if direction == "BUY" else (after.close > zhigh).any()
            if not broken:
                zone = (direction, zlow, zhigh)
                break
    side, stop = "WAIT", None
    if zone:
        direction, zlow, zhigh = zone
        if direction == "BUY" and c.low <= zhigh and c.close > zhigh and c.close > c.open:
            side, stop = "BUY", zlow - tolerance
        elif direction == "SELL" and c.high >= zlow and c.close < zlow and c.close < c.open:
            side, stop = "SELL", zhigh + tolerance
    add("supply", side, "Retest โซนแล้วมีแท่งยืนยัน" if side != "WAIT" else "รอ retest โซนจาก displacement", stop)

    buy = fast.iloc[-1] > slow.iloc[-1] and c.low <= fast.iloc[-1] + tolerance and c.close > fast.iloc[-1] and c.close > c.open
    sell = fast.iloc[-1] < slow.iloc[-1] and c.high >= fast.iloc[-1] - tolerance and c.close < fast.iloc[-1] and c.close < c.open
    add("ema", "BUY" if buy else "SELL" if sell else "WAIT", "EMA20/50: รอ pullback ตามแนวโน้ม",
        float(low.iloc[-5:].min()) - tolerance if buy else float(high.iloc[-5:].max()) + tolerance)

    mid = close.rolling(20).mean()
    dev = close.rolling(20).std(ddof=0) * 2
    lower, upper = mid-dev, mid+dev
    buy = prev.close < lower.iloc[-2] and c.close > lower.iloc[-1] and c.close < mid.iloc[-1]
    sell = prev.close > upper.iloc[-2] and c.close < upper.iloc[-1] and c.close > mid.iloc[-1]
    add("bollinger", "BUY" if buy else "SELL" if sell else "WAIT", "BB(20,2): รอราคากลับเข้ากรอบหลังทะลุ band")

    times = pd.to_datetime(frame.time, unit="s", utc=True).dt.tz_convert(BANGKOK)
    local = times.iloc[-1]
    mask = (times.dt.date == local.date()) & (times.dt.hour >= settings["asian_start"]) & (times.dt.hour < settings["asian_end"])
    asian = frame[mask]
    side, stop = "WAIT", None
    if local.hour >= settings["asian_end"] and len(asian) >= 3:
        top, bottom = float(asian.high.max()), float(asian.low.min())
        if prev.close <= top < c.close:
            side, stop = "BUY", bottom
        elif prev.close >= bottom > c.close:
            side, stop = "SELL", top
    add("asian", side, f"กรอบ {settings['asian_start']:02}:00–{settings['asian_end']:02}:00 เวลาไทย; รอแท่งปิด breakout", stop)

    result = model_result or {}
    probability = result.get("up_probability")
    side = "WAIT"
    if result.get("validated") and probability is not None:
        threshold = settings["playbook_threshold"]
        side = "BUY" if probability >= threshold else "SELL" if probability <= 1-threshold else "WAIT"
    validation_reason = result.get("error", f"Validation ยังไม่ผ่าน: {result.get('validation_signals', 0)}/20 signals, accuracy {result.get('validation_accuracy', 0)*100:.1f}% / ขั้นต่ำ 55%")
    add("ai", side, "โมเดล logistic regression ผ่าน validation" if result.get("validated") else validation_reason,
        extra={"model": result})
    return output
