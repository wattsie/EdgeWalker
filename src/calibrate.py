"""Bounded UDP calibration using independent authenticated measurement sessions."""

import csv
import json
import math
import signal
import subprocess
import sys
import time
from pathlib import Path
from statistics import median
from types import SimpleNamespace

from records import report

PHASES = ("baseline", "reference", "candidate", "verify_reference", "verify_candidate")
FIELDS = (
    "feedback_ms",
    "hold_ms",
    "suspend_ms",
    "ramp_step_bps",
    "probe_step_bps",
    "probe_ms",
    "early_loss_count",
)
FILES = {"forward": "series-fl.csv", "return": "series-rl.csv"}


def add_command(sub):
    p = sub.add_parser(
        "calibrate", help="calibrate UDP using fresh receiver measurements"
    )
    p.add_argument("--connect", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--config", type=Path)
    p.add_argument(
        "--duration",
        type=float,
        required=True,
        help="total seconds across all sessions",
    )
    p.add_argument(
        "--window-ms",
        type=int,
        help="override automatic bounded window (250..10000 ms)",
    )
    p.add_argument(
        "--check-ceiling",
        action="store_true",
        help="reserve time for a fresh session starting at each requested ceiling with normal backoff",
    )
    for direction in FILES:
        p.add_argument("--" + direction + "-max", type=int)


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    tmp.replace(path)


def read_rows(path):
    if not path.exists():
        return []
    with path.open() as stream:
        return list(csv.DictReader(stream))


def measure(directory, direction, ceiling, code):
    rows = read_rows(directory / FILES[direction])
    events = read_rows(directory / "events.csv")
    result = report(directory) if (directory / "session.json").exists() else {}
    total = (
        result.get("directions", {})
        .get(direction, {})
        .get("phases", {})
        .get("whole", {})
    )
    clean = [
        r
        for r in rows
        if int(r["udp_sent"]) >= 3
        and int(r["received_bytes"]) > 0
        and not any(
            int(r[k] or 0) for k in ("udp_lost", "udp_pending", "udp_reordered")
        )
        and r["last_clean_bps"]
        and int(r["last_clean_bps"]) == int(r["offer_bps"])
    ]
    spans = [int(r["decision_ms"]) - int(r["start_ms"]) for r in clean]
    duration = sum(int(r["end_ms"]) - int(r["start_ms"]) for r in rows)
    decisions = [int(r["decision_ms"]) for r in rows]
    span = decisions[-1] - int(rows[0]["start_ms"]) if rows else 0
    return dict(
        report_intervals_ms=[b - a for a, b in zip(decisions, decisions[1:])],
        gaps=len(result.get("coverage_gaps", [])),
        successful_restarts=sum(e["event"] == "receiver_ready_restart" for e in events),
        observed_span_ms=round(span),
        measured_ms=duration,
        whole_budget_bps=(
            total.get("received_bytes", 0)
            * 8
            / json.loads((directory / "session.json").read_text())["duration_s"]
        )
        if result
        else 0,
        exit_code=code,
        windows=len(rows),
        clean_windows=len(clean),
        clean_feedback_ms=spans,
        complete=result.get("coverage_complete", False),
        clean_rates=sorted({int(r["offer_bps"]) for r in clean}),
        goodput_bps=total.get("received_bytes", 0) * 8000 / max(1, duration),
        sent=total.get("udp_sent", 0),
        lost=total.get("udp_lost"),
        pending=total.get("udp_pending"),
        reordered=sum(int(r["udp_reordered"] or 0) for r in rows),
        holds=sum(e["event"] == "feedback_hold" for e in events),
        suspensions=sum(e["event"] == "feedback_suspend" for e in events),
        probes=sum(r["reason"] == "upward_probe" for r in rows),
        backoffs=sum(r["reason"] == "loss_backoff" for r in rows),
        cap_ok=all(
            max(int(r["offer_bps"]), int(r["next_bps"])) <= ceiling for r in rows
        ),
    )


def propose(baseline, reference, cap, window):
    changes = {}
    if not baseline["complete"] or baseline["lost"] or baseline["reordered"]:
        return changes
    if not all(
        x["exit_code"] == 0 and x["clean_windows"] >= 3 and x["cap_ok"]
        for x in (baseline, reference)
    ):
        return changes
    latency = max(baseline["clean_feedback_ms"] + reference["clean_feedback_ms"])
    hold = max(window + 1000, 2 * latency)
    # Amortize the observed reporting/drain cost while preserving the clean window.
    cadence = reference.get("report_intervals_ms", [])
    if len(cadence) >= 3:
        overhead = max(0, int(median(cadence)) - window)
        proposed = min(
            10000,
            800_000_000_000 // cap,
            hold - 501,
            max(window, min(2 * window, 2 * overhead)),
        )
        if proposed > window:
            changes["feedback_ms"] = proposed
    changes["hold_ms"] = hold
    if reference["suspensions"] and reference["successful_restarts"]:
        changes["suspend_ms"] = 3 * hold
    elif hold >= max(15000, window + 5000):
        del changes["hold_ms"]
    rates = reference["clean_rates"]
    if len(rates) >= 2:
        step = max(1, int(median(b - a for a, b in zip(rates, rates[1:]))))
        changes["ramp_step_bps"] = min(step * 2, cap)
        if reference["probes"]:
            changes.update(probe_step_bps=step, probe_ms=max(2 * latency, 2 * window))
    if reference["backoffs"] >= 2:
        changes["early_loss_count"] = 2
    return changes


def comparable(reference, candidate):
    if not all(
        x["exit_code"] == 0
        and (
            x["complete"]
            or (x.get("successful_restarts", 0) > 0 and x.get("pending") == 0)
        )
        and x["cap_ok"]
        and x["sent"] > 0
        and x["clean_windows"] >= 3
        for x in (reference, candidate)
    ):
        return False
    return (
        candidate["whole_budget_bps"] >= 0.95 * reference["whole_budget_bps"]
        and candidate["goodput_bps"] >= 0.95 * reference["goodput_bps"]
        and candidate["lost"] / candidate["sent"]
        <= reference["lost"] / reference["sent"] + 0.001
        and candidate["holds"] <= reference["holds"]
        and candidate["suspensions"] <= reference["suspensions"]
    )


def calibrate(args):
    caps = {
        d: getattr(args, d + "_max")
        for d in FILES
        if getattr(args, d + "_max") is not None
    }
    if not caps or any(type(c) is not int or c <= 0 for c in caps.values()):
        raise ValueError("supply at least one positive integer directional maximum")
    w = args.window_ms
    if w is None:
        w = min(5000, 800_000_000_000 // max(caps.values()))
    if (
        not caps
        or not 250 <= w <= 10000
        or any(c <= 0 or c * w > 800_000_000_000 for c in caps.values())
    ):
        raise ValueError(
            "supply positive maxima within the native packet budget and a 250..10000 ms window"
        )
    if not math.isfinite(args.duration) or not 0 < args.duration <= 1e9:
        raise ValueError("duration must be finite and positive")
    root = args.output
    root.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + args.duration
    observations = {d: {} for d in caps}
    candidates = {d: {} for d in caps}
    verified = {d: {"ceiling_bps": c} for d, c in caps.items()}
    phases = PHASES + (
        ("ceiling_check",) if getattr(args, "check_ceiling", False) else ()
    )
    slots = len(caps) * len(phases)
    for phase in phases:
        for d, cap in caps.items():
            budget = (deadline - time.monotonic()) / slots
            slots -= 1
            if budget < w / 1000 + 1:
                continue
            start = cap if phase == "ceiling_check" else max(1, cap // 4)
            run_cap = start if phase == "baseline" else cap
            config = dict(start_bps=start, ceiling_bps=run_cap)
            if phase == "candidate" and all(
                p in observations[d] for p in ("baseline", "reference")
            ):
                candidates[d] = propose(
                    observations[d]["baseline"], observations[d]["reference"], cap, w
                )
            if phase.endswith("candidate"):
                config.update(candidates[d])
            name = phase + "-" + d
            path = root / (name + ".json")
            save(path, {d: config})
            cmd = [
                sys.executable,
                str(Path(__file__).with_name("operator_cli.py")),
                "run",
                "--connect",
                args.connect,
                "--mode",
                "udp",
                "--window-ms",
                str(w),
                "--timing-every",
                "0",
                "--duration",
                str(budget - 0.35),
                "--plan",
                str(path),
                "--output",
                str(root / name),
            ]
            if args.config:
                cmd.extend(["--config", str(args.config)])
            with (root / (name + ".log")).open("x") as log:
                with subprocess.Popen(cmd, stdout=log, stderr=log) as child:
                    try:
                        code = child.wait(
                            timeout=min(budget, max(0.001, deadline - time.monotonic()))
                        )
                    except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
                        child.send_signal(signal.SIGTERM)
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                        if isinstance(exc, KeyboardInterrupt):
                            raise
                        code = 124
            observations[d][phase] = measure(root / name, d, run_cap, code)
            save(root / "measurements.json", observations)
    evidence = {}
    for d, data in observations.items():
        passed = all(p in data for p in PHASES) and all(
            comparable(data[a], data[b])
            for a, b in (
                ("reference", "candidate"),
                ("verify_reference", "verify_candidate"),
            )
        )
        verified[d]["start_bps"] = max(1, caps[d] // 4)
        verified[d]["window_ms"] = w
        accepted = {}
        for field, value in candidates[d].items():
            signal_key = {
                "early_loss_count": "backoffs",
                "suspend_ms": "suspensions",
                "probe_ms": "probes",
                "probe_step_bps": "probes",
            }.get(field)
            if passed and (
                not signal_key
                or all(data[p][signal_key] for p in ("candidate", "verify_candidate"))
            ):
                accepted[field] = value
        # Keep the exact verified bundle. Removing individual overrides can change its behavior.
        if accepted != candidates[d]:
            accepted.clear()
        verified[d].update(accepted)
        evidence[d] = {
            k: dict(
                confidence="limited to two paired sessions"
                if k in accepted
                else "unverified",
                rationale=(
                    "complete paired comparisons met throughput, loss and feedback gates"
                    if k in accepted
                    else "comparison gate failed or setting lacked a qualifying observation"
                ),
                status="verified_bundle" if k in accepted else "insufficient_evidence",
                candidate=candidates[d].get(k),
                windows={p: v["windows"] for p, v in data.items()},
            )
            for k in FIELDS
        }
    save(root / "measurements.json", observations)
    save(
        root / "calibration.json",
        dict(
            format_version=1,
            protocol="udp",
            duration_s=args.duration,
            elapsed_s=time.monotonic() - started,
            directions=caps,
            window_ms=w,
            active_goodput_ratio_floor=0.95,
            whole_budget_goodput_ratio_floor=0.95,
            max_loss_fraction_increase=0.001,
            policy="quarter-cap start; single-direction epochs; timing replies disabled",
            sessions_per_direction=len(phases),
        ),
    )
    save(root / "plan.json", verified)
    save(root / "evidence.json", evidence)
    return (
        0
        if all(
            any(v["status"] == "verified_bundle" for v in group.values())
            for group in evidence.values()
        )
        else 2
    )


def calibrate_link(
    *,
    connect,
    output,
    duration,
    forward_max=None,
    return_max=None,
    config=None,
    window_ms=None,
    check_ceiling=False,
):
    """Run the same bounded calibration as the CLI and return its per-setting evidence."""
    directory = Path(output)
    code = calibrate(
        SimpleNamespace(
            connect=connect,
            output=directory,
            duration=duration,
            forward_max=forward_max,
            return_max=return_max,
            config=config,
            window_ms=window_ms,
            check_ceiling=check_ceiling,
        )
    )
    return {
        "exit_code": code,
        "plan": json.loads((directory / "plan.json").read_text()),
        "evidence": json.loads((directory / "evidence.json").read_text()),
    }
