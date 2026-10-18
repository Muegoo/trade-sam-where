"""XAU/USD demo-only research alerts from closed H1 bars in MetaTrader 5.

This program never sends an order. The statistical model is a research aid,
not a claim that its signals are profitable.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


BANGKOK = timezone(timedelta(hours=7))
UTC = timezone.utc
BAR_COUNT = 1600
VALIDATION_BARS = 250
CSV_PATH = Path("signals.csv")


def allowed_now(now: datetime) -> bool:
    local = now.astimezone(BANGKOK)
    return local.weekday() < 5 and 19 <= local.hour < 22


def feature_rows(bars):
    """Build features from information available at each closed bar only."""
    closes = [float(bar["close"]) for bar in bars]
    rows = []
    for i in range(24, len(bars)):
        price = closes[i]
        if price <= 0 or any(closes[j] <= 0 for j in range(i - 24, i + 1)):
            continue
        high = float(bars[i]["high"])
        low = float(bars[i]["low"])
        open_price = float(bars[i]["open"])
        mean_24 = sum(closes[i - 23 : i + 1]) / 24
        rows.append(
            (
                i,
                [
                    price / closes[i - 1] - 1,
                    price / closes[i - 3] - 1,
                    price / closes[i - 6] - 1,
                    price / closes[i - 12] - 1,
                    price / mean_24 - 1,
                    (high - low) / price,
                    (price - open_price) / price,
                ],
            )
        )
    return rows


def classify(probability: float) -> str:
    if probability >= 0.60:
        return "WATCH_BUY"
    if probability <= 0.40:
        return "WATCH_SELL"
    return "WAIT"


def analyze(bars):
    """Train on earlier bars, check later bars, then score the latest bar."""
    features = feature_rows(bars)
    if len(features) < 600 + VALIDATION_BARS + 1:
        raise ValueError("Not enough closed H1 bars; need at least 875 usable bars")

    x = [item[1] for item in features[:-1]]
    y = [int(float(bars[index + 1]["close"]) > float(bars[index]["close"])) for index, _ in features[:-1]]
    split = len(x) - VALIDATION_BARS
    if len(set(y[:split])) < 2:
        raise ValueError("Historical labels contain only one class")

    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    model.fit(x[:split], y[:split])
    validation_probabilities = model.predict_proba(x[split:])[:, 1]
    selected = [j for j, p in enumerate(validation_probabilities) if classify(float(p)) != "WAIT"]
    correct = sum(
        (classify(float(validation_probabilities[j])) == "WATCH_BUY") == bool(y[split + j])
        for j in selected
    )
    validation_accuracy = correct / len(selected) if selected else 0.0

    # Weak historical validation results suppress actionable alerts.
    validated = len(selected) >= 20 and validation_accuracy >= 0.55
    model.fit(x, y)
    probability = float(model.predict_proba([features[-1][1]])[0, 1])
    raw_signal = classify(probability)
    signal = raw_signal if validated else "WAIT"
    return {
        "signal": signal,
        "raw_signal": raw_signal,
        "up_probability": probability,
        "validation_signals": len(selected),
        "validation_accuracy": validation_accuracy,
        "validated": validated,
    }


def append_signal(row: dict) -> None:
    fields = ["bar_utc", "symbol", "signal", "up_probability", "validation_signals", "validation_accuracy"]
    exists = CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row[key] for key in fields})


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo-only gold signal monitor; never sends orders")
    parser.add_argument("--symbol", default=os.getenv("MT5_GOLD_SYMBOL", "XAUUSD"))
    parser.add_argument("--once", action="store_true", help="Run one analysis regardless of local time")
    args = parser.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MT5 alerts require Windows and requirements-mt5.txt. The paper dashboard works without MT5.")
        return 2

    if not mt5.initialize():
        print(f"MT5 connection failed: {mt5.last_error()}")
        return 1
    try:
        account = mt5.account_info()
        if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            print("Stopped: connect MT5 to a DEMO account before running this monitor.")
            return 2
        if not mt5.symbol_select(args.symbol, True):
            print(f"Symbol unavailable: {args.symbol}; check its exact name in Market Watch.")
            return 3
        print(f"Connected to DEMO account. Monitoring {args.symbol} H1, 19:00–22:00 Bangkok time.")
        last_bar = None
        while True:
            now = datetime.now(UTC)
            if args.once or allowed_now(now):
                # MT5 position zero is the forming candle. Start at one.
                bars = mt5.copy_rates_from_pos(args.symbol, mt5.TIMEFRAME_H1, 1, BAR_COUNT)
                if bars is None or len(bars) < 900:
                    print(f"Insufficient history or MT5 error: {mt5.last_error()}")
                else:
                    bars = sorted(bars, key=lambda bar: int(bar["time"]))
                    bar_utc = datetime.fromtimestamp(int(bars[-1]["time"]), UTC).isoformat()
                    if bar_utc != last_bar:
                        try:
                            result = analyze(bars)
                        except ValueError as exc:
                            print(f"Analysis skipped: {exc}")
                        else:
                            row = {
                                "bar_utc": bar_utc,
                                "symbol": args.symbol,
                                "signal": result["signal"],
                                "up_probability": f"{result['up_probability']:.3f}",
                                "validation_signals": result["validation_signals"],
                                "validation_accuracy": f"{result['validation_accuracy']:.3f}",
                            }
                            print(row)
                            append_signal(row)
                            last_bar = bar_utc
            if args.once:
                break
            time.sleep(30)
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
