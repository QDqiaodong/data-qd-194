"""Acceptance tests for the offline playback fulfilment ledger."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pixel_type.pack import compile_pack

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
PANEL = 'PANEL-1'
PERIOD = 'P2026-11'


# --- fixtures ---------------------------------------------------------------

def _rows(dots):
    return [''.join('1' if (x, y) in dots else '0' for x in range(8))
            for y in range(8)]


SPACE = {'char': ' ', 'advance': 8, 'offset': [0, 0], 'rows': []}


def layout(glyph, text=None):
    return {'glyphs': [glyph, SPACE], 'kerning': [],
            'text': text if text is not None else glyph['char'],
            'line_width': 32, 'line_height': 8}


def pack_document(scenes):
    return {'width': 32, 'height': 8, 'payload_budget': 1000,
            'scenes': scenes}


def write_pack(path, scenes):
    with open(path, 'w') as handle:
        json.dump(compile_pack(pack_document(scenes)), handle)


def make_packs(directory):
    """Two packs.

    ``one/aa`` and ``two/zz`` are pixel-identical under different names;
    ``two/aa`` is the same name as ``one/aa`` with a different picture.
    """
    directory = Path(directory)
    write_pack(directory / 'pack1.json', [
        {'id': 'aa', 'direct_only': True,
         'layout': layout({'char': 'a', 'advance': 8, 'offset': [0, 0],
                           'rows': _rows({(0, 0)})})},
    ])
    write_pack(directory / 'pack2.json', [
        {'id': 'aa', 'direct_only': True,
         'layout': layout({'char': 'a', 'advance': 8, 'offset': [2, 0],
                           'rows': _rows({(0, 0)})})},
        {'id': 'zz', 'direct_only': False,
         'layout': layout({'char': 'z', 'advance': 4, 'offset': [0, 0],
                           'rows': ['1', '0', '0', '0', '0', '0', '0', '0']})},
        {'id': 'unused', 'direct_only': False,
         'layout': layout({'char': 'u', 'advance': 8, 'offset': [6, 0],
                           'rows': ['1', '1', '0', '0', '0', '0', '0', '0']})},
    ])


def identity(period_start=0, period_end=40):
    return {'panel_id': PANEL, 'width': 32, 'height': 8,
            'period_id': PERIOD, 'start_ms': period_start,
            'end_ms': period_end}


def revision_one():
    document = identity()
    document.update({
        'packs': {'one': 'pack1.json', 'two': 'pack2.json'},
        'orders': [
            {'id': 'A', 'start_ms': 0, 'end_ms': 30, 'required_ms': 8,
             'expected': {'pack': 'one', 'scene': 'aa'}},
            {'id': 'B', 'start_ms': 0, 'end_ms': 10, 'required_ms': 8,
             'expected': {'pack': 'one', 'scene': 'aa'}},
        ],
        'changes': [
            {'type': 'put', 'id': 'e1', 'start_ms': 0, 'end_ms': 6,
             'observed': {'pack': 'one', 'scene': 'aa'}},
            {'type': 'put', 'id': 'e2', 'start_ms': 4, 'end_ms': 10,
             'observed': {'pack': 'two', 'scene': 'zz'}},
            {'type': 'put', 'id': 'e3', 'start_ms': 20, 'end_ms': 30,
             'observed': {'pack': 'one', 'scene': 'aa'}},
        ],
    })
    return document


def correction(changes):
    document = identity()
    document['changes'] = changes
    return document


def write_json(path, document):
    with open(path, 'w') as handle:
        json.dump(document, handle)
    return str(path)


def run_cli(*args, env=None):
    environment = dict(os.environ, PYTHONPATH=str(SRC))
    if env:
        environment.update(env)
    return subprocess.run([sys.executable, '-m', 'pixel_type.cli', *args],
                          cwd=ROOT, env=environment, capture_output=True,
                          text=True)


def statement_args(ledger, revision):
    return ('statement', '--ledger', str(ledger), '--panel-id', PANEL,
            '--period-id', PERIOD, '--revision', str(revision))


def statement(ledger, revision):
    proc = run_cli(*statement_args(ledger, revision))
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


def statement_bytes(ledger, revision):
    proc = run_cli(*statement_args(ledger, revision))
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.encode('utf-8')


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_packs(self.root)
        self.ledger = self.root / 'ledger'

    def tearDown(self):
        self._tmp.cleanup()

    def intake(self, revision, document, env=None, name=None):
        path = self.root / (name or f'rev{revision}.json')
        write_json(path, document)
        return run_cli('intake', '--ledger', str(self.ledger),
                       '--source', str(path), '--revision', str(revision),
                       env=env)

    def assert_ok(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         {'committed_revision': int(
                             proc.args[proc.args.index('--revision') + 1])})


# --- acceptance 1 -----------------------------------------------------------

class FirstRevisionTests(LedgerCase):
    def test_pairing_uses_pixels_not_scene_names(self):
        self.assert_ok(self.intake(1, revision_one()))
        doc = statement(self.ledger, 1)
        billing = {item['id']: item for item in doc['billing']}
        self.assertEqual(billing['A']['status'], 'matched')
        self.assertEqual((billing['A']['segment']['start_ms'],
                          billing['A']['segment']['end_ms']), (20, 30))
        self.assertEqual(billing['B']['status'], 'matched')
        self.assertEqual((billing['B']['segment']['start_ms'],
                          billing['B']['segment']['end_ms']), (0, 10))

    def test_segment_coverage_names_each_evidence_interval(self):
        self.assert_ok(self.intake(1, revision_one()))
        doc = statement(self.ledger, 1)
        first = next(segment for segment in doc['segments']
                     if segment['start_ms'] == 0)
        self.assertEqual([(part['start_ms'], part['end_ms'],
                           part['evidence_ids']) for part in first['coverage']],
                         [(0, 4, ['e1']), (4, 6, ['e1', 'e2']),
                          (6, 10, ['e2'])])
        # Every confirmed and missing slice is traceable to evidence or to
        # the absence of it, never to a claim that one interval spans all.
        for segment in doc['segments']:
            self.assertEqual(segment['coverage'][0]['start_ms'],
                             segment['start_ms'])
            self.assertEqual(segment['coverage'][-1]['end_ms'],
                             segment['end_ms'])
        self.assertEqual(doc['missing'], [{'start_ms': 10, 'end_ms': 20},
                                          {'start_ms': 30, 'end_ms': 40}])

    def test_same_segment_identity_ignores_pack_alias(self):
        self.assert_ok(self.intake(1, revision_one()))
        doc = statement(self.ledger, 1)
        first = next(s for s in doc['segments'] if s['start_ms'] == 0)
        second = next(s for s in doc['segments'] if s['start_ms'] == 20)
        # Same picture under a different alias is the same stable identity,
        # but start/end keep them distinct vouchers.
        self.assertEqual(first['pixels_sha256'], second['pixels_sha256'])
        self.assertNotEqual(first['id'], second['id'])

    def test_revision_one_is_idempotent_only_once(self):
        self.assert_ok(self.intake(1, revision_one()))
        before = statement_bytes(self.ledger, 1)
        again = self.intake(1, revision_one())
        self.assertEqual(again.returncode, 2)
        self.assertTrue(again.stderr.strip())
        self.assertEqual(before, statement_bytes(self.ledger, 1))

    def test_every_bad_change_rejects_the_whole_revision(self):
        document = revision_one()
        document['changes'].append(
            {'type': 'drop', 'id': 'never-seen'})
        proc = self.intake(1, document)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('never-seen', proc.stderr)
        self.assertEqual(
            run_cli(*statement_args(self.ledger, 1)).returncode, 2)

    def test_duplicate_evidence_id_in_one_revision_is_rejected(self):
        document = revision_one()
        document['changes'].append(
            {'type': 'put', 'id': 'e1', 'start_ms': 30, 'end_ms': 35,
             'observed': {'pack': 'one', 'scene': 'aa'}})
        proc = self.intake(1, document)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('e1', proc.stderr)


# --- acceptance 2 -----------------------------------------------------------

class CorrectionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.assert_ok(self.intake(1, revision_one()))
        self.bytes_one = statement_bytes(self.ledger, 1)

    def test_conflict_breaks_the_voucher_and_replans(self):
        proc = self.intake(2, correction([
            {'type': 'put', 'id': 'e4', 'start_ms': 23, 'end_ms': 27,
             'observed': {'pack': 'two', 'scene': 'aa'}}]))
        self.assert_ok(proc)
        doc = statement(self.ledger, 2)
        billing = {item['id']: item for item in doc['billing']}
        self.assertEqual(billing['A']['status'], 'matched')
        self.assertEqual((billing['A']['segment']['start_ms'],
                          billing['A']['segment']['end_ms']), (0, 10))
        self.assertEqual(billing['B']['status'], 'unmatched')
        self.assertEqual(billing['B']['reason'],
                         'eligible_segments_allocated_elsewhere')
        self.assertEqual([(item['allocated_to'], item['start_ms'])
                          for item in billing['B']['eligible_segments']],
                         [('A', 0)])
        self.assertEqual([(c['start_ms'], c['end_ms'])
                          for c in doc['conflicts']], [(23, 27)])
        self.assertEqual(
            [(s['start_ms'], s['end_ms']) for s in doc['segments']],
            [(0, 10), (20, 23), (27, 30)])

    def test_conflict_delta_is_minus_one_and_names_both_sides(self):
        self.assert_ok(self.intake(2, correction([
            {'type': 'put', 'id': 'e4', 'start_ms': 23, 'end_ms': 27,
             'observed': {'pack': 'two', 'scene': 'aa'}}])))
        doc = statement(self.ledger, 2)
        self.assertEqual(
            {(item['order_id'], item['kind'], item['delta'])
             for item in doc['deltas']},
            {('A', 'revoke', -1), ('A', 'grant', 1), ('B', 'revoke', -1)})
        self.assertEqual(doc['summary']['net_delta'], -1)
        # One order changing segment still produces a paired -1/+1.
        a_items = [item for item in doc['deltas'] if item['order_id'] == 'A']
        self.assertEqual(len(a_items), 2)
        self.assertEqual(sorted(item['delta'] for item in a_items), [-1, 1])

    def test_revision_one_bytes_never_change(self):
        self.assert_ok(self.intake(2, correction([
            {'type': 'put', 'id': 'e4', 'start_ms': 23, 'end_ms': 27,
             'observed': {'pack': 'two', 'scene': 'aa'}}])))
        self.assertEqual(self.bytes_one, statement_bytes(self.ledger, 1))

    def test_withdrawing_the_conflict_restores_the_first_pairing(self):
        self.assert_ok(self.intake(2, correction([
            {'type': 'put', 'id': 'e4', 'start_ms': 23, 'end_ms': 27,
             'observed': {'pack': 'two', 'scene': 'aa'}}])))
        self.assert_ok(self.intake(3, correction(
            [{'type': 'drop', 'id': 'e4'}])))
        doc = statement(self.ledger, 3)
        first = statement(self.ledger, 1)
        self.assertEqual(doc['billing'], first['billing'])
        self.assertEqual(
            {(item['order_id'], item['kind'], item['delta'])
             for item in doc['deltas']},
            {('A', 'revoke', -1), ('B', 'grant', 1),
             ('A', 'grant', 1)})

class SwapTests(LedgerCase):
    """Two orders exchange their vouchers: the count never changes."""

    def setUp(self):
        super().setUp()
        first = identity()
        first.update({
            'packs': {'one': 'pack1.json', 'two': 'pack2.json'},
            'orders': [
                {'id': 'A', 'start_ms': 0, 'end_ms': 30, 'required_ms': 8,
                 'expected': {'pack': 'one', 'scene': 'aa'}},
                {'id': 'B', 'start_ms': 0, 'end_ms': 40, 'required_ms': 8,
                 'expected': {'pack': 'two', 'scene': 'aa'}},
            ],
            'changes': [
                {'type': 'put', 'id': 'e1', 'start_ms': 0, 'end_ms': 10,
                 'observed': {'pack': 'one', 'scene': 'aa'}},
                {'type': 'put', 'id': 'e2', 'start_ms': 20, 'end_ms': 30,
                 'observed': {'pack': 'two', 'scene': 'aa'}},
            ],
        })
        self.assert_ok(self.intake(1, first))

    def test_pure_segment_swap_keeps_a_zero_net_paired_delta(self):
        before = {item['id']: item['segment']['start_ms']
                  for item in statement(self.ledger, 1)['billing']}
        self.assertEqual(before, {'A': 0, 'B': 20})
        self.assert_ok(self.intake(2, correction([
            {'type': 'put', 'id': 'e1', 'start_ms': 0, 'end_ms': 10,
             'observed': {'pack': 'two', 'scene': 'aa'}},
            {'type': 'put', 'id': 'e2', 'start_ms': 20, 'end_ms': 30,
             'observed': {'pack': 'one', 'scene': 'aa'}}])))
        doc = statement(self.ledger, 2)
        after = {item['id']: item['segment']['start_ms']
                 for item in doc['billing']}
        self.assertEqual(after, {'A': 20, 'B': 0})
        self.assertEqual(doc['summary']['matched'], 2)
        self.assertEqual(doc['summary']['net_delta'], 0)
        self.assertEqual(
            sorted((item['order_id'], item['kind'], item['delta'])
                   for item in doc['deltas']),
            [('A', 'grant', 1), ('A', 'revoke', -1),
             ('B', 'grant', 1), ('B', 'revoke', -1)])


# --- acceptance 3 -----------------------------------------------------------

class DurabilityTests(LedgerCase):
    def test_statements_survive_moving_sources_and_packs(self):
        self.assert_ok(self.intake(1, revision_one()))
        before = statement_bytes(self.ledger, 1)
        archive = self.root / 'archive'
        archive.mkdir()
        for name in ('pack1.json', 'pack2.json', 'rev1.json'):
            shutil.move(str(self.root / name), str(archive / name))
        self.assertEqual(before, statement_bytes(self.ledger, 1))
        # A later correction uses only committed aliases and needs no file.
        self.assert_ok(self.intake(2, correction([
            {'type': 'put', 'id': 'e4', 'start_ms': 23, 'end_ms': 27,
             'observed': {'pack': 'two', 'scene': 'aa'}}])))
        self.assertEqual(before, statement_bytes(self.ledger, 1))

    def test_tampered_unreferenced_scene_rejects_first_intake(self):
        # A structurally damaged record for a scene no observation or order
        # names still rejects the whole pack, so it can never become the
        # committed material of revision 1.
        with open(self.root / 'pack2.json') as handle:
            document = json.load(handle)
        entry = next(item for item in document['catalog']
                     if item['id'] == 'unused')
        entry['offset'] += 1  # hole before the record
        with open(self.root / 'pack2.json', 'w') as handle:
            json.dump(document, handle)
        fresh = self.root / 'fresh'
        path = write_json(self.root / 'bad.json', revision_one())
        proc = run_cli('intake', '--ledger', str(fresh), '--source', path,
                       '--revision', '1')
        self.assertEqual(proc.returncode, 2)
        self.assertIn('two', proc.stderr)
        self.assertFalse((fresh / 'bands').exists())
        self.assertEqual(
            run_cli(*statement_args(fresh, 1)).returncode, 2)

    def test_concurrent_commits_admit_exactly_one_winner(self):
        self.assert_ok(self.intake(1, revision_one()))
        one = self.root / 'c1.json'
        two = self.root / 'c2.json'
        write_json(one, correction([
            {'type': 'put', 'id': 'e4', 'start_ms': 23, 'end_ms': 27,
             'observed': {'pack': 'two', 'scene': 'aa'}}]))
        write_json(two, correction([
            {'type': 'put', 'id': 'e5', 'start_ms': 31, 'end_ms': 38,
             'observed': {'pack': 'one', 'scene': 'aa'}}]))
        environment = dict(os.environ, PYTHONPATH=str(SRC))
        processes = [
            subprocess.Popen(
                [sys.executable, '-m', 'pixel_type.cli', 'intake',
                 '--ledger', str(self.ledger), '--source', str(path),
                 '--revision', '2'],
                cwd=ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for path in (one, two)]
        results = [process.communicate() + (process.returncode,)
                   for process in processes]
        codes = sorted(code for _out, _err, code in results)
        self.assertEqual(codes, [0, 2])
        winner = one if results[0][2] == 0 else two
        committed = statement(self.ledger, 2)
        self.assertIn('e5' if winner == two else 'e4',
                      [item['id'] for item in committed['evidence']])
        # The loser is a plain wrong-revision call, not a partial overwrite.
        self.assertEqual(committed['revision'], 2)

    def _stopped_intake(self, boundary, revision, document):
        path = self.root / f'stop{revision}.json'
        write_json(path, document)
        return run_cli('intake', '--ledger', str(self.ledger),
                       '--source', str(path), '--revision', str(revision),
                       env={'PIXEL_TYPE_TEST_STOP_AT': boundary})

    def test_stop_before_commit_point_leaves_no_revision(self):
        self.assert_ok(self.intake(1, revision_one()))
        before = statement_bytes(self.ledger, 1)
        for boundary in ('after_build', 'before_statement',
                         'after_statement', 'before_record'):
            with self.subTest(boundary=boundary):
                band = next((self.ledger / 'bands').iterdir())
                saved = self.root / f'saved-{boundary}'
                shutil.copytree(band, saved)
                proc = self._stopped_intake(
                    boundary, 2,
                    correction([{'type': 'put', 'id': 'e4', 'start_ms': 23,
                                 'end_ms': 27,
                                 'observed': {'pack': 'two', 'scene': 'aa'}}]))
                self.assertEqual(proc.returncode, 70)
                self.assertEqual(
                    run_cli(*statement_args(self.ledger, 2)).returncode, 2)
                self.assertEqual(before, statement_bytes(self.ledger, 1))
                # The same revision can be submitted again, and the retry
                # sees exactly the pre-stop state as its predecessor.
                self.assert_ok(self.intake(
                    2, correction([{'type': 'put', 'id': 'e4',
                                    'start_ms': 23, 'end_ms': 27,
                                    'observed': {'pack': 'two',
                                                 'scene': 'aa'}}]),
                    name=f'retry-{boundary}.json'))
                doc = statement(self.ledger, 2)
                self.assertEqual(doc['summary']['net_delta'], -1)
                self.assertIn('e4', [item['id'] for item in doc['evidence']])
                # Restore the pristine revision-1 band for the next boundary.
                shutil.rmtree(band)
                shutil.copytree(saved, band)

    def test_stop_after_commit_point_is_already_visible(self):
        self.assert_ok(self.intake(1, revision_one()))
        proc = self._stopped_intake(
            'after_record', 2,
            correction([{'type': 'put', 'id': 'e4', 'start_ms': 23,
                         'end_ms': 27,
                         'observed': {'pack': 'two', 'scene': 'aa'}}]))
        self.assertEqual(proc.returncode, 70)
        doc = statement(self.ledger, 2)
        self.assertEqual(doc['summary']['net_delta'], -1)
        self.assertIn('e4', [item['id'] for item in doc['evidence']])
        retry = self.intake(2, correction(
            [{'type': 'drop', 'id': 'e4'}]))
        self.assertEqual(retry.returncode, 2)


class AllocationUnitTests(unittest.TestCase):
    def test_maximum_then_lexicographic_across_random_graphs(self):
        from pixel_type.ledger import allocate
        import itertools
        import random

        def brute(order_ids, eligible, segments):
            best = None

            def walk(position, used, chosen):
                nonlocal best
                if position == len(order_ids):
                    key = tuple(chosen[oid] if chosen.get(oid) is not None
                                else len(segments) + 1 for oid in order_ids)
                    count = sum(1 for value in chosen.values()
                                if value is not None)
                    candidate = (-count, key)
                    if best is None or candidate < best[0]:
                        best = (candidate, dict(chosen))
                    return
                oid = order_ids[position]
                chosen[oid] = None
                walk(position + 1, used, chosen)
                for segment in eligible[oid]:
                    if segment in used:
                        continue
                    chosen[oid] = segment
                    used.add(segment)
                    walk(position + 1, used, chosen)
                    used.discard(segment)
                chosen.pop(oid, None)

            walk(0, set(), {})
            return best[1]

        rng = random.Random(20261122)
        for _ in range(400):
            order_ids = [f'O{index}' for index in range(rng.randint(1, 5))]
            rng.shuffle(order_ids)
            segments = list(range(rng.randint(1, 4)))
            eligible = {oid: [segment for segment in segments
                              if rng.random() < 0.6]
                        for oid in order_ids}
            orders = [{'id': oid} for oid in order_ids]
            got = allocate(orders, segments, eligible)
            want = brute(order_ids, eligible, segments)
            self.assertEqual(got, {oid: value for oid, value in want.items()
                                   if value is not None})


class MultiBandTests(LedgerCase):
    def test_bands_are_independent(self):
        self.assert_ok(self.intake(1, revision_one()))
        other = revision_one()
        other['period_id'] = 'P2026-12'
        other['panel_id'] = 'PANEL-2'
        self.assert_ok(self.intake(1, other, name='other.json'))
        # A correction in one band never changes the other's statement.
        before = run_cli('statement', '--ledger', str(self.ledger),
                         '--panel-id', 'PANEL-2', '--period-id', 'P2026-12',
                         '--revision', '1').stdout
        self.assert_ok(self.intake(2, correction([
            {'type': 'put', 'id': 'e4', 'start_ms': 23, 'end_ms': 27,
             'observed': {'pack': 'two', 'scene': 'aa'}}])))
        after = run_cli('statement', '--ledger', str(self.ledger),
                        '--panel-id', 'PANEL-2', '--period-id', 'P2026-12',
                        '--revision', '1').stdout
        self.assertEqual(before, after)

    def test_concurrent_winner_is_the_base_for_the_next_revision(self):
        self.assert_ok(self.intake(1, revision_one()))
        first = self.root / 'race1.json'
        second = self.root / 'race2.json'
        write_json(first, correction([
            {'type': 'put', 'id': 'e4', 'start_ms': 23, 'end_ms': 27,
             'observed': {'pack': 'two', 'scene': 'aa'}}]))
        write_json(second, correction([
            {'type': 'put', 'id': 'e5', 'start_ms': 31, 'end_ms': 38,
             'observed': {'pack': 'one', 'scene': 'aa'}}]))
        environment = dict(os.environ, PYTHONPATH=str(SRC))
        procs = [subprocess.Popen(
            [sys.executable, '-m', 'pixel_type.cli', 'intake',
             '--ledger', str(self.ledger), '--source', str(path),
             '--revision', '2'], cwd=ROOT, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for path in (first, second)]
        codes = sorted(proc.communicate() and proc.returncode for proc in procs)
        self.assertEqual(codes, [0, 2])
        doc = statement(self.ledger, 2)
        winner_ids = [item['id'] for item in doc['evidence']]
        self.assertEqual(len([eid for eid in winner_ids if eid in ('e4', 'e5')]), 1)
        # The next revision must build on the winner, not on the loser.
        loser_id = 'e5' if 'e4' in winner_ids else 'e4'
        conflict = self.intake(3, correction([{'type': 'drop', 'id': loser_id}]))
        self.assertEqual(conflict.returncode, 2)
        self.assertIn(loser_id, conflict.stderr)
        ok = self.intake(3, correction([]), name='noop3.json')
        self.assert_ok(ok)
        self.assertEqual(statement(self.ledger, 3)['deltas'], [])


class LimitAndTieBreakTests(LedgerCase):
    def test_evidence_cap_applies_after_edits(self):
        document = revision_one()
        document['changes'] = [
            {'type': 'put', 'id': f'e{index:03d}',
             'start_ms': 10 * (index % 4),
             'end_ms': 10 * (index % 4) + 9,
             'observed': {'pack': 'one', 'scene': 'aa'}}
            for index in range(128)]
        self.assert_ok(self.intake(1, document))
        proc = self.intake(2, correction([
            {'type': 'put', 'id': 'e128', 'start_ms': 0, 'end_ms': 1,
             'observed': {'pack': 'one', 'scene': 'aa'}}]))
        self.assertEqual(proc.returncode, 2)
        self.assertIn('129', proc.stderr)

    def test_more_than_128_changes_rejected(self):
        document = revision_one()
        document['changes'] = [
            {'type': 'put', 'id': f'x{index}', 'start_ms': 0, 'end_ms': 1,
             'observed': {'pack': 'one', 'scene': 'aa'}}
            for index in range(129)]
        self.assertEqual(self.intake(1, document).returncode, 2)

    def test_required_ms_must_fit_the_window(self):
        document = revision_one()
        document['orders'][0]['required_ms'] = 31
        self.assertEqual(self.intake(1, document).returncode, 2)
        document['orders'][0]['required_ms'] = 0
        self.assertEqual(self.intake(1, document).returncode, 2)

    def test_period_longer_than_a_day_rejected(self):
        document = revision_one()
        document['end_ms'] = 24 * 60 * 60 * 1000 + 1
        self.assertEqual(self.intake(1, document).returncode, 2)

    def test_pack_dimension_mismatch_rejected(self):
        with open(self.root / 'pack1.json') as handle:
            pack = json.load(handle)
        pack['width'] = 64
        pack['tile_cols'] = 8
        pack['payload_base64'] = 'AA=='
        pack['payload_bytes'] = 1
        with open(self.root / 'pack1.json', 'w') as handle:
            json.dump(pack, handle)
        self.assertEqual(self.intake(1, revision_one()).returncode, 2)

    def test_statement_bytes_are_repeatable_across_processes(self):
        self.assert_ok(self.intake(1, revision_one()))
        first = statement_bytes(self.ledger, 1)
        second = statement_bytes(self.ledger, 1)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b'\n'))
        parsed = json.loads(first)
        self.assertEqual(parsed['kind'], 'pixel-type.intake.statement')


class CliContractTests(LedgerCase):
    def test_missing_revision_and_bad_ids_exit_two(self):
        self.assert_ok(self.intake(1, revision_one()))
        self.assertEqual(run_cli(*statement_args(self.ledger, 9)).returncode, 2)
        bad = run_cli('statement', '--ledger', str(self.ledger),
                      '--panel-id', 'has space', '--period-id', PERIOD,
                      '--revision', '1')
        self.assertEqual(bad.returncode, 2)

    def test_second_revision_may_not_redefine_identity(self):
        self.assert_ok(self.intake(1, revision_one()))
        document = correction([])
        document['width'] = 64
        proc = self.intake(2, document)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('width', proc.stderr)

    def test_second_revision_may_not_resubmit_full_packs(self):
        self.assert_ok(self.intake(1, revision_one()))
        document = revision_one()
        proc = self.intake(2, document)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            run_cli(*statement_args(self.ledger, 2)).returncode, 2)

    def test_unknown_alias_and_scene_are_rejected(self):
        self.assert_ok(self.intake(1, revision_one()))
        for ref in ({'pack': 'three', 'scene': 'aa'},
                    {'pack': 'one', 'scene': 'nope'}):
            proc = self.intake(2, correction([
                {'type': 'put', 'id': 'e9', 'start_ms': 0, 'end_ms': 2,
                 'observed': ref}]))
            self.assertEqual(proc.returncode, 2)
            self.assertTrue(proc.stderr.strip())

    def test_evidence_outside_the_period_is_rejected(self):
        proc = self.intake(1, revision_one() | {'changes': [
            {'type': 'put', 'id': 'e1', 'start_ms': 0, 'end_ms': 41,
             'observed': {'pack': 'one', 'scene': 'aa'}}]})
        self.assertEqual(proc.returncode, 2)

    def test_bad_json_exits_two_without_creating_a_band(self):
        path = self.root / 'broken.json'
        path.write_text('{"panel_id": ')
        proc = run_cli('intake', '--ledger', str(self.ledger),
                       '--source', str(path), '--revision', '1')
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.strip())
        self.assertFalse((self.ledger / 'bands').exists())


if __name__ == '__main__':
    unittest.main()
