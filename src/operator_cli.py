"""EdgeWalker operator CLI and bounded native window orchestration."""

import copy
import argparse
import errno
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import threading
import tomllib
import uuid
from datetime import datetime
from pathlib import Path
from statistics import median_high

from live_menu import LiveMenu, OperatorStop, setup
from html_report import write_html
from rates import display_rate, normalize_rates, rate_argument
from records import Records, report, timestamp
from replay import Direction, integer, replay
from telemetry import InterfaceMonitor

ROOT = Path(__file__).resolve().parents[1]
DIRECTIONS = ("forward", "return")


def engine_path():
    for path in (
        os.getenv("EDGEWALKER_ENGINE"),
        ROOT / "bin/edgewalker-native",
        ROOT / "target/release/edgewalker-native",
        ROOT / "target/debug/edgewalker-native",
        shutil.which("edgewalker-native"),
    ):
        if path and os.access(path, os.X_OK):
            return str(path)
    raise ValueError("build the engine with cargo build --release --locked")


class Deadline(Exception):
    pass


class PeerOffline(ConnectionError):
    """The initial control socket could not reach its peer."""


class DataSetupTimeout(ConnectionError):
    """An authenticated session timed out during UDP reachability setup."""


class Native:
    def __init__(self, command):
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        self.buffer = b""

    def read(self, deadline):
        while b"\n" not in self.buffer:
            timeout = max(0, deadline - time.monotonic())
            if (
                timeout == 0
                or not select.select([self.process.stdout], [], [], timeout)[0]
            ):
                raise Deadline()
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ConnectionError("native connection ended")
            self.buffer += chunk
            if len(self.buffer) > 2_000_000:
                raise ValueError("native response too large")
        line, self.buffer = self.buffer.split(b"\n", 1)
        message = json.loads(line)
        if message.get("status") == "data_setup_timeout":
            raise DataSetupTimeout("admitted UDP path did not confirm readiness")
        if message.get("status") == "peer_offline":
            raise PeerOffline("control endpoint unavailable")
        return message

    def write(self, obj):
        self.process.stdin.write(json.dumps(obj).encode() + b"\n")
        self.process.stdin.flush()

    def ready(self, number, deadline):
        self.write("ready")
        answer = self.read(min(deadline, time.monotonic() + 5))
        if answer != {"status": "ready", "after": number}:
            raise ConnectionError("receiver readiness unconfirmed")

    def close(self, orderly=False):
        try:
            if orderly and self.process.poll() is None:
                self.write(None)
                return self.process.wait(timeout=3) == 0
        except (BrokenPipeError, subprocess.TimeoutExpired):
            pass
        finally:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
        return False


def send_time_available_ms(remaining_s, observed_overhead_ms):
    """Leave time for a measured receiver report before the original run deadline."""
    if remaining_s == float("inf"):
        return 10000
    reserve_ms = max(500, observed_overhead_ms + 100)
    return int(remaining_s * 1000) - reserve_ms


class LiveDirection:
    def __init__(self, cfg, mode):
        self.c = Direction(cfg)
        self.mode = mode
        self.flows = cfg["initial_flows"]
        self.sequence = 0
        self.baseline = None
        self.scale_reference = None
        self.flow_blocked_rate = None
        self.flow_retry_ms = 0
        self.loss_steps = 0
        self.prev_loss = None
        self.prev_rate = None
        self.previous_offer = None
        self.previous_lost = 0
        self.previous_window = None
        self.sparse_transit = []
        self.sparse_boundary = None
        self.suspended = False
        self.settle_left = cfg.get("settle_ms", 0)

    def update(self, sender, receiver, ms, start, stop, overload=False):
        c = self.c
        cfg = c.settings
        offer = c.rate
        state = c.state
        phase_state = c.before_delay_hold if state == "delay_hold" else state
        phase = {"ramp": "ramp", "probe": "probe"}.get(phase_state, "steady")
        settling = self.settle_left > 0
        self.settle_left = max(0, self.settle_left - ms)
        if settling and phase == "steady":
            phase = "settle"

        tx = sender["udp"]["sent"]
        udp_size = cfg.get("udp_size", 1000)
        rx = receiver["udp"]["received"]
        tcp = [f["received"] for f in receiver["tcp"]]
        tcp_bytes = sum(f["window_bytes"] for f in tcp)
        udp_bytes = rx["window_bytes"]
        delivered = (tcp_bytes + udp_bytes) * 8000 // ms
        tcp_goodput = tcp_bytes * 8000 // ms
        lost = rx["skipped"]
        pending = max(0, tx - rx["highest"])
        closed = receiver.get("closed_udp")
        if closed:
            pending, lost = closed["pending"], closed["lost"]
        reordered = closed["reordered"] if closed else rx["reordered"]
        duplicates = closed["duplicates"] if closed else rx["duplicates"]
        cohort_bytes = (tx - pending - lost + reordered) * udp_size
        preferred = rx if self.mode != "tcp" else tcp[0]
        transit = preferred["median_transit_us"]
        growth = preferred["delay_growth_us"]
        timing_source = "current"
        sparse = rx.get("sparse_transit_us", [])
        base = sender["udp_sequence_base"]
        clean_sparse = (self.mode == "udp" and sparse and growth is None and not overload
                        and not settling and tx == len(sparse) and rx["highest"] == tx
                        and not (lost or rx["reordered"] or rx["duplicates"]))
        if clean_sparse:
            if (self.sparse_boundary is None or self.sparse_boundary[:2] != (offer, base)
                    or not 0 <= start - self.sparse_boundary[2] < cfg["hold_ms"]):
                self.sparse_transit.clear()
            self.sparse_transit = (self.sparse_transit + sparse)[-6:]
            self.sparse_boundary = (offer, base + tx, stop)
            if len(self.sparse_transit) == 6:
                early, middle, late = (median_high(self.sparse_transit[i:i+2]) for i in (0, 2, 4))
                growth = min(middle - early, late - middle)
                transit = median_high(self.sparse_transit)
                timing_source = "contiguous_sparse_windows"
        else:
            self.sparse_transit.clear()
            self.sparse_boundary = None
        if self.baseline is None:
            self.baseline = transit
        delay = None if transit is None else max(0, transit - self.baseline)
        arrival_bytes = sum(
            lane.get("arrival_window_bytes", lane["window_bytes"])
            for lane in [rx, *tcp]
        )
        # Use the same receiver-interval metric on both sides of a delay hold.
        # Otherwise a queued cohort moving across the cutoff can look like a gain.
        delay_delivery = arrival_bytes * 8000 // ms if self.mode == "udp" else delivered
        if (
            self.mode == "udp"
            and pending
            and self.previous_offer == offer
            and growth is None
        ):
            late = receiver.get("late_udp_observations", {})
            if late.get("delay_growth_us") is not None and self.baseline is not None:
                growth = late["delay_growth_us"]
                delay = max(0, late["median_transit_us"] - self.baseline)
                timing_source = "previous_same_offer"
        prior = receiver.get("prior_udp") or {}
        late_loss = 0
        if (
            self.previous_offer == offer
            and self.previous_window is not None
            and self.previous_window == prior.get("window")
        ):
            late_loss = max(0, prior["lost"] - self.previous_lost)
        self.previous_lost = lost
        self.previous_offer = offer
        active_flows = self.flows
        retries = [lane["retransmissions"] for lane in sender["tcp"][:active_flows]]
        gain = cfg["min_gain_bps"]
        tried_extra = self.scale_reference is not None
        if tried_extra:
            if tcp_goodput - self.scale_reference < gain:
                self.flows = max(cfg["initial_flows"], self.flows - 1)
                self.flow_blocked_rate = offer
                self.flow_retry_ms = stop + cfg["probe_ms"]
            self.scale_reference = None
        tcp_offer = offer - offer // 10 if self.mode == "combined" else offer
        shortfall = tcp_offer - tcp_goodput
        if overload:
            phase = "overload"
            reason = "overload_end_revalidate"
            c.rate = c.last_clean
            c.state = "revalidate"
            c.edge = None
            c.reset_window()
            c.reset_plateau()
        elif pending and not lost and late_loss >= cfg["early_loss_count"]:
            c.loss_backoff()
            if offer > cfg["minimum_bps"]:
                c.rate = max(c.rate, cfg["minimum_bps"])
            reason = "confirmed_prior_window_loss_backoff"
        elif settling and lost < cfg["early_loss_count"]:
            reason = "settling"
        elif (
            self.mode != "udp"
            and not cfg["fixed_flows"]
            and not pending
            and not lost
            and (growth is None or growth < cfg["growth_us"])
            and shortfall >= max(gain, tcp_offer // 20)
            and self.flows < cfg["flow_limit"]
            and (self.flow_blocked_rate != offer or stop >= self.flow_retry_ms)
        ):
            self.scale_reference = tcp_goodput
            self.flows += 1
            reason = "add_tcp_flow"
        elif self.mode == "tcp":
            if ms >= cfg["window_ms"]:
                delay_reason = c.delay_decision(ms, delivered, delay, growth, {})
            else:
                delay_reason = "observing"
            if delay_reason:
                reason = delay_reason
            elif shortfall >= max(gain, tcp_offer // 20):
                c.state = "cruise"
                reason = "tcp_goodput_limited"
            else:
                c.last_clean = c.edge = offer
                if c.state == "ramp" or (c.state == "cruise" and stop >= c.probe_due):
                    step = (
                        cfg["ramp_step_bps"]
                        if c.state == "ramp"
                        else cfg["probe_step_bps"]
                    )
                    c.rate = min(cfg["ceiling_bps"], offer + step)
                    reason = (
                        "tcp_clean_increase" if c.rate > offer else "clean_at_ceiling"
                    )
                else:
                    reason = "tcp_clean_hold"
                c.state = (
                    "cruise"
                    if c.rate == cfg["ceiling_bps"] or c.state != "ramp"
                    else "ramp"
                )
                c.probe_due = (
                    stop + cfg["probe_ms"]
                    if reason != "tcp_clean_hold" or not c.probe_due
                    else c.probe_due
                )
        elif pending and not lost:
            c.edge = None
            c.reset_window()
            c.reset_plateau()
            reason = "trailing_delivery_unverified"
            # Delay can justify holding or backing off before the trailing
            # sequences arrive. An open range can never verify a clean offer.
            if (
                ms >= cfg["window_ms"]
                and c.last_clean is not None
                and (c.rate >= c.last_clean or c.state == "delay_hold" or c.recovering_delay)
                and growth is not None
                and growth >= cfg["growth_us"]
            ):
                reason = (
                    c.delay_decision(ms, delay_delivery, delay, growth, {}) or reason
                )
        elif (
            self.mode == "udp"
            and not lost
            and (offer * ms / 8000 - tx * udp_size)
            > max(2000, offer * ms / 8000 * 0.05)
        ):
            c.edge = None
            reason = "local_pacing_unverified"
        elif tx:
            event = dict(
                start_ms=start,
                end_ms=start + ms,
                offered_bps=offer,
                first=self.sequence + 1,
                last=self.sequence + tx,
                received_bytes=tcp_bytes + cohort_bytes,
                sequence_counts=dict(
                    lost=lost, reordered=reordered, duplicates=duplicates
                ),
                excess_delay_us=delay,
                delay_growth_us=growth,
            )
            if self.mode == "udp":
                event["same_stream_windows"] = True
                event["udp_quantum_bps"] = (udp_size * 8000 + ms - 1) // ms
            reason = c.feedback(event, stop, {})
        else:
            reason = "no_sender_evidence"
        self.sequence += tx
        if self.mode == "udp" and 0 < c.rate < offer:
            # Below this rate even one negotiated datagram cannot fit before
            # the feedback hold. Preserve measurement at the lowest usable rate.
            budget_ms = min(10000, cfg["hold_ms"] - 501)
            floor = (udp_size * 8000 + budget_ms - 1) // budget_ms
            c.rate = max(c.rate, min(offer, floor))
            if c.rate == offer:
                reason = "measurement_floor_hold"
        if tx and lost:
            fraction = lost / tx
            if self.prev_loss is not None and offer < self.prev_rate:
                self.loss_steps = (
                    self.loss_steps + 1 if fraction >= self.prev_loss else 0
                )
            self.prev_loss, self.prev_rate = fraction, offer
            if (
                self.loss_steps >= cfg["persistent_steps"]
                and offer <= cfg["minimum_bps"]
            ):
                reason = "persistent_loss_alert"
                if cfg["minimum_action"] == "suspend":
                    self.suspended = True
                    reason = "persistent_loss_suspend"
            elif offer > cfg["minimum_bps"] and c.rate < cfg["minimum_bps"]:
                c.rate = cfg["minimum_bps"]
        else:
            self.loss_steps = 0
            self.prev_loss = None
        if c.rate != offer:
            self.settle_left = cfg.get("settle_ms", 0)
            self.flow_blocked_rate = None
        utc = timestamp()
        return dict(
            utc=utc,
            local=datetime.fromisoformat(utc).astimezone().isoformat(),
            start_ms=start,
            end_ms=start + ms,
            decision_ms=stop,
            phase=phase,
            settling=settling,
            state=c.state,
            offer_bps=offer,
            actual_offer_bps=(
                tx * udp_size + 1000 * sum(f["sent"] for f in sender["tcp"])
            )
            * 8000
            // ms,
            delivered_bps=delivered,
            cohort_received_bytes=cohort_bytes if tx else None,
            cohort_udp_bps=cohort_bytes * 8000 // ms if tx else None,
            arrival_bps=arrival_bytes * 8000 // ms,
            delay_evidence_bps=delay_delivery,
            timing_source=timing_source,
            newly_confirmed_prior_loss=late_loss,
            received_bytes=tcp_bytes + udp_bytes,
            next_bps=0 if self.suspended else c.rate,
            last_clean_bps=c.last_clean,
            edge_bps=c.edge,
            target_bps=cfg["target_bps"],
            reason=reason,
            udp_sent=tx if self.mode != "tcp" else None,
            udp_pending=pending if self.mode != "tcp" else None,
            udp_lost=lost if self.mode != "tcp" else None,
            udp_reordered=reordered if tx else None,
            udp_duplicates=duplicates if tx else None,
            tcp_goodput_bps=tcp_goodput if tcp else None,
            tcp_flows=active_flows if tcp else None,
            retransmissions=sum(retries) if retries and None not in retries else None,
            udp_first=sender["udp_sequence_base"] + 1 if tx else None,
            udp_last=sender["udp_sequence_base"] + tx if tx else None,
            timing_requested=sender["timing"]["requested"],
            timing_valid=sender["timing"]["valid"],
            missing_timing=sender["timing"]["requested"] - sender["timing"]["valid"],
            raw_rtt_us=sender["timing"]["mean_raw_rtt_us"],
            adjusted_rtt_us=sender["timing"]["mean_adjusted_rtt_us"],
            jitter_us=preferred["mean_abs_variation_us"],
            excess_delay_us=delay,
            delay_growth_us=growth,
        )


def plan(args, overrides=None):
    if overrides is None:
        overrides = json.loads(args.plan.read_text()) if args.plan else {}
    if not isinstance(overrides, dict) or set(overrides) - set(DIRECTIONS):
        raise ValueError("plan must contain forward and/or return objects")
    settings = {}
    for name in DIRECTIONS:
        extra = overrides.get(name, {})
        if not isinstance(extra, dict):
            raise ValueError("direction settings must be an object")
        extra = normalize_rates(extra)
        maximum = getattr(args, name + "_max")
        if maximum is None:
            maximum = extra.get("ceiling_bps", 0)
        elif "ceiling_bps" in extra and maximum != extra["ceiling_bps"]:
            raise ValueError("conflicting directional maxima")
        integer(maximum, "ceiling_bps")
        if not maximum:
            if extra:
                raise ValueError("configured direction needs a positive maximum")
            continue
        initial = min(maximum, getattr(args, name + "_start") or maximum)
        cfg = dict(
            ceiling_bps=maximum,
            start_bps=initial,
            ramp_step_bps=max(1, initial // 2),
            probe_step_bps=max(1, initial // 10),
            window_ms=args.window_ms,
            feedback_ms=args.window_ms,
            min_packets=3,
            early_loss_count=3,
            settle_ms=0,
            probe_ms=10000,
            hold_ms=max(7000, args.window_ms + 2000),
            suspend_ms=max(15000, args.window_ms + 5000),
            growth_us=20000,
            observe_ms=5000,
            min_gain_bps=8000,
            loss_only=False,
            minimum_bps=0,
            minimum_action="alert",
            persistent_steps=2,
            target_bps=None,
            flow_limit=args.flow_limit,
            initial_flows=1 if args.flows is None else args.flows,
            fixed_flows=args.flows is not None,
        )
        if set(extra) - set(cfg):
            raise ValueError(
                "unknown directional setting: " + str(set(extra) - set(cfg))
            )
        cfg.update(extra)
        if getattr(args, name + "_start") is not None:
            cfg["start_bps"] = getattr(args, name + "_start")
        for key in (
            "persistent_steps",
            "flow_limit",
            "initial_flows",
            "growth_us",
            "observe_ms",
            "min_gain_bps",
        ):
            integer(cfg[key], key, 1)
        integer(cfg["minimum_bps"], "minimum_bps")
        integer(cfg["settle_ms"], "settle_ms")
        if cfg["target_bps"] is not None:
            integer(cfg["target_bps"], "target_bps", 1)
        for key in ("loss_only", "fixed_flows"):
            if type(cfg[key]) is not bool:
                raise ValueError(key + " must be boolean")
        if not (1 <= cfg["initial_flows"] <= cfg["flow_limit"] <= 16):
            raise ValueError("flow counts must be in 1..16 and initial <= limit")
        if (
            cfg["minimum_action"] not in ("alert", "suspend")
            or not 0 <= cfg["minimum_bps"] <= cfg["start_bps"]
        ):
            raise ValueError("invalid minimum-rate policy")
        integer(cfg["feedback_ms"], "feedback_ms", 250)
        if cfg["feedback_ms"] > 10000:
            raise ValueError("feedback window must be 250..10000 ms")
        if cfg["hold_ms"] <= max(args.window_ms, cfg["feedback_ms"]) + 500:
            raise ValueError("hold timer must accommodate the measurement and drain")
        if not cfg["loss_only"]:
            cfg["delay_policy"] = {
                k: cfg[k] for k in ("growth_us", "observe_ms", "min_gain_bps")
            }
        max_window = 800_000_000 * min(args.udp_size, 1000) // maximum
        if max(args.window_ms, cfg["feedback_ms"], args.overload_ms) > max_window:
            raise ValueError(f"{name} ceiling exceeds native window packet limit; "
                             f"maximum window at this ceiling is {max_window} ms")
        cfg["udp_size"] = args.udp_size
        Direction(cfg)
        settings[name] = cfg
    if not settings:
        raise ValueError("specify at least one directional maximum")
    return settings


def run(args):
    if args.window_ms is None:
        args.window_ms = 5000
        if args.plan:
            supplied = json.loads(args.plan.read_text())
            if isinstance(supplied, dict) and supplied and all(
                isinstance(v, dict) and type(v.get("window_ms")) is int for v in supplied.values()
            ):
                windows = {v["window_ms"] for v in supplied.values()}
                if len(windows) == 1:
                    args.window_ms = windows.pop()
    if not 250 <= args.window_ms <= 10000 or (
        args.duration is not None and not 0 < args.duration <= 1e9
    ):
        raise ValueError("window must be 250..10000 ms and duration must be positive")
    integer(args.timing_every, "timing_every")
    if not 128 <= args.udp_size <= 9000:
        raise ValueError("UDP size must be 128..9000")
    if args.overload_ms and not 250 <= args.overload_ms <= 10000:
        raise ValueError("overload window must be 250..10000 ms")
    if not 1 <= args.flow_limit <= 16 or (args.flows is not None and not 1 <= args.flows <= 16):
        raise ValueError("flow counts must be in 1..16")
    if args.tcp_send_buffer is not None and not 8192 <= args.tcp_send_buffer <= 4194304:
        raise ValueError("TCP send buffer must be 8192..4194304 bytes")
    settings = plan(args)
    menu = LiveMenu(settings, args.mode) if getattr(args, "interactive", False) else None
    binary = engine_path()
    config = ["--config", str(args.config)] if args.config else []
    if args.tcp_send_buffer is not None:
        config_buffer = ["--tcp-send-buffer", str(args.tcp_send_buffer)]
    else:
        config_buffer = []
    if args.key is not None:
        config += ["--key", args.key]
    prepared_flows = max(s["flow_limit"] for s in settings.values())
    setup_seconds = 12 + (3 * prepared_flows if args.mode != "udp" else 0)
    command = [
        binary,
        *config,
        "client",
        "--connect",
        args.connect,
        "--mode",
        args.mode,
        *config_buffer,
        "--udp-size",
        str(args.udp_size),
        "--timing-every",
        str(args.timing_every),
        "--flow-limit",
        str(prepared_flows),
    ]
    if args.overload_ms:
        command += ["--overload-ms", str(args.overload_ms)]
    for name in DIRECTIONS:
        command += [
            "--" + name + "-max",
            str(settings.get(name, {}).get("ceiling_bps", 0)),
        ]
    rec = Records(
        args.output,
        dict(
            run_id=str(uuid.uuid4()),
            mode=args.mode,
            settings=copy.deepcopy(settings),
            stop_acknowledged=False,
            connect=args.connect,
            duration_s=args.duration,
            overload_ms=args.overload_ms,
            timing_every=args.timing_every,
            tcp_send_buffer=args.tcp_send_buffer,
            telemetry_interface=args.interface,
            telemetry_interval_s=args.telemetry_interval,
            native_version=subprocess.check_output(
                [binary, "--version"], text=True
            ).strip(),
            sessions=0,
            window_ms=args.window_ms,
        ),
    )
    started = time.monotonic()
    end = started + args.duration if args.duration is not None else float("inf")

    def elapsed():
        return int((time.monotonic() - started) * 1000)

    status, reason = "failed", "setup_failed"
    number = 0
    proc = None
    overload_pending = bool(args.overload_ms)
    last_probe = None
    monitor = None
    previous_link = "unknown"
    backoff = 0.5
    measured_windows = 0
    resume = threading.Event()
    def record_edit(kind, direction, value):
        rec.event(kind, elapsed(), direction, value)

    def poll_disconnected():
        nonlocal end
        if menu:
            end = menu.poll({}, settings, end, record_edit, None, resume, None)

    previous_signal = signal.signal(signal.SIGUSR1, lambda *_: resume.set())
    try:
        if args.interface:
            monitor = InterfaceMonitor(
                args.interface, args.telemetry_interval, rec, started
            )
            monitor.thread.start()
        while time.monotonic() < end:
            try:
                poll_disconnected()
                proc = Native(command)
                ready = proc.read(min(end, time.monotonic() + setup_seconds))
                if ready.get("status") != "ready":
                    raise ConnectionError("receiver unavailable")
                rec.manifest["stop_acknowledged"] = False
                rec.manifest["sessions"] += 1
                rec.save()
                rec.event("session_ready", elapsed(), detail=rec.manifest["sessions"])
                session_window = 0
                report_overhead_ms = 0
                active = {n: LiveDirection(s, args.mode) for n, s in settings.items()}
                def resize(requested):
                    if not ready.get("resize"):
                        raise ValueError("gateway does not support live resizing")
                    proc.write({"resize": {"forward": requested.get("forward", 0),
                                           "reverse": requested.get("return", 0)}})
                    reply = proc.read(min(end, time.monotonic() + 10))
                    if reply.get("resize") != "Accepted":
                        rec.event("operator_reservation_rejected", elapsed(), detail=reply.get("resize"))
                        raise ValueError(f"gateway rejected reservation: {reply.get('resize')}")
                    for d in DIRECTIONS:
                        command[command.index("--" + d + "-max") + 1] = str(requested.get(d, 0))

                def validate_edit(direction, changes):
                    test_args = copy.copy(args)
                    for d in DIRECTIONS:
                        setattr(test_args, d + "_max", None)
                        setattr(test_args, d + "_start", None)
                    cfg = {k: v for k, v in settings[direction].items() if k not in ("udp_size", "delay_policy")}
                    cfg.update(changes)
                    checked = plan(test_args, {direction: cfg})[direction]
                    if checked["hold_ms"] <= checked["feedback_ms"] + 500:
                        raise ValueError("hold timer must accommodate feedback and drain")
                    if "feedback_ms" in changes or "start_bps" in changes:
                        rate = min(active[direction].c.rate, checked["start_bps"])
                        tcp = rate - rate // 10 if args.mode == "combined" else rate
                        udp = rate // 10 if args.mode == "combined" else rate
                        interval = min([checked["feedback_ms"]] + [
                            s["feedback_ms"] for n, s in settings.items() if n != direction])
                        if ((args.mode != "udp" and tcp // active[direction].flows * interval < 8000000)
                            or (args.mode != "tcp" and udp * interval < args.udp_size * 8000)):
                            raise ValueError("feedback interval or starting rate too small for active streams")
                    return checked

                while time.monotonic() < end:
                    if menu:
                        end = menu.poll(active, settings, end, record_edit,
                                       resize, resume, validate_edit)
                    if resume.is_set():
                        resume.clear()
                        proc.ready(session_window, end)
                        for n, v in list(active.items()):
                            if v.suspended:
                                active[n] = LiveDirection(settings[n], args.mode)
                                rec.event("manual_ready_restart", elapsed(), n)
                    active_names = [n for n, v in active.items() if not v.suspended]
                    if not active_names:
                        proc.ready(session_window, end)
                        resume.wait(min(1, max(0, end - time.monotonic())))
                        continue
                    remaining = end - time.monotonic()
                    available_ms = send_time_available_ms(remaining, report_overhead_ms)
                    feedback_ms = min(settings[n]["feedback_ms"] for n in active_names)
                    ms = min(feedback_ms, available_ms)
                    # The native trend estimator needs three pairs of samples.
                    # Extend this send window at a low offer without relaxing
                    # feedback deadlines or exceeding either direction's budget.
                    if available_ms >= feedback_ms and args.mode != "tcp":
                        desired = ms
                        limit = min(10000, available_ms)
                        for n in active_names:
                            cfg = settings[n]
                            rate = active[n].c.rate
                            udp_rate = rate // 10 if args.mode == "combined" else rate
                            controller = active[n].c
                            if (args.mode == "udp" and not cfg["loss_only"]
                                    and (controller.state == "probe" or controller.checking_offer == rate)):
                                # A longer continuous send interval limits the influence
                                # of queue credit accumulated between short samples.
                                desired = max(desired, 3 * args.window_ms)
                                limit = min(limit, max(args.window_ms, cfg["hold_ms"] - 1000))
                            if udp_rate:
                                sample_count = 1 if cfg["loss_only"] else 7
                                desired = max(desired, (sample_count * args.udp_size * 8000 + udp_rate - 1) // udp_rate)
                            limit = min(limit, cfg["hold_ms"] - 501,
                                        800_000_000 * min(args.udp_size, 1000) // rate)
                        ms = min(desired, limit)
                    overload = (
                        overload_pending
                        and all(
                            active[n].c.last_clean is not None for n in active_names
                        )
                        and any(
                            active[n].c.last_clean < settings[n]["ceiling_bps"]
                            for n in active_names
                        )
                    )
                    if overload and available_ms >= args.overload_ms:
                        ms = args.overload_ms
                        overload_pending = False
                        for n in active_names:
                            active[n].c.rate = settings[n]["ceiling_bps"]
                        rec.event("overload_begin", elapsed(), detail=ms)
                    else:
                        overload = False
                    minimum_ms = 250
                    for n in active_names:
                        offer = active[n].c.rate
                        lanes = []
                        if args.mode != "tcp":
                            lanes.append(
                                (
                                    offer // 10 if args.mode == "combined" else offer,
                                    args.udp_size,
                                )
                            )
                        if args.mode != "udp":
                            lanes.append(
                                (
                                    (
                                        offer - offer // 10
                                        if args.mode == "combined"
                                        else offer
                                    )
                                    // active[n].flows,
                                    1000,
                                )
                            )
                        for rate, size in lanes:
                            if rate <= 0:
                                raise ValueError("rate too small for requested streams")
                            minimum_ms = max(
                                minimum_ms, (8000 * size + rate - 1) // rate
                            )
                    if ms < minimum_ms and (
                        minimum_ms > 10000 or available_ms >= minimum_ms
                        or end == float("inf")
                    ):
                        raise ValueError(
                            "offer too small for active streams within window"
                        )
                    if overload and ms < minimum_ms:
                        raise ValueError(
                            "overload interval too short for active streams"
                        )
                    if ms < minimum_ms:
                        time.sleep(max(0, end - time.monotonic()))
                        break
                    number += 1
                    rates = {
                        n: active[n].c.rate if n in active_names else 0
                        for n in DIRECTIONS
                    }
                    flows = {
                        n: active[n].flows if n in active_names else 1
                        for n in DIRECTIONS
                    }
                    window = dict(
                        id=number,
                        ms=ms,
                        rates=dict(forward=rates["forward"], reverse=rates["return"]),
                        flows=dict(forward=flows["forward"], reverse=flows["return"]),
                    )
                    if overload:
                        window["overload"] = True
                    begin = elapsed()
                    proc.write(window)
                    due = time.monotonic()
                    # Each direction owns its feedback timer. A stalled shared control connection
                    # can hold both, then causes a fresh authenticated replacement.
                    suspended = set()
                    held = set()
                    timers = sorted(
                        (settings[n][timer], n, timer)
                        for n in active_names
                        for timer in ("hold_ms", "suspend_ms")
                    )
                    for timeout, n, timer in timers:
                        try:
                            result = proc.read(min(end + 3, due + timeout / 1000))
                            break
                        except Deadline:
                            if time.monotonic() >= end:
                                raise
                            if timer == "suspend_ms":
                                suspended.add(n)
                                rec.event(
                                    "feedback_suspend",
                                    elapsed(),
                                    n,
                                    "waiting for same-session readiness",
                                )
                            else:
                                held.add(n)
                                rec.event(
                                    "feedback_hold", elapsed(), n, "delivery unknown"
                                )
                    else:
                        # The bounded window has ended. Give the active peer a
                        # chance to finish its authenticated report before replacing it.
                        result = proc.read(min(end + 3, time.monotonic() + 5))
                    if result.get("window") != window:
                        raise ValueError("native window mismatch")
                    session_window = number
                    stop = elapsed()
                    if not held:
                        # The physical combined-return trace needed 532–538 ms
                        # after each send window for the report to reach the client.
                        report_overhead_ms = max(0, stop - begin - ms)
                    rec.sample(
                        dict(
                            session=rec.manifest["sessions"],
                            start_ms=begin,
                            decision_ms=stop,
                            **result,
                        )
                    )
                    if suspended:
                        proc.ready(session_window, end)
                    link = monitor.state(stop) if monitor else "unknown"
                    if link != previous_link:
                        rec.event("interface_state", stop, detail=link)
                        previous_link = link
                    pending_rows = {}
                    for n in active_names:
                        receiver = "client" if n == "forward" else "server"
                        correction = result[receiver]["prior_udp"]
                        if correction is not None and correction["sent"]:
                            rec.csv(
                                "udp-corrections.csv",
                                dict(
                                    session=rec.manifest["sessions"],
                                    direction=n,
                                    **correction,
                                ),
                            )
                        if n in held:
                            rec.event(
                                "coverage_gap",
                                stop,
                                n,
                                "late window archived without rate verification",
                            )
                            if n in suspended:
                                active[n] = LiveDirection(settings[n], args.mode)
                                active[n].suspended = args.manual_resume
                                rec.event(
                                    "manual_resume_required"
                                    if args.manual_resume
                                    else "receiver_ready_restart",
                                    stop,
                                    n,
                                )
                            else:
                                active[n].c.reset_window()
                                active[n].c.reset_plateau()
                                active[n].c.edge = None
                                active[n].c.state = "revalidate"
                            continue
                        sender, receiver = (
                            ("server", "client")
                            if n == "forward"
                            else ("client", "server")
                        )
                        last_clean = active[n].c.last_clean
                        measured = (
                            copy.deepcopy(active[n]) if link == "down" else active[n]
                        )
                        row = measured.update(
                            result[sender], result[receiver], ms, begin, stop, overload
                        )
                        measured.previous_window = number
                        row["link_state"] = link
                        if link == "down":
                            active[n].c.edge = None
                            active[n].c.rate = row["offer_bps"]
                            active[n].c.state = "revalidate"
                            active[n].c.last_clean = last_clean
                            row.update(
                                reason="observed_interface_down",
                                state="revalidate",
                                next_bps=row["offer_bps"],
                                edge_bps=None,
                                last_clean_bps=last_clean,
                            )
                        row.update(session=rec.manifest["sessions"], window=number)
                        pending_rows[n] = row
                    probes = [
                        n
                        for n, row in pending_rows.items()
                        if row["reason"] == "upward_probe"
                    ]
                    if len(probes) == 2:
                        admitted = next(n for n in probes if n != last_probe)
                        deferred = next(n for n in probes if n != admitted)
                        c = active[deferred].c
                        c.rate = pending_rows[deferred]["offer_bps"]
                        c.state = "cruise"
                        active[deferred].settle_left = 0
                        pending_rows[deferred].update(
                            reason="probe_deferred", state=c.state, next_bps=c.rate
                        )
                        probes = [admitted]
                    if probes:
                        last_probe = probes[0]
                    for n, row in pending_rows.items():
                        rec.series(n, row)
                        if menu:
                            print(f"{n}: offered {display_rate(row['offer_bps'])}, delivered {display_rate(row['delivered_bps'])}, "
                                  f"TCP flows {row['tcp_flows']}", file=sys.stderr)
                        rec.event(row["reason"], stop, n, row["next_bps"])
                    if pending_rows:
                        measured_windows += 1
                    backoff = 0.5
                if not proc.close(orderly=True):
                    raise ConnectionError("stop unconfirmed")
                rec.manifest["stop_acknowledged"] = True
                proc = None
                break
            except (ConnectionError, BrokenPipeError, Deadline) as exc:
                rec.event("coverage_gap", elapsed(), detail=type(exc).__name__)
                if proc:
                    proc.close()
                    proc = None
                if time.monotonic() >= end:
                    break
                if rec.manifest["sessions"] == 0 and not isinstance(exc, (PeerOffline, Deadline, DataSetupTimeout)):
                    raise
                if args.manual_resume and rec.manifest["sessions"]:
                    rec.event("manual_resume_required", elapsed())
                    while not resume.is_set() and time.monotonic() < end:
                        poll_disconnected()
                        resume.wait(min(0.25, max(0, end - time.monotonic())))
                    resume.clear()
                rec.event("replacement_wait", elapsed(), detail=backoff)
                retry_at = time.monotonic() + backoff
                while time.monotonic() < min(retry_at, end):
                    poll_disconnected()
                    time.sleep(min(0.25 if menu else backoff, max(0, min(retry_at, end) - time.monotonic())))
                backoff = min(8, 2 * backoff)
        if not measured_windows:
            status, reason = "failed", "no_receiver_windows"
            print("edgewalker: no receiver measurement windows collected", file=sys.stderr)
            return 1
        status, reason = (
            ("completed_with_gaps" if rec.gaps else "completed"),
            "deadline",
        )
        return 0
    except OperatorStop:
        status, reason = "interrupted", "operator_stop"
        if proc:
            rec.manifest["stop_acknowledged"] = proc.close(orderly=True)
            proc = None
        rec.event("operator_stop", elapsed())
        return 130
    except KeyboardInterrupt:
        status, reason = "interrupted", "operator_stop"
        rec.event("coverage_gap", elapsed(), detail="interrupted window")
        return 130
    except BaseException as exc:
        reason = (
            "storage_full"
            if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)
            else type(exc).__name__
        )
        raise
    finally:
        if proc:
            proc.close()
        if monitor and monitor.thread.ident is not None:
            monitor.close()
        signal.signal(signal.SIGUSR1, previous_signal)
        rec.finish(status, reason)


def main():
    argv = sys.argv[1:]
    interactive = argv[:1] in (["-m"], ["--menu"])
    if interactive:
        argv = ["run", *argv[1:]]
    elif argv[:1] in (["-s"], ["--server"]):
        argv = ["server", *argv[1:]]
    elif argv and argv[0].startswith("-") and argv[0] not in ("--help", "-h", "--version", "-V"):
        argv = ["run", *argv]
    parser = argparse.ArgumentParser(prog="edgewalker", epilog=(
        "Use --connect for a client, -s/--server for the gateway, or -m/--menu for interactive setup."
    ))
    parser.add_argument("-V", "--version", action="store_true", help="print the EdgeWalker version and exit")
    if argv[:1] in (["--version"], ["-V"]):
        if len(argv) != 1:
            parser.error("--version takes no other arguments")
        with (ROOT / "Cargo.toml").open("rb") as manifest:
            version = tomllib.load(manifest)["package"]["version"]
        print(f"edgewalker {version}")
        return 0
    sub = parser.add_subparsers(dest="action", required=True)
    from calibrate import add_command, calibrate
    add_command(sub)
    sub.add_parser("server", add_help=False, help="start the measurement gateway")
    sub.add_parser("keygen", add_help=False, help="generate a shared key configuration")
    for name in ("report", "replay"):
        command = sub.add_parser(name, help={"report": "summarize a recorded run", "replay": "replay a controller trace"}[name])
        command.add_argument("path", type=Path)
        if name == "report":
            command.add_argument(
                "--html", type=Path, help="write a new standalone HTML report"
            )
    p = sub.add_parser("run", help="measure a link as a client")
    menu_help = argv[:1] == ["help"] and argv[-1:] == ["menu"]
    p.add_argument("--connect", required=not (interactive or menu_help))
    p.add_argument("--mode", choices=["udp", "tcp", "combined"], required=not (interactive or menu_help))
    p.add_argument("--output", required=not (interactive or menu_help), type=Path)
    p.add_argument(
        "--duration", type=float, help="wall-clock seconds including setup and recovery"
    )
    p.add_argument(
        "--overload-ms",
        type=int,
        default=0,
        help="one explicitly admitted ceiling window after clean verification",
    )
    p.add_argument("--config", type=Path)
    p.add_argument("--key")
    p.add_argument(
        "--interface",
        help="read-only telemetry for the operator-selected local test interface",
    )
    p.add_argument("--telemetry-interval", type=float, default=1.0)
    p.add_argument(
        "--udp-size",
        type=int,
        default=1000,
        help="UDP data record bytes including measurement header",
    )
    p.add_argument("--window-ms", type=int, help="measurement window in ms (common plan value or 5000)")
    p.add_argument("--plan", type=Path, help="per-direction JSON settings")
    p.add_argument(
        "--timing-every",
        type=int,
        default=32,
        help="request a UDP timing reply every N records (0 disables)",
    )
    p.add_argument(
        "--tcp-send-buffer",
        type=int,
        help="optional per-flow sender socket buffer in bytes",
    )
    p.add_argument("--flow-limit", type=int, default=4)
    p.add_argument("--flows", type=int, help="fixed active TCP flows")
    p.add_argument(
        "--manual-resume",
        action="store_true",
        help="require SIGUSR1 before resuming after suspension or connection loss",
    )
    for n in DIRECTIONS:
        p.add_argument("--" + n + "-max", type=rate_argument, metavar="RATE")
        p.add_argument("--" + n + "-start", type=rate_argument, metavar="RATE")
    help_command = sub.add_parser("help", help="show usage for a command")
    help_command.add_argument("topic", nargs="?", choices=[*sub.choices, "menu"])
    if not argv:
        parser.print_help()
        return 0
    if argv[:1] in (["server"], ["keygen"]):
        binary = engine_path()
        os.execv(binary, [binary, *argv])
    args = parser.parse_args(argv)
    if args.action == "help":
        if args.topic in ("server", "keygen"):
            binary = engine_path()
            os.execv(binary, [binary, args.topic, "--help"])
        (p if args.topic == "menu" else sub.choices.get(args.topic, parser)).print_help()
        return 0
    args.interactive = interactive
    if interactive:
        try:
            if not setup(args, p):
                return 0
        except (EOFError, KeyboardInterrupt):
            return 130
    if args.action == "calibrate":
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        return calibrate(args)
    if args.action == "report":
        if args.html:
            write_html(args.path, args.html)
        print(json.dumps(report(args.path), indent=2))
    elif args.action == "replay":
        for row in replay(json.loads(args.path.read_text())):
            print(json.dumps(row, sort_keys=True))
    else:
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        code = run(args)
        if code == 0:
            summary = report(args.output)
            for name, direction in summary["directions"].items():
                whole = direction["phases"].get("whole", {})
                rate = direction["active_delivered_bps"]
                loss = whole.get("udp_loss_pct")
                clean = direction["last_verified_clean_bps"]
                print(f"{name}: active delivery {display_rate(rate) if rate is not None else 'unknown'}, "
                      f"UDP loss {f'{loss:.2f}%' if loss is not None else 'n/a'}, "
                      f"pending {whole.get('udp_pending', 0)}, last verified {display_rate(clean) if clean else 'unknown'}, "
                      f"verdict {direction['last_reason'] or 'unverified'}")
        return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, Deadline) as exc:
        sys.exit(f"edgewalker: {exc or type(exc).__name__}")
