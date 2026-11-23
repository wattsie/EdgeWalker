import json
import subprocess
import sys
import unittest
from pathlib import Path

from src.replay import replay


ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "examples/udp-replay.json"


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.trace = json.loads(TRACE.read_text())
        rows = replay(self.trace)
        self.forward = {r["at_ms"]: r for r in rows if r["direction"] == "forward"}
        self.reverse = {r["at_ms"]: r for r in rows if r["direction"] == "return"}

    def test_independent_processes(self):
        cmd = [sys.executable, str(ROOT / "src/replay.py"), str(TRACE)]
        first = subprocess.check_output(cmd)
        self.assertEqual(first, subprocess.check_output(cmd))
        self.assertEqual([json.loads(line) for line in first.splitlines()], replay(self.trace))

    def test_loss_and_directional_ceiling(self):
        f = self.forward[5000]
        self.assertEqual((f["lost"], f["reordered"], f["duplicates"]), (1, 1, 0))
        self.assertEqual((f["delivered_bps"], f["offered_bps"]), (6400, 3200))
        self.assertEqual(self.reverse[5000]["reason"], "clean_ramp")
        self.assertEqual(self.reverse[10000]["reason"], "clean_at_ceiling")
        self.assertLessEqual(max(row["offered_bps"] for row in self.reverse.values()), 8000)

    def test_sample_gate_probe_and_revalidation(self):
        f = self.forward
        self.assertIsNone(f[10000]["last_clean_bps"])
        self.assertEqual(f[12500]["window_interval_ms"], [5000, 12500])
        self.assertEqual((f[12500]["last_clean_bps"], f[12500]["window_sent"]), (3200, 3))
        self.assertEqual((f[20000]["state"], f[20000]["offered_bps"]), ("probe", 4800))
        self.assertEqual((f[25000]["state"], f[25000]["offered_bps"]), ("revalidate", 3200))
        self.assertIsNone(f[25000]["edge_bps"])
        self.assertEqual(f[32500]["edge_bps"], 3200)

    def test_stale_feedback_and_readiness(self):
        f = self.forward
        self.assertEqual((f[39500]["state"], f[39500]["offered_bps"]), ("hold", 3200))
        self.assertIsNone(f[39500]["edge_bps"])
        self.assertEqual(f[40000]["reason"], "ignored_feedback")
        for time in (42500, 43100, 43200):
            self.assertEqual((f[time]["state"], f[time]["offered_bps"]), ("suspended", 0))
        self.assertEqual(f[43300]["offered_bps"], 6400)
        self.assertIsNone(f[43300]["edge_bps"])
        self.assertEqual(f[48300]["edge_bps"], 6400)

    def test_time_gate_and_window_boundaries(self):
        first = dict(self.trace["events"][0], at_ms=1000, end_ms=1000, arrivals=[1,2,3,4])
        second = dict(first, at_ms=2000, start_ms=1000, end_ms=2000, first=5,
                      last=9, arrivals=[6,6,8])
        self.trace["events"] = [first, second]
        rows = replay(self.trace)
        self.assertEqual(rows[-2]["reason"], "observing")
        self.assertEqual((rows[-1]["lost"], rows[-1]["duplicates"]), (3, 1))
        self.assertEqual(rows[-1]["reason"], "loss_backoff")

    def test_invalid_timeline(self):
        self.trace["events"][-1]["at_ms"] = 1
        with self.assertRaisesRegex(ValueError, "at_ms"):
            replay(self.trace)


class PlateauTests(unittest.TestCase):
    path = ROOT / "examples/fixed-limit-plateau.json"

    def setUp(self):
        self.trace = json.loads(self.path.read_text())

    def test_fixed_limit_and_periodic_probes(self):
        rows = replay(self.trace)
        expected = [
            (0, "ramp", "start", 3200),
            (5000, "ramp", "clean_ramp", 4800),
            (10000, "ramp", "clean_ramp", 6400),
            (15000, "ramp", "clean_ramp", 8000),
            (20000, "ramp", "clean_ramp", 9600),
            (25000, "cruise", "plateau_hold", 6400),
            (30000, "cruise", "clean_hold", 6400),
            (35000, "probe", "upward_probe", 7200),
            (40000, "cruise", "plateau_probe_return", 6400),
            (45000, "cruise", "clean_hold", 6400),
            (50000, "probe", "upward_probe", 7200),
            (55000, "cruise", "plateau_probe_return", 6400),
            (60000, "cruise", "clean_hold", 6400),
        ]
        self.assertEqual([(r["at_ms"], r["state"], r["reason"], r["offered_bps"])
                          for r in rows], expected)
        ceiling = self.trace["directions"]["forward"]["ceiling_bps"]
        self.assertEqual(ceiling, 9600)
        for r in rows[1:]:
            self.assertLessEqual(r["offered_bps"], ceiling)
            self.assertEqual(r["lost"], 0)
            self.assertEqual(r["window_delivered_bps"], min(r["measured_offer_bps"], 6400))
        self.assertEqual(rows[5]["best_offer_bps"], 6400)
        self.assertEqual(rows[5]["last_clean_bps"], 9600)
        self.assertEqual(rows[5]["edge_bps"], 6400)

    def test_independent_plateau_runs(self):
        cmd = [sys.executable, str(ROOT / "src/replay.py"), str(self.path)]
        first = subprocess.check_output(cmd)
        self.assertEqual(first, subprocess.check_output(cmd))
        self.assertEqual([json.loads(line) for line in first.splitlines()], replay(self.trace))

    def test_probe_can_retain_gain(self):
        self.trace["events"] = self.trace["events"][:8]
        self.trace["events"][-1]["received_bytes"] = 4500
        row = replay(self.trace)[-1]
        self.assertEqual((row["offered_bps"], row["best_delivered_bps"]), (7200, 7200))
        self.assertEqual((row["state"], row["reason"]), ("cruise", "clean_hold"))

    def test_delay_change_breaks_plateau_evidence(self):
        self.trace["events"] = self.trace["events"][:5]
        self.trace["events"][-1]["excess_delay_us"] += 1000
        self.assertEqual(replay(self.trace)[-1]["reason"], "clean_at_ceiling")

    def test_delivered_rate_uses_whole_window(self):
        events = self.trace["events"][:3]
        original = events.pop()
        seq = original["first"]
        events.extend([
            dict(original, at_ms=11000, end_ms=11000, last=seq,
                 arrivals=[seq], received_bytes=500),
            dict(original, start_ms=11000, first=seq+1,
                 arrivals=original["arrivals"][1:], received_bytes=3500),
        ])
        self.trace["events"] = events
        row = replay(self.trace)[-1]
        self.assertEqual(row["delivered_bps"], 7000)
        self.assertEqual(row["best_delivered_bps"], 6400)
