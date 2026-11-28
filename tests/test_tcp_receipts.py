"""Replay a session-final TCP watermark without changing the original windows."""

import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from src.records import report
from src.tcp_receipts import final_window_bytes, tcp_final_accounting


ROOT = Path(__file__).resolve().parents[1]


def endpoint(sent, received):
    return {"tcp": [{"sent": sent, "received": {"window_bytes": received * 1000,
                     "bytes": received * 1000, "arrival_window_bytes": received * 1000}}]}


def window(session, number, sent=3, seen=1):
    return dict(session=session, start_ms=0, decision_ms=1,
                window=dict(id=number, ms=250, rates=dict(forward=0, reverse=100000)),
                server=endpoint(0, seen), client=endpoint(sent, 0))


def final(session, window, sequence, written, seen, status="complete"):
    return dict(session=session, status=status, stop_acknowledged=status != "unknown",
                control_rtt_ms=640, sample_age_ms=5000, budget_ms=2020, effective_max_ms=5000,
                wait_ms=710, reason="all_records_confirmed" if status == "complete" else "outstanding_records",
                directions={"return": [dict(sent_records=written, received_records=seen,
                                            last_window=window, last_sequence=sequence)]})


class FinalTCP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.run.mkdir()
        self.manifest = dict(mode="tcp", settings={"return": {}}, native_version="edgewalker 0.2.2-rc.7",
                             sessions=1, status="completed", stop_acknowledged=True)

    def evidence(self, windows, finals):
        (self.run / "session.json").write_text(json.dumps(self.manifest))
        for filename, data in [("windows.jsonl", windows), ("tcp-final.jsonl", finals)]:
            (self.run / filename).write_text("".join(json.dumps(r) + "\n" for r in data))

    def test_final_prefix_credits_original_windows_and_leaves_unsampled_tail(self):
        receipts = [final(1, 2, 2, 6, 5, "incomplete")]
        self.evidence([window(1, 1), window(1, 2)], receipts)
        sessions, notes, complete = tcp_final_accounting(self.run, self.manifest)
        self.assertFalse(complete)
        self.assertEqual(notes, [])
        self.assertEqual(sessions[1]["directions"]["return"][0]["outstanding_records"], 1)
        self.assertEqual(final_window_bytes(sessions, 1, "return", 0, 1, 3), 3000)
        self.assertEqual(final_window_bytes(sessions, 1, "return", 0, 2, 3), 2000)
        self.assertFalse(report(self.run)["coverage_complete"])

    def test_incomplete_window_archive_disables_attribution(self):
        self.evidence([window(1, 1), window(1, 3)], [final(1, 3, 3, 9, 9)])
        sessions, notes, complete = tcp_final_accounting(self.run, self.manifest)
        self.assertFalse(complete)
        self.assertTrue(notes)
        self.assertIsNone(final_window_bytes(sessions, 1, "return", 0, 3, 3))

    def test_first_missing_window_or_session_keeps_coverage_incomplete(self):
        self.evidence([window(1, 2)], [final(1, 2, 3, 3, 3)])
        sessions, notes, complete = tcp_final_accounting(self.run, self.manifest)
        self.assertFalse(complete)
        self.assertTrue(notes)
        self.assertIsNone(final_window_bytes(sessions, 1, "return", 0, 2, 3))
        self.manifest["sessions"] = 2
        self.evidence([window(2, 2)], [final(2, 2, 3, 3, 3)])
        sessions, notes, complete = tcp_final_accounting(self.run, self.manifest)
        self.assertFalse(complete)
        self.assertIn("Missing session-final TCP coverage.", notes)
        self.assertEqual(list(sessions), [2])

    def test_session_boundary_and_missing_ack(self):
        self.manifest["sessions"] = 2
        self.evidence([window(1, 1), window(2, 3)],
                      [final(1, None, None, 3, None, "unknown"), final(2, 3, 3, 3, 3)])
        sessions, notes, complete = tcp_final_accounting(self.run, self.manifest)
        self.assertFalse(complete)
        self.assertIn("Missing or out-of-order original TCP window.", notes)
        self.assertIsNone(final_window_bytes(sessions, 1, "return", 0, 1, 3))
        self.assertEqual(final_window_bytes(sessions, 2, "return", 0, 3, 3), 3000)

    def test_field_bundle_preserves_original_observations_and_only_selected_fields(self):
        self.manifest.update(connect="PRIVATE-GATEWAY", key="PRIVATE-CONFIG",
                             drain_settings={"min_ms": 250, "secret": "PRIVATE-CONFIG"})
        r = final(1, 1, 3, 3, 3)
        r.update(token="PRIVATE-CONFIG", reason="PRIVATE-TEXT")
        self.evidence([window(1, 1)], [r])
        proc = subprocess.run([sys.executable, ROOT / "validation/review.py", self.run,
                               "--profile", "higher-return", "--exit-code", "0"],
                              capture_output=True, text=True, timeout=3)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        path, = self.root.glob("*.zip")
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                self.assertNotIn(b"PRIVATE", archive.read(member))
            summary = json.loads(archive.read("session-summary.json"))
            self.assertTrue(summary["tcp_final_complete"])
            self.assertEqual(summary["tcp_final"][0]["wait_ms"], 710)
            self.assertEqual(summary["drain_settings"], {"min_ms": 250})
            row, = csv.DictReader(io.StringIO(archive.read("tcp-windows.csv").decode()))
            self.assertEqual((row["cutoff_bytes"], row["final_received_bytes"]), ("1000", "3000"))

    def test_offline_review_of_legacy_or_missing_receipt_remains_possible(self):
        self.evidence([window(1, 1)], [])
        (self.run / "tcp-final.jsonl").unlink()
        proc = subprocess.run([sys.executable, ROOT / "validation/review.py", self.run],
                              capture_output=True, text=True, timeout=3)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path, = self.root.glob("*.zip")
        with zipfile.ZipFile(path) as archive:
            row, = csv.DictReader(io.StringIO(archive.read("tcp-windows.csv").decode()))
            self.assertEqual(row["final_received_bytes"], "")
            self.assertFalse(json.loads(archive.read("session-summary.json"))["tcp_final_complete"])
