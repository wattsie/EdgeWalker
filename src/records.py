"""Incremental session records and offline loss accounting."""

import csv
import io
import os
import json
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .tcp_receipts import tcp_final_accounting
else:
    from tcp_receipts import tcp_final_accounting


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    tmp.replace(path)


class Records:
    def __init__(self, directory, manifest):
        self.gaps = 0
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.manifest = dict(
            manifest, format_version=1, started_utc=timestamp(), status="running"
        )
        self.save()
        self.reserve = self.directory / ".finalise-reserve"
        with self.reserve.open("xb") as f:
            f.write(bytes(65536))
            f.flush()
            os.fsync(f.fileno())

    def append(self, filename, data):
        with (self.directory / filename).open("ab", buffering=0) as f:
            previous = f.tell()
            try:
                encoded = data.encode("utf-8")
                if f.write(encoded) != len(encoded):
                    raise OSError(28, "incomplete evidence write")
            except OSError:
                f.truncate(previous)
                raise

    def save(self):
        atomic_json(self.directory / "session.json", self.manifest)

    def csv(self, filename, row):
        path = self.directory / filename
        first = not path.exists()
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=list(row))
        if first:
            writer.writeheader()
        writer.writerow(row)
        self.append(filename, out.getvalue())

    def event(self, kind, at, direction="", detail=""):
        if kind == "coverage_gap":
            self.gaps += 1
        self.csv(
            "events.csv",
            dict(
                utc=timestamp(),
                at_ms=at,
                direction=direction,
                event=kind,
                detail=detail,
            ),
        )

    def series(self, direction, row):
        self.csv("series-fl.csv" if direction == "forward" else "series-rl.csv", row)

    def sample(self, value):
        self.append("windows.jsonl", json.dumps(value, separators=(",", ":")) + "\n")

    def finish(self, status, reason):
        self.reserve.unlink(missing_ok=True)
        self.manifest.update(
            status=status, termination_reason=reason, ended_utc=timestamp()
        )
        self.save()


def report(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "session.json").read_text())
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("status"), str)
            or not isinstance(manifest.get("settings"), dict)
            or not manifest["settings"] or set(manifest["settings"]) - {"forward", "return"}):
        raise ValueError("invalid session manifest")
    result = dict(status=manifest["status"], directions={})
    corrections = {}
    if (directory / "udp-corrections.csv").exists():
        with (directory / "udp-corrections.csv").open() as f:
            corrections = {
                (r["direction"], r["session"], r["window"]): r
                for r in csv.DictReader(f)
            }

    # Earlier recorders omitted corrections carried in late feedback windows.
    # Replay those authenticated endpoint records without changing the receipt.
    archive = directory / "windows.jsonl"
    if archive.exists():
        with archive.open() as records:
            for line in records:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if line.endswith("\n"):
                        raise
                    break
                if not isinstance(record, dict):
                    raise ValueError("invalid archived receiver report")
                for direction, endpoint in (("forward", "client"), ("return", "server")):
                    receiver = record.get(endpoint, {})
                    if not isinstance(receiver, dict):
                        raise ValueError("invalid archived endpoint")
                    prior = receiver.get("prior_udp")
                    if prior is not None and not isinstance(prior, dict):
                        raise ValueError("invalid UDP correction")
                    if prior and prior["sent"]:
                        key = (direction, str(record["session"]), str(prior["window"]))
                        corrections.setdefault(key, prior)

    pending_ranges = []
    for name, filename in (("forward", "series-fl.csv"), ("return", "series-rl.csv")):
        if name not in manifest["settings"]:
            continue
        path = directory / filename
        sums = {}
        rows = measured_ms = cohort_bytes = 0
        last_reason = last_clean = None
        has_cohort = True
        if path.exists():
            with path.open() as f:
                for row in csv.DictReader(f):
                    rows += 1
                    corr = corrections.get(
                        (name, row.get("session"), row.get("window"))
                    )
                    if corr:
                        if int(corr["sent"]) != int(row["udp_sent"]):
                            raise ValueError("UDP sender range mismatch")
                        row.update(udp_pending=corr["pending"], udp_lost=corr["lost"])

                    last_reason = row.get("reason")
                    last_clean = int(row["last_clean_bps"]) if row.get("last_clean_bps") else None
                    measured_ms += int(row["end_ms"]) - int(row["start_ms"])
                    if row.get("udp_sent"):
                        size = manifest["settings"][name].get("udp_size", 1000)
                        if row.get("cohort_received_bytes") and corr:
                            cohort_bytes += (int(corr["sent"]) - int(corr["pending"]) - int(corr["lost"])
                                             + int(corr["reordered"])) * size
                        elif row.get("cohort_received_bytes"):
                            cohort_bytes += int(row["cohort_received_bytes"])
                        elif int(row["udp_sent"]):
                            has_cohort = False
                    if int(row.get("udp_pending") or 0):
                        pending_ranges.append(
                            dict(
                                direction=name,
                                start_ms=int(row["start_ms"]),
                                end_ms=int(row["end_ms"]),
                                pending=int(row["udp_pending"]),
                            )
                        )
                    groups = ["whole", row["phase"]]
                    if row.get("link_state") != "down":
                        groups.append("outside_observed_interface_down")
                    for phase in groups:
                        entry = sums.setdefault(
                            phase,
                            dict(
                                udp_sent=0, udp_lost=0, udp_pending=0, received_bytes=0
                            ),
                        )
                        entry["udp_pending"] += int(row.get("udp_pending") or 0)
                        entry["udp_sent"] += int(row["udp_sent"] or 0)
                        entry["udp_lost"] += int(row["udp_lost"] or 0)
                        entry["received_bytes"] += int(row["received_bytes"])
        for entry in sums.values():
            n = entry["udp_sent"]
            entry["udp_loss_pct"] = (
                100 * entry["udp_lost"] / n if n and not entry["udp_pending"] else None
            )
        result["directions"][name] = dict(
            windows=rows, last_reason=last_reason, last_verified_clean_bps=last_clean,
            active_delivered_bps=sums["whole"]["received_bytes"] * 8000 // measured_ms if measured_ms else None,
            udp_cohort_bytes=cohort_bytes if has_cohort and sums.get("whole", {}).get("udp_sent") else None,
            udp_cohort_bps=cohort_bytes * 8000 // measured_ms if has_cohort and measured_ms and sums["whole"]["udp_sent"] else None,
            phases=sums,
            steady_udp_loss_pct=sums.get("steady", {}).get("udp_loss_pct"),
        )
    gaps = []
    if (directory / "events.csv").exists():
        with (directory / "events.csv").open() as f:
            gaps = [row for row in csv.DictReader(f) if row["event"] == "coverage_gap"]
    result["pending_ranges"] = pending_ranges
    result["coverage_gaps"] = gaps
    result["coverage_complete"] = (
        manifest["status"] == "completed"
        and not gaps
        and all(
            d["windows"] and not d["phases"]["whole"]["udp_pending"]
            for d in result["directions"].values()
        )
    )
    if manifest.get("mode") in ("tcp", "combined"):
        sessions, notes, complete = tcp_final_accounting(directory, manifest)
        result["tcp_final"] = dict(sessions=list(sessions.values()), evidence_notes=notes,
                                   coverage_complete=complete)
        result["coverage_complete"] &= complete
    return result
