"""Create an offline TCP evidence ZIP using only selected measurement fields."""
import argparse
import csv
import io
import json
from pathlib import Path
import re
import tempfile
import zipfile


def counts(run, rows, notes):
    source = run / "windows.jsonl"
    if not source.is_file():
        notes.append("Window recording unavailable.")
        return
    with source.open() as stream:
        for number, line in enumerate(stream, 1):
            try:
                r = json.loads(line)
                w = r["window"]
                window_rows = []
                for direction, lane, sender, receiver in (
                    ("forward", "forward", "server", "client"),
                    ("return", "reverse", "client", "server"),
                ):
                    if not w["rates"][lane]:
                        continue
                    tx, rx = r[sender]["tcp"], r[receiver]["tcp"]
                    if len(tx) != len(rx):
                        raise ValueError("flow count mismatch")
                    for i, (a, b) in enumerate(zip(tx, rx), 1):
                        received = b["received"]
                        stats = a.get("tcp_stats") or {}
                        values = [r["session"], w["id"], r["start_ms"], r["decision_ms"],
                                  w["ms"], w["rates"][lane], i, a["sent"] * 1000,
                                  received["window_bytes"], received["bytes"],
                                  received["arrival_window_bytes"], a.get("retransmissions"),
                                  stats.get("srtt_us"), stats.get("cwnd_bytes")]
                        if any(v is not None and type(v) is not int for v in values):
                            raise ValueError("invalid measurement")
                        window_rows.append([direction, *values])
                rows.writerows(window_rows)
            except (KeyError, ValueError, TypeError):
                notes.append("Unreadable window receipt at line {}.".format(number))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run", type=Path)
    p.add_argument("--profile", choices=("baseline", "higher-forward", "higher-return"))
    p.add_argument("--exit-code", type=int)
    args = p.parse_args()
    run = args.run.resolve()
    if not run.is_dir() and args.exit_code is None:
        p.error("run directory does not exist")
    notes = []
    summary = {"profile": args.profile, "client_exit": args.exit_code}
    try:
        m = json.loads((run / "session.json").read_text())
        patterns = {"native_version": r"edgewalker(?:-native)? [0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?", "started_utc": r"[0-9TZ.:+\-]+",
                    "ended_utc": r"[0-9TZ.:+\-]+", "status": r"running|completed|completed_with_gaps|failed|interrupted"}
        for key, pattern in patterns.items():
            value = m.get(key)
            if isinstance(value, str) and re.fullmatch(pattern, value):
                summary[key] = value
        for key in ("sessions", "duration_s", "stop_acknowledged"):
            if type(m.get(key)) in (int, float, bool):
                summary[key] = m[key]
    except (OSError, ValueError):
        notes.append("Session manifest unavailable or unreadable.")
    data = io.StringIO()
    rows = csv.writer(data)
    rows.writerow(("direction", "session", "window", "start_ms", "decision_ms", "duration_ms",
                   "direction_offer_bps", "flow", "written_bytes", "cutoff_bytes", "through_drain_bytes",
                   "all_cohorts_at_cutoff_bytes", "sender_retransmissions", "sender_kernel_rtt_us",
                   "sender_cwnd_bytes"))
    counts(run, rows, notes)
    if len(data.getvalue().splitlines()) == 1:
        notes.append("No TCP flow rows available.")
    summary["evidence_notes"] = notes
    events = io.StringIO()
    writer = csv.writer(events)
    writer.writerow(("utc", "at_ms", "direction", "event"))
    source = run / "events.csv"
    if source.is_file():
        with source.open(newline="") as stream:
            for row in csv.DictReader(stream):
                kind = row.get("event")
                if kind in {"coverage_gap", "replacement_wait", "session_ready", "operator_stop",
                            "interface_state", "manual_resume_required", "manual_ready_restart"}:
                    if (re.fullmatch(r"[0-9TZ.:+\-]+", row.get("utc", ""))
                            and row.get("at_ms", "").isdigit()
                            and row.get("direction") in {"", "forward", "return"}):
                        writer.writerow([row[k] for k in ("utc", "at_ms", "direction", "event")])
    with tempfile.NamedTemporaryFile(prefix="field-review-", suffix=".zip", dir=run.parent, delete=False) as out:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("session-summary.json", json.dumps(summary, indent=2))
            archive.writestr("tcp-windows.csv", data.getvalue())
            archive.writestr("events.csv", events.getvalue())
    print("Review ZIP:", out.name)
    for note in notes:
        print(note)
    return int(bool(notes))


if __name__ == "__main__":
    raise SystemExit(main())
