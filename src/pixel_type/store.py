"""Crash-safe frozen-plan storage with process-level mutual exclusion."""

import fcntl
import json
import os
from pathlib import Path

from .planner import build_plan

INDEX_NAME = 'index.json'
LOCK_NAME = '.lock'
PLAN_SUFFIX = '.json'
TMP_SUFFIX = '.tmp'
INDEX_TMP = 'index.tmp'
SHA256_LEN = 64
HEX_DIGITS = set('0123456789abcdef')


class StoreError(Exception):
    """Raised when the store contents or arguments make a request impossible."""


def canonical_bytes(document):
    """Byte-stable serialization shared by every on-disk and stdout artifact."""
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def validate_sha256(value):
    if not isinstance(value, str) or len(value) != SHA256_LEN or any(c not in HEX_DIGITS for c in value):
        raise StoreError('request-sha256 must be 64 lowercase hex characters')
    return value


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.releases = self.root / 'releases'
        self.index_path = self.root / INDEX_NAME
        self.lock_path = self.root / LOCK_NAME

    def _ensure_layout(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.releases.mkdir(exist_ok=True)
        self.lock_path.touch(exist_ok=True)

    def _locked(self):
        handle = open(self.lock_path, 'a+b')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _read_index(self):
        try:
            raw = self.index_path.read_bytes()
        except FileNotFoundError:
            return {'schema_version': 1, 'kind': 'pixel-type.store.index', 'releases': {}}
        try:
            index = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError(f'store index is unreadable: {exc}') from exc
        if not isinstance(index, dict) or not isinstance(index.get('releases'), dict):
            raise StoreError('store index has an unknown shape')
        return index

    def _write_index(self, index, fail_at=None):
        tmp = self.root / INDEX_TMP
        with open(tmp, 'wb') as handle:
            handle.write(canonical_bytes(index))
            handle.flush()
            os.fsync(handle.fileno())
        if fail_at == 'index_replace':
            raise RuntimeError('injected crash before index replace')
        os.replace(tmp, self.index_path)
        _fsync_dir(self.root)
        tmp = None

    def _sweep_staging(self):
        """Remove leftovers from processes interrupted inside a freeze boundary."""
        for path in self.root.glob(f'*{TMP_SUFFIX}'):
            if path.is_file() or path.is_symlink():
                path.unlink()
        for path in self.releases.glob(f'*{TMP_SUFFIX}'):
            if path.is_file() or path.is_symlink():
                path.unlink()
        known = set(self._read_index()['releases'])
        for path in self.releases.glob(f'*{PLAN_SUFFIX}'):
            if path.name[:-len(PLAN_SUFFIX)] not in known:
                path.unlink()

    def seal(self, release_id, request_sha256, source=None, fail_at=None):
        """Freeze a release; returns the exact stored plan bytes.

        An already-frozen identity is returned from disk without touching
        ``source``, so refetches survive the source being replaced or deleted.
        """
        validate_sha256(request_sha256)
        self._ensure_layout()
        lock = self._locked()
        try:
            self._sweep_staging()
            index = self._read_index()
            existing = index['releases'].get(release_id)
            if existing is not None:
                if existing.get('sha256') != request_sha256:
                    raise StoreError(
                        f'release {release_id} is already frozen with a different request-sha256')
                return self._load_plan_bytes(release_id)
            if source is None:
                raise StoreError(
                    f'release {release_id} is not frozen and no source file is available')
            # The expensive validation happens only for a first freeze.
            plan = build_plan(source, release_id)
            plan['request'] = {'sha256': request_sha256}
            payload = canonical_bytes(plan)
            if fail_at == 'before_plan_write':
                raise RuntimeError('injected crash before plan write')
            tmp = self.releases / f'{release_id}{TMP_SUFFIX}'
            with open(tmp, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if fail_at == 'plan_replace':
                raise RuntimeError('injected crash before plan replace')
            os.replace(tmp, self.releases / f'{release_id}{PLAN_SUFFIX}')
            _fsync_dir(self.releases)
            index['releases'][release_id] = {'sha256': request_sha256}
            self._write_index(index, fail_at=fail_at)
            return payload
        finally:
            lock.close()

    def _load_plan_bytes(self, release_id):
        path = self.releases / f'{release_id}{PLAN_SUFFIX}'
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise StoreError(f'frozen release {release_id} is missing its plan file') from exc

    def load(self, release_id, request_sha256):
        """Return the frozen plan for an identity without changing the store."""
        validate_sha256(request_sha256)
        self._ensure_layout()
        lock = self._locked()
        try:
            self._sweep_staging()
            index = self._read_index()
            entry = index['releases'].get(release_id)
            if entry is None:
                raise StoreError(f'no frozen release matches {release_id}')
            if entry.get('sha256') != request_sha256:
                raise StoreError(
                    f'request-sha256 does not match the frozen release {release_id}')
            raw = self._load_plan_bytes(release_id)
            try:
                plan = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StoreError(f'frozen plan {release_id} is unreadable: {exc}') from exc
            return plan, raw
        finally:
            lock.close()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
