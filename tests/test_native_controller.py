"""Regression for a queue spanning two native receiver windows at the same offer."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from operator_cli import LiveDirection, send_time_available_ms


class DeadlineBudget(unittest.TestCase):
    def test_last_combined_return_window_fits_measured_feedback(self):
        # The previous 1,400 ms window closed at 8,154 ms.
        elapsed_ms = 8154
        previous_window_start_ms = 6222
        feedback_ms = elapsed_ms - previous_window_start_ms - 1400
        remaining_s = (10000 - elapsed_ms) / 1000
        ms = send_time_available_ms(remaining_s, feedback_ms)
        self.assertGreaterEqual(ms, 250)
        self.assertLessEqual(elapsed_ms + ms + 538, 10000)
        self.assertEqual(send_time_available_ms(0.6, feedback_ms), -32)

    def test_unmeasured_and_open_deadlines_preserve_existing_bounds(self):
        self.assertEqual(send_time_available_ms(1.5, 0), 1000)
        self.assertEqual(send_time_available_ms(float("inf"), 800), 10000)


class QueuedDelivery(unittest.TestCase):
    def setUp(self):
        cfg = dict(
            start_bps=400000,
            ceiling_bps=560000,
            ramp_step_bps=80000,
            probe_step_bps=40000,
            window_ms=2000,
            min_packets=12,
            early_loss_count=3,
            probe_ms=3000,
            hold_ms=7000,
            suspend_ms=15000,
            initial_flows=1,
            min_gain_bps=12000,
            growth_us=10000,
            target_bps=None,
            minimum_bps=0,
            delay_policy=dict(growth_us=10000, observe_ms=2000, min_gain_bps=12000),
        )
        direction = LiveDirection(cfg, "udp")
        direction.baseline = 0
        control = direction.c
        control.state = "revalidate"
        control.last_clean = 400000
        control.clean_history = {240000: (1000, 230000), 400000: (2000, 350000)}
        control.clean_delay = 2000
        control.recovering_delay = True
        sender = dict(
            udp=dict(sent=100),
            tcp=[],
            udp_sequence_base=0,
            timing=dict(
                requested=0, valid=0, mean_raw_rtt_us=None, mean_adjusted_rtt_us=None
            ),
        )
        rx = dict(
            window_bytes=19000,
            arrival_window_bytes=60000,
            skipped=0,
            highest=30,
            median_transit_us=1253000,
            delay_growth_us=240000,
            mean_abs_variation_us=None,
            reordered=0,
            duplicates=0,
        )
        receiver = dict(udp=dict(received=rx), tcp=[])
        self.direction, self.sender, self.receiver = direction, sender, receiver

    def test_cross_window_drain_does_not_invent_throughput_gain(self):
        direction, sender, receiver = self.direction, self.sender, self.receiver
        rx = receiver["udp"]["received"]
        held = direction.update(sender, receiver, 2000, 0, 2400)
        self.assertEqual(
            (held["reason"], held["delivered_bps"], held["delay_evidence_bps"]),
            ("rising_delay_hold", 76000, 240000),
        )
        rx.update(
            window_bytes=0, highest=0, median_transit_us=None, delay_growth_us=None
        )
        receiver["late_udp_observations"] = dict(
            median_transit_us=1900000, delay_growth_us=230000
        )
        backoff = direction.update(sender, receiver, 2000, 2400, 4800)
        self.assertEqual(
            (backoff["reason"], backoff["next_bps"], backoff["udp_lost"]),
            ("rising_delay_flat_throughput", 240000, 0),
        )
        self.assertEqual(backoff["timing_source"], "previous_same_offer")
        self.assertIsNone(backoff["edge_bps"])

    def test_only_new_confirmed_loss_at_same_offer_can_back_off(self):
        direction, sender, receiver = self.direction, self.sender, self.receiver
        for prior_window, previous_offer, old_loss, should_reduce in [
            (6, 400000, 0, False),
            (7, 240000, 0, False),
            (7, 400000, 4, False),
            (7, 400000, 1, True),
        ]:
            with self.subTest(
                prior_window=prior_window,
                previous_offer=previous_offer,
                old_loss=old_loss,
            ):
                direction.previous_window = 7
                direction.previous_offer = previous_offer
                direction.previous_lost = old_loss
                receiver["prior_udp"] = dict(window=prior_window, lost=5)
                receiver["udp"]["received"].update(
                    median_transit_us=None, delay_growth_us=None
                )
                row = direction.update(sender, receiver, 2000, 0, 2400)
                self.assertEqual(
                    row["reason"] == "confirmed_prior_window_loss_backoff",
                    should_reduce,
                )
                self.assertEqual(row["udp_lost"], 0)
                self.assertIsNone(row["edge_bps"])

    def test_four_packet_rain_fade_windows_recover_without_zero_filling_timing(self):
        cfg = dict(self.direction.c.settings, start_bps=5000, min_packets=3,
                   window_ms=3000, probe_ms=3500, probe_step_bps=20000)
        direction = LiveDirection(cfg, "udp")
        self.sender["udp"]["sent"] = 4
        self.receiver["udp"]["received"].update(highest=4, window_bytes=4000,
            arrival_window_bytes=4000, median_transit_us=None, delay_growth_us=None,
            sparse_transit_us=[400,405,398,401])
        observed = []
        for i in range(3):
            self.sender["udp_sequence_base"] = 4*i
            observed.append(direction.update(self.sender,self.receiver,6499,i*6920,(i+1)*6920))
            if i==0:
                self.assertEqual(observed[0]["reason"],"timing_unverified")
                self.assertIsNone(observed[0]["edge_bps"])
        self.assertEqual(observed[1]["timing_source"],"contiguous_sparse_windows")
        self.assertEqual(observed[1]["edge_bps"],5000)
        self.assertGreater(observed[1]["next_bps"],5000)

    def test_sparse_timing_cannot_span_missing_sender_range(self):
        cfg = dict(self.direction.c.settings, start_bps=5000, min_packets=3)
        direction = LiveDirection(cfg, "udp")
        self.sender["udp"]["sent"] = 4
        self.receiver["udp"]["received"].update(highest=4, window_bytes=4000,
            arrival_window_bytes=4000, median_transit_us=None, delay_growth_us=None,
            sparse_transit_us=[400,405,398,401])
        direction.update(self.sender,self.receiver,6499,0,6920)
        self.sender["udp_sequence_base"] = 8
        row = direction.update(self.sender,self.receiver,6499,6920,13840)
        self.assertEqual(row["reason"],"timing_unverified")
        self.assertIsNone(row["edge_bps"])

    def test_sparse_single_packet_feedback_keeps_packet_gate(self):
        cfg = dict(self.direction.c.settings, start_bps=1250, min_packets=3)
        direction = LiveDirection(cfg, "udp")
        self.sender["udp"]["sent"] = 1
        self.receiver["udp"]["received"].update(highest=1, window_bytes=1000,
            arrival_window_bytes=1000, median_transit_us=None, delay_growth_us=None,
            sparse_transit_us=[401])
        observed=[]
        for i in range(9):
            self.sender["udp_sequence_base"] = i
            row=direction.update(self.sender,self.receiver,6499,i*6920,(i+1)*6920)
            observed.append(row)
            if row["next_bps"]>1250:break
        self.assertTrue(all(r["edge_bps"] is None for r in observed[:5]))
        self.assertGreater(observed[-1]["next_bps"],1250)
