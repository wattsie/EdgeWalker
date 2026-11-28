import csv
import json
from pathlib import Path
import tempfile
import unittest

from src.records import report


class RecordRecovery(unittest.TestCase):
    def test_late_feedback_correction_without_csv_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'session.json').write_text(json.dumps({
                'status': 'completed_with_gaps', 'settings': {'forward': {}}
            }))
            row = dict(session=1, window=7, phase='steady', udp_sent=10,
                       udp_lost=1, udp_pending=3, received_bytes=6000,
                       start_ms=0, end_ms=1000)
            with (root / 'series-fl.csv').open('w') as f:
                writer = csv.DictWriter(f, fieldnames=row)
                writer.writeheader()
                writer.writerow(row)
            correction = dict(window=7, sent=10, pending=1, lost=2)
            samples = root / 'windows.jsonl'
            samples.write_text(json.dumps(dict(session=1, client=dict(prior_udp=correction)))+'\n{"')
            result = report(root)['directions']['forward']['phases']['whole']
            self.assertEqual((result['udp_pending'], result['udp_lost']), (1, 2))
            self.assertIsNone(result['udp_loss_pct'])

    def test_corrected_udp_cohort_and_cutoff_delivery_use_separate_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "session.json").write_text(json.dumps({
                "status": "completed", "settings": {"return": {"udp_size": 800}}
            }))
            row = dict(session=1, window=1, phase="steady", udp_sent=10,
                       udp_pending=3, udp_lost=0, received_bytes=4800,
                       cohort_received_bytes=5600, start_ms=0, end_ms=1000,
                       last_clean_bps="", reason="trailing_delivery_unverified")
            with (root / "series-rl.csv").open("w") as f:
                writer = csv.DictWriter(f, fieldnames=row)
                writer.writeheader()
                writer.writerow(row)
            correction = dict(window=1, sent=10, pending=0, lost=2, reordered=1)
            (root / "windows.jsonl").write_text(json.dumps({
                "session": 1, "server": {"prior_udp": correction}
            }))
            result = report(root)
            returned = result["directions"]["return"]
            self.assertTrue(result["coverage_complete"])
            self.assertEqual(returned["active_delivered_bps"], 38400)
            self.assertEqual(returned["udp_cohort_bytes"], 7200)
            self.assertEqual(returned["udp_cohort_bps"], 57600)
            self.assertEqual(returned["steady_udp_loss_pct"], 20)
            self.assertIsNone(returned["last_verified_clean_bps"])
