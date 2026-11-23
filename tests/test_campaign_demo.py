"""End-to-end demo for the offline campaign-fit report, using committed fixtures.

fixtures/campaign-demo/ holds a two-display signage batch: GATE-64 (64x32
with a 60x24 safe rectangle) and PIER-32 (32x16 with a 30x12 safe
rectangle), four approved messages sharing one v1 glyph set and kerning
table.  M-EXIT and M-GATE fit the gate display; the tall '7' pushes M-GATE's
ink below the pier display's safe rectangle; M-LONG needs three lines on the
pier but has only two; M-WIDE is one protected 64-advance chunk that fits
neither safe width.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
DEMO = ROOT / 'fixtures' / 'campaign-demo'


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, '-m', 'pixel_type.cli', *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


class CampaignDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_demo_report_matches_committed_bytes(self):
        out_path = self.dir / 'report.json'
        proc = run_cli('campaign-fit', '--source', str(DEMO / 'source.json'),
                       '--output', str(out_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary, {'combinations': 8, 'eligible': 4,
                                   'ineligible': 4})
        self.assertEqual(out_path.read_bytes(),
                         (DEMO / 'report.json').read_bytes())

    def test_demo_verdicts(self):
        report = json.loads((DEMO / 'report.json').read_bytes())
        verdicts = {(r['message_id'], r['display_id']):
                    r['eligible'] or r['reason'] for r in report['results']}
        self.assertEqual(verdicts, {
            ('M-EXIT', 'GATE-64'): True,
            ('M-EXIT', 'PIER-32'): True,
            ('M-GATE', 'GATE-64'): True,
            ('M-GATE', 'PIER-32'): 'ink_out_of_bounds',
            ('M-LONG', 'GATE-64'): True,
            ('M-LONG', 'PIER-32'): 'line_or_width_limit_exceeded',
            ('M-WIDE', 'GATE-64'): 'line_or_width_limit_exceeded',
            ('M-WIDE', 'PIER-32'): 'line_or_width_limit_exceeded',
        })
        # The tall '7' (raw index 6) is the only glyph leaving the pier's
        # safe rectangle: its bottom two pixels land at y=14 and y=15.
        gate_on_pier = next(r for r in report['results']
                            if r['message_id'] == 'M-GATE'
                            and r['display_id'] == 'PIER-32')
        self.assertEqual(gate_on_pier['details']['out_of_bounds_ink'],
                         [{'character_index': 6, 'x': 24, 'y': 14,
                           'outside': 'safe_rect'},
                          {'character_index': 6, 'x': 24, 'y': 15,
                           'outside': 'safe_rect'}])

    def test_demo_source_shuffle_keeps_report_bytes(self):
        source = json.loads((DEMO / 'source.json').read_bytes())
        for key in ('glyphs', 'kerning', 'displays', 'messages'):
            source[key] = list(reversed(source[key]))
        for msg in source['messages']:
            msg['protected_spans'] = list(reversed(msg['protected_spans']))
        shuffled = self.dir / 'shuffled.json'
        shuffled.write_bytes(json.dumps(source).encode('utf-8'))
        out_path = self.dir / 'shuffled.report.json'
        proc = run_cli('campaign-fit', '--source', str(shuffled),
                       '--output', str(out_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out_path.read_bytes(),
                         (DEMO / 'report.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
