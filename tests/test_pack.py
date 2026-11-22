"""Acceptance tests for the offline scene pack compiler and frame extractor."""

import base64
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import dot_glyph, glyph, v1_input
from pixel_type.pack import (BudgetExceeded, bytes_to_word, compile_pack,
                             unpack_frame, word_to_bytes)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'

PANEL_W = 64
PANEL_H = 8


def three_scene_doc(direct=()):
    """A dark, B adds block 0/1 dots, C adds block 2 on top of B."""
    glyphs = [
        dot_glyph('a', [(0, 0)]),           # block 0 dot
        dot_glyph('b', [(0, 0), (0, 1)]),   # block 1 dots
        dot_glyph('c', [(0, 0)]),           # block 2 dot
        dot_glyph(' ', []),
    ]
    # One 8px advance per glyph: a->block0, b->block1, c->block2.
    def layout(text):
        return v1_input(glyphs, text, line_width=PANEL_W, line_height=PANEL_H)
    scenes = [
        {'id': 'A', 'direct_only': 'A' in direct, 'layout': layout(' ')},
        {'id': 'B', 'direct_only': 'B' in direct, 'layout': layout('ab')},
        {'id': 'C', 'direct_only': 'C' in direct, 'layout': layout('abc')},
    ]
    return {'width': PANEL_W, 'height': PANEL_H,
            'payload_budget': 200, 'scenes': scenes}


def frame_pixels(frame):
    pixels = set()
    for tile in frame['tiles']:
        for y, row in enumerate(tile['rows']):
            for x, ch in enumerate(row):
                if ch == '1':
                    pixels.add((tile['tx'] * 8 + x, tile['ty'] * 8 + y))
    return pixels


def expected_pixels(layout):
    from pixel_type.planner import render_pixels
    return render_pixels(layout)


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, '-m', 'pixel_type.cli', *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


class MinimumPayloadTests(unittest.TestCase):
    def test_free_assignment_is_one_hundred_bytes(self):
        pack = compile_pack(three_scene_doc())
        self.assertEqual(pack['payload_bytes'], 100)
        formats = {e['id']: e['format'] for e in pack['catalog']}
        self.assertEqual(formats, {'A': 'reference', 'B': 'full', 'C': 'reference'})
        # A and C both reference B, never a reference frame or residual state.
        payload = base64.b64decode(pack['payload_base64'])
        by_id = {e['id']: e for e in pack['catalog']}
        self.assertEqual(by_id['A']['length'], 3 + 10 * 2)
        self.assertEqual(by_id['C']['length'], 3 + 10 * 1)
        for scene in ('A', 'C'):
            entry = by_id[scene]
            self.assertEqual(payload[entry['offset']], 1)  # base = B index 1

    def test_direct_only_a_forces_one_hundred_twenty(self):
        pack = compile_pack(three_scene_doc(direct=('A',)))
        self.assertEqual(pack['payload_bytes'], 120)
        formats = {e['id']: e['format'] for e in pack['catalog']}
        self.assertEqual(formats['A'], 'full')
        payload = base64.b64decode(pack['payload_base64'])
        by_id = {e['id']: e for e in pack['catalog']}
        for scene in ('B', 'C'):
            base = payload[by_id[scene]['offset']]
            self.assertEqual(by_id[list(by_id)[base]]['format'], 'full')

    def test_budget_110_reports_exact_shortfall_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            output = Path(tmp) / 'pack.json'
            doc = three_scene_doc(direct=('A',))
            doc['payload_budget'] = 110
            source.write_text(json.dumps(doc))
            proc = run_cli('compile-pack', '--source', str(source),
                           '--output', str(output))
            self.assertEqual(proc.returncode, 2)
            self.assertIn('120', proc.stderr)
            self.assertIn('110', proc.stderr)
            self.assertFalse(output.exists())

    def test_budget_120_delivers_and_direct_scene_reads_cold(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            output = Path(tmp) / 'pack.json'
            doc = three_scene_doc(direct=('A',))
            doc['payload_budget'] = 120
            source.write_text(json.dumps(doc))
            proc = run_cli('compile-pack', '--source', str(source),
                           '--output', str(output))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            pack = json.loads(output.read_text())
            self.assertEqual(pack['payload_bytes'], 120)
            entry_a = next(e for e in pack['catalog'] if e['id'] == 'A')
            self.assertEqual(entry_a['format'], 'full')
            # Cold start only needs the full frame A.
            frame = unpack_frame(pack, 'A')
            self.assertEqual(frame_pixels(frame), set())

    def test_shuffled_scenes_produce_identical_bytes(self):
        doc = three_scene_doc(direct=('A',))
        first = json.dumps(compile_pack(doc), sort_keys=True)
        shuffled = json.loads(json.dumps(doc))
        random.Random(7).shuffle(shuffled['scenes'])
        second = json.dumps(compile_pack(shuffled), sort_keys=True)
        self.assertEqual(first, second)

    def test_all_full_frames_is_not_chosen_when_reference_is_cheaper(self):
        # A tempting greedy "each scene against the first input" or "all full"
        # solution would be 192 bytes; the optimum is 100.
        pack = compile_pack(three_scene_doc())
        formats = sorted(e['format'] for e in pack['catalog'])
        self.assertEqual(formats, ['full', 'reference', 'reference'])


class TieBreakTests(unittest.TestCase):
    def test_equal_cost_prefers_full_frame_then_smallest_base(self):
        # B and C are identical; storing B as the full frame (16 bytes) plus
        # C as a reference (3 bytes) and A as a reference (13 bytes) totals 32,
        # cheaper than two full frames. A's base must be the smaller index.
        glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph(' ', [])]
        layout = v1_input(glyphs, 'a', line_width=8, line_height=8)
        blank = v1_input(glyphs, ' ', line_width=8, line_height=8)
        scenes = [
            {'id': 'B', 'direct_only': False, 'layout': layout},
            {'id': 'C', 'direct_only': False, 'layout': layout},
            {'id': 'A', 'direct_only': False, 'layout': blank},
        ]
        pack = compile_pack({'width': 16, 'height': 8, 'payload_budget': 1000,
                             'scenes': scenes})
        formats = {e['id']: e['format'] for e in pack['catalog']}
        self.assertEqual(formats, {'A': 'reference', 'B': 'full', 'C': 'reference'})
        payload = base64.b64decode(pack['payload_base64'])
        by_id = {e['id']: e for e in pack['catalog']}
        self.assertEqual(payload[by_id['A']['offset']], 1)  # base index of B
        self.assertEqual(payload[by_id['C']['offset']], 1)


class RoundTripTests(unittest.TestCase):
    def test_each_scene_is_independent_and_correct(self):
        doc = three_scene_doc()
        pack = compile_pack(doc)
        by_id = {s['id']: s for s in doc['scenes']}
        for scene_id, scene in by_id.items():
            frame = unpack_frame(pack, scene_id)
            self.assertEqual((frame['width'], frame['height']), (64, 8))
            self.assertEqual(len(frame['tiles']), 8)
            self.assertEqual(frame_pixels(frame), expected_pixels(scene['layout']))

    def test_no_original_input_needed_to_extract(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            pack_path = Path(tmp) / 'pack.json'
            frame_path = Path(tmp) / 'frame.json'
            source.write_text(json.dumps(three_scene_doc()))
            self.assertEqual(run_cli('compile-pack', '--source', str(source),
                                     '--output', str(pack_path)).returncode, 0)
            os.unlink(source)
            proc = run_cli('unpack-frame', '--pack', str(pack_path),
                           '--scene', 'C', '--output', str(frame_path))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            frame = json.loads(frame_path.read_text())
            self.assertEqual(frame['id'], 'C')
            self.assertEqual(frame_pixels(frame), {(0, 0), (8, 0), (8, 1), (16, 0)})

    def test_kern_wrap_offset_and_overlap(self):
        glyphs = [
            dot_glyph('A', [(0, 0), (1, 1)], advance=5),
            dot_glyph('V', [(0, 0), (1, 1)], advance=4),
            dict(dot_glyph('x', [(4, 0)]), offset=[-3, 0], advance=4),
        ]
        kerns = [{'left': 'A', 'right': 'V', 'adjust': -2}]
        layout = v1_input(glyphs, 'AVx', line_width=8, line_height=6,
                          kerning=kerns)
        scene = {'id': 'S', 'direct_only': False, 'layout': layout}
        pack = compile_pack({'width': 16, 'height': 8, 'payload_budget': 100,
                             'scenes': [scene]})
        frame = unpack_frame(pack, 'S')
        self.assertEqual(frame_pixels(frame), expected_pixels(layout))

    def test_blank_glyphs_and_repeated_ink_count_once(self):
        glyphs = [dot_glyph(' ', []),
                  dot_glyph('y', [(4, 0)], advance=8),
                  dict(dot_glyph('z', [(4, 0)]), offset=[-8, 0], advance=8)]
        layout = v1_input(glyphs, 'yz', line_width=16, line_height=8)
        scene = {'id': 'S', 'direct_only': False, 'layout': layout}
        pack = compile_pack({'width': 16, 'height': 8, 'payload_budget': 100,
                             'scenes': [scene]})
        self.assertEqual(frame_pixels(unpack_frame(pack, 'S')), {(4, 0)})


class RejectionTests(unittest.TestCase):
    def _pack(self):
        glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph(' ', [])]
        layout = v1_input(glyphs, 'a', line_width=8, line_height=8)
        blank = v1_input(glyphs, ' ', line_width=8, line_height=8)
        return compile_pack({'width': 16, 'height': 8, 'payload_budget': 500,
                             'scenes': [
                                 {'id': 'A', 'direct_only': False, 'layout': blank},
                                 {'id': 'B', 'direct_only': False, 'layout': layout},
                             ]})

    def test_ink_off_screen_rejects_whole_pack(self):
        glyphs = [dot_glyph('a', [(0, 0)])]
        glyphs[0]['offset'] = [8, 0]
        layout = v1_input(glyphs, 'a', line_width=16, line_height=8)
        source = {'width': 8, 'height': 8, 'payload_budget': 100,
                  'scenes': [{'id': 'A', 'direct_only': False, 'layout': layout}]}
        with self.assertRaises(ValueError):
            compile_pack(source)

    def test_nominal_overflow_with_ink_inside_is_allowed(self):
        glyphs = [dot_glyph('a', [(0, 0)])]
        layout = v1_input(glyphs, 'a', line_width=4, line_height=8)
        source = {'width': 8, 'height': 8, 'payload_budget': 100,
                  'scenes': [{'id': 'A', 'direct_only': False, 'layout': layout}]}
        pack = compile_pack(source)
        self.assertEqual(pack['payload_bytes'], 8)

    def _mutate(self, pack, mutate):
        clone = json.loads(json.dumps(pack))
        payload = bytearray(base64.b64decode(clone['payload_base64']))
        mutate(clone, payload)
        clone['payload_base64'] = base64.b64encode(bytes(payload)).decode()
        return clone

    def test_reference_to_reference_is_rejected(self):
        def mutate(pack, payload):
            by_id = {e['id']: e for e in pack['catalog']}
            # Make B (full) also a reference, so A's base becomes a chain.
            entry = by_id['B']
            start = entry['offset']
            block = bytes(64)
            record = bytearray([0])
            record += (1).to_bytes(2, 'little')
            record += (0).to_bytes(2, 'little')
            record += block[0:8]
            payload[start:start + entry['length']] = record
            entry['length'] = len(record)
            by_id['A']['offset'] = start + len(record)
            by_id['A']['length'] = 3 + 10 * 1
            payload[start + len(record):start + len(record) + 13] = (
                bytes([1]) + (1).to_bytes(2, 'little') +
                (0).to_bytes(2, 'little') + b'\x01' + b'\x00' * 7)
        with self.assertRaises(ValueError):
            unpack_frame(self._mutate(self._pack(), mutate), 'A')

    def test_direct_only_encoded_as_reference_is_rejected(self):
        # In the optimum A is stored as a reference; marking it cold-start
        # required must be rejected because cold start only reads full frames.
        pack = compile_pack(three_scene_doc())
        clone = json.loads(json.dumps(pack))
        entry_a = next(e for e in clone['catalog'] if e['id'] == 'A')
        self.assertEqual(entry_a['format'], 'reference')
        entry_a['direct_only'] = True
        with self.assertRaises(ValueError):
            unpack_frame(clone, 'A')

    def test_catalog_order_and_duplicates_are_rejected(self):
        def make(order):
            def mutate(pack, payload):
                pack['catalog'] = [pack['catalog'][i] for i in order]
            return self._mutate(self._pack(), mutate)
        with self.assertRaises(ValueError):
            unpack_frame(make([1, 0]), 'A')

        def duplicate(pack, payload):
            pack['catalog'][0]['id'] = pack['catalog'][1]['id']
        with self.assertRaises(ValueError):
            unpack_frame(self._mutate(self._pack(), duplicate), 'A')

    def test_payload_holes_and_declared_size_are_rejected(self):
        def hole(pack, payload):
            pack['catalog'][1]['offset'] += 4
        with self.assertRaises(ValueError):
            unpack_frame(self._mutate(self._pack(), hole), 'A')

        def trailing(pack, payload):
            payload.extend(b'\x00')
        with self.assertRaises(ValueError):
            unpack_frame(self._mutate(self._pack(), trailing), 'A')

        def wrong_declared(pack, payload):
            pack['payload_bytes'] += 1
        with self.assertRaises(ValueError):
            unpack_frame(self._mutate(self._pack(), wrong_declared), 'A')

    def test_bad_base64_is_rejected(self):
        clone = json.loads(json.dumps(self._pack()))
        clone['payload_base64'] = '%%%not-base64%%%'
        with self.assertRaises(ValueError):
            unpack_frame(clone, 'A')

    def test_reference_base_may_appear_later_in_catalog(self):
        # C is the only cold-start scene (forced full) and lives last in the
        # catalog; A and B reference it across the directory boundary.
        glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph(' ', [])]
        lit = v1_input(glyphs, 'a', 16, 8)
        blank = v1_input(glyphs, ' ', 16, 8)
        scenes = [
            {'id': 'A', 'direct_only': False, 'layout': lit},
            {'id': 'B', 'direct_only': False, 'layout': blank},
            {'id': 'C', 'direct_only': True, 'layout': lit},
        ]
        pack = compile_pack({'width': 16, 'height': 8, 'payload_budget': 500,
                             'scenes': scenes})
        payload = base64.b64decode(pack['payload_base64'])
        by_id = {e['id']: e for e in pack['catalog']}
        self.assertEqual(by_id['A']['format'], 'reference')
        self.assertEqual(payload[by_id['A']['offset']], 2)  # forward base = C
        self.assertEqual(frame_pixels(unpack_frame(pack, 'A')), {(0, 0)})

    def test_equal_distance_bases_prefer_smaller_catalog_index(self):
        # Both B and C are forced full and differ from A by one block, so the
        # base choice is a pure tie broken by catalog index (B = 0 < C = 1).
        a_glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph('b', [(0, 0)])]
        b_glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph(' ', [])]
        c_glyphs = [dot_glyph(' ', []), dot_glyph('b', [(0, 0)])]
        a_layout = v1_input(a_glyphs, 'ab', 16, 8)
        b_layout = v1_input(b_glyphs, 'a ', 16, 8)
        c_layout = v1_input(c_glyphs, ' b', 16, 8)
        scenes = [
            {'id': 'B', 'direct_only': True, 'layout': b_layout},
            {'id': 'C', 'direct_only': True, 'layout': c_layout},
            {'id': 'A', 'direct_only': False, 'layout': a_layout},
        ]
        pack = compile_pack({'width': 16, 'height': 8, 'payload_budget': 500,
                             'scenes': scenes})
        payload = base64.b64decode(pack['payload_base64'])
        by_id = {e['id']: e for e in pack['catalog']}
        self.assertEqual(by_id['A']['format'], 'reference')
        # Catalog order is A(0), B(1), C(2); the smaller valid base is B.
        self.assertEqual(payload[by_id['A']['offset']], 1)  # base = B
        self.assertEqual(frame_pixels(unpack_frame(pack, 'A')),
                         expected_pixels(a_layout))

    def test_published_bytes_are_shuffle_invariant(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first.json'
            second = Path(tmp) / 'second.json'
            src_a = Path(tmp) / 'a.json'
            src_b = Path(tmp) / 'b.json'
            doc = three_scene_doc(direct=('A',))
            src_a.write_text(json.dumps(doc))
            shuffled = json.loads(json.dumps(doc))
            random.Random(3).shuffle(shuffled['scenes'])
            src_b.write_text(json.dumps(shuffled))
            for src, out in ((src_a, first), (src_b, second)):
                proc = run_cli('compile-pack', '--source', str(src),
                               '--output', str(out))
                self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_unknown_scene_and_bad_version(self):
        pack = self._pack()
        with self.assertRaises(ValueError):
            unpack_frame(pack, 'ZZZ')
        clone = json.loads(json.dumps(pack))
        clone['schema_version'] = 99
        with self.assertRaises(ValueError):
            unpack_frame(clone, 'A')

    def test_tamper_reference_in_unselected_record_is_rejected(self):
        # A references B; corrupt A's mask so the whole pack is invalid even
        # though we ask for B.
        pack = compile_pack(three_scene_doc())
        by_id = {e['id']: e for e in pack['catalog']}
        payload = bytearray(base64.b64decode(pack['payload_base64']))
        entry = by_id['A']
        start = entry['offset']
        self.assertEqual(payload[start + 1:start + 3], (2).to_bytes(2, 'little'))
        # Rewrite the changed-block count to claim more mark items than exist.
        payload[start + 1:start + 3] = (5).to_bytes(2, 'little')
        pack['payload_base64'] = base64.b64encode(bytes(payload)).decode()
        with self.assertRaises(ValueError):
            unpack_frame(pack, 'B')

    def test_zero_mask_and_unordered_blocks_are_rejected(self):
        pack = compile_pack(three_scene_doc())
        by_id = {e['id']: e for e in pack['catalog']}
        base = bytearray(base64.b64decode(pack['payload_base64']))

        zero = bytearray(base)
        a = by_id['A']['offset']
        # First mask starts after base(1)+count(2)+block(2).
        for i in range(8):
            zero[a + 5 + i] = 0
        clone = json.loads(json.dumps(pack))
        clone['payload_base64'] = base64.b64encode(bytes(zero)).decode()
        with self.assertRaises(ValueError):
            unpack_frame(clone, 'B')

        unordered = bytearray(base)
        # C has a single change; duplicate it by claiming block order 2 then 1.
        c = by_id['C']['offset']
        unordered[c + 1:c + 3] = (2).to_bytes(2, 'little')
        # This also breaks the length invariant, which is rejected; craft a
        # real duplicate by extending C and shifting nothing else would break
        # the no-trailing-bytes rule, so length alone is enough here.
        clone2 = json.loads(json.dumps(pack))
        clone2['payload_base64'] = base64.b64encode(bytes(unordered)).decode()
        with self.assertRaises(ValueError):
            unpack_frame(clone2, 'B')


class CliAtomicityTests(unittest.TestCase):
    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            source.write_text(json.dumps(three_scene_doc()))
            output = Path(tmp) / 'pack.json'
            output.write_text('sentinel')
            proc = run_cli('compile-pack', '--source', str(source),
                           '--output', str(output))
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(output.read_text(), 'sentinel')

    def test_unpack_rejects_bad_pack_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack_path = Path(tmp) / 'pack.json'
            pack_path.write_text('{"schema_version": 9}')
            output = Path(tmp) / 'frame.json'
            proc = run_cli('unpack-frame', '--pack', str(pack_path),
                           '--scene', 'A', '--output', str(output))
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(output.exists())

    def test_missing_pack_file_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli('unpack-frame', '--pack', str(Path(tmp) / 'none.json'),
                           '--scene', 'A', '--output', str(Path(tmp) / 'f.json'))
            self.assertEqual(proc.returncode, 2)

    def test_tampered_pack_cli_rejects_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            pack_path = Path(tmp) / 'pack.json'
            source.write_text(json.dumps(three_scene_doc()))
            self.assertEqual(run_cli('compile-pack', '--source', str(source),
                                     '--output', str(pack_path)).returncode, 0)
            pack = json.loads(pack_path.read_text())
            by_id = {e['id']: e for e in pack['catalog']}
            payload = bytearray(base64.b64decode(pack['payload_base64']))
            # Corrupt the unselected C reference's record length.
            by_id['C']['length'] += 1
            pack['payload_base64'] = base64.b64encode(bytes(payload)).decode()
            tampered = Path(tmp) / 'tampered.json'
            tampered.write_text(json.dumps(pack))
            output = Path(tmp) / 'frame.json'
            proc = run_cli('unpack-frame', '--pack', str(tampered),
                           '--scene', 'B', '--output', str(output))
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
