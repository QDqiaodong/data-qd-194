import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import dot_glyph, dump_source, source, v1_input, window
from pixel_type.store import Store, StoreError, canonical_bytes

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'


def simple_doc(limit=1, windows=()):
    glyphs = [dot_glyph('a', [(0, 0)]), dot_glyph(' ', [])]
    old = v1_input(glyphs, 'a', 8, 8)
    new = v1_input(glyphs, ' ', 8, 8)
    return source('panel-x', 8, 8, limit, old, new, list(windows))


def freeze_bytes(store_dir, release_id, doc):
    raw = json.dumps(doc, sort_keys=True).encode()
    digest = hashlib.sha256(raw).hexdigest()
    payload = Store(store_dir).seal(release_id, digest, doc)
    return digest, payload


class StoreTests(unittest.TestCase):
    def test_refetch_returns_identical_bytes_without_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp) / 'store')
            source_path = Path(tmp) / 'request.json'
            doc = simple_doc()
            digest = dump_source(source_path, doc)
            first = Store(store_dir).seal('r1', digest, doc)
            # Source replaced with different bytes, then deleted entirely.
            dump_source(source_path, simple_doc(windows=[window('w', 0, 0, 1, 1)]))
            second = Store(store_dir).load('r1', digest)[1]
            os.unlink(source_path)
            third = Store(store_dir).load('r1', digest)[1]
            self.assertEqual(first, second)
            self.assertEqual(second, third)

    def test_same_id_different_digest_conflicts_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp) / 'store')
            digest, original = freeze_bytes(store_dir, 'r1', simple_doc())
            other_doc = simple_doc(windows=[window('w', 0, 0, 8, 8)])
            other_digest = hashlib.sha256(
                json.dumps(other_doc, sort_keys=True).encode()).hexdigest()
            with self.assertRaises(StoreError):
                Store(store_dir).seal('r1', other_digest, other_doc)
            self.assertEqual(Store(store_dir).load('r1', digest)[1], original)
            with self.assertRaises(StoreError):
                Store(store_dir).load('r1', other_digest)

    def test_interrupted_freezes_leave_no_usable_or_half_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp) / 'store')
            doc = simple_doc()
            digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
            store = Store(store_dir)
            for fail_at in ('before_plan_write', 'plan_replace', 'index_replace'):
                with self.assertRaises(RuntimeError):
                    store.seal('r-crash', digest, doc, fail_at=fail_at)
                with self.assertRaises(StoreError):
                    store.load('r-crash', digest)
                self.assertNotIn('r-crash', store._read_index()['releases'])
            # A later clean freeze succeeds and the staging debris is swept.
            payload = store.seal('r-crash', digest, doc)
            self.assertEqual(store.load('r-crash', digest)[1], payload)
            leftovers = list(Path(store_dir).rglob('*.tmp'))
            self.assertEqual(leftovers, [])

    def test_orphan_plan_without_index_entry_is_not_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp) / 'store')
            digest, payload = freeze_bytes(store_dir, 'r1', simple_doc())
            # Simulate a process that died after writing the plan but before
            # the index commit, by removing the index entry and sweeping.
            index_path = Path(store_dir) / 'index.json'
            index = json.loads(index_path.read_text())
            del index['releases']['r1']
            index_path.write_bytes(canonical_bytes(index))
            store = Store(store_dir)
            store._ensure_layout()
            with store._locked():
                store._sweep_staging()
            with self.assertRaises(StoreError):
                store.load('r1', digest)
            # A subsequent freeze of the same identity starts cleanly.
            new_payload = store.seal('r1', digest, simple_doc())
            self.assertEqual(new_payload, payload)

    def test_rejected_freeze_leaves_no_refetchable_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp) / 'store')
            doc = simple_doc()
            doc['max_tiles_per_write'] = 0  # invalid for this panel
            digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
            with self.assertRaises(ValueError):
                Store(store_dir).seal('r-rejected', digest, doc)
            with self.assertRaises(StoreError):
                Store(store_dir).load('r-rejected', digest)
            self.assertEqual(Store(store_dir)._read_index()['releases'], {})
            # No staging debris survives the failed boundary.
            self.assertEqual(list(Path(store_dir).rglob('*.tmp')), [])

    def test_two_distinct_plans_are_never_both_visible_under_one_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp) / 'store')
            digest_a, _payload_a = freeze_bytes(store_dir, 'r1', simple_doc())
            other = simple_doc(windows=[window('w', 0, 0, 8, 8)])
            digest_b = hashlib.sha256(
                json.dumps(other, sort_keys=True).encode()).hexdigest()
            # The second request cannot create a competing plan directory entry.
            with self.assertRaises(StoreError):
                Store(store_dir).seal('r1', digest_b, other)
            files = sorted(p.name for p in Path(store_dir, 'releases').iterdir())
            self.assertEqual(files, ['r1.json'])
            self.assertEqual(Store(store_dir).load('r1', digest_a)[0]
                             ['request']['sha256'], digest_a)
            with self.assertRaises(StoreError):
                Store(store_dir).load('r1', digest_b)

    def test_concurrent_same_id_only_one_request_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp) / 'store')
            doc_a = simple_doc()
            doc_b = simple_doc(windows=[window('w', 0, 0, 1, 1)])
            digest_a = hashlib.sha256(
                json.dumps(doc_a, sort_keys=True).encode()).hexdigest()
            digest_b = hashlib.sha256(
                json.dumps(doc_b, sort_keys=True).encode()).hexdigest()
            script = f"""
import json, sys
sys.path.insert(0, {str(SRC)!r})
from pixel_type.store import Store
with open(sys.argv[1]) as handle:
    doc = json.load(handle)
try:
    Store({store_dir!r}).seal('r-race', sys.argv[2], doc)
    print('WIN')
except Exception as exc:
    print('LOSE:' + type(exc).__name__)
"""
            env = dict(os.environ)
            path_a = Path(tmp) / 'a.json'
            path_b = Path(tmp) / 'b.json'
            path_a.write_text(json.dumps(doc_a))
            path_b.write_text(json.dumps(doc_b))
            procs = [
                subprocess.Popen([sys.executable, '-c', script, str(path_a), digest_a],
                                 stdout=subprocess.PIPE, env=env),
                subprocess.Popen([sys.executable, '-c', script, str(path_b), digest_b],
                                 stdout=subprocess.PIPE, env=env),
            ]
            outcomes = sorted(p.communicate()[0].decode().strip() for p in procs)
            self.assertEqual([o.split(':')[0] for o in outcomes], ['LOSE', 'WIN'])
            store = Store(store_dir)
            entries = store._read_index()['releases']
            self.assertEqual(list(entries), ['r-race'])
            winner = entries['r-race']['sha256']
            self.assertIn(winner, (digest_a, digest_b))
            # The loser cannot fetch its own plan under the shared identity.
            loser = digest_b if winner == digest_a else digest_a
            with self.assertRaises(StoreError):
                store.load('r-race', loser)


class CliTests(unittest.TestCase):
    def run_cli(self, *args, input_text=None):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        proc = subprocess.run(
            [sys.executable, '-m', 'pixel_type.cli', *args],
            cwd=ROOT, env=env, capture_output=True, text=True, input=input_text)
        return proc

    def test_seal_and_reconcile_cli_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / 'request.json'
            doc = simple_doc(windows=[window('w', 0, 0, 8, 8)])
            digest = dump_source(source_path, doc)
            store_dir = str(Path(tmp) / 'store')
            sealed = self.run_cli(
                'seal', '--store', store_dir, '--release-id', 'rel-1',
                '--request-sha256', digest, '--source', str(source_path))
            self.assertEqual(sealed.returncode, 0, sealed.stderr)
            first = sealed.stdout.encode()
            # Byte-identical refetch even after the source disappears.
            os.unlink(source_path)
            refetched = self.run_cli(
                'seal', '--store', store_dir, '--release-id', 'rel-1',
                '--request-sha256', digest, '--source', str(source_path))
            self.assertEqual(refetched.returncode, 0, refetched.stderr)
            self.assertEqual(refetched.stdout.encode(), first)

            plan = json.loads(first)
            old_rows = plan['screens']['old']['tiles'][0]['rows']
            readback_path = Path(tmp) / 'readback.json'
            readback_path.write_text(json.dumps(
                {'panel_id': 'panel-x',
                 'tiles': [{'tx': 0, 'ty': 0, 'rows': old_rows}]}))
            report_path = Path(tmp) / 'report.json'
            done = self.run_cli(
                'reconcile', '--store', store_dir, '--release-id', 'rel-1',
                '--request-sha256', digest, '--readback', str(readback_path),
                '--output', str(report_path))
            self.assertEqual(done.returncode, 0, done.stderr)
            report = json.loads(report_path.read_text())
            self.assertEqual(report['groups'][0]['status'], 'ready')
            self.assertTrue(report['groups'][0]['next_write'])

            # Existing reports are never overwritten; rerun exits 2 and the
            # original bytes survive.
            before = report_path.read_bytes()
            rerun = self.run_cli(
                'reconcile', '--store', store_dir, '--release-id', 'rel-1',
                '--request-sha256', digest, '--readback', str(readback_path),
                '--output', str(report_path))
            self.assertEqual(rerun.returncode, 2)
            self.assertEqual(report_path.read_bytes(), before)

    def test_bad_readback_exits_two_without_creating_a_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / 'request.json'
            digest = dump_source(source_path, simple_doc())
            store_dir = str(Path(tmp) / 'store')
            sealed = self.run_cli(
                'seal', '--store', store_dir, '--release-id', 'rel-bad',
                '--request-sha256', digest, '--source', str(source_path))
            self.assertEqual(sealed.returncode, 0, sealed.stderr)
            bad_readback = Path(tmp) / 'bad-readback.json'
            bad_readback.write_text(json.dumps(
                {'panel_id': 'wrong-panel', 'tiles': []}))
            report_path = Path(tmp) / 'must-not-exist.json'
            failed = self.run_cli(
                'reconcile', '--store', store_dir, '--release-id', 'rel-bad',
                '--request-sha256', digest, '--readback', str(bad_readback),
                '--output', str(report_path))
            self.assertEqual(failed.returncode, 2)
            self.assertFalse(report_path.exists())

    def test_seal_rejects_wrong_digest_unknown_release_and_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / 'request.json'
            digest = dump_source(source_path, simple_doc())
            store_dir = str(Path(tmp) / 'store')
            wrong = self.run_cli(
                'seal', '--store', store_dir, '--release-id', 'rel-x',
                '--request-sha256', '0' * 64, '--source', str(source_path))
            self.assertEqual(wrong.returncode, 2)
            missing = self.run_cli(
                'reconcile', '--store', store_dir, '--release-id', 'ghost',
                '--request-sha256', digest,
                '--readback', str(source_path),
                '--output', str(Path(tmp) / 'r.json'))
            self.assertEqual(missing.returncode, 2)
            bad = Path(tmp) / 'bad.json'
            bad.write_text('{"broken": true}')
            bad_run = self.run_cli(
                'seal', '--store', store_dir, '--release-id', 'rel-y',
                '--request-sha256', hashlib.sha256(bad.read_bytes()).hexdigest(),
                '--source', str(bad))
            self.assertEqual(bad_run.returncode, 2)
            bad_hex = self.run_cli(
                'seal', '--store', store_dir, '--release-id', 'rel-z',
                '--request-sha256', 'ABC', '--source', str(source_path))
            self.assertEqual(bad_hex.returncode, 2)


class LegacyCliTests(unittest.TestCase):
    def test_v1_invocation_report_svg_summary_and_exit_code_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'v1.json'
            env = dict(os.environ, PYTHONPATH=str(SRC))
            proc = subprocess.run(
                [sys.executable, '-m', 'pixel_type.cli',
                 '--source', str(ROOT / 'fixtures' / 'input.json'),
                 '--output', str(output)],
                cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary, {
                'drawn_pixels': 48, 'lines': 3, 'overflow_glyphs': 0,
                'placed_glyphs': 6, 'unique_ink_pixels': 48})
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 1)
            self.assertIn('<svg', report['svg_text'])
            # Existing target is not overwritten.
            again = subprocess.run(
                [sys.executable, '-m', 'pixel_type.cli',
                 '--source', str(ROOT / 'fixtures' / 'input.json'),
                 '--output', str(output)],
                cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(again.returncode, 2)


if __name__ == '__main__':
    unittest.main()
