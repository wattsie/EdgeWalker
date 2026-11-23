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
