import json
import sqlite3
from pathlib import Path

DEFAULTS = {
    "max_lot": 0.10, "risk_percent": 1.0, "daily_loss": 200.0,
    "max_orders": 5, "max_spread": 50.0, "daily_target": 100.0,
    "kill_switch": False, "magic_names": {}, "playbook_threshold": 0.60,
    "reward_ratio": 2.0, "asian_start": 7, "asian_end": 13,
    "enabled_strategies": ["smc", "supply", "ema", "bollinger", "asian", "ai"],
}


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS equity (scope TEXT, time INTEGER, balance REAL,
              equity REAL, PRIMARY KEY(scope,time));
          CREATE TABLE IF NOT EXISTS requests (scope TEXT, id TEXT, value TEXT,
              PRIMARY KEY(scope,id));
          CREATE TABLE IF NOT EXISTS simulation (id INTEGER PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS monitor_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
              scope TEXT, time INTEGER, value TEXT);
          CREATE TABLE IF NOT EXISTS monitor_session (id INTEGER PRIMARY KEY, value TEXT);
        """)
        self.db.commit()

    def settings(self):
        row = self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return DEFAULTS | (json.loads(row[0]) if row else {})

    def save_settings(self, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (1,?)", (json.dumps(value),))
        self.db.commit()

    def sample(self, scope, now, account):
        self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?)",
                        (scope, now // 30 * 30, account["balance"], account["equity"]))
        self.db.commit()

    def curve(self, scope):
        rows = self.db.execute("SELECT time,balance,equity FROM equity WHERE scope=? ORDER BY time DESC LIMIT 3000", (scope,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def request(self, scope, key):
        row = self.db.execute("SELECT value FROM requests WHERE scope=? AND id=?", (scope, key)).fetchone()
        return json.loads(row[0]) if row else None

    def save_request(self, scope, key, value):
        self.db.execute("INSERT OR REPLACE INTO requests VALUES (?,?,?)", (scope, key, json.dumps(value)))
        self.db.commit()

    def simulation(self):
        row = self.db.execute("SELECT value FROM simulation WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def monitor_event(self, scope, now, value):
        self.db.execute("INSERT INTO monitor_events(scope,time,value) VALUES (?,?,?)", (scope, now, json.dumps(value)))
        self.db.commit()

    def monitor_events(self, scope):
        return [{"time": r["time"], **json.loads(r["value"])} for r in self.db.execute(
            "SELECT time,value FROM monitor_events WHERE scope=? ORDER BY id DESC LIMIT 40", (scope,))]

    def unresolved_auto(self, scope):
        return any(json.loads(row[0]).get("pending") for row in self.db.execute(
            "SELECT value FROM requests WHERE scope=? AND id LIKE 'auto-%'", (scope,)))

    def monitor_session(self):
        row = self.db.execute("SELECT value FROM monitor_session WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    def save_monitor_session(self, value):
        self.db.execute("INSERT OR REPLACE INTO monitor_session VALUES (1,?)", (json.dumps(value),))
        self.db.commit()

    def save_simulation(self, value):
        self.db.execute("INSERT OR REPLACE INTO simulation VALUES (1,?)", (json.dumps(value),))
        self.db.commit()
