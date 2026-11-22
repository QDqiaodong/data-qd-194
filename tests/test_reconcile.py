import json
import tempfile
import unittest
from pathlib import Path

from helpers import (dot_glyph, readback, screen_tiles, source, v1_input,
                     window)
from pixel_type.reconcile import build_report, parse_readback
from pixel_type.store import Store

PANEL_W = 32
PANEL_H = 8
JIA = window('win-jia', 0, 0, 9, 8)
YI = window('win-yi', 8, 0, 17, 8)


def freeze(tmp, release_id, limit, windows, old_text='aaaa', new_text='    '):
    glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph(' ', [])]
    old = v1_input(glyphs, old_text, 32, 8)
    new = v1_input(glyphs, new_text, 32, 8)
    doc = source('panel-1', PANEL_W, PANEL_H, limit, old, new, windows)
    import hashlib
    raw = json.dumps(doc, sort_keys=True).encode()
    store = Store(str(Path(tmp) / 'store'))
    payload = store.seal(release_id, hashlib.sha256(raw).hexdigest(), doc)
    return store, json.loads(payload)


def state_tiles(plan, which, coords):
    rows = screen_tiles(plan, which)
    return [(tx, ty, rows[(tx, ty)]) for tx, ty in coords]


def mixed_readback(plan):
    """T0,T1,T3 read old; T2 read new (the scenario-1 sample)."""
    old_rows = screen_tiles(plan, 'old')
    new_rows = screen_tiles(plan, 'new')
    tiles = []
    for tx in range(4):
        rows = new_rows[(tx, 0)] if tx == 2 else old_rows[(tx, 0)]
        tiles.append({'tx': tx, 'ty': 0, 'rows': rows})
    return readback('panel-1', [(t['tx'], t['ty'], t['rows']) for t in tiles])


class ReconcileScenarioOneTests(unittest.TestCase):
    def test_chain_group_blocks_partial_state_t3_alone_is_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = freeze(tmp, 'r1', 3, [JIA, YI])
            report = build_report(plan, mixed_readback(plan))
            by_id = {g['group_id']: g for g in report['groups']}
            chain, solo = by_id['G0001'], by_id['G0002']
            # T0/T1 old and T2 new cannot share one interpretation.
            self.assertEqual(chain['status'], 'conflict')
            self.assertNotIn('next_write', chain)
            self.assertTrue(chain['excluding_evidence'])
            self.assertEqual(solo['status'], 'ready')
            self.assertEqual(solo['next_write']['tile_count'], 1)
            target = solo['next_write']['tiles'][0]
            self.assertEqual((target['tx'], target['ty']), (3, 0))
            # Executor-facing precondition is the frozen old state.
            self.assertEqual(target['precondition_rows'],
                             screen_tiles(plan, 'old')[(3, 0)])
            self.assertEqual(target['target_rows'],
                             screen_tiles(plan, 'new')[(3, 0)])

    def test_revision_without_yi_delivers_two_ready_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = freeze(tmp, 'r2', 3, [JIA])
            report = build_report(plan, mixed_readback(plan))
            statuses = {g['group_id']: g['status'] for g in report['groups']}
            # G0001 = T0/T1 still old and fully read -> ready (one atomic
            # write covering both tiles), G0002 = T2 already new -> applied,
            # G0003 = T3 still old -> ready.
            self.assertEqual(statuses,
                             {'G0001': 'ready', 'G0002': 'applied',
                              'G0003': 'ready'})
            delivered = sorted(t['tx']
                               for g in report['groups']
                               if g['status'] == 'ready'
                               for t in g['next_write']['tiles'])
            self.assertEqual(delivered, [0, 1, 3])
            joined = next(g for g in report['groups'] if g['group_id'] == 'G0001')
            self.assertEqual(joined['next_write']['tile_count'], 2)
            applied = next(g for g in report['groups'] if g['status'] == 'applied')
            self.assertNotIn('next_write', applied)

    def test_missing_reads_are_unconfirmed_not_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = freeze(tmp, 'r3', 3, [JIA, YI])
            # Only T0 and T1 are read, and they are still old.
            sample = readback('panel-1',
                              state_tiles(plan, 'old', [(0, 0), (1, 0)]))
            report = build_report(plan, sample)
            chain = next(g for g in report['groups'] if g['group_id'] == 'G0001')
            self.assertEqual(chain['status'], 'unconfirmed')
            missing = {(m['tx'], m['ty']) for m in chain['missing_reads']}
            self.assertIn((2, 0), missing)
            self.assertNotIn('next_write', chain)

    def test_conflict_wins_even_when_scope_has_missing_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = freeze(tmp, 'r3b', 3, [JIA, YI])
            # T0 old, T2 new already excludes both explanations; T1 unread.
            old_rows = screen_tiles(plan, 'old')
            new_rows = screen_tiles(plan, 'new')
            sample = readback('panel-1', [
                (0, 0, old_rows[(0, 0)]),
                (2, 0, new_rows[(2, 0)]),
                (3, 0, old_rows[(3, 0)]),
            ])
            report = build_report(plan, sample)
            chain = next(g for g in report['groups'] if g['group_id'] == 'G0001')
            self.assertEqual(chain['status'], 'conflict')
            self.assertTrue(chain['missing_reads'])
            self.assertNotIn('next_write', chain)

    def test_empty_readback_leaves_every_group_unconfirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = freeze(tmp, 'r3c', 3, [JIA, YI])
            report = build_report(plan, readback('panel-1', []))
            self.assertEqual(report['status_counts']['unconfirmed'], 2)
            self.assertTrue(all('next_write' not in g for g in report['groups']))

    def test_no_change_release_reconciles_with_empty_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            glyphs = [dot_glyph('a', [(0, 0)])]
            same = v1_input(glyphs, 'a', 8, 8)
            import hashlib
            doc = source('panel-1', 8, 8, 1, same,
                         json.loads(json.dumps(same)), [])
            raw = json.dumps(doc, sort_keys=True).encode()
            plan = json.loads(Store(str(Path(tmp) / 'store')).seal(
                'r-empty', hashlib.sha256(raw).hexdigest(), doc))
            report = build_report(plan, readback('panel-1', []))
            self.assertEqual(report['groups'], [])
            self.assertEqual(report['group_count'], 0)


class ProtectedUnwrittenTileTests(unittest.TestCase):
    """Window coverage of an unwritten tile governs delivery of the group."""

    def _plan(self, tmp):
        # T0 flips (the 'a' disappears); T1 keeps a stable pixel ('b' lives
        # in both screens); the window spans both tiles, so T1 is inside the
        # check scope even though it is never written.
        space = dot_glyph(' ', [])
        glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph('b', [(0, 0)]), space]
        old = v1_input(glyphs, 'ab', 32, 8)
        new = v1_input(glyphs, ' b', 32, 8)
        win = window('win-spanning', 0, 0, 16, 8)
        doc = source('panel-9', 32, 8, 1, old, new, [win])
        import hashlib
        raw = json.dumps(doc, sort_keys=True).encode()
        store = Store(str(Path(tmp) / 'store'))
        return store, json.loads(store.seal('r-prot', hashlib.sha256(raw).hexdigest(), doc))

    def test_unread_protected_tile_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = self._plan(tmp)
            # T0 fully read as old; T1 (inside the window) is not read at all.
            sample = readback('panel-9', state_tiles(plan, 'old', [(0, 0)]))
            report = build_report(plan, sample)
            group = report['groups'][0]
            self.assertEqual(group['status'], 'unconfirmed')
            self.assertIn((1, 0),
                          {(m['tx'], m['ty']) for m in group['missing_reads']})
            self.assertNotIn('next_write', group)

    def test_drifted_stable_pixel_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = self._plan(tmp)
            broken_rows = [list(r) for r in screen_tiles(plan, 'old')[(1, 0)]]
            # Flip the stable pixel at (8,0) off.
            broken_rows[0][0] = '0'
            sample = readback('panel-9',
                              state_tiles(plan, 'old', [(0, 0)]) +
                              [(1, 0, [''.join(r) for r in broken_rows])])
            report = build_report(plan, sample)
            group = report['groups'][0]
            self.assertEqual(group['status'], 'conflict')
            evidence = group['excluding_evidence']
            self.assertTrue(any(e['tx'] == 1 and e['ty'] == 0 for e in evidence))
            self.assertEqual(evidence[0]['samples'][0]['reason'],
                             'stable-pixel-drift')

    def test_correct_supplementary_read_allows_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = self._plan(tmp)
            sample = readback(
                'panel-9', state_tiles(plan, 'old', [(0, 0), (1, 0)]))
            report = build_report(plan, sample)
            self.assertEqual(report['groups'][0]['status'], 'ready')

    def test_applied_group_is_not_written_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = self._plan(tmp)
            # Whole-scope readback after the atomic write succeeded.
            coords = [(0, 0), (1, 0), (2, 0), (3, 0)]
            sample = readback('panel-9', state_tiles(plan, 'new', coords))
            report = build_report(plan, sample)
            self.assertEqual(report['groups'][0]['status'], 'applied')
            self.assertNotIn('next_write', report['groups'][0])

    def test_drift_outside_scope_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = self._plan(tmp)
            tiles = state_tiles(plan, 'old', [(0, 0), (1, 0)])
            # T3 is outside both the changed tile and every joining window.
            drifted = screen_tiles(plan, 'old')[(3, 0)]
            drifted = [('1' + r[1:]) if i == 0 else r for i, r in enumerate(drifted)]
            tiles.append((3, 0, drifted))
            report = build_report(plan, readback('panel-9', tiles))
            self.assertEqual(report['groups'][0]['status'], 'ready')


class ReadbackValidationTests(unittest.TestCase):
    def test_reconcile_uses_only_the_frozen_plan(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph(' ', [])]
            old = v1_input(glyphs, 'aaaa', 32, 8)
            new = v1_input(glyphs, '    ', 32, 8)
            doc = source('panel-1', 32, 8, 3, old, new, [JIA, YI])
            source_path = Path(tmp) / 'request.json'
            raw = json.dumps(doc, sort_keys=True).encode()
            source_path.write_bytes(raw)
            store = Store(str(Path(tmp) / 'store'))
            plan = json.loads(
                store.seal('r5', hashlib.sha256(raw).hexdigest(), doc))
            # The request source disappears; reconciliation must rely solely
            # on the frozen plan and never re-derive anything from a source.
            source_path.unlink()
            report = build_report(plan, mixed_readback(plan))
            self.assertEqual(report['release']['release_id'], 'r5')
            self.assertEqual(report['panel']['panel_id'], 'panel-1')
            statuses = {g['group_id']: g['status'] for g in report['groups']}
            self.assertEqual(statuses, {'G0001': 'conflict', 'G0002': 'ready'})
            reloaded, raw_a = store.load('r5', plan['request']['sha256'])
            self.assertEqual(reloaded['groups'], plan['groups'])
    def test_bad_samples_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, plan = freeze(tmp, 'r4', 3, [JIA, YI])
            good = mixed_readback(plan)
            with self.assertRaises(ValueError):
                parse_readback({'panel_id': 'other', 'tiles': []},
                               plan['panel']['panel_id'], 4, 1)
            dup = dict(good, tiles=good['tiles'] + [dict(good['tiles'][0])])
            with self.assertRaises(ValueError):
                build_report(plan, dup)
            out_of_range = readback('panel-1', [(9, 9, good['tiles'][0]['rows'])])
            with self.assertRaises(ValueError):
                build_report(plan, out_of_range)
            bad_bitmap = readback('panel-1', [(0, 0, ['000'] * 8)])
            with self.assertRaises(ValueError):
                build_report(plan, bad_bitmap)
            # Partial reads are legal.
            partial = readback('panel-1',
                               [(0, 0, good['tiles'][0]['rows'])])
            report = build_report(plan, partial)
            self.assertEqual(report['readback']['tiles_read'], 1)


if __name__ == '__main__':
    unittest.main()
