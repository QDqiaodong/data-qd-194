"""End-to-end demo for the offline inspection flow, using committed fixtures.

fixtures/inspect-demo/ holds a 72x8 panel work order: one material pack with
four scenes (A dark, B lights group G, C lights G+Y, D lights group X), four
candidates R0/R1/S0/S1 mapped to them, nine probe tiles, at most six reads
and one tolerated bad read.  Only R0 and S1 are approved.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pixel_type.pack import compile_pack
from test_inspect import ALLOWED, verdict_of, walk_card

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
DEMO = ROOT / 'fixtures' / 'inspect-demo'


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, '-m', 'pixel_type.cli', *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


class InspectDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_committed_pack_matches_the_compiler(self):
        scenes = json.loads((DEMO / 'scenes.json').read_bytes())
        rebuilt = compile_pack(scenes)
        committed = json.loads((DEMO / 'pack.json').read_bytes())
        self.assertEqual(rebuilt, committed)
        formats = {e['id']: e['format'] for e in committed['catalog']}
        self.assertEqual(formats['A'], 'full')
        self.assertEqual(formats['B'], 'reference')
        self.assertEqual(formats['C'], 'reference')
        self.assertEqual(formats['D'], 'reference')

    def test_plan_card_and_verdicts(self):
        card_path = self.dir / 'card.json'
        proc = run_cli('inspect-plan', '--source', str(DEMO / 'plan.json'),
                       '--output', str(card_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary['guarantee'], 'guaranteed')
        # The demo card is byte-stable.
        self.assertEqual(card_path.read_bytes(),
                         (DEMO / 'card.json').read_bytes())
        card = json.loads(card_path.read_bytes())
        # Every listed screen is decided within six reads, with zero or one
        # bad read, whichever branch the card takes.
        for true_id, allowed in ALLOWED.items():
            for reads in walk_card(card, true_id, max_bad=1):
                self.assertLessEqual(len(reads), 6)
                self.assertEqual(verdict_of(card, reads),
                                 'cleared' if allowed else 'rejected')
        # The two committed receipts judge as labelled.
        for name, want in (('readback-cleared.json', 'cleared'),
                           ('readback-rejected.json', 'rejected')):
            report_path = self.dir / (name + '.report.json')
            proc = run_cli('inspect-result', '--card', str(card_path),
                           '--readback', str(DEMO / name),
                           '--output', str(report_path))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(report_path.read_bytes())
            self.assertEqual(report['verdict'], want)
            self.assertEqual(report['card_sha256'],
                             hashlib.sha256(card_path.read_bytes()).hexdigest())

    def test_card_alone_is_enough_after_sources_are_gone(self):
        # Only the card and the receipt travel to the acceptance desk.
        lonely = self.dir / 'acceptance'
        lonely.mkdir()
        shutil.copy(DEMO / 'card.json', lonely / 'card.json')
        shutil.copy(DEMO / 'readback-cleared.json', lonely / 'readback.json')
        report_path = lonely / 'report.json'
        proc = run_cli('inspect-result', '--card', str(lonely / 'card.json'),
                       '--readback', str(lonely / 'readback.json'),
                       '--output', str(report_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'cleared')
        self.assertEqual(report['possible_candidates'], ['S1'])


if __name__ == '__main__':
    unittest.main()
