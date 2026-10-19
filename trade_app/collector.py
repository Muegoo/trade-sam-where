"""Convert bounded MT5 history into an immutable research dataset."""

import json
import math

from .datasets import DatasetError, load_dataset

MAX_DATASET_BYTES = 8 * 1024 * 1024
MAX_RAW_TICKS = 500_000
MAX_QUOTES = 100_000


def _finite(value, path, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DatasetError(f"{path}: must be a finite number")
    if positive and value <= 0:
        raise DatasetError(f"{path}: must be positive")
    return value


def _integer(value, path, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DatasetError(f"{path}: must be an integer >= {minimum}")
    return value


def _gaps(rows, start, end, threshold):
    internal = []
    internal_count = 0
    durations = []
    for left, right in zip(rows, rows[1:]):
        duration = right["time"] - left["time"]
        durations.append(duration)
        if duration > threshold:
            internal_count += 1
            if len(internal) < 100:
                internal.append({"from": left["time"], "to": right["time"], "seconds": duration})
    first = rows[0]["time"] - start if rows else None
    last = end - rows[-1]["time"] if rows else None
    boundary = []
    for label, duration in (("start", first), ("end", last)):
        if duration is not None and duration > threshold:
            boundary.append({"boundary": label, "seconds": duration})
    return {"internal": internal, "internal_count": internal_count, "boundary": boundary,
            "first_quote_after_start_seconds": first, "last_quote_before_end_seconds": last,
            "maximum_gap_seconds": max(durations + [item["seconds"] for item in boundary], default=0)}


def collect_mt5_dataset(raw_history, costs, margin_per_lot, *, gap_threshold_seconds=60):
    """Build a version-1 dataset and collection report from MT5 history."""
    if not isinstance(raw_history, dict):
        raise DatasetError("raw_history: must be an object")
    symbol = raw_history.get("symbol")
    currency = raw_history.get("currency")
    if not isinstance(symbol, str) or not symbol.strip() or not isinstance(currency, str) or not currency.strip():
        raise DatasetError("raw_history: symbol and account currency are required")
    info = raw_history.get("info")
    if not isinstance(info, dict):
        raise DatasetError("raw_history.info: must be an object")
    if info.get("currency_profit") != currency:
        raise DatasetError("instrument.quote_currency: currency conversion is unsupported")
    bounds = raw_history.get("requested_range")
    if not isinstance(bounds, dict):
        raise DatasetError("requested_range: must be an object")
    start = _integer(bounds.get("start"), "requested_range.start", 1)
    end = _integer(bounds.get("end"), "requested_range.end", 1)
    fetched_at = _integer(raw_history.get("fetched_at"), "fetched_at", 1)
    if start >= end or end > fetched_at:
        raise DatasetError("requested_range: requires start < end <= fetched_at")
    threshold = _integer(gap_threshold_seconds, "gap_threshold_seconds", 1)
    if not isinstance(costs, dict):
        raise DatasetError("costs: all explicit cost fields are required")
    required_costs = {"commission_per_lot_side", "fee_per_lot_side", "slippage_points",
                      "swap_long_per_lot_day", "swap_short_per_lot_day", "rollover_hour_utc",
                      "rollover_weekdays", "triple_swap_weekday"}
    if set(costs) != required_costs:
        raise DatasetError("costs: all and only the explicit dataset cost fields are required")

    instrument = {"symbol": symbol, "currency": currency, "quote_currency": info.get("currency_profit"),
                  "point": info.get("point"), "tick_size": info.get("trade_tick_size"),
                  "contract_size": info.get("trade_contract_size"), "volume_min": info.get("volume_min"),
                  "volume_max": info.get("volume_max"), "volume_step": info.get("volume_step"),
                  "stops_level": info.get("trade_stops_level"), "margin_per_lot": margin_per_lot}
    raw_ticks = raw_history.get("ticks")
    if not isinstance(raw_ticks, list) or not raw_ticks or len(raw_ticks) > MAX_RAW_TICKS:
        raise DatasetError("ticks: must contain 1..500000 records")
    # Stable ordering makes same-millisecond observations deterministic; last observation wins.
    ordered = []
    invalid = 0
    for index, tick in enumerate(raw_ticks):
        if not isinstance(tick, dict):
            raise DatasetError(f"ticks[{index}]: must be an object")
        milliseconds = _integer(tick.get("time_msc"), f"ticks[{index}].time_msc", 1)
        bid, ask = tick.get("bid"), tick.get("ask")
        for key, value in (("bid", bid), ("ask", ask)):
            _finite(value, f"ticks[{index}].{key}")
        if bid <= 0 or ask <= bid:
            invalid += 1
            continue
        ordered.append((milliseconds, index, bid, ask))
    ordered.sort(key=lambda row: (row[0], row[1]))
    per_second = {}
    deduplicated = 0
    for milliseconds, _, bid, ask in ordered:
        timestamp = (milliseconds + 999) // 1000
        if not start <= timestamp <= end:
            continue
        if timestamp in per_second:
            deduplicated += 1
        per_second[timestamp] = {"time": timestamp, "bid": bid, "ask": ask, "symbol": symbol}
    quotes = list(per_second.values())
    if not quotes:
        raise DatasetError("ticks: no valid quotes in requested range")
    if len(quotes) > MAX_QUOTES:
        raise DatasetError("quotes: exceeds 100000 sampled records")

    raw_bars = raw_history.get("bars")
    if not isinstance(raw_bars, dict) or set(raw_bars) != {"M15", "H1"}:
        raise DatasetError("bars: M15 and H1 history are required")
    bars, bar_gaps = {}, {}
    for frame, seconds in (("M15", 900), ("H1", 3600)):
        source = raw_bars[frame]
        if not isinstance(source, list) or len(source) > 20_000:
            raise DatasetError(f"bars.{frame}: must be an array of at most 20000 records")
        rows = []
        for index, bar in enumerate(source):
            if not isinstance(bar, dict):
                raise DatasetError(f"bars.{frame}[{index}]: must be an object")
            opened = _integer(bar.get("time"), f"bars.{frame}[{index}].time", 1)
            if opened + seconds > end:
                continue
            row = {"time": opened, "available_at": opened + seconds,
                   **{key: bar.get(key) for key in ("open", "high", "low", "close")}}
            if "tick_volume" in bar:
                row["volume"] = bar["tick_volume"]
            rows.append(row)
        rows.sort(key=lambda row: row["time"])
        bars[frame] = rows
        bar_gaps[frame] = {"count": sum(1 for a, b in zip(rows, rows[1:]) if b["time"] - a["time"] > seconds),
                           "availability_assumption": "bar is available at open time plus timeframe; source history does not verify original publication time"}
    payload = {"version": 1, "source": "mt5_historical; bar_availability=assumed_close; spec=current; costs=user_supplied", "timezone": "UTC", "instrument": instrument,
               "costs": costs, "quotes": quotes, "bars": bars, "evidence": []}
    try:
        encoded_size = len(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                      allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise DatasetError("dataset: contains non-JSON values") from exc
    if encoded_size > MAX_DATASET_BYTES:
        raise DatasetError("dataset: exceeds 8 MiB canonical limit")
    dataset = load_dataset(payload)
    canonical_size = len(json.dumps(dataset.to_dict(), sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if canonical_size > MAX_DATASET_BYTES:
        raise DatasetError("dataset: exceeds 8 MiB canonical limit")
    return {"dataset": dataset.to_dict(), "dataset_hash": dataset.dataset_hash,
            "collection": {"requested_range": {"start": start, "end": end}, "fetched_at": fetched_at,
                           "raw_tick_count": len(raw_ticks), "invalid_quote_count": invalid,
                           "same_second_deduplicated_count": deduplicated, "sampled_quote_count": len(quotes),
                           "quote_gaps": _gaps(quotes, start, end, threshold), "bar_gaps": bar_gaps,
                           "instrument_specification": "current MT5 specification; historical specifications are not verified",
                           "cost_assumptions": "explicit user supplied costs; historical costs are not inferred",
                           "bar_availability": "assumed timeframe close; original availability is not verified",
                           "dataset_bytes": canonical_size}}
