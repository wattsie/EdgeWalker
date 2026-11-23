"""Offline contracts for structured input and text rendered from saved records."""
import csv
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'edgewalker'


class TextNodes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)


class InputContracts(unittest.TestCase):
    def test_invalid_saved_input_has_a_bounded_diagnostic_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / 'Café, June.json'
            trace.write_text('[]')
            (root / 'session.json').write_text('[]')
            output = root / 'report.html'
            for argv in ([CLI, 'replay', trace], [CLI, 'report', root, '--html', output]):
                with self.subTest(command=argv[1]):
                    result = subprocess.run(argv, capture_output=True, text=True, timeout=3)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('edgewalker:', result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertFalse(result.stdout)
                    self.assertFalse(output.exists())

    def test_report_preserves_literal_event_text_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = 'Café & "June" <band A>'
            manifest = dict(status='completed_with_gaps', mode='udp', started_utc='2026-01-01',
                            run_id='sample', settings={'forward': {}})
            (root / 'session.json').write_text(json.dumps(manifest))
            with (root / 'events.csv').open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['utc','at_ms','direction','event','detail'])
                writer.writerow(['2026-01-01',0,'forward','operator_note',note])
            target = root / 'session report (Δ).html'
            cmd = [CLI, 'report', root, '--html', target]
            first = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            self.assertEqual(first.returncode, 0, first.stderr)
            html = target.read_text()
            nodes = TextNodes()
            nodes.feed(html)
            self.assertIn(note, nodes.text)
            self.assertIn('&lt;band A&gt;', html)
            self.assertNotIn('<band A>', html)
            repeat = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            self.assertNotEqual(repeat.returncode, 0)
            self.assertEqual(target.read_text(), html)
