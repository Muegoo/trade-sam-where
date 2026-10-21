"""Session-scoped statistics and causal market capture, independent of calendar days."""
import csv
import json
import math
from collections import defaultdict

from .analytics import APP_MAGICS, BANGKOK, closed_trades, metrics
from .datasets import load_dataset


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def export_csv(path, rows, fields):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
                             for key, value in row.items()})
    temporary.replace(path)


class SessionRecorder:
    def __init__(self, output, plan, baseline):
        self.output, self.plan, self.baseline = output, plan, baseline
        self.initial_risk = {}
        account = baseline["account"]
        self.peak = account.get("equity", account["balance"])
        self.drawdown = self.drawdown_percent = 0.0
        self.connection = None
        self.disconnected_at = None
        self.disconnected_seconds = 0
        self.quotes = {}
        self.bars = {"M15": {}, "H1": {}}
        self.seed = None

    def seed_dataset(self, dataset):
        self.seed = dataset
        for row in dataset["quotes"]:
            self.quotes.setdefault(row["time"], row)
        for frame, rows in dataset["bars"].items():
            for row in rows:
                self.bars[frame][row["time"]] = row

    def capture(self, current, now):
        valid = bool(current.get("connected") and current.get("scope") == self.plan["expected_scope"])
        if valid != self.connection:
            with (self.output / "connection-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": now, "connected": valid, "error": current.get("error")}, ensure_ascii=False) + "\n")
            if not valid:
                self.disconnected_at = now
            elif self.disconnected_at is not None:
                self.disconnected_seconds += max(0, now - self.disconnected_at)
                self.disconnected_at = None
            self.connection = valid
        if not valid:
            return None
        for pos in current.get("positions", []):
            if pos.get("symbol") == self.plan["symbol"] and pos.get("magic") in APP_MAGICS and pos.get("sl"):
                contract = current.get("info", {}).get("trade_contract_size")
                if contract and current["account"].get("currency") == current.get("info", {}).get("currency_profit"):
                    self.initial_risk.setdefault(pos.get("identifier", pos["ticket"]),
                        abs(pos["price_open"] - pos["sl"]) * pos["volume"] * contract)
        tick = current.get("tick", {})
        if (tick.get("bid", 0) > 0 and tick.get("ask", 0) > tick["bid"] and
                -5 <= now - tick.get("time", 0) <= 180):
            stamp = math.ceil(now)
            quote = {"time": stamp, "bid": tick["bid"], "ask": tick["ask"]}
            if stamp not in self.quotes:
                self.quotes[stamp] = quote
                with (self.output / "market-quotes.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(quote | {"source_tick_time": tick["time"]}) + "\n")
        for bar in current.get("bars", []):
            opened = int(bar["time"])
            if opened + 900 > now or opened in self.bars["M15"]:
                continue
            row = {key: bar[key] for key in ("time", "open", "high", "low", "close")}
            row["available_at"] = max(opened + 900, math.ceil(now))
            self.bars["M15"][opened] = row
        grouped = defaultdict(list)
        for bar in self.bars["M15"].values():
            grouped[bar["time"] // 3600 * 3600].append(bar)
        for opened, bars in grouped.items():
            bars.sort(key=lambda b: b["time"])
            if opened in self.bars["H1"] or [b["time"] for b in bars] != [opened + i * 900 for i in range(4)]:
                continue
            self.bars["H1"][opened] = {"time": opened, "available_at": max(b["available_at"] for b in bars),
                "open": bars[0]["open"], "high": max(b["high"] for b in bars),
                "low": min(b["low"] for b in bars), "close": bars[-1]["close"]}
        start = self.plan["session_requested_at"]
        all_rows = current.get("journal", [])
        ids = {row["position_id"] for row in all_rows if row.get("symbol") == self.plan["symbol"]
               and row.get("magic") in APP_MAGICS and row.get("entry") in (0, 2)
               and start <= row["time"] < self.plan["expires_at"]}
        rows = [row for row in all_rows if row["position_id"] in ids]
        positions = [pos for pos in current.get("positions", []) if pos.get("identifier", pos["ticket"]) in ids]
        trades = closed_trades(rows, positions)
        for trade in trades:
            risk = self.initial_risk.get(trade["position_id"])
            trade["initial_risk_cash"] = risk
            trade["net_r"] = trade["net"] / risk if risk else None
        equity = current["account"].get("equity", current["account"]["balance"])
        self.peak = max(self.peak, equity)
        self.drawdown = max(self.drawdown, self.peak - equity)
        self.drawdown_percent = max(self.drawdown_percent, 100 * (self.peak - equity) / self.peak if self.peak else 0)
        names = {row["strategy"] for row in rows}
        strategy_rows = []
        for name in sorted(names):
            selected = [t for t in trades if t["strategy"] == name]
            values = [t["net_r"] for t in selected if t["net_r"] is not None]
            strategy_rows.append({"strategy": name, **metrics(selected),
                "win_rate": metrics(selected)["win_rate"] if selected else None,
                "sum_r": sum(values) if values else None, "r_samples": len(values)})
        measured = [t["net_r"] for t in trades if t["net_r"] is not None]
        session_metrics = metrics(trades)
        if not trades:
            session_metrics["win_rate"] = None
        result = {"date": self.plan["date"], "timezone": str(BANGKOK), "updated_at": now,
            "session_start": start, "deadline": self.plan["expires_at"], "currency": current["account"].get("currency"),
            "session_entries": len(ids), "session_net": round(sum(row.get("net", 0) for row in rows), 2),
            "session_metrics": session_metrics, "session_strategies": strategy_rows,
            "session_initial_balance": self.baseline["account"]["balance"],
            "session_final_balance": current["account"]["balance"], "session_equity": equity,
            "session_equity_drawdown": self.drawdown, "session_equity_drawdown_percent": self.drawdown_percent,
            "open_app_positions": positions, "closed_positions": len(trades),
            "disconnected_seconds_observed": self.disconnected_seconds,
            "sum_r": sum(measured) if measured else None,
            "r_samples": sum(t["net_r"] is not None for t in trades), "trades": trades,
            "note": "Position lifecycles opened during this session; includes exits after midnight. Missing risk samples remain null. Equity drawdown is sampled for this account during this session."}
        atomic_json(self.output / "session-results.json", result | {"deals": rows})
        fields = ["ticket", "position_id", "local_time", "symbol", "side", "entry_label", "volume", "price", "strategy", "net", "commission", "swap", "fee", "comment"]
        export_csv(self.output / "session-journal.csv", rows, fields)
        export_csv(self.output / "session-trades.csv", trades,
                   ["position_id", "strategy", "side", "volume", "opened", "time", "net", "initial_risk_cash", "net_r"])
        return result

    def export_dataset(self):
        if self.seed is None or not self.quotes:
            return None
        payload = dict(self.seed, source="mt5_demo_session;historical_warmup_and_observed_quotes;costs=explicit",
            quotes=sorted(self.quotes.values(), key=lambda row: row["time"]),
            bars={frame: sorted(rows.values(), key=lambda row: row["time"]) for frame, rows in self.bars.items()})
        dataset = load_dataset(payload)
        atomic_json(self.output / "research-session-dataset.json", dataset.to_dict())
        return dataset.to_dict()
