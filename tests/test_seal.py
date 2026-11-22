import json
import unittest

from helpers import (dot_glyph, dump_source, readback, screen_tiles, source,
                     v1_input, window)
from pixel_type.bitmap import tile_of
from pixel_type.planner import build_plan, render_pixels
from pixel_type.store import Store, StoreError

PANEL_W = 32
PANEL_H = 8


def four_tile_doc(limit, windows, old_text='aaaa', new_text='    '):
    """One lit pixel per tile T0..T3 in old state; blank spaces in new state."""
    glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph(' ', [])]
    old = v1_input(glyphs, old_text, line_width=32, line_height=8)
    new = v1_input(glyphs, new_text, line_width=32, line_height=8)
    return source('panel-1', PANEL_W, PANEL_H, limit, old, new, windows)


JIA = window('win-jia', 0, 0, 9, 8)    # flip pixels of T0 and T1
YI = window('win-yi', 8, 0, 17, 8)     # flip pixels of T1 and T2


class PlanningTests(unittest.TestCase):
    def test_window_chain_builds_minimal_groups(self):
        doc = four_tile_doc(3, [JIA, YI])
        plan = build_plan(doc, 'release-1')
        groups = [(g['group_id'], [(t['tx'], t['ty']) for t in g['tiles']],
                   g['window_ids']) for g in plan['groups']]
        self.assertEqual(groups, [
            ('G0001', [(0, 0), (1, 0), (2, 0)], ['win-jia', 'win-yi']),
            ('G0002', [(3, 0)], []),
        ])
        # Every changed tile appears exactly once; nothing unchanged is written.
        self.assertEqual(plan['changed_tiles'],
                         [{'tx': tx, 'ty': 0} for tx in range(4)])

    def test_oversized_group_rejects_the_whole_revision(self):
        import tempfile
        from pathlib import Path
        doc = four_tile_doc(2, [JIA, YI])
        with self.assertRaises(ValueError) as caught:
            build_plan(doc, 'release-1')
        message = str(caught.exception)
        self.assertIn('max_tiles_per_write=2', message)
        # The diagnostic names the offending tile coordinates and both windows.
        for token in ('(0, 0)', '(1, 0)', '(2, 0)', 'win-jia', 'win-yi'):
            self.assertIn(token, message)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'store'))
            import hashlib, json
            raw = json.dumps(doc, sort_keys=True).encode()
            with self.assertRaises(ValueError):
                store.seal('release-1', hashlib.sha256(raw).hexdigest(), doc)
            self.assertEqual(store._read_index()['releases'], {})

    def test_removing_one_window_splits_the_group(self):
        plan = build_plan(four_tile_doc(3, [JIA]), 'release-2')
        by_tiles = {tuple(sorted((t['tx'], t['ty']) for t in g['tiles'])): g
                    for g in plan['groups']}
        self.assertEqual(sorted(by_tiles),
                         [((0, 0), (1, 0)), ((2, 0),), ((3, 0),)])
        joined = by_tiles[((0, 0), (1, 0),)]
        self.assertEqual(joined['window_ids'], ['win-jia'])

    def test_intersection_without_flip_inside_does_not_join(self):
        # The window intersects T3 but excludes its flipped pixel at (24, 0).
        only_intersects = window('win-touch', 25, 1, 32, 8)
        plan = build_plan(four_tile_doc(4, [only_intersects]), 'release-3')
        self.assertEqual([len(g['tiles']) for g in plan['groups']], [1, 1, 1, 1])
        self.assertEqual(plan['protected_windows'][0]['tiles'], [])
        t3 = next(g for g in plan['groups'] if g['tiles'][0]['tx'] == 3)
        self.assertEqual(t3['window_ids'], [])

    def test_ink_lit_in_both_states_is_not_a_flip(self):
        glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph('b', [(0, 0), (1, 0)])]
        old = v1_input(glyphs, 'a', line_width=8, line_height=8)
        new = v1_input(glyphs, 'b', line_width=8, line_height=8)
        win = window('win-1', 0, 0, 8, 8)
        plan = build_plan(source('p', 8, 8, 1, old, new, [win]), 'r')
        # Only (1,0) flips; (0,0) stays lit and must not drive the decision.
        self.assertEqual(len(plan['changed_tiles']), 1)
        tile = plan['groups'][0]['tiles'][0]
        self.assertEqual(tile['old_rows'][0], '10000000')
        self.assertEqual(tile['new_rows'][0], '11000000')

    def test_repeated_drawing_of_one_point_lights_one_lamp(self):
        # 'b' is pulled back by its offset so both glyphs ink the same physical
        # point (1,0); the ink union collapses the double draw into one lamp.
        glyphs = [dot_glyph('a', [(1, 0)]),
                  dict(dot_glyph('b', [(0, 0)]), offset=[-7, 0])]
        doc = v1_input(glyphs, 'ab', line_width=16, line_height=8)
        self.assertEqual(render_pixels(doc), {(1, 0)})
        # A kern pair genuinely moves the second pen position, which still
        # feeds the same ink pipeline.
        kerns = [{'left': 'a', 'right': 'b', 'adjust': -2}]
        simple = [dot_glyph('a', [(0, 0)]), dot_glyph('b', [(0, 0)])]
        kerned = v1_input(simple, 'ab', line_width=16, line_height=8,
                          kerning=kerns)
        self.assertEqual(render_pixels(kerned), {(0, 0), (6, 0)})

    def test_ink_outside_physical_screen_rejects_without_cropping(self):
        glyphs = [dot_glyph('a', [(0, 0)])]
        old = v1_input(glyphs, 'a', line_width=8, line_height=8)
        shifted = [dot_glyph('a', [(0, 0)])]
        shifted[0]['offset'] = [8, 0]
        new = v1_input(shifted, 'a', line_width=16, line_height=8)
        doc = source('p', 8, 8, 1, old, new, [])
        with self.assertRaises(ValueError):
            build_plan(doc, 'r')

    def test_nominal_line_overflow_with_ink_inside_screen_is_allowed(self):
        glyphs = [dot_glyph('a', [(0, 0)])]
        old = v1_input(glyphs, 'a', line_width=4, line_height=8)  # advance 8 overflows
        new = v1_input([dot_glyph(' ', [])], ' ', line_width=4, line_height=8)
        plan = build_plan(source('p', 8, 8, 1, old, new, []), 'r')
        self.assertEqual(len(plan['groups']), 1)

    def test_no_change_freezes_with_empty_groups(self):
        glyphs = [dot_glyph('a', [(0, 0)])]
        old = new = v1_input(glyphs, 'a', line_width=8, line_height=8)
        plan = build_plan(source('p', 8, 8, 1, old, new, []), 'r-empty')
        self.assertEqual(plan['groups'], [])
        self.assertEqual(plan['changed_tiles'], [])

    def test_blank_glyph_lights_nothing(self):
        glyphs = [dot_glyph(' ', [])]
        self.assertEqual(render_pixels(v1_input(glyphs, '   ', 8, 8)), set())

    def test_identifiers_and_bounds_are_validated(self):
        doc = four_tile_doc(1, [])
        for bad_id in ('', 'x' * 65, 'bad/id', '中文'):
            cloned = dict(doc, panel_id=bad_id)
            with self.assertRaises(ValueError):
                build_plan(cloned, 'r')
        with self.assertRaises(ValueError):
            build_plan(dict(doc, width=12), 'r')       # not a multiple of 8
        with self.assertRaises(ValueError):
            build_plan(dict(doc, height=136), 'r')     # over 128
        with self.assertRaises(ValueError):
            build_plan(dict(doc, max_tiles_per_write=99), 'r')
        bad_window = dict(doc, protected_windows=[window('w', 0, 0, 33, 8)])
        with self.assertRaises(ValueError):
            build_plan(bad_window, 'r')

    def test_tile_of_helper_matches_screen_origin(self):
        self.assertEqual(tile_of(0, 0), (0, 0))
        self.assertEqual(tile_of(7, 7), (0, 0))
        self.assertEqual(tile_of(8, 8), (1, 1))

    def test_plan_bytes_are_deterministic_across_builds(self):
        from pixel_type.store import canonical_bytes
        doc = four_tile_doc(3, [JIA, YI])
        first = canonical_bytes(build_plan(doc, 'release-1'))
        second = canonical_bytes(build_plan(json.loads(json.dumps(doc)),
                                            'release-1'))
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
