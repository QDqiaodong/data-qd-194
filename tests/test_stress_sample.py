"""压力样例的数据契约断言（不做 RSS 测量；测量走 bench/stress_bench.py）。

RSS 预算需要在独立进程用 resource.getrusage 测，放在 bench 驱动里；
这里固定样例本身的结构性与可装配性，保证它确实是“40000 个可到达节点”的
极限样例，而不是用无引用大清单占数。
"""

import io
import unittest

from pixel_type.assembly import (assemble_stream, cell_source, iter_cells,
                                 normalize_stream)
from pixel_type.service import process
from pixel_type.stress import (FRAGMENT_TOTAL, NODE_TOTAL, ROOT_COUNT,
                               build_stress_manifest)
from pixel_type.store import canonical_bytes


class StressSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = build_stress_manifest()
        cls.raw = canonical_bytes(cls.manifest)
        cls.normalized = normalize_stream(io.BytesIO(cls.raw))
        cls.delivery = assemble_stream(io.BytesIO(cls.raw))

    def test_node_and_fragment_counters(self):
        records = self.normalized[0]
        self.assertEqual(len(records[0]), NODE_TOTAL)
        self.assertEqual(sum(1 for kind in records[0] if kind == 1),
                         FRAGMENT_TOTAL)
        self.assertEqual(len(self.normalized[5]), ROOT_COUNT)

    def test_manifest_within_32_mib(self):
        self.assertLessEqual(len(self.raw), 32 * 1024 * 1024)

    def test_every_node_reachable_from_bindings(self):
        kind, _suppliers, _rows, layers = self.normalized[0]
        roots = {b['node_index'] for b in self.normalized[5]}
        seen = set()
        stack = list(roots)
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            if kind[node] == 2:
                stack.extend(layer[0] for layer in layers[node])
        self.assertEqual(seen, set(range(NODE_TOTAL)))

    def test_longest_path_is_exactly_64_nodes(self):
        kind, _suppliers, _rows, layers = self.normalized[0]
        depth = {}

        def measure(node):
            if node in depth:
                return depth[node]
            if kind[node] == 1:
                depth[node] = 1
                return 1
            depth[node] = 1 + max(measure(layer[0])
                                  for layer in layers[node])
            return depth[node]

        for root in {b['node_index'] for b in self.normalized[5]}:
            measure(root)
        self.assertEqual(max(depth.values()), 64)

    def test_delivery_has_every_binding_and_only_approved_sources(self):
        layout = self.delivery['layout_input']
        self.assertEqual(len(layout['glyphs']), ROOT_COUNT)
        approved = set(self.normalized[10])
        for glyph in layout['glyphs']:
            for cell in iter_cells(self.delivery, glyph['char']):
                self.assertIn(cell['supplier'], approved)
                self.assertTrue(0 <= cell['fragment_x'] <= 31)
                self.assertTrue(0 <= cell['fragment_y'] <= 31)

    def test_shared_part_preserves_original_fragment_coordinates(self):
        index_to_id = {index: node_id
                       for node_id, index in self.normalized[2].items()}
        char_of_node = {index_to_id[b['node_index']]: b['char']
                        for b in self.normalized[5]}
        char_ga, char_gb = char_of_node['GA'], char_of_node['GB']
        ga_cell = cell_source(self.delivery, char_ga, 1, 2)
        gb_cell = cell_source(self.delivery, char_gb, 5, 6)
        self.assertEqual(ga_cell['fragment'], gb_cell['fragment'])
        self.assertEqual((ga_cell['fragment_x'], ga_cell['fragment_y']),
                         (1, 2))
        self.assertEqual((gb_cell['fragment_x'], gb_cell['fragment_y']),
                         (9, 10))

    def test_v1_layout_renders_with_kerning_offsets_and_ink(self):
        report = process(self.delivery['layout_input'])
        self.assertEqual(report['summary']['placed_glyphs'],
                         len(self.manifest['text']))
        self.assertTrue(any(p['applied_kerning'] != 0
                            for p in report['placements']))
        self.assertTrue(any(g['offset'] != [0, 0]
                            for g in self.delivery['layout_input']['glyphs']))
        self.assertGreater(report['summary']['unique_ink_pixels'], 0)

    def test_deterministic_bytes(self):
        again = canonical_bytes(assemble_stream(io.BytesIO(self.raw)))
        self.assertEqual(again, canonical_bytes(self.delivery))


if __name__ == '__main__':
    unittest.main()
