from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math

BANGKOK = timezone(timedelta(hours=7))
NAMES = {"smc": "SMC Liquidity Sweep + BOS", "supply": "Supply / Demand Retest",
         "ema": "EMA Trend Pullback", "bollinger": "Bollinger Bands Pullback",
         "asian": "Asian Range Breakout", "ai": "AI Playbook"}
MAGICS = {key: 26001 + i for i, key in enumerate(NAMES)}
APP_MANUAL_MAGIC = 26000
APP_MAGICS = {APP_MANUAL_MAGIC, *MAGICS.values()}


def label(magic, names):
    if str(magic) in names:
        return names[str(magic)]
    return next((NAMES[k] for k, m in MAGICS.items() if m == magic),
                "Manual" if magic in (0, APP_MANUAL_MAGIC) else f"EA #{magic}")


def journal(deals, names):
    rows = []
    for d in deals:
        if d["type"] not in (0, 1):
            continue  # Deposits / credit are cash flows, never trading wins.
        row = dict(d)
        row["strategy"] = label(d["magic"], names)
        row["source"] = "Manual" if d["magic"] in (0, APP_MANUAL_MAGIC) else "EA"
        row["side"] = "BUY" if d["type"] == 0 else "SELL"
        row["entry_label"] = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}.get(d["entry"], "UNKNOWN")
        row["net"] = round(sum(d.get(k, 0) for k in ("profit", "commission", "swap", "fee")), 2)
        row["local_time"] = datetime.fromtimestamp(d["time"], BANGKOK).isoformat()
        rows.append(row)
    return sorted(rows, key=lambda d: (d["time"], d["ticket"]))


def closed_trades(rows, positions):
    """One completed position lifecycle per result; aggregate partial exits and entry fees.

    Reversal (INOUT) deals end one lifecycle and start another. Commission is
    allocated by the closing/opening volume share of that reversal deal.
    """
    active = {p.get("identifier", p["ticket"]) for p in positions}
    groups = defaultdict(list)
    for d in rows:
        groups[d["position_id"]].append(d)
    results = []
    for pid, deals in groups.items():
        lifecycle = None
        remaining = 0.0
        for d in deals:
            if d["entry"] == 0:
                if lifecycle is None:
                    lifecycle = dict(d) | {"net": 0.0, "volume": 0.0, "opened": d["time"]}
                lifecycle["net"] += d["net"]
                lifecycle["volume"] += d["volume"]
                remaining += d["volume"]
            elif d["entry"] in (1, 3) and lifecycle is not None:
                lifecycle["net"] += d["net"]
                remaining -= d["volume"]
                if remaining <= 1e-8:
                    results.append(lifecycle | {"time": d["time"], "exit_price": d["price"]})
                    lifecycle = None
                    remaining = 0.0
            elif d["entry"] == 2 and lifecycle is not None:
                closing = min(remaining, d["volume"])
                costs = sum(d.get(k, 0) for k in ("commission", "fee"))
                share = closing / d["volume"] if d["volume"] else 1
                lifecycle["net"] += d["profit"] + d.get("swap", 0) + costs * share
                results.append(lifecycle | {"time": d["time"], "exit_price": d["price"]})
                remaining = max(0, d["volume"] - closing)
                lifecycle = (dict(d) | {"net": costs * (1 - share), "volume": remaining,
                                        "opened": d["time"]}) if remaining else None
        # Incomplete histories are excluded rather than inventing a closed trade.
        if lifecycle is not None and remaining <= 1e-8 and pid not in active:
            results.append(lifecycle)
    return sorted(results, key=lambda d: d["time"])


def metrics(trades):
    values = [t["net"] for t in trades]
    gains = sum(max(0, x) for x in values)
    losses = -sum(min(0, x) for x in values)
    peak = cumulative = drawdown = 0.0
    streak = max_streak = 0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
        streak = streak + 1 if value < 0 else 0
        max_streak = max(max_streak, streak)
    mean = sum(values) / len(values) if values else 0
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) if values else 0
    return {"trades": len(values), "win_rate": 100 * sum(v > 0 for v in values) / len(values) if values else 0,
            "profit_factor": gains / losses if losses else None, "gross_profit": gains,
            "gross_loss": losses, "net": sum(values), "drawdown": drawdown,
            "expectancy": mean, "stddev": sd, "max_loss_streak": max_streak}


def analytics(rows, positions, curve):
    trades = closed_trades(rows, positions)
    daily = defaultdict(lambda: {"net": 0.0, "trades": 0, "wins": 0})
    for d in rows:
        day = datetime.fromtimestamp(d["time"], BANGKOK).date().isoformat()
        daily[day]["net"] += d["net"]
    for t in trades:
        day = datetime.fromtimestamp(t["time"], BANGKOK).date().isoformat()
        daily[day]["trades"] += 1
        daily[day]["wins"] += t["net"] > 0
    peak = drawdown = percent = 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        drawdown = max(drawdown, peak - point["equity"])
        percent = max(percent, 100 * (peak - point["equity"]) / peak if peak else 0)
    labels = {d["magic"]:d["strategy"] for d in rows}
    open_by_strategy = defaultdict(list)
    for position in positions:
        open_by_strategy[labels.get(position["magic"], label(position["magic"], {}))].append(position)
    strategy_names = {t["strategy"] for t in trades} | set(NAMES.values()) | set(open_by_strategy)
    strategy_rows = []
    for name in sorted(strategy_names):
        opened = open_by_strategy[name]
        strategy_rows.append({"strategy": name, **metrics([t for t in trades if t["strategy"] == name]),
            "open_count": len(opened), "open_volume": sum(p["volume"] for p in opened),
            "floating_pnl": sum(p.get("profit", 0) for p in opened)})
    return {"summary": metrics(trades), "equity_drawdown": drawdown,
            "equity_drawdown_percent": percent,
            "daily": [{"day": k, **v} for k, v in sorted(daily.items())],
            "strategies": strategy_rows,
            "closed_trades": trades}
