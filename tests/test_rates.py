from argparse import Namespace
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rates import display_rate, rate_bps
from replay import replay
from operator_cli import plan


class RateTests(unittest.TestCase):
    def test_exact_decimal_units(self):
        for value, expected in [("1k", 1000), ("1.5M", 1500000), ("1G", 10**9),
                                ("1000000", 10**6), ("1.5000000m", 1500000),
                                ("0.000000001g", 1), ("18446744073.709551615G", 2**64-1)]:
            self.assertEqual(rate_bps(value), expected)
        for value in ["1MB", "1Mi", "1e6", "0.0001k", "-1", "1.2.3M", "18446744073.709551616G", True, 1.5]:
            with self.assertRaises(ValueError):
                rate_bps(value)
        self.assertEqual(display_rate(1500000), "1.5 Mbps")

    def test_plan_compares_cli_and_json_after_normalizing(self):
        args = Namespace(forward_max=1500000, return_max=None, forward_start=None,
                         return_start=None, window_ms=500, flow_limit=4, flows=None,
                         overload_ms=0, udp_size=1000)
        result = plan(args, {"forward": {"ceiling_bps": "1.5M", "start_bps": "1k",
                                        "probe_step_bps": "1.25k"}})["forward"]
        self.assertEqual((result["ceiling_bps"], result["start_bps"], result["probe_step_bps"]),
                         (1500000, 1000, 1250))

    def test_replay_normalizes_operator_units(self):
        trace = json.loads((ROOT / "examples/udp-replay.json").read_text())
        expected = replay(trace)
        for cfg in trace["directions"].values():
            cfg["start_bps"] = f'{cfg["start_bps"] / 1000}k'
        for event in trace["events"]:
            if "offered_bps" in event:
                event["offered_bps"] = f"{event['offered_bps']/1000}k"
        self.assertEqual(replay(trace), expected)
