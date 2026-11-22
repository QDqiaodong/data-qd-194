"""End-to-end demo of the ledger using the checked-in fixture files."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
DEMO = ROOT / 'fixtures' / 'intake-demo'


def run(*args):
    environment = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, '-m', 'pixel_type.cli', *args],
                          cwd=ROOT, env=environment, capture_output=True,
                          text=True)


class DemoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.ledger = self.dir / 'ledger'
        self.sources = []
        for name in ('pack-main.json', 'pack-alt.json',
                     'rev1.json', 'rev2.json', 'rev3.json'):
            target = self.dir / name
            shutil.copy(DEMO / name, target)
            self.sources.append(target)

    def tearDown(self):
        self._tmp.cleanup()

    def intake(self, revision):
        proc = run('intake', '--ledger', str(self.ledger),
                   '--source', str(self.dir / f'rev{revision}.json'),
                   '--revision', str(revision))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         {'committed_revision': revision})
        return proc

    def statement(self, revision):
        proc = run('statement', '--ledger', str(self.ledger),
                   '--panel-id', 'PANEL-1', '--period-id', 'P2026-11',
                   '--revision', str(revision))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_three_revision_demo(self):
        self.intake(1)
        first = self.statement(1)
        self.assertEqual(
            {item['id']: (item['segment']['start_ms'], item['segment']['end_ms'])
             for item in first['billing']},
            {'A': (20, 30), 'B': (0, 10)})
        self.assertEqual(first['missing'],
                         [{'start_ms': 10, 'end_ms': 20},
                          {'start_ms': 30, 'end_ms': 40}])
        self.assertEqual(first['conflicts'], [])

        self.intake(2)
        second = self.statement(2)
        self.assertEqual(
            [(item['id'], item['status'], item.get('reason'))
             for item in second['billing']],
            [('A', 'matched', None),
             ('B', 'unmatched', 'eligible_segments_allocated_elsewhere')])
        self.assertEqual([(item['start_ms'], item['end_ms'])
                          for item in second['conflicts']], [(23, 27)])
        self.assertEqual(second['summary']['net_delta'], -1)

        self.intake(3)
        third = self.statement(3)
        self.assertEqual(third['billing'], first['billing'])
        self.assertEqual(third['summary']['net_delta'], 1)

    def test_history_is_byte_stable_after_moving_inputs(self):
        self.intake(1)
        proc = run('statement', '--ledger', str(self.ledger),
                   '--panel-id', 'PANEL-1', '--period-id', 'P2026-11',
                   '--revision', '1')
        before = proc.stdout.encode('utf-8')
        for path in self.sources:
            if path.name != 'rev2.json':
                path.unlink()
        self.intake(2)
        proc = run('statement', '--ledger', str(self.ledger),
                   '--panel-id', 'PANEL-1', '--period-id', 'P2026-11',
                   '--revision', '1')
        self.assertEqual(proc.stdout.encode('utf-8'), before)


if __name__ == '__main__':
    unittest.main()
