"""离线字形装配的数据契约与三态合成回归测试。"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from pixel_type.assembly import (AssemblyError, assemble, assemble_file,
                                 assemble_stream, cell_source, iter_cells,
                                 load_manifest, normalize_stream)
from pixel_type.json_stream import (ArrayCursor, ObjectCursor, StreamingReader,
                                    JsonSyntaxError, _END)
from pixel_type.service import process
from pixel_type.store import canonical_bytes


def fragment(node_id, supplier, rows, width=None, height=None):
    return {'id': node_id, 'kind': 'fragment', 'supplier': supplier,
            'width': width if width is not None else len(rows[0]),
            'height': height if height is not None else len(rows),
            'rows': list(rows)}


def assembly(node_id, layers, width=2, height=1):
    return {'id': node_id, 'kind': 'assembly', 'width': width,
            'height': height,
            'layers': [{'ref': ref, 'src': list(src), 'dst': list(dst)}
                       for ref, src, dst in layers]}


def binding(char, node, advance=4, offset=(0, 0)):
    return {'char': char, 'node': node, 'advance': advance,
            'offset': list(offset)}


def manifest(nodes, text='A', approved=('S0',), bindings=None, kerning=None,
             line_width=80, line_height=8):
    if bindings is None:
        bindings = [binding('A', nodes and _first_assembly(nodes))]
    return {'nodes': nodes, 'bindings': bindings,
            'approved_suppliers': list(approved), 'text': text,
            'kerning': list(kerning or []), 'line_width': line_width,
            'line_height': line_height}


def _first_assembly(nodes):
    return next(n['id'] for n in nodes if n['kind'] == 'assembly')


class AcceptanceScenarioTests(unittest.TestCase):
    """离线验收主线：底版 + 中间件覆盖/清除/透明三种放行口径。"""

    def _build(self, clear_supplier, clear_rows):
        base = fragment('BASE', 'S0', ['11'])
        mid = assembly('MID', [
            ('BAD', (0, 0, 1, 1), (0, 0)),
            ('COVER', (0, 0, 1, 1), (0, 0)),
            ('CLEAR', (1, 0, 2, 1), (1, 0))])
        glyph = assembly('G', [
            ('BASE', (0, 0, 2, 1), (0, 0)),
            ('MID', (0, 0, 2, 1), (0, 0))])
        return manifest([
            base, glyph, mid,
            fragment('BAD', 'SX', ['1.']),
            fragment('COVER', 'S0', ['1.']),
            fragment('CLEAR', clear_supplier, clear_rows)])

    def test_approved_clear_yields_10_with_clear_provenance(self):
        delivery = assemble(self._build('S0', ['00']))
        glyph = delivery['layout_input']['glyphs'][0]
        self.assertEqual(glyph['rows'], ['10'])
        left = cell_source(delivery, 'A', 0, 0)
        right = cell_source(delivery, 'A', 1, 0)
        self.assertEqual(left['state'], '1')
        self.assertEqual(left['fragment'], 'COVER')
        # 右格虽为 0，也必须追到清除原料而非透明。
        self.assertEqual(right['state'], '0')
        self.assertEqual(right['supplier'], 'S0')
        self.assertEqual(right['fragment'], 'CLEAR')
        self.assertEqual((right['fragment_x'], right['fragment_y']), (1, 0))
        self.assertEqual(delivery['summary'], {
            **delivery['summary'], 'sourced_cells': 2,
            'lit_cells': 1, 'clear_cells': 1})

    def test_identical_pixel_unapproved_clear_must_fail(self):
        with self.assertRaises(AssemblyError) as caught:
            assemble(self._build('SX', ['00']))
        message = str(caught.exception)
        self.assertIn('SX', message)
        self.assertIn('CLEAR', message)
        self.assertIn("'A'", message)
        self.assertIn('(1,0)', message)

    def test_transparent_instead_of_clear_reveals_base_as_11(self):
        delivery = assemble(self._build('S0', ['..']))
        self.assertEqual(
            delivery['layout_input']['glyphs'][0]['rows'], ['11'])
        # 透明清除层不写入：右格来源回退为底版 BASE（而非透明无来源）。
        right = cell_source(delivery, 'A', 1, 0)
        self.assertEqual(right['fragment'], 'BASE')
        self.assertEqual(right['state'], '1')

    def test_covered_unapproved_supplier_does_not_block(self):
        # BAD 的未获批左格被 COVER 获批亮点盖住，交付放行。
        delivery = assemble(self._build('S0', ['00']))
        self.assertEqual(
            cell_source(delivery, 'A', 0, 0)['supplier'], 'S0')

    def test_unapproved_library_fragment_not_used_is_ignored(self):
        data = self._build('S0', ['00'])
        data['nodes'].append(fragment('UNUSED_BAD', 'SX', ['1']))
        # 未被任何绑定到达：不影响放行，结构仍需校验通过。
        delivery = assemble(data)
        self.assertEqual(
            delivery['layout_input']['glyphs'][0]['rows'], ['10'])


class ThreeStateAndGeometryTests(unittest.TestCase):
    def test_transparent_never_overwrites_lower_layer(self):
        lower = fragment('LO', 'S0', ['11'])
        upper = fragment('UP', 'S0', ['.0'])
        glyph = assembly('G', [
            ('LO', (0, 0, 2, 1), (0, 0)),
            ('UP', (0, 0, 2, 1), (0, 0))])
        delivery = assemble(manifest([lower, upper, glyph]))
        self.assertEqual(
            delivery['layout_input']['glyphs'][0]['rows'], ['10'])
        self.assertEqual(
            cell_source(delivery, 'A', 0, 0)['fragment'], 'LO')

    def test_off_canvas_cells_are_dropped_including_bad_supplier(self):
        # 位移把整窗推到画布外：未获批格被裁掉，不构成违规。
        bad = fragment('BAD', 'SX', ['1'])
        glyph = assembly('G', [('BAD', (0, 0, 1, 1), (5, 0))], width=2)
        delivery = assemble(manifest([bad, glyph]))
        self.assertEqual(delivery['layout_input']['glyphs'][0]['rows'], ['00'])
        self.assertEqual(delivery['summary']['sourced_cells'], 0)

    def test_negative_dst_partial_clipping(self):
        src = fragment('SRC', 'S0', ['11', '11'])
        glyph = assembly('G', [('SRC', (0, 0, 2, 2), (-1, 0))], width=2,
                         height=2)
        delivery = assemble(manifest([src, glyph]))
        self.assertEqual(delivery['layout_input']['glyphs'][0]['rows'],
                         ['10', '10'])
        # 保留格保留原料原始坐标（不是装配坐标）。
        cell = cell_source(delivery, 'A', 0, 0)
        self.assertEqual((cell['fragment_x'], cell['fragment_y']), (1, 0))

    def test_reused_assembly_keeps_three_state_and_original_coords(self):
        f0 = fragment('F0', 'S0', ['1.', '0.'])
        shared = assembly('H', [('F0', (0, 0, 2, 2), (0, 0))], width=2,
                          height=2)
        ga = assembly('GA', [('H', (0, 0, 2, 2), (0, 0))], width=2, height=2)
        gb = assembly('GB', [('H', (0, 0, 1, 1), (1, 1))], width=2, height=2)
        data = manifest(
            [f0, shared, ga, gb], text='AB',
            bindings=[binding('A', 'GA'), binding('B', 'GB')])
        delivery = assemble(data)
        # 追溯必须落到原料 F0 与原始坐标，而非中间节点 H。
        ga_cell = cell_source(delivery, 'A', 0, 1)
        gb_cell = cell_source(delivery, 'B', 1, 1)
        self.assertEqual(ga_cell['fragment'], 'F0')
        self.assertEqual(ga_cell['state'], '0')
        self.assertEqual((gb_cell['fragment_x'], gb_cell['fragment_y']),
                         (0, 0))
        self.assertEqual(iter_cells(delivery, 'B').__next__()['fragment'],
                         'F0')


class StructuralValidationTests(unittest.TestCase):
    def _base_nodes(self):
        return [fragment('F0', 'S0', ['1']),
                assembly('G', [('F0', (0, 0, 1, 1), (0, 0))], width=1,
                         height=1)]

    def test_missing_reference_rejected_even_when_unused(self):
        data = manifest(self._base_nodes())
        data['nodes'].append(
            assembly('ORPHAN', [('GHOST', (0, 0, 1, 1), (0, 0))], width=1,
                     height=1))
        with self.assertRaises(AssemblyError):
            assemble(data)

    def test_cycle_rejected(self):
        a = assembly('A', [('B', (0, 0, 1, 1), (0, 0))], width=1, height=1)
        b = assembly('B', [('A', (0, 0, 1, 1), (0, 0))], width=1, height=1)
        with self.assertRaises(AssemblyError):
            assemble(manifest([a, b]))

    def test_path_depth_limit_enforced(self):
        nodes = [fragment('F0', 'S0', ['1'])]
        previous = 'F0'
        for index in range(64):       # 64 个装配 + 1 原料 = 65 节点路径
            node_id = f'A{index}'
            nodes.append(assembly(
                node_id, [(previous, (0, 0, 1, 1), (0, 0))], width=1,
                height=1))
            previous = node_id
        data = manifest(nodes, bindings=[binding('A', previous)])
        with self.assertRaises(AssemblyError):
            assemble(data)

    def test_depth_64_is_accepted(self):
        nodes = [fragment('F0', 'S0', ['1'])]
        previous = 'F0'
        for index in range(63):       # 63 装配 + 1 原料 = 64 节点，恰好上限
            node_id = f'A{index}'
            nodes.append(assembly(
                node_id, [(previous, (0, 0, 1, 1), (0, 0))], width=1,
                height=1))
            previous = node_id
        delivery = assemble(
            manifest(nodes, bindings=[binding('A', previous)]))
        self.assertEqual(
            delivery['layout_input']['glyphs'][0]['rows'], ['1'])

    def test_window_out_of_reference_bounds_rejected(self):
        nodes = [fragment('F0', 'S0', ['1']),
                 assembly('G', [('F0', (0, 0, 2, 1), (0, 0))], width=2)]
        with self.assertRaises(AssemblyError):
            assemble(manifest(nodes))

    def test_binding_must_match_text_exactly(self):
        nodes = self._base_nodes()
        with self.assertRaises(AssemblyError):
            assemble(manifest(nodes, text='AB'))      # 缺绑 B
        with self.assertRaises(AssemblyError):
            assemble(manifest(
                nodes, text='A',
                bindings=[binding('A', 'G'), binding('B', 'G')]))

    def test_ragged_fragment_rows_rejected(self):
        bad = fragment('F0', 'S0', ['11', '1'])
        glyph = assembly('G', [('F0', (0, 0, 2, 1), (0, 0))], width=2)
        with self.assertRaises(AssemblyError):
            assemble(manifest([bad, glyph]))

    def test_forward_reference_accepted(self):
        glyph = assembly('G', [('F0', (0, 0, 1, 1), (0, 0))], width=1,
                         height=1)
        data = manifest([glyph, fragment('F0', 'S0', ['1'])])
        delivery = assemble(data)
        self.assertEqual(
            delivery['layout_input']['glyphs'][0]['rows'], ['1'])


class V1IntegrationTests(unittest.TestCase):
    def test_layout_input_renders_through_v1_with_kerning_and_offset(self):
        f_a = fragment('FA', 'S0', ['11', '11'])
        f_b = fragment('FB', 'S0', ['10', '00'])
        ga = assembly('GA', [('FA', (0, 0, 2, 2), (0, 0))], width=2,
                      height=2)
        gb = assembly('GB', [('FB', (0, 0, 2, 2), (0, 0))], width=2,
                      height=2)
        data = manifest(
            [f_a, f_b, ga, gb], text='AB',
            bindings=[binding('A', 'GA', advance=3, offset=(1, 0)),
                      binding('B', 'GB', advance=3, offset=(0, 1))],
            kerning=[{'left': 'A', 'right': 'B', 'adjust': -1}],
            line_width=40, line_height=8)
        delivery = assemble(data)
        report = process(delivery['layout_input'])
        self.assertEqual(report['summary']['placed_glyphs'], 2)
        placements = report['placements']
        self.assertEqual(placements[0]['applied_kerning'], 0)
        self.assertEqual(placements[1]['applied_kerning'], -1)
        self.assertEqual(placements[1]['pen_x'], 3 - 1)
        self.assertEqual(placements[0]['ink_origin'], [1, 0])

    def test_render_pixel_budget_violation_rejects_delivery(self):
        # 32x32 全亮字形重复 200 次 → 204800 墨点，超过 200000 预算。
        rows = ['1' * 32 for _ in range(32)]
        nodes = [fragment('F0', 'S0', rows),
                 assembly('G', [('F0', (0, 0, 32, 32), (0, 0))], width=32,
                          height=32)]
        data = manifest(nodes, text='A' * 200,
                        bindings=[binding('A', 'G')], line_width=10000,
                        line_height=1000)
        with self.assertRaises(ValueError):
            assemble(data)


class DeterminismAndFileTests(unittest.TestCase):
    def _data(self):
        base = fragment('BASE', 'S0', ['11'])
        glyph = assembly('G', [
            ('BASE', (0, 0, 2, 1), (0, 0)),
            ('CLR', (1, 0, 2, 1), (1, 0))])
        return [base, glyph, fragment('CLR', 'S0', ['00'])]

    def test_record_reordering_does_not_change_bytes(self):
        nodes = self._data()
        first = canonical_bytes(assemble(manifest(nodes)))
        reordered = list(reversed(nodes))
        second = canonical_bytes(assemble(
            manifest(reordered, approved=['S0'])))
        self.assertEqual(first, second)

    def test_dict_and_streaming_paths_produce_identical_delivery(self):
        data = manifest(self._data(), approved=['S0', 'S1'])
        via_dict = canonical_bytes(assemble(data))
        via_stream = canonical_bytes(assemble_stream(
            io.BytesIO(canonical_bytes(data))))
        self.assertEqual(via_dict, via_stream)

    def test_failed_delivery_never_overwrites_existing_file(self):
        good = manifest(self._data())
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, 'delivery.json')
            assemble_file_write = assemble_file(
                _write(tmp, 'good.json', good), target)
            original_bytes = target.read_bytes()
            # 未获批清除 → 拒绝；同名目标必须保持原字节。
            bad = manifest(self._data(), approved=['S0'])
            bad['nodes'][-1] = fragment('CLR', 'SX', ['00'])
            source = _write(tmp, 'bad.json', bad)
            with self.assertRaises(AssemblyError):
                assemble_file(source, target)
            self.assertEqual(target.read_bytes(), original_bytes)

    def test_reject_when_target_is_symlink(self):
        good = manifest(self._data())
        with tempfile.TemporaryDirectory() as tmp:
            link_target = Path(tmp, 'existing.json')
            link_target.write_bytes(b'{}')
            out = Path(tmp, 'link.json')
            os.symlink(link_target, out)
            with self.assertRaises(OSError):
                assemble_file(_write(tmp, 'good.json', good), out)
            self.assertEqual(link_target.read_bytes(), b'{}')

    def test_duplicate_json_key_rejected(self):
        raw = b'{"nodes": [],"nodes": [],"bindings": [],"approved_suppliers": [],' \
              b'"text": "A","kerning": [],"line_width": 1,"line_height": 1}'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'dup.json')
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                load_manifest(str(path))


class StreamingParserTests(unittest.TestCase):
    def test_roundtrip_against_standard_library(self):
        samples = [
            {'a': [1, 2.5, -3, True, False, None, 'x'], 'b': {'c': 'd'}},
            {'unicode': 'héllo\U0001F600', 'esc': 'a"b\\c\n\t'},
            {'nested': [[1, [2, [3, [4]]]]], 'empty': [], 'obj': {}},
        ]
        for sample in samples:
            raw = json.dumps(sample, ensure_ascii=False).encode('utf-8')
            parsed = _materialize(
                StreamingReader(io.BytesIO(raw)).consume())
            self.assertEqual(parsed, sample)

    def test_syntax_errors(self):
        for raw in (b'{', b'[1,]', b'{"a"}', b'{"a":1 "b":2}', b'"unterminated',
                    b'{"a": tru}', b'[1 2]'):
            with self.assertRaises(JsonSyntaxError):
                _materialize(
                    StreamingReader(io.BytesIO(raw)).consume())

    def test_streaming_normalize_rejects_bad_manifest(self):
        with self.assertRaises(AssemblyError):
            normalize_stream(io.BytesIO(b'{"nodes":[]}'))

    def test_parser_correct_under_forced_buffer_compaction(self):
        import pixel_type.json_stream as stream_mod

        def tiny_fill(self, need=1):
            if self._pos > 64:
                self._buf = self._buf[self._pos:]
                self._pos = 0
            while not self._eof and len(self._buf) - self._pos < need:
                chunk = self._stream.read(64)
                if not chunk:
                    self._eof = True
                    break
                self._buf += chunk

        original = stream_mod.StreamingReader._fill
        stream_mod.StreamingReader._fill = tiny_fill
        try:
            nodes = [
                fragment(f'F{i}', f'S{i % 4}',
                         [''.join('01.'[(x + y + i) % 3] for x in range(32))
                          for y in range(32)])
                for i in range(300)]
            glyph = assembly('G', [
                (f'F{i}', (0, 0, 32, 32), (0, 0)) for i in range(4)],
                width=32, height=32)
            nodes.append(glyph)
            data = manifest(nodes, approved=[f'S{i}' for i in range(4)])
            via_dict = canonical_bytes(assemble(data))
            via_stream = canonical_bytes(assemble_stream(
                io.BytesIO(canonical_bytes(data))))
            self.assertEqual(via_dict, via_stream)
        finally:
            stream_mod.StreamingReader._fill = original


def _write(tmp, name, data):
    path = Path(tmp, name)
    path.write_bytes(canonical_bytes(data))
    return str(path)


def _materialize(cursor):
    if isinstance(cursor, ObjectCursor):
        result = {}
        while True:
            key = cursor.next_key()
            if key is None:
                return result
            result[key] = _materialize(cursor.value())
    if isinstance(cursor, ArrayCursor):
        result = []
        while True:
            value = cursor.next_value()
            if value is _END:
                return result
            result.append(_materialize(value))
    return cursor


if __name__ == '__main__':
    unittest.main()
