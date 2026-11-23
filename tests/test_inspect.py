"""Acceptance tests for offline inspection cards and readback verdicts."""

import hashlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import dot_glyph, v1_input, write_bytes
from pixel_type.pack import compile_pack

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'

DARK = '0000000000000000'
LIT = '8000000000000000'
OFFLIST = '0100000000000000'  # a legal bitmap no candidate ever expects

PANEL_W = 72
PANEL_H = 8
ALLOWED = {'R0': True, 'R1': False, 'S0': False, 'S1': True}


def scene_layout(text):
    glyphs = [dot_glyph('d', [(0, 0)], advance=8), dot_glyph(' ', [], advance=8)]
    return v1_input(glyphs, text, line_width=PANEL_W, line_height=PANEL_H)


def pack_source(extra_scenes=()):
    """A dark, B lights group G, C lights G+Y, D lights group X."""
    scenes = [
        {'id': 'A', 'direct_only': False, 'layout': scene_layout(' ' * 9)},
        {'id': 'B', 'direct_only': False, 'layout': scene_layout('ddd' + ' ' * 6)},
        {'id': 'C', 'direct_only': False,
         'layout': scene_layout('ddd' + ' ' * 3 + 'ddd')},
        {'id': 'D', 'direct_only': False,
         'layout': scene_layout(' ' * 3 + 'ddd' + ' ' * 3)},
    ]
    scenes += extra_scenes
    return {'width': PANEL_W, 'height': PANEL_H, 'payload_budget': 256,
            'scenes': scenes}


def four_candidate_plan(**overrides):
    plan = {
        'panel_id': 'PANEL-72', 'width': PANEL_W, 'height': PANEL_H,
        'packs': {'main': 'pack.json'},
        'candidates': [
            {'id': 'R0', 'pack': 'main', 'scene': 'A', 'allowed': True},
            {'id': 'R1', 'pack': 'main', 'scene': 'D', 'allowed': False},
            {'id': 'S0', 'pack': 'main', 'scene': 'B', 'allowed': False},
            {'id': 'S1', 'pack': 'main', 'scene': 'C', 'allowed': True},
        ],
        'probe_tiles': [{'tx': i, 'ty': 0} for i in range(9)],
        'max_reads': 6, 'max_bad_reads': 1,
    }
    plan.update(overrides)
    return plan


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, '-m', 'pixel_type.cli', *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


def expected_map(card):
    return {cand['id']: {tile['tx']: tile['bits'] for tile in cand['tiles']}
            for cand in card['candidates']}


def verdict_of(card, reads):
    """Independent reimplementation of the evidence verdict rule."""
    max_bad = card['max_bad_reads']
    possible = []
    for cand in card['candidates']:
        expect = {tile['tx']: tile['bits'] for tile in cand['tiles']}
        mismatches = sum(1 for tx, bits in reads.items() if expect[tx] != bits)
        if mismatches <= max_bad:
            possible.append(cand)
    if not possible:
        return 'inconsistent'
    if all(cand['allowed'] for cand in possible):
        return 'cleared'
    if not any(cand['allowed'] for cand in possible):
        return 'rejected'
    return 'undecided'


def walk_card(card, true_id, max_bad):
    """Every strategy path for one true screen and every in-tolerance channel.

    At each read node the channel either answers honestly or spends one error
    to return another candidate's value or an off-list legal bitmap.  Returns
    the list of read maps collected at stop nodes.
    """
    nodes = {node['id']: node for node in card['strategy']['nodes']}
    expect = expected_map(card)
    paths = []

    def rec(node_id, errors_left, reads):
        node = nodes[node_id]
        if node['action'] == 'stop':
            paths.append(reads)
            return
        tx = node['tx']
        assert tx not in reads, 'a coordinate must be sampled at most once'
        assert len(reads) < card['max_reads'], 'read budget exceeded'
        true_bits = expect[true_id][tx]
        options = [(true_bits, errors_left)]
        if errors_left:
            wrongs = {OFFLIST}
            wrongs |= {exp[tx] for exp in expect.values()
                       if exp[tx] != true_bits}
            options += [(bits, errors_left - 1) for bits in sorted(wrongs)]
        for bits, left in options:
            nxt = next((b['next'] for b in node.get('branches', [])
                        if b['bits'] == bits), node['fallback'])
            rec(nxt, left, {**reads, tx: bits})

    rec(card['strategy']['start'], max_bad, {})
    return paths


def follow(card, true_id, corrupt=()):
    """One instrument run: answer with the true screen's values except the
    tiles named in ``corrupt`` (tx -> wrong bits), following the card's own
    branches until it stops."""
    nodes = {node['id']: node for node in card['strategy']['nodes']}
    expect = expected_map(card)[true_id]
    corrupt = dict(corrupt)
    reads = {}
    node_id = card['strategy']['start']
    while True:
        node = nodes[node_id]
        if node['action'] == 'stop':
            return reads
        tx = node['tx']
        bits = corrupt.pop(tx, expect[tx])
        reads[tx] = bits
        node_id = next((b['next'] for b in node.get('branches', [])
                        if b['bits'] == bits), node['fallback'])


def receipt(panel_id, card_path, reads):
    digest = hashlib.sha256(Path(card_path).read_bytes()).hexdigest()
    return {'panel_id': panel_id, 'card_sha256': digest,
            'reads': [{'tx': tx, 'ty': 0, 'bits': bits}
                      for tx, bits in reads]}


class Workspace:
    def __init__(self, case, plan=None, source=None):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        pack = compile_pack(source if source is not None else pack_source())
        self.pack = pack
        self.pack_path = self.dir / 'pack.json'
        write_bytes(self.pack_path, pack)
        self.plan_path = self.dir / 'plan.json'
        write_bytes(self.plan_path, plan if plan is not None
                    else four_candidate_plan())

    def make_card(self, name='card.json', plan_path=None):
        card_path = self.dir / name
        proc = run_cli('inspect-plan',
                       '--source', str(plan_path or self.plan_path),
                       '--output', str(card_path))
        assert proc.returncode == 0, proc.stderr
        return card_path

    def judge(self, card_path, readback, name='report.json'):
        readback_path = self.dir / (name + '.readback.json')
        write_bytes(readback_path, readback)
        report_path = self.dir / name
        proc = run_cli('inspect-result', '--card', str(card_path),
                       '--readback', str(readback_path),
                       '--output', str(report_path))
        return proc, report_path


class GuaranteedCardTests(unittest.TestCase):
    """Scenario 1: four candidates on a 72x8 panel, 6 reads, 1 bad read."""

    def setUp(self):
        self.ws = Workspace(self)
        self.card_path = self.ws.make_card()
        self.card = json.loads(self.card_path.read_bytes())

    def test_pack_uses_reference_frames_for_some_candidates(self):
        formats = {e['id']: e['format'] for e in self.ws.pack['catalog']}
        self.assertEqual(formats['A'], 'full')
        # S0, S1 and R1 are all restored from reference frames.
        self.assertEqual(formats['B'], 'reference')
        self.assertEqual(formats['C'], 'reference')
        self.assertEqual(formats['D'], 'reference')

    def test_card_is_guaranteed_and_self_contained(self):
        card = self.card
        self.assertEqual(card['kind'], 'pixel-type.inspect.card')
        self.assertEqual(card['schema_version'], 1)
        self.assertEqual(card['guarantee'], 'guaranteed')
        self.assertEqual(card['max_reads'], 6)
        self.assertEqual(card['max_bad_reads'], 1)
        self.assertEqual(len(card['probe_tiles']), 9)
        self.assertEqual(len(card['candidates']), 4)
        expect = expected_map(card)
        self.assertEqual(set(expect['R0'].values()), {DARK})
        self.assertEqual([expect['S1'][tx] for tx in range(9)],
                         [LIT, LIT, LIT, DARK, DARK, DARK, LIT, LIT, LIT])
        strategy = card['strategy']
        self.assertIsInstance(strategy['start'], int)
        self.assertGreater(len(strategy['nodes']), 1)

    def test_same_input_gives_identical_card_bytes(self):
        second = self.ws.make_card('card2.json')
        self.assertEqual(self.card_path.read_bytes(), second.read_bytes())

    def test_no_fixed_six_reads_can_decide(self):
        # Every fixed 6-tile subset leaves at least one in-tolerance readback
        # undecided: the opposite pairs R0/R1, R0/S0, S0/S1 differ in one
        # group each, and six fixed reads cannot cover all three groups with
        # the three reads one error tolerant decision needs.
        expect = expected_map(self.card)
        for subset in itertools.combinations(range(9), 6):
            found_undecided = False
            for true_id in expect:
                for corrupt in [None, *subset]:
                    reads = {tx: expect[true_id][tx] for tx in subset}
                    if corrupt is not None:
                        reads[corrupt] = next(
                            bits for bits in (DARK, LIT, OFFLIST)
                            if bits != expect[true_id][corrupt])
                    if verdict_of(self.card, reads) == 'undecided':
                        found_undecided = True
            self.assertTrue(found_undecided,
                            f'fixed subset {subset} unexpectedly decides')

    def test_strategy_walks_decide_every_screen_within_budget(self):
        for true_id, allowed in ALLOWED.items():
            paths = walk_card(self.card, true_id, max_bad=1)
            self.assertTrue(paths)
            want = 'cleared' if allowed else 'rejected'
            for reads in paths:
                self.assertLessEqual(len(reads), 6)
                self.assertEqual(verdict_of(self.card, reads), want,
                                 (true_id, reads))

    def test_cli_receipts_following_card_branches(self):
        expect = expected_map(self.card)
        cases = []
        for true_id, allowed in ALLOWED.items():
            cases.append((true_id, {}, 'cleared' if allowed else 'rejected'))
        # one error: another candidate's value, and an off-list legal bitmap
        cases.append(('R0', {0: LIT}, 'cleared'))
        cases.append(('S1', {6: OFFLIST}, 'cleared'))
        cases.append(('R1', {3: DARK}, 'rejected'))
        cases.append(('S0', {1: OFFLIST}, 'rejected'))
        for index, (true_id, corrupt, want) in enumerate(cases):
            reads = follow(self.card, true_id, corrupt)
            self.assertLessEqual(len(reads), 6)
            proc, report_path = self.ws.judge(
                self.card_path,
                receipt('PANEL-72', self.card_path, sorted(reads.items())),
                name=f'follow-{index}.json')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(report_path.read_bytes())
            self.assertEqual(report['verdict'], want, (true_id, corrupt))
            self.assertEqual(report['reads_used'], len(reads))


class ScenarioTwoTests(unittest.TestCase):
    """Two opposite candidates, two readable tiles, budget two."""

    def plan(self, max_bad):
        return four_candidate_plan(
            candidates=[
                {'id': 'R0', 'pack': 'main', 'scene': 'A', 'allowed': True},
                {'id': 'S0', 'pack': 'main', 'scene': 'B', 'allowed': False},
            ],
            probe_tiles=[{'tx': 0, 'ty': 0}, {'tx': 1, 'ty': 0}],
            max_reads=2, max_bad_reads=max_bad)

    def test_one_bad_read_budget_is_not_guaranteed(self):
        ws = Workspace(self, plan=self.plan(1))
        card_path = ws.make_card()
        card = json.loads(card_path.read_bytes())
        self.assertEqual(card['guarantee'], 'not_guaranteed')
        self.assertIsNone(card['strategy'])
        # 0 then 1 keeps both opposite conclusions possible.
        proc, report_path = ws.judge(
            card_path, receipt('PANEL-72', card_path, [(0, DARK), (1, LIT)]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'undecided')
        self.assertEqual(report['possible_candidates'], ['R0', 'S0'])
        # 0 then 0 still clears.
        proc, report_path = ws.judge(
            card_path, receipt('PANEL-72', card_path, [(0, DARK), (1, DARK)]),
            name='both-dark.json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'cleared')
        self.assertEqual(report['possible_candidates'], ['R0'])

    def test_zero_bad_read_budget_is_guaranteed(self):
        ws = Workspace(self, plan=self.plan(0))
        card_path = ws.make_card()
        card = json.loads(card_path.read_bytes())
        self.assertEqual(card['guarantee'], 'guaranteed')
        self.assertIsNotNone(card['strategy'])
        # The formerly undecided 0,1 receipt is now contradictory evidence.
        proc, report_path = ws.judge(
            card_path, receipt('PANEL-72', card_path, [(0, DARK), (1, LIT)]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'inconsistent')
        self.assertEqual(report['possible_candidates'], [])
        counts = {c['id']: c['mismatch_count'] for c in report['candidates']}
        self.assertEqual(counts, {'R0': 1, 'S0': 1})

    def test_identical_pixels_with_opposite_approval_never_decide(self):
        plan = four_candidate_plan(
            candidates=[
                {'id': 'PASS', 'pack': 'main', 'scene': 'A', 'allowed': True},
                {'id': 'FAIL', 'pack': 'main', 'scene': 'A', 'allowed': False},
            ],
            max_reads=9, max_bad_reads=0)
        ws = Workspace(self, plan=plan)
        card_path = ws.make_card()
        card = json.loads(card_path.read_bytes())
        # Both identities survive on the card; neither represents the other.
        self.assertEqual(card['guarantee'], 'not_guaranteed')
        self.assertEqual([c['id'] for c in card['candidates']], ['PASS', 'FAIL'])
        proc, report_path = ws.judge(
            card_path, receipt('PANEL-72', card_path,
                               [(tx, DARK) for tx in range(9)]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'undecided')
        self.assertEqual(report['possible_candidates'], ['PASS', 'FAIL'])


class ReceiptJudgmentTests(unittest.TestCase):
    """Scenario 3: judging receipts from the card alone."""

    def setUp(self):
        self.ws = Workspace(self)
        self.card_path = self.ws.make_card()
        self.card = json.loads(self.card_path.read_bytes())
        # Sources and packs are gone; the card must be self-sufficient.
        os.rename(self.ws.plan_path, str(self.ws.plan_path) + '.gone')
        os.rename(self.ws.pack_path, str(self.ws.pack_path) + '.gone')

    def judge(self, reads, name='report.json', panel='PANEL-72', digest=None):
        readback = receipt(panel, self.card_path, reads)
        if digest is not None:
            readback['card_sha256'] = digest
        return self.ws.judge(self.card_path, readback, name=name)

    def test_sufficient_receipt_out_of_strategy_order_clears(self):
        # S1 on screen; Y group listed before G group, not the card's order.
        reads = [(8, LIT), (7, LIT), (6, LIT), (2, LIT), (1, LIT), (0, LIT)]
        proc, report_path = self.judge(reads)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['panel_id'], 'PANEL-72')
        self.assertEqual(report['card_sha256'],
                         hashlib.sha256(self.card_path.read_bytes()).hexdigest())
        self.assertEqual(report['verdict'], 'cleared')
        self.assertEqual(report['possible_candidates'], ['S1'])
        self.assertEqual(report['guarantee'], 'guaranteed')
        self.assertIn('前提', report['premise'])
        s0 = next(c for c in report['candidates'] if c['id'] == 'S0')
        self.assertFalse(s0['possible'])
        self.assertEqual([m['tx'] for m in s0['mismatches']], [6, 7, 8])

    def test_insufficient_evidence_is_not_cleared(self):
        # Only group G read dark: R0 and R1 both remain possible.
        proc, report_path = self.judge([(0, DARK), (1, DARK), (2, DARK)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'undecided')
        self.assertEqual(report['possible_candidates'], ['R0', 'R1'])
        # An empty receipt (instrument disconnected) is not a rejection.
        proc, report_path = self.judge([], name='empty.json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'undecided')
        self.assertEqual(len(report['possible_candidates']), 4)

    def test_later_evidence_overrides_a_cleared_route(self):
        # The first five reads alone would clear R0 (one mismatch); the
        # sixth pushes R0 over the error budget, so the whole receipt is
        # inconsistent -- the passed-through cleared leaf is not a clearance.
        prefix = [(0, LIT), (1, DARK), (2, DARK), (3, DARK), (4, DARK)]
        proc, report_path = self.judge(prefix, name='prefix.json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(report_path.read_bytes())['verdict'],
                         'cleared')
        proc, report_path = self.judge(prefix + [(5, LIT)], name='full.json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'inconsistent')
        self.assertEqual(report['possible_candidates'], [])
        r0 = next(c for c in report['candidates'] if c['id'] == 'R0')
        self.assertEqual(r0['mismatch_count'], 2)
        self.assertEqual([m['tx'] for m in r0['mismatches']], [0, 5])

    def test_contradicting_evidence_can_still_turn_inconsistent(self):
        prefix = [(0, DARK), (1, DARK), (3, DARK), (4, DARK), (5, LIT)]
        proc, report_path = self.judge(prefix, name='prefix.json')
        self.assertEqual(json.loads(report_path.read_bytes())['verdict'],
                         'cleared')
        proc, report_path = self.judge(prefix + [(2, LIT)], name='full.json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'inconsistent')
        self.assertEqual(report['possible_candidates'], [])
        # Every candidate shows more mismatches than the error budget.
        for cand in report['candidates']:
            self.assertGreater(cand['mismatch_count'], report['max_bad_reads'])
            self.assertFalse(cand['possible'])

    def test_receipt_binding_and_shape_failures_leave_no_report(self):
        other = Workspace(self, plan=four_candidate_plan(max_bad_reads=0))
        other_card = other.make_card('other-card.json')
        other_digest = hashlib.sha256(other_card.read_bytes()).hexdigest()
        good = receipt('PANEL-72', self.card_path, [(0, DARK)])
        cases = []
        wrong_card = dict(good, card_sha256=other_digest)
        cases.append(('bound-to-other-card', wrong_card))
        cases.append(('zero-digest', dict(good, card_sha256='0' * 64)))
        cases.append(('bad-panel', dict(good, panel_id='PANEL-OTHER')))
        duplicate = dict(good, reads=[{'tx': 0, 'ty': 0, 'bits': DARK},
                                      {'tx': 0, 'ty': 0, 'bits': DARK}])
        cases.append(('duplicate-read', duplicate))
        over = dict(good, reads=[{'tx': tx, 'ty': 0, 'bits': DARK}
                                 for tx in range(7)])
        cases.append(('over-budget', over))
        offprobe = dict(good, reads=[{'tx': 9, 'ty': 0, 'bits': DARK}])
        cases.append(('off-probe', offprobe))
        badbits = dict(good, reads=[{'tx': 0, 'ty': 0, 'bits': 'ABCDEF0123456789'}])
        cases.append(('bad-bits', badbits))
        for name, readback in cases:
            readback_path = self.ws.dir / f'{name}.json'
            write_bytes(readback_path, readback)
            report_path = self.ws.dir / f'{name}.report.json'
            proc = run_cli('inspect-result', '--card', str(self.card_path),
                           '--readback', str(readback_path),
                           '--output', str(report_path))
            self.assertEqual(proc.returncode, 2, name)
            self.assertFalse(report_path.exists(), name)

    def test_tampered_card_data_drives_the_verdict(self):
        # Two expectations flipped: structurally valid, but R0 now shows two
        # mismatches against dark reads and drops out over the error budget.
        card = json.loads(self.card_path.read_bytes())
        card['candidates'][0]['tiles'][0]['bits'] = LIT
        card['candidates'][0]['tiles'][1]['bits'] = LIT
        tampered = self.ws.dir / 'tampered-card.json'
        write_bytes(tampered, card)
        readback = receipt('PANEL-72', tampered, [(0, DARK), (1, DARK)])
        proc, report_path = self.ws.judge(tampered, readback)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        r0 = next(c for c in report['candidates'] if c['id'] == 'R0')
        self.assertFalse(r0['possible'])
        self.assertEqual([m['tx'] for m in r0['mismatches']], [0, 1])

    def test_structurally_bad_cards_exit_two(self):
        import copy
        card = json.loads(self.card_path.read_bytes())
        missing_bits = copy.deepcopy(card)
        missing_bits['candidates'][0]['tiles'] = \
            missing_bits['candidates'][0]['tiles'][1:]
        no_strategy = copy.deepcopy(card)
        del no_strategy['strategy']
        variants = {
            'bad-kind': dict(card, kind='pixel-type.other'),
            'bad-version': dict(card, schema_version=99),
            'no-strategy': no_strategy,
            'missing-probe-bits': missing_bits,
        }
        for name, variant in variants.items():
            bad_card = self.ws.dir / f'{name}.json'
            write_bytes(bad_card, variant)
            readback = receipt('PANEL-72', bad_card, [(0, DARK)])
            readback_path = self.ws.dir / f'{name}.rb.json'
            write_bytes(readback_path, readback)
            report_path = self.ws.dir / f'{name}.report.json'
            proc = run_cli('inspect-result', '--card', str(bad_card),
                           '--readback', str(readback_path),
                           '--output', str(report_path))
            self.assertEqual(proc.returncode, 2, name)
            self.assertFalse(report_path.exists(), name)


class PlanRejectionTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def plan_fails(self, plan, name='card.json'):
        plan_path = self.ws.dir / 'bad-plan.json'
        write_bytes(plan_path, plan)
        card_path = self.ws.dir / name
        proc = run_cli('inspect-plan', '--source', str(plan_path),
                       '--output', str(card_path))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(card_path.exists())

    def test_corrupted_unreferenced_pack_record_blocks_the_card(self):
        # Scene E is not referenced by any candidate, but its record is bad.
        source = pack_source(extra_scenes=[
            {'id': 'E', 'direct_only': False,
             'layout': scene_layout('dd' + ' ' * 7)}])
        ws = Workspace(self, source=source)
        pack = json.loads(ws.pack_path.read_bytes())
        import base64
        payload = bytearray(base64.b64decode(pack['payload_base64']))
        entry = next(e for e in pack['catalog'] if e['id'] == 'E')
        # Corrupt the unreferenced record's declared length.
        entry['length'] += 1
        pack['payload_base64'] = base64.b64encode(bytes(payload)).decode()
        write_bytes(ws.pack_path, pack)
        proc = run_cli('inspect-plan', '--source', str(ws.plan_path),
                       '--output', str(ws.dir / 'card.json'))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((ws.dir / 'card.json').exists())

    def test_bad_source_shapes(self):
        base = four_candidate_plan()
        cases = [
            ('duplicate probe', {'probe_tiles': [{'tx': 0, 'ty': 0}] * 2}),
            ('probe outside', {'probe_tiles': [{'tx': 9, 'ty': 0}]}),
            ('no probes', {'probe_tiles': []}),
            ('budget too large', {'max_reads': 10}),
            ('bad-reads above one', {'max_bad_reads': 2}),
            ('unknown pack alias',
             {'candidates': [{'id': 'X', 'pack': 'none', 'scene': 'A',
                              'allowed': True}]}),
            ('unknown scene',
             {'candidates': [{'id': 'X', 'pack': 'main', 'scene': 'ZZ',
                              'allowed': True}]}),
            ('duplicate candidate id', {'candidates': [
                {'id': 'X', 'pack': 'main', 'scene': 'A', 'allowed': True},
                {'id': 'X', 'pack': 'main', 'scene': 'B', 'allowed': False}]}),
            ('allowed not boolean', {'candidates': [
                {'id': 'X', 'pack': 'main', 'scene': 'A', 'allowed': 1}]}),
            ('too many packs', {'packs': {f'p{i}': 'pack.json'
                                          for i in range(5)}}),
            ('bad panel id', {'panel_id': 'bad panel'}),
            ('width not multiple of 8', {'width': 70}),
        ]
        for name, patch in cases:
            plan = dict(base)
            plan.update(patch)
            self.plan_fails(plan, name=f'{name}.json')

    def test_pack_size_mismatch_rejected(self):
        small = compile_pack({'width': 8, 'height': 8, 'payload_budget': 100,
                              'scenes': [{'id': 'A', 'direct_only': False,
                                          'layout': scene_layout(' ') }]})
        write_bytes(self.ws.pack_path, small)
        proc = run_cli('inspect-plan', '--source', str(self.ws.plan_path),
                       '--output', str(self.ws.dir / 'card.json'))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((self.ws.dir / 'card.json').exists())

    def test_missing_files_and_bad_json_exit_two(self):
        card_path = self.ws.dir / 'card.json'
        proc = run_cli('inspect-plan', '--source', str(self.ws.dir / 'no.json'),
                       '--output', str(card_path))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(card_path.exists())
        bad = self.ws.dir / 'bad.json'
        bad.write_text('{not json')
        proc = run_cli('inspect-plan', '--source', str(bad),
                       '--output', str(card_path))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(card_path.exists())
        # missing pack file
        plan = four_candidate_plan(packs={'main': 'gone.json'})
        plan_path = self.ws.dir / 'missing-pack-plan.json'
        write_bytes(plan_path, plan)
        proc = run_cli('inspect-plan', '--source', str(plan_path),
                       '--output', str(card_path))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(card_path.exists())

    def test_existing_outputs_are_never_overwritten(self):
        card_path = self.ws.dir / 'card.json'
        card_path.write_text('sentinel')
        proc = run_cli('inspect-plan', '--source', str(self.ws.plan_path),
                       '--output', str(card_path))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(card_path.read_text(), 'sentinel')
        # Same for reports.
        real_card = self.ws.make_card('real.json')
        report_path = self.ws.dir / 'report.json'
        report_path.write_text('sentinel')
        readback_path = self.ws.dir / 'rb.json'
        write_bytes(readback_path,
                    receipt('PANEL-72', real_card, [(0, DARK)]))
        proc = run_cli('inspect-result', '--card', str(real_card),
                       '--readback', str(readback_path),
                       '--output', str(report_path))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(report_path.read_text(), 'sentinel')


class EdgeCaseTests(unittest.TestCase):
    def test_all_allowed_needs_no_sampling(self):
        plan = four_candidate_plan(
            candidates=[
                {'id': 'P1', 'pack': 'main', 'scene': 'A', 'allowed': True},
                {'id': 'P2', 'pack': 'main', 'scene': 'B', 'allowed': True},
            ],
            max_reads=0)
        ws = Workspace(self, plan=plan)
        card_path = ws.make_card()
        card = json.loads(card_path.read_bytes())
        self.assertEqual(card['guarantee'], 'guaranteed')
        self.assertEqual(card['strategy']['nodes'],
                         [{'action': 'stop', 'id': 0}])
        proc, report_path = ws.judge(
            card_path, receipt('PANEL-72', card_path, []))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(report_path.read_bytes())['verdict'],
                         'cleared')

    def test_all_rejected_needs_no_sampling(self):
        plan = four_candidate_plan(
            candidates=[
                {'id': 'F1', 'pack': 'main', 'scene': 'A', 'allowed': False},
                {'id': 'F2', 'pack': 'main', 'scene': 'B', 'allowed': False},
            ],
            max_reads=0)
        ws = Workspace(self, plan=plan)
        card_path = ws.make_card()
        card = json.loads(card_path.read_bytes())
        self.assertEqual(card['guarantee'], 'guaranteed')
        proc, report_path = ws.judge(
            card_path, receipt('PANEL-72', card_path, []))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(report_path.read_bytes())['verdict'],
                         'rejected')

    def test_zero_budget_mixed_approval_is_not_guaranteed(self):
        plan = four_candidate_plan(max_reads=0)
        ws = Workspace(self, plan=plan)
        card_path = ws.make_card()
        card = json.loads(card_path.read_bytes())
        self.assertEqual(card['guarantee'], 'not_guaranteed')
        proc, report_path = ws.judge(
            card_path, receipt('PANEL-72', card_path, []))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(report_path.read_bytes())['verdict'],
                         'undecided')

    def test_multiple_packs_and_cross_pack_pixel_identity(self):
        # A second pack whose scene Z is pixel-identical to main's A but a
        # different business identity (name, pack, record).
        alt_source = {'width': PANEL_W, 'height': PANEL_H, 'payload_budget': 100,
                      'scenes': [{'id': 'Z', 'direct_only': True,
                                  'layout': scene_layout(' ' * 9)}]}
        alt = compile_pack(alt_source)
        ws = Workspace(self)
        write_bytes(ws.dir / 'alt.json', alt)
        plan = four_candidate_plan(
            packs={'main': 'pack.json', 'alt': 'alt.json'},
            candidates=[
                {'id': 'R0a', 'pack': 'main', 'scene': 'A', 'allowed': True},
                {'id': 'R0b', 'pack': 'alt', 'scene': 'Z', 'allowed': True},
                {'id': 'S0', 'pack': 'main', 'scene': 'B', 'allowed': False},
            ],
            max_reads=6, max_bad_reads=1)
        write_bytes(ws.plan_path, plan)
        card_path = ws.make_card()
        card = json.loads(card_path.read_bytes())
        self.assertEqual(card['guarantee'], 'guaranteed')
        self.assertEqual(len(card['candidates']), 3)
        reads = [(tx, DARK) for tx in range(3)]  # group G dark, 3 reads
        reads += [(tx, DARK) for tx in range(3, 6)]
        proc, report_path = ws.judge(
            card_path, receipt('PANEL-72', card_path, reads))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report['verdict'], 'cleared')
        # Both pixel-identical identities are reported separately.
        self.assertEqual(report['possible_candidates'], ['R0a', 'R0b'])


if __name__ == '__main__':
    unittest.main()
