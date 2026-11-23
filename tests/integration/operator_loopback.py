"""Exercise the installed operator entry points on ephemeral local sockets."""

import csv
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import socket
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "edgewalker"


class OperatorLoopback(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.cfg = self.work / "config.json"
        self.cfg.touch(mode=0o600)
        self.cfg.write_text(json.dumps(dict(key=secrets.token_hex(8))))
        self.env = dict(
            os.environ,
            EDGEWALKER_CONFIG=str(self.cfg),
            EDGEWALKER_ENGINE=str(ROOT / "target/debug/edgewalker-native"),
        )
        self.launch_server("127.0.0.1:0")
        self.addCleanup(self.stop_server)

    def launch_server(self, addr):
        self.server = subprocess.Popen(
            [CLI, "server", "--listen", addr, "--capacity", "1000000"],
            stdout=subprocess.PIPE,
            env=self.env,
            text=True,
        )
        self.addr = json.loads(self.server.stdout.readline())["listening"]

    def stop_server(self):
        self.server.terminate()
        self.server.wait(timeout=3)
        self.server.stdout.close()

    def run_client(self, name, *args):
        return subprocess.Popen(
            [
                CLI,
                "run",
                "--connect",
                self.addr,
                "--window-ms",
                "1000",
                "--output",
                self.work / name,
                *args,
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def wait_rows(self, name):
        start = time.monotonic()
        while not (self.work / name / "windows.jsonl").exists():
            if time.monotonic() - start > 6:
                self.fail("no measured window")
            time.sleep(0.05)

    def test_invalid_native_budget_before_connection(self):
        p = self.run_client("invalid", "--mode", "tcp", "--forward-max", "2350000000",
                            "--duration", "2")
        _, err = p.communicate(timeout=4)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("maximum window at this ceiling is 340 ms", err)
        self.assertFalse((self.work / "invalid").exists())

    def test_explicit_invalid_flow_options_before_connect(self):
        for flag, value, message in [
            ("--flows", "0", "flow counts"), ("--flow-limit", "17", "flow counts"),
            ("--tcp-send-buffer", "0", "TCP send buffer"),
            ("--tcp-send-buffer", "8191", "TCP send buffer"),
            ("--tcp-send-buffer", "4194305", "TCP send buffer"),
        ]:
            with self.subTest(flag=flag, value=value):
                p = self.run_client("invalid", "--mode", "udp", "--forward-max", "400000",
                                    "--duration", "2", flag, value)
                _, err = p.communicate(timeout=4)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(message, err)
                self.assertFalse((self.work / "invalid").exists())
        for value in ("0", "8191", "4194305"):
            p = subprocess.run([self.env["EDGEWALKER_ENGINE"], "client", "--connect",
                                "127.0.0.1:0", "--mode", "udp", "--forward-max", "400000",
                                "--tcp-send-buffer", value], env=self.env, text=True,
                               capture_output=True, timeout=4)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("TCP send buffer", p.stderr)
            self.assertNotIn("peer_offline", p.stdout)

    def test_no_receiver_windows_is_failure(self):
        p = self.run_client("empty", "--mode", "udp", "--forward-max", "400000",
                            "--duration", "0.4")
        _, err = p.communicate(timeout=4)
        self.assertNotEqual(p.returncode, 0)
        manifest = json.loads((self.work / "empty/session.json").read_text())
        self.assertEqual(manifest["status"], "failed", err)
        self.assertIn("no_receiver_windows", manifest.values())

    def test_initial_offline_then_online(self):
        addr = self.addr
        self.stop_server()
        p = self.run_client("retry", "--mode", "udp", "--forward-max", "400000",
                            "--duration", "6")
        try:
            time.sleep(1)
        finally:
            self.launch_server(addr)
        _, err = p.communicate(timeout=9)
        self.assertEqual(p.returncode, 0, err)
        folder = self.work / "retry"
        self.assertEqual(json.loads((folder / "session.json").read_text())["status"],
                         "completed_with_gaps")
        self.assertIn("replacement_wait", (folder / "events.csv").read_text())
        self.assertGreater((folder / "series-fl.csv").stat().st_size, 0)

    def test_report_and_deadline(self):
        p = self.run_client(
            "data",
            "--mode",
            "combined",
            "--forward-start",
            "640000",
            "--forward-max",
            "800000",
            "--return-max",
            "400000",
            "--duration",
            "4",
        )
        _, err = p.communicate(timeout=8)
        self.assertEqual(p.returncode, 0, err)
        report = json.loads(
            subprocess.check_output([CLI, "report", self.work / "data"], env=self.env)
        )
        self.assertEqual(report["status"], "completed")
        self.assertTrue(all(r["windows"] >= 2 for r in report["directions"].values()))
        with (self.work / "data/series-fl.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["next_bps"], "800000")
        self.assertGreater(int(rows[0]["tcp_goodput_bps"]), 0)
        self.assertGreater(int(rows[0]["udp_sent"]), 0)
        self.assertNotIn(
            json.loads(self.cfg.read_text())["key"],
            "".join(p.read_text() for p in (self.work / "data").iterdir()),
        )

    def test_overload_and_standalone_report(self):
        self.stop_server()
        self.server = subprocess.Popen(
            [
                CLI,
                "server",
                "--listen",
                "127.0.0.1:0",
                "--capacity",
                "1000000",
                "--allow-overload-ms",
                "500",
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
        )
        self.addr = json.loads(self.server.stdout.readline())["listening"]
        loop = next(n for _, n in socket.if_nameindex() if n in ("lo", "lo0"))
        p = self.run_client(
            "optin",
            "--mode",
            "udp",
            "--return-start",
            "400000",
            "--return-max",
            "800000",
            "--overload-ms",
            "500",
            "--duration",
            "5",
            "--interface",
            loop,
        )
        _, err = p.communicate(timeout=8)
        self.assertEqual(p.returncode, 0, err)
        folder = self.work / "optin"
        with (folder / "series-rl.csv").open() as f:
            trials = [r for r in csv.DictReader(f) if r["phase"] == "overload"]
        self.assertEqual(len(trials), 1)
        self.assertEqual(trials[0]["next_bps"], "400000")
        self.assertTrue((folder / "linkstate.csv").is_file())
        html = folder / "report.html"
        result = json.loads(
            subprocess.check_output(
                [CLI, "report", folder, "--html", html], env=self.env
            )
        )
        self.assertIn("overload", result["directions"]["return"]["phases"])
        self.assertIn("<svg", html.read_text())

    def test_interrupt_releases_capacity(self):
        p = self.run_client("first", "--mode", "udp", "--forward-max", "1000000")
        self.wait_rows("first")
        time.sleep(0.12)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=3)
        self.assertEqual(p.returncode, 130)
        self.assertEqual(
            json.loads((self.work / "first/session.json").read_text())["status"],
            "interrupted",
        )
        time.sleep(0.15)
        p = self.run_client(
            "second", "--mode", "tcp", "--forward-max", "1000000", "--duration", "2"
        )
        _, err = p.communicate(timeout=6)
        self.assertEqual(p.returncode, 0, err)

    def test_same_session_suspend_and_manual_readiness(self):
        plan = self.work / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "return": {
                        "ceiling_bps": 800000,
                        "start_bps": 400000,
                        "hold_ms": 1800,
                        "suspend_ms": 2300,
                    }
                }
            )
        )
        p = self.run_client(
            "hold",
            "--mode",
            "udp",
            "--plan",
            str(plan),
            "--manual-resume",
            "--duration",
            "12",
        )
        try:
            self.wait_rows("hold")
            self.server.send_signal(signal.SIGSTOP)
            events = self.work / "hold/events.csv"

            def await_event(name):
                deadline = time.monotonic() + 6
                while name not in events.read_text():
                    if time.monotonic() > deadline:
                        self.fail(f"missing {name} in events:\n{events.read_text()}")
                    time.sleep(0.03)

            await_event("feedback_suspend")
            self.server.send_signal(signal.SIGCONT)
            await_event("manual_resume_required")
            size = (self.work / "hold/windows.jsonl").stat().st_size
            time.sleep(0.3)
            self.assertEqual((self.work / "hold/windows.jsonl").stat().st_size, size)
            p.send_signal(signal.SIGUSR1)
            await_event("manual_ready_restart")
            _, err = p.communicate(timeout=14)
            self.assertEqual(p.returncode, 0, err)
            manifest = json.loads((self.work / "hold/session.json").read_text())
            self.assertEqual(manifest["sessions"], 1)
            with (self.work / "hold/series-rl.csv").open() as f:
                rows = list(csv.DictReader(f))
            self.assertGreaterEqual(len(rows), 2)
            self.assertEqual(rows[1]["offer_bps"], "400000")
        finally:
            self.server.send_signal(signal.SIGCONT)
            if p.poll() is None:
                p.kill()
                p.communicate()

    def test_stop_during_manual_recovery(self):
        client = self.run_client(
            "stopped",
            "--mode",
            "udp",
            "--forward-max",
            "400000",
            "--manual-resume",
            "--duration",
            "14",
        )
        try:
            self.wait_rows("stopped")
            self.stop_server()
            events = self.work / "stopped/events.csv"
            deadline = time.monotonic() + 6
            while "manual_resume_required" not in events.read_text():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.03)
            self.launch_server(self.addr)
            client.send_signal(signal.SIGINT)
            client.communicate(timeout=4)
            self.assertEqual(client.returncode, 130)
            manifest = json.loads((self.work / "stopped/session.json").read_text())
            self.assertEqual(
                (manifest["status"], manifest["sessions"]), ("interrupted", 1)
            )
            self.assertNotIn("replacement_wait", events.read_text())
        finally:
            if client.poll() is None:
                client.kill()
                client.communicate()

    def test_reconnect_after_server_exit(self):
        p = self.run_client(
            "recovery", "--mode", "udp", "--return-max", "800000", "--duration", "7"
        )
        self.wait_rows("recovery")
        self.stop_server()
        time.sleep(0.25)
        self.launch_server(self.addr)
        _, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, err)
        report = json.loads(
            subprocess.check_output(
                [CLI, "report", self.work / "recovery"], env=self.env
            )
        )
        self.assertTrue(report["coverage_gaps"])
        manifest = json.loads((self.work / "recovery/session.json").read_text())
        self.assertGreaterEqual(manifest["sessions"], 2)


if __name__ == "__main__":
    unittest.main()
