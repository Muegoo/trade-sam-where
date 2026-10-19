import hashlib
import json
import math
from dataclasses import dataclass

# Keep UTC timestamps within year 9999 after the existing Bangkok risk-day conversion.
MAX_TIMESTAMP = 253402275599


class DatasetError(ValueError):
    pass


def _fail(path, message):
    raise DatasetError(f"{path}: {message}")


def _number(value, path, minimum=None, positive=False):
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
        _fail(path, "must be a finite number")
    if positive and value <= 0 or minimum is not None and value < minimum:
        _fail(path, "out of range")
    return value


def _integer(value, path, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(path, f"must be an integer >= {minimum}")
    return value


def _timestamp(value, path):
    value = _integer(value, path, 1)
    if value > MAX_TIMESTAMP:
        _fail(path, f"must be <= {MAX_TIMESTAMP} (representable through Asia/Bangkok year 9999)")
    return value


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    if len(value) > 500:
        _fail(path, "must be at most 500 characters")
    return value


def _mapping(value, path):
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


@dataclass(frozen=True)
class Dataset:
    _canonical_payload: str
    dataset_hash: str

    def to_dict(self):
        return json.loads(self._canonical_payload)

    def snapshot(self, as_of):
        as_of = _timestamp(as_of, "as_of")
        data = self.to_dict()
        quotes = [q for q in data["quotes"] if q["time"] <= as_of]
        bars = {}
        for frame, rows in data["bars"].items():
            bars[frame] = [b for b in rows if b["time"] + data["bar_seconds"][frame] <= as_of and b["available_at"] <= as_of]
        evidence = [e for e in data["evidence"] if e["published_at"] <= as_of and e["available_at"] <= as_of]
        causal = {"as_of": as_of, "instrument": data["instrument"], "quotes": quotes,
                  "latest_quote": quotes[-1] if quotes else None,
                  "quote_age_seconds": as_of - quotes[-1]["time"] if quotes else None,
                  "bars": bars, "evidence": evidence,
                  "evidence_availability_rule": "published_at and source-verified available_at must be <= as_of; fetched_at is archival provenance",
                  "missing": {"quote": not bool(quotes), "bars": [k for k, v in bars.items() if not v]}}
        causal["snapshot_hash"] = hashlib.sha256(_canonical(causal)).hexdigest()
        causal["dataset_hash"] = self.dataset_hash
        return causal


def _canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def load_dataset(payload):
    p = _mapping(payload, "payload")
    if isinstance(p.get("version"), bool) or not isinstance(p.get("version"), int) or p.get("version") != 1:
        _fail("version", "must be 1")
    source = _text(p.get("source"), "source")
    if p.get("timezone") != "UTC":
        _fail("timezone", "must be UTC")
    inst = _mapping(p.get("instrument"), "instrument")
    symbol = _text(inst.get("symbol"), "instrument.symbol")
    currency = _text(inst.get("currency"), "instrument.currency")
    if inst.get("quote_currency", currency) != currency:
        _fail("instrument.quote_currency", "currency conversion is unsupported")
    instrument = {"symbol": symbol, "currency": currency, "point": _number(inst.get("point"), "instrument.point", positive=True),
                  "tick_size": _number(inst.get("tick_size"), "instrument.tick_size", positive=True),
                  "contract_size": _number(inst.get("contract_size"), "instrument.contract_size", positive=True),
                  "volume_min": _number(inst.get("volume_min"), "instrument.volume_min", positive=True),
                  "volume_max": _number(inst.get("volume_max"), "instrument.volume_max", positive=True),
                  "volume_step": _number(inst.get("volume_step"), "instrument.volume_step", positive=True),
                  "stops_level": _integer(inst.get("stops_level"), "instrument.stops_level"),
                  "margin_per_lot": _number(inst.get("margin_per_lot"), "instrument.margin_per_lot", positive=True)}
    if instrument["volume_max"] < instrument["volume_min"]:
        _fail("instrument.volume_max", "must be >= volume_min")
    costs_raw = _mapping(p.get("costs"), "costs")
    costs = {key: _number(costs_raw.get(key), f"costs.{key}", minimum=0) for key in
             ("commission_per_lot_side", "fee_per_lot_side", "slippage_points")}
    for key in ("swap_long_per_lot_day", "swap_short_per_lot_day"):
        costs[key] = _number(costs_raw.get(key), f"costs.{key}")
    costs["rollover_hour_utc"] = _integer(costs_raw.get("rollover_hour_utc"), "costs.rollover_hour_utc")
    if costs["rollover_hour_utc"] > 23:
        _fail("costs.rollover_hour_utc", "must be 0..23")
    weekdays = costs_raw.get("rollover_weekdays")
    if not isinstance(weekdays, list) or any(_integer(x, "costs.rollover_weekdays[]") > 6 for x in weekdays) or len(set(weekdays)) != len(weekdays):
        _fail("costs.rollover_weekdays", "must contain unique weekdays 0..6")
    costs["rollover_weekdays"] = weekdays
    triple = costs_raw.get("triple_swap_weekday")
    if triple is not None and (_integer(triple, "costs.triple_swap_weekday") > 6 or triple not in weekdays):
        _fail("costs.triple_swap_weekday", "must be a rollover weekday")
    costs["triple_swap_weekday"] = triple
    quotes_raw = p.get("quotes")
    if not isinstance(quotes_raw, list) or not quotes_raw:
        _fail("quotes", "must be a non-empty array")
    quotes = []
    previous = -1
    if len(quotes_raw) > 2_000_000:
        _fail("quotes", "too many records")
    for i, row in enumerate(quotes_raw):
        path = f"quotes[{i}]"; q = _mapping(row, path)
        t = _timestamp(q.get("time"), path + ".time")
        if t <= previous: _fail(path + ".time", "must be strictly increasing")
        previous = t
        if q.get("symbol", symbol) != symbol: _fail(path + ".symbol", "must exactly match instrument symbol")
        bid = _number(q.get("bid"), path + ".bid", positive=True); ask = _number(q.get("ask"), path + ".ask", positive=True)
        if ask <= bid: _fail(path + ".ask", "must exceed bid")
        quotes.append({"time": t, "bid": bid, "ask": ask, "symbol": symbol})
    bars_input = _mapping(p.get("bars", {}), "bars")
    unknown_frames = set(bars_input) - {"M15", "H1"}
    if unknown_frames:
        _fail("bars", f"unsupported timeframe(s): {', '.join(sorted(str(x) for x in unknown_frames))}")
    bars = {}; bar_seconds = {"M15": 900, "H1": 3600}
    for frame, seconds in bar_seconds.items():
        rows = bars_input.get(frame, [])
        if not isinstance(rows, list): _fail(f"bars.{frame}", "must be an array")
        if len(rows) > 2_000_000: _fail(f"bars.{frame}", "too many records")
        output = []; prev_time = prev_available = -1
        for i, row in enumerate(rows):
            path = f"bars.{frame}[{i}]"; b = _mapping(row, path)
            t = _timestamp(b.get("time"), path + ".time"); available = _timestamp(b.get("available_at"), path + ".available_at")
            if t <= prev_time: _fail(path + ".time", "must be strictly increasing")
            if t + seconds > MAX_TIMESTAMP or available < t + seconds or available < prev_available: _fail(path + ".available_at", "must be after close, representable, and monotonic")
            prev_time, prev_available = t, available
            if "symbol" in b and b["symbol"] != symbol: _fail(path + ".symbol", "must exactly match instrument symbol")
            o, h, l, c = (_number(b.get(k), path + "." + k, positive=True) for k in ("open", "high", "low", "close"))
            if h < max(o, c, l) or l > min(o, c): _fail(path, "invalid OHLC geometry")
            item = {"time": t, "available_at": available, "open": o, "high": h, "low": l, "close": c}
            if "volume" in b: item["volume"] = _number(b["volume"], path + ".volume", minimum=0)
            output.append(item)
        bars[frame] = output
    evid_raw = p.get("evidence", [])
    if not isinstance(evid_raw, list) or len(evid_raw) > 100_000: _fail("evidence", "must be an array of at most 100000 records")
    evidence = []; ids = set()
    for i, row in enumerate(evid_raw):
        path = f"evidence[{i}]"; e = _mapping(row, path)
        eid = _text(e.get("id"), path + ".id")
        if eid in ids: _fail(path + ".id", "must be unique")
        ids.add(eid)
        kind = e.get("kind")
        if kind not in ("news", "macro", "calendar"): _fail(path + ".kind", "unsupported kind")
        pub = _timestamp(e.get("published_at"), path + ".published_at"); avail = _timestamp(e.get("available_at"), path + ".available_at"); fetched = _timestamp(e.get("fetched_at"), path + ".fetched_at")
        if not pub <= avail <= fetched: _fail(path, "requires published_at <= available_at <= fetched_at")
        if e.get("symbol") != symbol: _fail(path + ".symbol", "must exactly match instrument symbol")
        value = e.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            _fail(path + ".value", "must be scalar text or finite number")
        if isinstance(value, (int, float)):
            _number(value, path + ".value")
        if isinstance(value, (int, float)):
            _number(value, path + ".value")
        if isinstance(value, str) and len(value) > 20000: _fail(path + ".value", "must be at most 20000 characters")
        item = {"id": eid, "kind": kind, "source": _text(e.get("source"), path + ".source"), "symbol": symbol,
                "published_at": pub, "available_at": avail, "fetched_at": fetched, "value": value}
        if "event_at" in e: item["event_at"] = _timestamp(e["event_at"], path + ".event_at")
        if kind == "calendar" and "event_at" not in item: _fail(path + ".event_at", "required for calendar records")
        if "impact" in e:
            if e["impact"] not in ("low", "medium", "high"): _fail(path + ".impact", "must be low, medium, or high")
            item["impact"] = e["impact"]
        evidence.append(item)
    data = {"version": 1, "source": source, "timezone": "UTC", "instrument": instrument, "costs": costs,
            "quotes": quotes, "bars": bars, "bar_seconds": bar_seconds, "evidence": evidence}
    digest = hashlib.sha256(_canonical(data)).hexdigest()
    return Dataset(_canonical(data).decode(), digest)
