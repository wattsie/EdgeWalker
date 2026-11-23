import unittest

from src.replay import Direction


class DelayTests(unittest.TestCase):
    def setUp(self):
        settings = dict(
            start_bps=100000,
            ceiling_bps=300000,
            ramp_step_bps=100000,
            probe_step_bps=10000,
            window_ms=5000,
            min_packets=3,
            probe_ms=10000,
            hold_ms=12000,
            suspend_ms=20000,
            early_loss_count=3,
            delay_policy=dict(growth_us=20000, observe_ms=5000, min_gain_bps=8000),
        )
        self.c = Direction(settings)
        self.now = self.seq = 0
        self.report(100000, 0)

    def report(self, delivery, growth, excess=0, lost=False, quantum=None):
        seq = list(range(self.seq + 1, self.seq + 6))
        event = dict(
            start_ms=self.now,
            end_ms=self.now + 5000,
            offered_bps=self.c.rate,
            first=seq[0],
            last=seq[-1],
            arrivals=seq[1:] if lost else seq,
            received_bytes=delivery * 5 // 8,
            excess_delay_us=excess,
        )
        if quantum is not None:
            event["udp_quantum_bps"] = quantum
        if growth is not None:
            event["delay_growth_us"] = growth
        self.seq += 5
        self.now += 5000
        return self.c.feedback(event, self.now, {})

    def test_drained_offer_needs_sustained_receiver_gain(self):
        self.c.last_clean = self.c.edge = 320000
        self.c.rate = 340000
        self.c.state = "probe"
        self.c.settings.update(ceiling_bps=480000, probe_step_bps=20000)
        self.assertEqual(self.report(328000, 0, quantum=8000), "verify_delivered_offer")
        self.assertIsNone(self.c.edge)
        self.assertEqual(self.c.last_clean, 320000)
        self.assertEqual(self.report(322667, 0, quantum=2667), "plateau_revalidate")
        self.assertEqual(self.c.rate, 320000)
        self.assertEqual(self.report(312000, 0, quantum=8000), "clean_hold")
        self.assertEqual(self.c.edge, 320000)

    def test_longer_check_can_retain_a_supported_gain(self):
        self.c.last_clean = self.c.edge = 320000
        self.c.rate = 340000
        self.c.state = "probe"
        self.c.settings.update(ceiling_bps=480000, probe_step_bps=20000)
        self.assertEqual(self.report(328000, 0, quantum=8000), "verify_delivered_offer")
        self.assertEqual(self.report(336000, 0, quantum=2667), "clean_hold")
        self.assertEqual(self.c.edge, 340000)
        self.assertIsNone(self.c.checking_offer)

    def test_growth_needs_flat_delivery_for_backoff(self):
        self.assertEqual(self.report(130000, 31000), "rising_delay_hold")
        self.assertEqual(self.report(160000, 31000), "delay_rising_with_gain")
        self.assertEqual(self.c.rate, 200000)
        self.assertEqual(self.report(161000, 31000), "rising_delay_flat_throughput")
        self.assertEqual(self.c.rate, 100000)
        self.assertIsNone(self.c.edge)
        self.assertEqual(self.report(100000, -30000, 130000), "delay_revalidating")
        self.assertEqual(self.report(100000, 1000, 1500), "clean_hold")

    def test_capacity_drop_during_revalidation(self):
        self.report(130000, 31000, 60000)
        self.report(130000, 31000, 120000)
        self.assertEqual(self.c.rate, 100000)
        self.assertEqual(self.report(55000, 30000, 170000), "rising_delay_hold")
        self.assertEqual(
            self.report(50000, 30000, 230000), "rising_delay_flat_throughput"
        )
        self.assertEqual(self.c.rate, 50000)
        self.assertIsNone(self.c.edge)

    def test_stable_delay_step_uses_verified_receiver_goodput(self):
        self.c.rate = 100000
        self.c.recovering_delay = True
        self.c.clean_history[100000] = (0, 85000)
        self.assertEqual(self.report(84000, 1000, 40000), "delay_revalidating")
        self.assertEqual(self.report(84000, 1000, 41000), "clean_ramp")

    def test_settled_growth_resumes_ramp(self):
        self.report(130000, 31000)
        self.assertEqual(self.report(130000, 0), "clean_ramp")

    def test_missing_timing_and_loss_precedence(self):
        self.report(130000, 31000)
        self.assertEqual(self.report(130000, None), "timing_unverified")
        self.assertEqual(self.report(130000, 31000, lost=True), "loss_backoff")

    def test_loss_only_records_growth(self):
        self.c.delay_policy = None
        self.assertEqual(self.report(130000, 31000), "clean_ramp")
