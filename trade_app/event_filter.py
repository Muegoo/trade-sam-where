"""Point-in-time filtering for known scheduled high-impact events."""

from .datasets import MAX_TIMESTAMP


def _integer(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum if maximum is not None else MAX_TIMESTAMP}")
    return value


def event_blackout(evidence, symbol, as_of, before_minutes=0, after_minutes=0):
    """Return known matching calendar events whose half-open blackout contains as_of."""
    as_of = _integer(as_of, "as_of", 1, MAX_TIMESTAMP)
    before_minutes = _integer(before_minutes, "before_minutes", 0, 1440)
    after_minutes = _integer(after_minutes, "after_minutes", 0, 1440)
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be non-empty text")
    if not isinstance(evidence, list):
        raise ValueError("evidence must be an array")
    known_calendar = False
    active = []
    windows = []
    for index, row in enumerate(evidence):
        if not isinstance(row, dict):
            raise ValueError(f"evidence[{index}] must be an object")
        if row.get("kind") != "calendar" or row.get("symbol") != symbol:
            continue
        if not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError(f"evidence[{index}].id must be non-empty text")
        pub, available = row.get("published_at"), row.get("available_at")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 or v > MAX_TIMESTAMP for v in (pub, available)):
            raise ValueError(f"evidence[{index}] has invalid publication timestamps")
        if pub > available or available > as_of:
            continue
        known_calendar = True
        if row.get("impact") != "high" or "event_at" not in row:
            continue
        event_at = _integer(row["event_at"], f"evidence[{index}].event_at", 1, MAX_TIMESTAMP)
        start = event_at - before_minutes * 60
        end = event_at + after_minutes * 60
        if start < end:
            windows.append((start, end, row["id"]))
        if start <= as_of < end:
            active.append({"id": row["id"], "event_at": event_at,
                           "reason": str(row.get("value", "scheduled high-impact event"))[:20000],
                           "window_start": start, "window_end": end})
    active.sort(key=lambda event: (event["event_at"], event["id"]))
    connected_ids = []
    next_allowed = None
    if active:
        windows.sort(key=lambda item: (item[0], item[1], item[2]))
        component_start = component_end = None
        component_ids = []
        for start, end, event_id in windows:
            if component_end is None or start > component_end:
                if component_start is not None and component_start <= as_of < component_end:
                    next_allowed = component_end
                    connected_ids = component_ids
                    break
                component_start, component_end, component_ids = start, end, [event_id]
            else:
                component_end = max(component_end, end)
                component_ids.append(event_id)
        if next_allowed is None and component_start is not None and component_start <= as_of < component_end:
            next_allowed = component_end
            connected_ids = component_ids
    return {"blocked": bool(active), "status": "known_calendar" if known_calendar else "not_configured",
            "events": active, "event_ids": [event["id"] for event in active],
            "reasons": [event["reason"] for event in active],
            "blocking_window_event_ids": connected_ids, "next_allowed_at": next_allowed}
