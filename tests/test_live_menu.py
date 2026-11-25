import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from live_menu import LiveMenu, OperatorStop


class LiveMenuContracts(unittest.TestCase):
    def setUp(self):
        self.settings = {"forward": {"ceiling_bps": 200000, "start_bps": 100000,
                                    "minimum_bps": 0, "flow_limit": 2, "feedback_ms": 500}}
        self.direction = Mock(suspended=False, flows=1)
        self.direction.c.rate = 100000
        self.menu = LiveMenu(self.settings, "tcp")
        self.events = Mock()
        self.resize = Mock()

    def command(self, text):
        with patch("sys.stdin", io.StringIO(text + "\n")), patch(
            "select.select", return_value=([True], [], [])
        ), contextlib.redirect_stderr(io.StringIO()):
            return self.menu.poll({"forward": self.direction}, self.settings, 123,
                                  self.events, self.resize, Mock(), Mock())

    def test_rejects_outside_limits_before_effect(self):
        for text in ("flows forward 3", "rate forward 200001", "rate return 100000",
                     "rate forward -1", "set forward key abc", "remaining -1"):
            with self.subTest(command=text):
                self.assertEqual(self.command(text), 123)
        self.assertEqual(self.direction.flows, 1)
        self.assertEqual(self.direction.c.rate, 100000)
        self.events.assert_not_called()
        self.resize.assert_not_called()

    def test_save_round_trip_and_preserve_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Field A, June.json"
            self.command("save " + str(path))
            self.assertEqual(json.loads(path.read_text()), self.settings)
            path.write_text("existing")
            self.command("save " + str(path))
            self.assertEqual(path.read_text(), "existing")

    def test_open_deadline_and_terminal_stop(self):
        self.assertEqual(self.command("remaining open"), float("inf"))
        self.events.assert_called_once_with("operator_remaining_seconds", "", "open")
        with self.assertRaises(OperatorStop):
            self.command("stop")

    def test_wrapper_modes_and_explicit_terminal(self):
        cli = str(ROOT / "edgewalker")
        for argv in ([], ["--help"], ["-m", "--help"], ["--menu", "--help"],
                     ["--server", "--help"], ["-s", "--help"], ["run", "--help"]):
            with self.subTest(args=argv):
                p = subprocess.run([cli, *argv], capture_output=True, text=True, timeout=3)
                self.assertEqual(p.returncode, 0, p.stderr)
        p = subprocess.run([cli, "--menu"], input="", capture_output=True, text=True, timeout=3)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("requires a terminal", p.stderr)
