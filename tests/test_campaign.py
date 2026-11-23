"""Acceptance tests for the offline campaign-fit batch report (v6)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import write_bytes
from pixel_type import campaign
from pixel_type.layout import layout as v1_layout
from pixel_type.model import validate as v1_validate
from pixel_type.store import canonical_bytes

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
FIXTURE = json.loads((ROOT / 'fixtures' / 'input.json').read_text())

# The real v1 font from fixtures/input.json: A(4), V(4), space(2), kern A->V -1.
V1_GLYPHS = FIXTURE['glyphs']
V1_KERNING = FIXTURE['kerning']

# A tiny font where every advance is 1 and only letters carry ink.
UNIT_FONT = [
    {'char': c, 'advance': 1, 'offset': [0, 0], 'rows': ['1']}
    for c in 'abcde'
] + [{'char': ' ', 'advance': 1, 'offset': [0, 0], 'rows': []}]

# A font exercising offsets, blank glyphs and overlapping ink.
INK_FONT = [
    {'char': 'A', 'advance': 4, 'offset': [0, 0], 'rows': ['1']},
    {'char': 'x', 'advance': 4, 'offset': [-1, 0], 'rows': ['1']},
    {'char': 'y', 'advance': 4, 'offset': [0, -2], 'rows': ['1']},
    {'char': 'C', 'advance': 8, 'offset': [0, 0],
     'rows': ['11111111', '00000000', '00000000',
              '00000000', '00000000', '10000001']},
    {'char': 'O', 'advance': 2, 'offset': [0, 0], 'rows': ['11', '11']},
    {'char': 'P', 'advance': 2, 'offset': [-1, 0], 'rows': ['11', '11']},
    {'char': ' ', 'advance': 2, 'offset': [0, 0], 'rows': []},
]

SEARCH_FONT = [
    {'char': 'a', 'advance': 2, 'offset': [0, 0], 'rows': ['1']},
    {'char': 'y', 'advance': 2, 'offset': [0, -2], 'rows': ['1']},
    {'char': ' ', 'advance': 2, 'offset': [0, 0], 'rows': []},
]


def display(did, width, height, safe, max_lines):
    return {'id': did, 'width': width, 'height': height,
            'safe_rect': safe, 'max_lines': max_lines}


def message(mid, text, spans=()):
    return {'id': mid, 'text': text,
            'protected_spans': [{'start': s, 'end': e} for s, e in spans]}


def source(glyphs, kerning, displays, messages, line_height=6):
    return {'line_height': line_height, 'glyphs': glyphs, 'kerning': kerning,
            'displays': displays, 'messages': messages}


def result_of(report, mid, did):
    for result in report['results']:
        if result['message_id'] == mid and result['display_id'] == did:
            return result
    raise AssertionError(f'no result for {mid} x {did}')


def ragged(lines, safe_width, text):
    """Raggedness recomputed from a report's lines: every line except each
    explicit-segment's last line contributes (safe_width - advance)^2."""
    total = 0
    for line in lines:
        indices = line['character_indices']
        if not indices:
            continue  # an empty line always ends its explicit segment
        last = indices[-1]
        if last == len(text) - 1 or text[last + 1] == '\n':
            continue
        total += (safe_width - line['advance']) ** 2
    return total


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, '-m', 'pixel_type.cli', *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


class GlobalChoiceTests(unittest.TestCase):
    """Area 1: global breaking choice, two widths, real v1 font, tie-break."""

    def fit(self, text, widths, max_lines=8):
        displays = [display(f'W{w}', 24, 24,
                            {'x': 1, 'y': 2, 'width': w, 'height': 20}, max_lines)
                    for w in widths]
        report = campaign.build_report(
            source(V1_GLYPHS, V1_KERNING, displays, [message('M', text)]))
        return {r['display_id']: r for r in report['results']}

    def test_greedy_wrap_is_not_the_spec_optimum(self):
        results = self.fit('AA AVA', [10, 14])
        narrow = results['W10']
        self.assertTrue(narrow['eligible'])
        # Spec optimum: 3 lines, breaks [1, 4], advances 8/9/4, raggedness 5.
        self.assertEqual(narrow['breaks'], [1, 4])
        self.assertEqual([l['advance'] for l in narrow['lines']], [8, 9, 4])
        self.assertEqual(ragged(narrow['lines'], 10, 'AA AVA'), 5)
        self.assertEqual([l['character_indices'] for l in narrow['lines']],
                         [[0, 1], [2, 3, 4], [5]])
        # The v1 greedy wrap packs line 1 fully: advances 10/7/4, breaks
        # [2, 4], raggedness 9 -- locally fine, globally worse.
        glyphs, kerning, text = v1_validate(
            {'glyphs': V1_GLYPHS, 'kerning': V1_KERNING, 'text': 'AA AVA',
             'line_width': 10, 'line_height': 6})
        greedy_lines, _ = v1_layout(glyphs, kerning, text, 10, 6)
        self.assertEqual([l['advance'] for l in greedy_lines], [10, 7, 4])
        greedy_ragged = (10 - 10) ** 2 + (10 - 7) ** 2
        self.assertEqual(greedy_ragged, 9)
        self.assertGreater(greedy_ragged, 5)
        # The wider display admits a different, also optimal, 2-line split.
        wide = results['W14']
        self.assertTrue(wide['eligible'])
        self.assertEqual(wide['breaks'], [3])
        self.assertEqual([l['advance'] for l in wide['lines']], [14, 8])

    def test_placements_carry_raw_indices_and_absolute_positions(self):
        results = self.fit('AA AVA', [10])
        narrow = results['W10']
        self.assertEqual([p['character_index'] for p in narrow['placements']],
                         [0, 1, 2, 3, 4, 5])
        self.assertEqual([p['line'] for p in narrow['placements']],
                         [0, 0, 1, 1, 1, 2])
        # Pen starts at safe_rect.x; line tops at safe_rect.y + line*height.
        self.assertEqual(narrow['placements'][0]['pen_x'], 1)
        self.assertEqual(narrow['placements'][0]['ink_origin'], [1, 2])
        # The A->V kern of -1 applies inside line 1 (raw indices 3, 4).
        self.assertEqual(narrow['placements'][4]['applied_kerning'], -1)
        self.assertEqual(narrow['placements'][4]['pen_x'], 1 + 2 + 4 - 1)
        self.assertEqual(narrow['ink_bbox'], {'min': [1, 2], 'max_exclusive': [9, 19]})
        self.assertEqual(narrow['unique_ink_pixels'], 49)

    def test_raggedness_tie_broken_by_smallest_break_indices(self):
        # Both [1, 4] and [2, 4] give 3 lines with raggedness 4; the
        # lexicographically smaller break sequence [1, 4] must win.  (The v1
        # greedy wrap happens to pick the other side of this tie.)
        results = self.fit('AA AAAA', [10])
        narrow = results['W10']
        self.assertTrue(narrow['eligible'])
        self.assertEqual(narrow['breaks'], [1, 4])
        self.assertEqual([l['advance'] for l in narrow['lines']], [8, 10, 8])
        self.assertEqual(ragged(narrow['lines'], 10, 'AA AAAA'), 4)


class ProtectedSpanTests(unittest.TestCase):
    """Area 2: protected spans, explicit newlines, order independence."""

    def fit(self, text, spans, width, max_lines=8):
        displays = [display('D', 24, 24, {'x': 1, 'y': 1, 'width': width,
                                          'height': 20}, max_lines)]
        report = campaign.build_report(
            source(UNIT_FONT, [], displays, [message('M', text, spans)]))
        return report['results'][0]

    def test_span_forbids_breaks_that_would_reduce_line_count(self):
        free = self.fit('aa bb cc', [], 4)
        self.assertTrue(free['eligible'])
        self.assertEqual(len(free['lines']), 2)
        self.assertEqual(free['breaks'], [3])
        # Protecting "bb" (indices 3..4) forbids the only 2-line break; the
        # best legal alternative needs 3 lines.
        protected = self.fit('aa bb cc', [(3, 5)], 4)
        self.assertTrue(protected['eligible'])
        self.assertEqual(len(protected['lines']), 3)
        self.assertEqual(protected['breaks'], [2, 6])
        self.assertEqual([l['character_indices'] for l in protected['lines']],
                         [[0, 1, 2], [3, 4, 5, 6], [7]])

    def test_span_wider_than_safe_width_is_reported_not_split(self):
        free = self.fit('a bcde', [], 3)
        self.assertTrue(free['eligible'])
        self.assertEqual(len(free['lines']), 2)
        # "bcde" (indices 2..5) cannot be split and is wider than the safe
        # width: no legal breaking exists at all.
        protected = self.fit('a bcde', [(2, 6)], 3)
        self.assertFalse(protected['eligible'])
        self.assertEqual(protected['reason'], 'line_or_width_limit_exceeded')
        self.assertIsNone(protected['details']['min_lines_within_safe_width'])
        self.assertEqual(protected['details']['min_widest_line_advance'], 4)
        self.assertEqual(protected['details']['safe_width'], 3)

    def test_span_covering_multiple_boundaries(self):
        # A span over 4 codepoints forbids 3 internal break positions.
        result = self.fit('abcde', [(0, 4)], 4)
        self.assertTrue(result['eligible'])
        # Only the boundary after index 4 (none, text ends) and after the
        # span are legal; "abcd" must stay together on line 1.
        self.assertEqual(result['breaks'], [3])
        self.assertEqual([l['character_indices'] for l in result['lines']],
                         [[0, 1, 2, 3], [4]])

    def test_explicit_and_consecutive_newlines_and_edge_spaces(self):
        font = [
            {'char': 'a', 'advance': 2, 'offset': [0, 0], 'rows': ['1']},
            {'char': ' ', 'advance': 2, 'offset': [0, 0], 'rows': []},
        ]
        displays = [display('D', 24, 24, {'x': 1, 'y': 1, 'width': 8,
                                          'height': 20}, 8)]
        report = campaign.build_report(
            source(font, [], displays, [message('M', ' a\n\na ')]))
        result = report['results'][0]
        self.assertTrue(result['eligible'])
        # Segments " a", "", "a ": the empty middle line is kept, spaces are
        # ordinary glyphs, and indices point at the raw text (newline at 2
        # and 3 consumed, never placed).
        self.assertEqual([l['advance'] for l in result['lines']], [4, 0, 4])
        self.assertEqual([l['character_indices'] for l in result['lines']],
                         [[0, 1], [], [4, 5]])
        self.assertEqual(result['breaks'], [])
        self.assertEqual([p['character_index'] for p in result['placements']],
                         [0, 1, 4, 5])
        self.assertEqual([p['char'] for p in result['placements']],
                         [' ', 'a', 'a', ' '])
        self.assertEqual(result['unique_ink_pixels'], 2)

    def test_empty_ink_text_still_fits(self):
        font = [{'char': ' ', 'advance': 2, 'offset': [0, 0], 'rows': []}]
        displays = [display('D', 24, 24, {'x': 1, 'y': 1, 'width': 8,
                                          'height': 20}, 8)]
        report = campaign.build_report(
            source(font, [], displays, [message('M', ' \n ')]))
        result = report['results'][0]
        self.assertTrue(result['eligible'])
        self.assertEqual([l['advance'] for l in result['lines']], [2, 2])
        self.assertIsNone(result['ink_bbox'])
        self.assertEqual(result['unique_ink_pixels'], 0)

    def test_report_bytes_are_input_order_independent(self):
        glyphs = V1_GLYPHS + [
            {'char': 'x', 'advance': 4, 'offset': [-1, 0], 'rows': ['1']},
            {'char': 'y', 'advance': 2, 'offset': [0, -2], 'rows': ['1']},
        ]
        kerning = V1_KERNING + [{'left': 'x', 'right': 'A', 'adjust': 1}]
        displays = [
            display('WIDE', 24, 24, {'x': 1, 'y': 2, 'width': 14, 'height': 20}, 8),
            display('NARROW', 16, 24, {'x': 1, 'y': 2, 'width': 10, 'height': 18}, 3),
            display('TALL', 16, 32, {'x': 0, 'y': 4, 'width': 8, 'height': 24}, 4),
        ]
        messages = [
            message('M2', 'AA AVA'),
            message('M1', 'xA y', [(0, 2)]),
            message('M3', 'A\n\nV '),
        ]
        doc = source(glyphs, kerning, displays, messages)
        baseline = canonical_bytes(campaign.build_report(doc))
        shuffled = json.loads(json.dumps(doc))
        shuffled['glyphs'].reverse()
        shuffled['kerning'].reverse()
        shuffled['displays'].reverse()
        shuffled['messages'].reverse()
        for msg in shuffled['messages']:
            msg['protected_spans'].reverse()
        again = canonical_bytes(campaign.build_report(shuffled))
        self.assertEqual(baseline, again)


class InkBoundsTests(unittest.TestCase):
    """Area 3: advance fits but real ink leaves the safe rectangle."""

    SAFE = {'x': 2, 'y': 1, 'width': 8, 'height': 6}

    def fit(self, texts, safe=None, line_height=6, max_lines=4, size=(16, 16)):
        displays = [display('D', size[0], size[1], safe or self.SAFE, max_lines)]
        report = campaign.build_report(
            source(INK_FONT, [], displays,
                   [message(f'M{i}', t) for i, t in enumerate(texts)],
                   line_height=line_height))
        return [r for r in report['results']]

    def test_negative_offset_ink_outside_safe_rect_is_diagnosed(self):
        bad, good = self.fit(['xA', 'Ax'])
        # 'x' has offset -1: as the first glyph on a line its ink lands one
        # pixel left of the safe rectangle, whatever the breaking.
        self.assertFalse(bad['eligible'])
        self.assertEqual(bad['reason'], 'ink_out_of_bounds')
        self.assertEqual(bad['details']['out_of_bounds_ink'],
                         [{'character_index': 0, 'x': 1, 'y': 1,
                           'outside': 'safe_rect'}])
        # The same glyph further right on the line is fully inside.
        self.assertTrue(good['eligible'])
        self.assertEqual(good['unique_ink_pixels'], 2)
        self.assertEqual(good['ink_bbox'], {'min': [2, 1], 'max_exclusive': [6, 2]})

    def test_high_glyph_outside_physical_screen_is_diagnosed(self):
        bad, good = self.fit(['y', 'A\ny'])
        # 'y' reaches 2px above its line top: on line 0 with safe y=1 its
        # ink leaves the physical screen entirely.
        self.assertFalse(bad['eligible'])
        self.assertEqual(bad['reason'], 'ink_out_of_bounds')
        self.assertEqual(bad['details']['out_of_bounds_ink'],
                         [{'character_index': 0, 'x': 2, 'y': -1,
                           'outside': 'physical_screen'}])
        # On line 1 (after an explicit newline) the same glyph is safe.
        self.assertTrue(good['eligible'])
        self.assertEqual([l['character_indices'] for l in good['lines']],
                         [[0], [2]])
        self.assertEqual(good['ink_bbox'], {'min': [2, 1], 'max_exclusive': [3, 6]})

    def test_ink_exactly_on_safe_rect_boundary_is_eligible(self):
        (result,) = self.fit(['C'])
        self.assertTrue(result['eligible'])
        # Ink spans x 2..9 and y 1..6: exactly the safe rectangle's edges.
        self.assertEqual(result['ink_bbox'], {'min': [2, 1], 'max_exclusive': [10, 7]})
        self.assertEqual(result['unique_ink_pixels'], 10)

    def test_blank_glyph_and_overlapping_ink(self):
        blank, overlap = self.fit([' ', 'OP'])
        self.assertTrue(blank['eligible'])
        self.assertIsNone(blank['ink_bbox'])
        self.assertEqual(blank['unique_ink_pixels'], 0)
        # O covers x 2..3, P (offset -1) covers x 3..4: the shared column
        # lights once, but both placements keep their character identity.
        self.assertTrue(overlap['eligible'])
        self.assertEqual(overlap['unique_ink_pixels'], 6)
        self.assertEqual([p['character_index'] for p in overlap['placements']], [0, 1])
        self.assertEqual(overlap['ink_bbox'], {'min': [2, 1], 'max_exclusive': [5, 3]})

    def test_nominal_line_overflow_with_ink_inside_is_eligible(self):
        # line_height 8 exceeds the safe height 4, but the only ink pixel
        # sits at y=1, inside the safe rectangle: nominal overflow is fine.
        (result,) = self.fit(['A'], safe={'x': 2, 'y': 1, 'width': 8, 'height': 4},
                             line_height=8, max_lines=1)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['ink_bbox'], {'min': [2, 1], 'max_exclusive': [3, 2]})


class SearchCompletenessTests(unittest.TestCase):
    """Area 4: exhaustive search behind the eligibility verdict."""

    SAFE = {'x': 1, 'y': 2, 'width': 6, 'height': 12}

    def fit(self, text, max_lines, spans=(), safe=None):
        displays = [display('D', 16, 16, safe or self.SAFE, max_lines)]
        report = campaign.build_report(
            source(SEARCH_FONT, [], displays, [message('M', text, spans)]))
        return report['results'][0]

    def test_finds_the_only_ink_safe_breaking(self):
        # Advance-only optimum is one line ("aay", advance 6 <= 6), but then
        # 'y' (offset -2) inks above the safe top.  The only ink-safe
        # min-raggedness alternative is two lines breaking after index 1.
        result = self.fit('aay', 3)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['breaks'], [1])
        self.assertEqual([l['advance'] for l in result['lines']], [4, 2])
        self.assertEqual([l['character_indices'] for l in result['lines']],
                         [[0, 1], [2]])

    def test_ink_failure_is_not_confused_with_advance_failure(self):
        # With max_lines=1 the one-line advance-feasible breaking exists but
        # inks outside; this is an ink failure, not a line/width failure.
        result = self.fit('aay', 1)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'], 'ink_out_of_bounds')
        self.assertEqual(result['details']['breaks'], [])
        self.assertEqual(result['details']['out_of_bounds_ink'],
                         [{'character_index': 2, 'x': 5, 'y': 0,
                           'outside': 'safe_rect'}])

    def test_line_count_limit_is_diagnosed_separately(self):
        result = self.fit('aa aa aa', 2)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'], 'line_or_width_limit_exceeded')
        self.assertEqual(result['details']['min_lines_within_safe_width'], 3)
        self.assertEqual(result['details']['max_lines'], 2)
        self.assertEqual(result['details']['min_widest_line_advance'], 2)

    def test_unsplittable_width_overflow_is_diagnosed_separately(self):
        result = self.fit('aaaa', 4, spans=[(0, 4)], safe={'x': 1, 'y': 2, 'width': 3, 'height': 12})
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'], 'line_or_width_limit_exceeded')
        self.assertIsNone(result['details']['min_lines_within_safe_width'])
        self.assertEqual(result['details']['min_widest_line_advance'], 8)
        self.assertEqual(result['details']['safe_width'], 3)


class StructuralLimitTests(unittest.TestCase):
    """Area 5 (cont.): structural limits reject the whole batch."""

    def doc(self):
        displays = [display('D', 24, 24, {'x': 1, 'y': 1, 'width': 14,
                                          'height': 20}, 4)]
        doc = source(V1_GLYPHS, V1_KERNING, displays, [message('M', 'AA AVA')])
        return json.loads(json.dumps(doc))  # deep copy: mutations stay local

    def assert_rejected(self, doc, fragment):
        with self.assertRaises(ValueError) as ctx:
            campaign.build_report(doc)
        self.assertIn(fragment, str(ctx.exception))

    def test_unknown_and_missing_fields(self):
        doc = self.doc()
        doc['unexpected'] = 1
        self.assert_rejected(doc, 'expected fields')
        doc = self.doc()
        del doc['line_height']
        self.assert_rejected(doc, 'expected fields')
        doc = self.doc()
        doc['displays'][0]['rotation'] = 90
        self.assert_rejected(doc, 'expected fields')
        doc = self.doc()
        doc['messages'][0]['protected_spans'] = [{'start': 0, 'end': 1, 'note': 'x'}]
        self.assert_rejected(doc, 'expected fields')

    def test_line_height_and_display_geometry(self):
        doc = self.doc()
        doc['line_height'] = 0
        self.assert_rejected(doc, 'integer 1..1000 required')
        doc = self.doc()
        doc['displays'][0]['width'] = 20  # not a multiple of 8
        self.assert_rejected(doc, 'multiples of 8')
        doc = self.doc()
        doc['displays'][0]['safe_rect'] = {'x': 20, 'y': 1, 'width': 14, 'height': 20}
        self.assert_rejected(doc, 'inside the screen')
        doc = self.doc()
        doc['displays'][0]['safe_rect'] = {'x': 1, 'y': 1, 'width': 0, 'height': 20}
        self.assert_rejected(doc, 'integer 1..256 required')
        doc = self.doc()
        doc['displays'][0]['max_lines'] = 0
        self.assert_rejected(doc, 'integer 1..128 required')

    def test_id_rules_and_collection_sizes(self):
        doc = self.doc()
        doc['displays'][0]['id'] = 'bad id'
        self.assert_rejected(doc, 'display id')
        doc = self.doc()
        doc['messages'][0]['id'] = ''
        self.assert_rejected(doc, 'message id')
        doc = self.doc()
        doc['displays'] = []
        self.assert_rejected(doc, 'array size 1..16 required')
        doc = self.doc()
        doc['messages'] = [message(f'M{i}', 'A') for i in range(33)]
        self.assert_rejected(doc, 'array size 1..32 required')

    def test_text_and_budget_limits(self):
        doc = self.doc()
        doc['messages'][0]['text'] = ''
        self.assert_rejected(doc, '1..2000 text codepoints required')
        doc = self.doc()
        doc['messages'][0]['text'] = 'A' * 2001
        self.assert_rejected(doc, '1..2000 text codepoints required')
        doc = self.doc()
        doc['messages'] = [message(f'M{i}', 'A' * 267) for i in range(31)]
        doc['messages'][0]['text'] = 'A' * 2000
        self.assert_rejected(doc, '8000 codepoints')
        doc = self.doc()
        doc['messages'][0]['text'] = 'A\x01A'
        self.assert_rejected(doc, 'printable Unicode scalar')

    def test_glyph_and_kerning_rules(self):
        doc = self.doc()
        doc['glyphs'] = doc['glyphs'] + [doc['glyphs'][0]]
        self.assert_rejected(doc, 'duplicate glyph')
        doc = self.doc()
        doc['kerning'] = [{'left': 'A', 'right': 'Z', 'adjust': 1}]
        self.assert_rejected(doc, 'unknown or duplicate kern pair')
        doc = self.doc()
        doc['kerning'] = [{'left': 'A', 'right': 'V', 'adjust': -4}]
        self.assert_rejected(doc, 'must remain positive')
        doc = self.doc()
        doc['glyphs'][0]['rows'][0] = '102'
        self.assert_rejected(doc, 'bitmap row')


class ValidationAndCliTests(unittest.TestCase):
    """Area 5: batch rejection, CLI exit codes, output-path safety."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.displays = [display('D', 24, 24, {'x': 1, 'y': 1, 'width': 14,
                                               'height': 20}, 4)]
        self.messages = [message('M', 'AA AVA')]

    def doc(self):
        return source(V1_GLYPHS, V1_KERNING, self.displays, self.messages)

    def run_doc(self, doc, name='src.json', out='report.json'):
        src_path = self.dir / name
        write_bytes(src_path, doc)
        out_path = self.dir / out
        return run_cli('campaign-fit', '--source', str(src_path),
                       '--output', str(out_path)), out_path

    def test_successful_run_writes_report_and_summary(self):
        proc, out_path = self.run_doc(self.doc())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary, {'combinations': 1, 'eligible': 1,
                                   'ineligible': 0})
        report = json.loads(out_path.read_bytes())
        self.assertEqual(report['kind'], 'pixel-type.campaign-fit.report')
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual([d['id'] for d in report['displays']], ['D'])
        self.assertEqual([m['id'] for m in report['messages']], ['M'])

    def test_existing_output_and_symlink_are_never_overwritten(self):
        proc, out_path = self.run_doc(self.doc())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        original = out_path.read_bytes()
        again, _ = self.run_doc(self.doc())
        self.assertEqual(again.returncode, 2)
        self.assertEqual(out_path.read_bytes(), original)
        link = self.dir / 'link.json'
        link.symlink_to(out_path.name)
        third, _ = self.run_doc(self.doc(), out='link.json')
        self.assertEqual(third.returncode, 2)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), original)

    def test_failures_leave_no_report(self):
        bad_docs = []
        doc = self.doc()
        doc['messages'][0]['text'] = 'AA ZVA'  # no glyph for Z
        bad_docs.append(doc)
        doc = self.doc()
        doc['messages'][0]['protected_spans'] = [{'start': 0, 'end': 2},
                                                 {'start': 1, 'end': 3}]
        bad_docs.append(doc)
        doc = self.doc()
        doc['messages'][0]['protected_spans'] = [{'start': 2, 'end': 2}]
        bad_docs.append(doc)
        doc = self.doc()
        doc['messages'][0]['protected_spans'] = [{'start': 0, 'end': 99}]
        bad_docs.append(doc)
        doc = self.doc()
        doc['messages'][0]['text'] = 'AB\nCD'
        doc['messages'][0]['protected_spans'] = [{'start': 0, 'end': 3}]
        bad_docs.append(doc)
        doc = self.doc()
        doc['displays'] = [self.displays[0], self.displays[0]]
        bad_docs.append(doc)
        doc = self.doc()
        doc['messages'] = [self.messages[0], self.messages[0]]
        bad_docs.append(doc)
        for i, doc in enumerate(bad_docs):
            proc, out_path = self.run_doc(doc, name=f'bad{i}.json',
                                          out=f'bad{i}.report.json')
            self.assertEqual(proc.returncode, 2, f'case {i}: {proc.stdout}')
            self.assertTrue(proc.stderr.strip(), f'case {i}: empty stderr')
            self.assertFalse(out_path.exists(), f'case {i}: report left behind')

    def test_bad_json_and_duplicate_keys_are_rejected(self):
        src_path = self.dir / 'broken.json'
        src_path.write_bytes(b'{not json')
        proc = run_cli('campaign-fit', '--source', str(src_path),
                       '--output', str(self.dir / 'broken.report.json'))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((self.dir / 'broken.report.json').exists())
        dup = self.dir / 'dup.json'
        dup.write_bytes(b'{"line_height": 6, "line_height": 6, "glyphs": [],'
                        b' "kerning": [], "displays": [], "messages": []}')
        proc = run_cli('campaign-fit', '--source', str(dup),
                       '--output', str(self.dir / 'dup.report.json'))
        self.assertEqual(proc.returncode, 2)
        self.assertIn('duplicate JSON key', proc.stderr)
        self.assertFalse((self.dir / 'dup.report.json').exists())

    def test_missing_source_file_is_an_error(self):
        proc = run_cli('campaign-fit', '--source', str(self.dir / 'nope.json'),
                       '--output', str(self.dir / 'nope.report.json'))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((self.dir / 'nope.report.json').exists())

    def test_v1_cli_still_works(self):
        out_path = self.dir / 'v1.json'
        proc = run_cli('--source', str(ROOT / 'fixtures' / 'input.json'),
                       '--output', str(out_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary, {'drawn_pixels': 48, 'lines': 3,
                                   'overflow_glyphs': 0, 'placed_glyphs': 6,
                                   'unique_ink_pixels': 48})
        report = json.loads(out_path.read_bytes())
        self.assertEqual(report['schema_version'], 1)
        self.assertIn('svg_text', report)


if __name__ == '__main__':
    unittest.main()
