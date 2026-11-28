"""Reconcile ordered 1,000-byte TCP records at session close.

The original receiver snapshots and controller decisions remain in windows.jsonl.
Keep only per-session/per-flow totals in memory when checking them against close
receipts. A stream's sequence starts at one in each sending window.
"""

import json
import math


RECORD_BYTES = 1000
DIRECTIONS = (("forward", "server", "client"), ("return", "client", "server"))
FLOW_KEYS = ("sent_records", "received_records", "last_window", "last_sequence")
SAFE_REASONS = {
    "all_records_confirmed", "outstanding_records", "gateway_receipt_unconfirmed",
    "receipt_timeout", "invalid_receipt", "control_closed", "legacy_gateway",
    "control_or_feedback_interrupted", "operator_stop", "storage_full", "session_interrupted",
}
TIMING_KEYS = ("control_rtt_ms", "sample_age_ms", "budget_ms", "effective_max_ms", "wait_ms")


def _count(n):
    return type(n) is int and 0 <= n < 2**64


def _finite(n):
    return type(n) in (int, float) and 0 <= n < 2**63 and math.isfinite(n)


def numeric_fields(row, fields):
    """Allow-list numeric fields for the portable evidence bundle."""
    return {k: row[k] for k in fields if _finite(row.get(k))}


def _flow(row):
    if not isinstance(row, dict):
        raise ValueError("invalid final TCP flow")
    out = {k: row[k] for k in FLOW_KEYS}
    sent, received, window, seq = (out[k] for k in FLOW_KEYS)
    if not _count(sent):
        raise ValueError("invalid final TCP sender count")
    if received is None and window is None and seq is None:
        return out
    if (not all(_count(n) for n in (received, window, seq)) or received > sent
            or seq > received or (received == 0) != (window == 0)
            or (received == 0) != (seq == 0)):
        raise ValueError("invalid final TCP receiver counts")
    return out


def _credited(flow, window, sent):
    if window < flow["last_window"]:
        return sent
    if window == flow["last_window"]:
        return flow["last_sequence"]
    return 0


def final_window_bytes(final, session, direction, index, window, written_records):
    """Return the final observed bytes for one recorded sender cohort, or None."""
    flows = final.get(session, {}).get("directions", {}).get(direction, [])
    if index >= len(flows) or not flows[index].get("window_counts_verified"):
        return None
    return _credited(flows[index], window, written_records) * RECORD_BYTES


def tcp_final_accounting(directory, manifest):
    """Return sanitized close records, source coverage, and aggregate completeness."""
    sessions = {}
    notes = set()
    close_path = directory / "tcp-final.jsonl"
    if close_path.is_file():
        with close_path.open() as stream:
            for line in stream:
                try:
                    r = json.loads(line)
                    key = r["session"]
                    if not _count(key) or key == 0 or key in sessions:
                        raise ValueError("invalid final session number")
                    status = r["status"]
                    if status not in ("complete", "incomplete", "unknown", "unsupported"):
                        raise ValueError("invalid final status")
                    if type(r["stop_acknowledged"]) is not bool:
                        raise ValueError("invalid stop flag")
                    lanes = r["directions"]
                    if not isinstance(lanes, dict) or set(lanes) - {"forward", "return"}:
                        raise ValueError("invalid final direction")
                    directions = {}
                    for direction, flows in lanes.items():
                        if not isinstance(flows, list) or len(flows) > 16:
                            raise ValueError("invalid final TCP flow count")
                        directions[direction] = [_flow(f) for f in flows]
                    sessions[key] = dict(session=key, status=status,
                                         stop_acknowledged=r["stop_acknowledged"],
                                         directions=directions, **numeric_fields(r, TIMING_KEYS))
                    if r.get("reason") in SAFE_REASONS:
                        sessions[key]["reason"] = r["reason"]
                except (KeyError, ValueError, TypeError):
                    notes.add("Unreadable or duplicate final TCP receipt.")
    if not sessions:
        notes.add("Final TCP receipt unavailable.")

    # Validate a high-water mark against the original sender windows before
    # using it to assign bytes. A missing window must never become a zero row.
    totals = {}
    last_window = {}
    bad_windows = set()
    previous_global_window = 0
    invalid_archive = False
    archive = directory / "windows.jsonl"
    if archive.is_file():
        with archive.open() as stream:
            for line in stream:
                try:
                    r = json.loads(line)
                    session, window = r["session"], r["window"]["id"]
                    if not _count(session) or session == 0 or not _count(window) or window == 0:
                        raise ValueError("invalid window ID")
                    # Window IDs are global to the logical run, including across
                    # replacement sessions. Missing earlier reports cannot be
                    # filled from the new connection's receiver receipt.
                    if window != previous_global_window + 1:
                        notes.add("Missing or out-of-order original TCP window.")
                        if session == 1 or session in last_window:
                            bad_windows.add(session)
                    previous_global_window = window
                    if session in last_window and window != last_window[session] + 1:
                        bad_windows.add(session)
                    last_window[session] = window
                    for direction, sender, receiver in DIRECTIONS:
                        if direction not in manifest.get("settings", {}):
                            continue
                        flows = sessions.get(session, {}).get("directions", {}).get(direction)
                        if not flows:
                            continue
                        tx, rx = r[sender]["tcp"], r[receiver]["tcp"]
                        if len(tx) != len(flows) or len(rx) != len(flows):
                            bad_windows.add(session)
                            continue
                        for i, (s, d, flow) in enumerate(zip(tx, rx, flows)):
                            sent = s["sent"]
                            seen = d["received"]["bytes"]
                            if not _count(sent) or not _count(seen) or seen > sent * RECORD_BYTES:
                                raise ValueError("invalid window counts")
                            total = totals.setdefault((session, direction, i), [0, 0])
                            total[0] += sent
                            if flow["received_records"] is not None:
                                credited = _credited(flow, window, sent)
                                if credited > sent or credited * RECORD_BYTES < seen:
                                    bad_windows.add(session)
                                total[1] += credited
                except (KeyError, ValueError, TypeError):
                    invalid_archive = True
                    notes.add("Unreadable TCP window receipt.")
    else:
        notes.add("TCP window recording unavailable.")

    expected_sessions = manifest.get("sessions")
    if (not _count(expected_sessions) or len(sessions) != expected_sessions
            or max(sessions, default=0) != expected_sessions or set(last_window) - sessions.keys()):
        notes.add("Missing session-final TCP coverage.")
    complete = bool(sessions and last_window)
    for session, r in sessions.items():
        if session not in last_window:
            bad_windows.add(session)
        complete &= r["status"] == "complete" and r["stop_acknowledged"]
        for direction in manifest.get("settings", {}):
            flows = r["directions"].get(direction, [])
            complete &= bool(flows)
            for i, flow in enumerate(flows):
                total = totals.get((session, direction, i))
                counted = flow["received_records"]
                matches = (total == [flow["sent_records"], counted]
                           and flow["last_window"] is not None
                           and flow["last_window"] <= last_window.get(session, 0))
                if counted is not None and not matches:
                    bad_windows.add(session)
                complete &= counted == flow["sent_records"] and matches
    if bad_windows:
        notes.add("Final TCP counts could not be reconciled with all original window records.")
    for session, r in sessions.items():
        for direction, flows in r["directions"].items():
            for i, flow in enumerate(flows):
                flow["window_counts_verified"] = (
                    not invalid_archive and "Unreadable or duplicate final TCP receipt." not in notes
                    and session not in bad_windows and flow["received_records"] is not None
                    and (session, direction, i) in totals
                )
                counted = flow["received_records"]
                flow["outstanding_records"] = flow["sent_records"] - counted if counted is not None else None
    return sessions, sorted(notes), bool(complete and not notes)
