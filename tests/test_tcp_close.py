"""Client contracts for bounded TCP receipt collection."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from live_menu import LiveMenu
from operator_cli import DRAIN_DEFAULTS, Deadline, TcpClose, drain_settings


class TcpFinalReceiptTests(unittest.TestCase):
    @staticmethod
    def session(rtt=None):
        session = TcpClose(DRAIN_DEFAULTS, 5000, 2)
        for window in (1, 2):
            session.observe({
                "window": {"id": window}, "control_rtt_ms": rtt,
                "server": {"tcp": [{"sent": 1}, {"sent": 1}]},
                "client": {"tcp": [{"sent": 2}, {"sent": 2}]},
            }, 50.0)
        return session

    @staticmethod
    def response():
        return {"status": "stopped", "tcp_final": {
            d: [dict(sent_records=count, received_records=count,
                     last_window=2, last_sequence=count // 2) for _ in range(2)]
            for d, count in (("forward", 2), ("return", 4))}}

    def test_end_to_end_sample_fallback_and_peer_cap(self):
        self.assertEqual(self.session(1).allowance(51), (250, 1000))
        sat = self.session(640)
        self.assertEqual(sat.allowance(51), (2020, 1000))
        self.assertEqual(sat.allowance(66), (5000, 16000))
        self.assertEqual(self.session().allowance(51), (5000, None))
        low_cap = TcpClose(DRAIN_DEFAULTS, 100, 1)
        self.assertEqual(low_cap.allowance(51), (100, None))

    @patch("operator_cli.time.monotonic", side_effect=[51, 51.02])
    def test_early_complete_and_one_shared_deadline(self, _clock):
        proc = Mock()
        proc.read.return_value = self.response()
        receipt = self.session(640).finish(proc, 51.5)
        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["stop_acknowledged"])
        self.assertEqual(receipt["wait_ms"], 20)
        self.assertEqual(receipt["remaining_budget_ms"], 500)
        proc.write.assert_called_once_with({"finish_ms": 470})
        proc.read.assert_called_once_with(51.5)

    def test_receiver_deficit_reported_per_flow_even_on_orderly_stop(self):
        reply = self.response()
        reply["tcp_final"]["return"][1].update(received_records=3, last_sequence=1)
        dirs, ack, status = self.session(640).check_reply(reply)
        self.assertEqual((ack, status), (True, "incomplete"))
        self.assertEqual(dirs["return"][1]["received_records"], 3)

    def test_matching_local_counts_never_confirm_missing_gateway(self):
        reply = self.response()
        reply["status"] = "unconfirmed"
        for flow in reply["tcp_final"]["return"]:
            flow.update(received_records=None, last_sequence=None, last_window=None)
        dirs, ack, status = self.session().check_reply(reply)
        self.assertEqual((ack, status), (False, "unknown"))
        self.assertEqual(dirs["forward"][0]["received_records"], 2)

    def test_receipt_timeout_or_malformed_count_retains_unknown(self):
        for kind in ("timeout", "malformed", "expired"):
            with self.subTest(kind=kind), patch("operator_cli.time.monotonic", return_value=51):
                proc = Mock()
                reply = self.response()
                reply["tcp_final"]["forward"][0]["sent_records"] = 999
                proc.read.return_value = reply
                if kind == "timeout":
                    proc.read.side_effect = Deadline
                r = self.session().finish(proc, 51 if kind == "expired" else 55)
                self.assertFalse(r["stop_acknowledged"])
                self.assertEqual(r["status"], "unknown")
                if kind == "expired":
                    proc.write.assert_not_called()

    def test_plan_precedence_constraints_and_menu_save(self):
        args = argparse.Namespace(drain_max_ms=760)
        settings = drain_settings(args, {"max_ms": 1500, "jitter_ms": 73})
        self.assertEqual(settings, dict(DRAIN_DEFAULTS, max_ms=760, jitter_ms=73))
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp, "saved.json")
            directional = {"forward": {"ceiling_bps": 1000000}}
            with patch("sys.stdin", io.StringIO("save " + str(target) + "\n")), patch(
                    "select.select", return_value=([True], [], [])), contextlib.redirect_stderr(io.StringIO()):
                LiveMenu(directional, "tcp", settings).poll({}, directional, 55, Mock(), None, Mock(), None)
            saved = json.loads(target.read_text())
            self.assertEqual(drain_settings(argparse.Namespace(), saved["drain"]), settings)
        for rejected in ([1], {"max_ms": True}, {"floor": 1}, {"min_ms": 5001},
                         {"rtt_multiplier": -1}, {"jitter_ms": 30001}):
            with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                drain_settings(argparse.Namespace(), rejected)
