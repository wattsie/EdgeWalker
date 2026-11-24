import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from calibrate import calibrate, comparable, propose


def sample(**changes):
    data = dict(
        complete=True,
        exit_code=0,
        clean_windows=5,
        cap_ok=True,
        clean_feedback_ms=[920, 950, 900],
        clean_rates=[20000, 30000],
        suspensions=0,
        holds=0,
        probes=0,
        backoffs=0,
        goodput_bps=20000,
        whole_budget_bps=19000,
        lost=0,
        reordered=0,
        sent=100,
    )
    return dict(data, **changes)


class Calibration(unittest.TestCase):
    def test_insufficient_baseline(self):
        self.assertEqual(propose(sample(clean_windows=0), sample(), 40000, 700), {})
        self.assertFalse(comparable(sample(), sample(complete=False)))
        self.assertFalse(comparable(sample(), sample(cap_ok=False)))

    def test_supported_settings_only(self):
        p = propose(sample(), sample(), 40000, 700)
        self.assertEqual(p, dict(hold_ms=1900, ramp_step_bps=20000))
        self.assertFalse(comparable(sample(), sample(goodput_bps=15000)))
        self.assertFalse(comparable(sample(), sample(lost=1)))

    def test_feedback_cadence_and_recovery_gate(self):
        ref = sample(
            report_intervals_ms=[1150] * 5, successful_restarts=1, suspensions=1
        )
        chosen = propose(sample(), ref, 400000, 700)
        self.assertEqual(chosen["feedback_ms"], 900)
        self.assertGreater(chosen["suspend_ms"], chosen["hold_ms"])
        self.assertEqual(propose(sample(lost=2), ref, 400000, 700), {})
        self.assertFalse(comparable(sample(), sample(whole_budget_bps=8000)))

    def test_budget_and_output_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence"
            args = SimpleNamespace(
                forward_max=100000,
                return_max=None,
                window_ms=700,
                duration=0.01,
                connect="127.0.0.1:1",
                config=None,
                output=out,
            )
            self.assertEqual(calibrate(args), 2)
            self.assertFalse((out / "baseline-forward").exists())
            self.assertNotIn(
                "hold_ms", json.loads((out / "plan.json").read_text())["forward"]
            )
            with self.assertRaises(FileExistsError):
                calibrate(args)

    def test_automatic_window_honors_gigabit_packet_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence"
            args = SimpleNamespace(
                forward_max=1_000_000_000,
                return_max=None,
                window_ms=None,
                duration=0.001,
                output=out,
            )
            self.assertEqual(calibrate(args), 2)
            self.assertEqual(
                json.loads((out / "calibration.json").read_text())["window_ms"], 800
            )

    def test_invalid_inputs_have_no_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "unused"
            args = SimpleNamespace(
                forward_max=0, return_max=None, duration=60, window_ms=5000, output=out
            )
            with self.assertRaises(ValueError):
                calibrate(args)
            self.assertFalse(out.exists())
