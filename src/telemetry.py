"""Optional read-only observations of an operator-selected client interface."""

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from records import timestamp


class InterfaceMonitor:
    def __init__(self, name, interval, records, started):
        if name not in dict(socket.if_nameindex()).values():
            raise ValueError("unknown local interface")
        if not 0.1 <= interval <= 60:
            raise ValueError("telemetry interval must be 0.1..60 seconds")
        self.name = name
        self.interval = interval
        self.records = records
        self.started = started
        self.done = threading.Event()
        self.latest = None
        self.error = None
        self.thread = threading.Thread(target=self.collect, name="interface-telemetry")

    def sample(self):
        speed = rx = tx = None
        if sys.platform.startswith("linux"):
            path = Path("/sys/class/net") / self.name
            state = path.joinpath("operstate").read_text().strip()

            def counter(file):
                try:
                    value = int((path / file).read_text())
                    return value if value >= 0 else None
                except (OSError, ValueError):
                    return None

            speed = counter("speed")
            rx = counter("statistics/rx_dropped")
            tx = counter("statistics/tx_dropped")
        elif sys.platform == "darwin":
            result = subprocess.run(
                ["/sbin/ifconfig", self.name],
                capture_output=True,
                text=True,
                timeout=1,
                check=True,
            )
            flags = result.stdout.partition("<")[2].partition(">")[0].split(",")
            if "status: inactive" in result.stdout or "UP" not in flags:
                state = "down"
            elif "status: active" in result.stdout or (
                "LOOPBACK" in flags and "RUNNING" in flags
            ):
                state = "up"
            else:
                state = "unknown"
        else:
            state = "unknown"
        return state, speed, rx, tx

    def collect(self):
        try:
            while not self.done.is_set():
                start = time.monotonic()
                try:
                    state, speed, rx, tx = self.sample()
                except (OSError, subprocess.SubprocessError):
                    state, speed, rx, tx = "unknown", None, None, None
                now = time.monotonic()
                row = dict(
                    utc=timestamp(),
                    at_ms=int((now - self.started) * 1000),
                    interface=self.name,
                    state=state,
                    speed_mbps=speed,
                    rx_dropped=rx,
                    tx_dropped=tx,
                    collection_us=round((now - start) * 1e6),
                )
                self.records.csv("linkstate.csv", row)
                self.latest = row
                self.done.wait(max(0, self.interval - (now - start)))
        except OSError as exc:
            self.error = exc

    def state(self, at_ms):
        if self.error:
            raise self.error
        if self.latest and at_ms - self.latest["at_ms"] <= self.interval * 2000:
            return self.latest["state"]
        return "unknown"

    def close(self):
        self.done.set()
        self.thread.join()
