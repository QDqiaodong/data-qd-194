"""Offline playback fulfilment ledger.

One ledger directory holds any number of ``(panel_id, period_id)`` bands.  A
band is an append-only sequence of committed revisions: revision 1 freezes the
panel geometry, the material packs and the orders; every later revision only
edits evidence.  A committed revision bundles the post-edit evidence, the
complete allocation and the delta against the previously committed allocation,
so a reader never observes a partially applied change.

Only the standard library is used and no device, release or pack format is
touched.  See ``docs/contract-v4.md`` for the external contract.
"""

import base64
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

from .checks import array, fields, integer
from .io import load
from .pack import decode_pack, word_to_bytes

ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
MAX_TIME = (1 << 53) - 1
MAX_PERIOD_MS = 24 * 60 * 60 * 1000
MAX_WIDTH = 256
MAX_HEIGHT = 128
TILE = 8

MAX_PACKS = 8
MAX_ORDERS = 24
MAX_CHANGES = 128
MAX_EVIDENCE = 128

MATERIAL_KIND = 'pixel-type.intake.material'
RECORD_KIND = 'pixel-type.intake.record'
STATEMENT_KIND = 'pixel-type.intake.statement'
SCHEMA_VERSION = 1

FIRST_REQUIRED = ('panel_id', 'width', 'height', 'period_id', 'start_ms',
                  'end_ms', 'packs', 'orders')
NEXT_REQUIRED = ('panel_id', 'width', 'height', 'period_id', 'start_ms',
                 'end_ms')
FULL_RESUBMISSION = ('packs', 'orders')

MATCHED = 'matched'
UNMATCHED = 'unmatched'
NO_ELIGIBLE = 'no_eligible_segment'
ALLOCATED_ELSEWHERE = 'eligible_segments_allocated_elsewhere'


class LedgerError(ValueError):
    """A request is impossible against the committed ledger state."""


# --- small validation helpers ----------------------------------------------

def canonical_bytes(document):
    """Byte-stable serialization shared by every committed artifact."""
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def _ident(value, label):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(
            f'{label} must be 1..64 ASCII letters, digits, underscore or hyphen')
    return value


def _time(value, label):
    if type(value) is not int or not 0 <= value <= MAX_TIME:
        raise ValueError(f'{label} must be an integer in 0..2^53-1 milliseconds')
    return value


def _interval(obj, label, period=None):
    start = _time(obj['start_ms'], label + '.start_ms')
    end = _time(obj['end_ms'], label + '.end_ms')
    if start >= end:
        raise ValueError(f'{label}: interval must be non-empty and half-open')
    if end - start > MAX_PERIOD_MS:
        raise ValueError(f'{label}: interval is longer than 24 hours')
    if period is not None and (start < period[0] or end > period[1]):
        raise ValueError(f'{label}: interval must lie inside the period')
    return start, end


def _identity(source):
    """Validate the panel/period identity shared by every revision."""
    panel_id = _ident(source['panel_id'], 'panel_id')
    period_id = _ident(source['period_id'], 'period_id')
    width = integer(source['width'], TILE, MAX_WIDTH)
    height = integer(source['height'], TILE, MAX_HEIGHT)
    if width % TILE or height % TILE:
        raise ValueError('width and height must be positive multiples of 8')
    start, end = _interval(source, 'period')
    return {'panel_id': panel_id, 'period_id': period_id, 'width': width,
            'height': height, 'start': start, 'end': end}


def _frame_bytes(words):
    return b''.join(word_to_bytes(word) for word in words)


def _pixel_sha(frame):
    return hashlib.sha256(frame).hexdigest()


# --- material packs ---------------------------------------------------------

def _material_to_json(material):
    packs = {}
    for alias in sorted(material['packs']):
        entry = material['packs'][alias]
        packs[alias] = {
            'path': entry['path'],
            'scenes': {scene: base64.b64encode(entry['scenes'][scene]).decode('ascii')
                       for scene in sorted(entry['scenes'])},
        }
    return {'schema_version': SCHEMA_VERSION, 'kind': MATERIAL_KIND,
            'width': material['width'], 'height': material['height'],
            'packs': packs}


def _material_from_json(document):
    if not isinstance(document, dict) or document.get('kind') != MATERIAL_KIND:
        raise ValueError('ledger material has an unknown shape')
    width = integer(document['width'], TILE, MAX_WIDTH)
    height = integer(document['height'], TILE, MAX_HEIGHT)
    packs = document['packs']
    if not isinstance(packs, dict):
        raise ValueError('ledger material packs must be an object')
    result = {'width': width, 'height': height, 'packs': {}}
    for alias in packs:
        _ident(alias, 'material pack alias')
        entry = packs[alias]
        if not isinstance(entry, dict) or not isinstance(entry.get('scenes'), dict):
            raise ValueError(f'material pack {alias} has an unknown shape')
        scenes = {}
        for scene in entry['scenes']:
            _ident(scene, f'material pack {alias} scene')
            try:
                scenes[scene] = base64.b64decode(entry['scenes'][scene],
                                                 validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f'material pack {alias} scene {scene} is not valid base64') from exc
        result['packs'][alias] = {'path': entry.get('path'), 'scenes': scenes}
    return result


def _parse_packs(packs, base_dir, width, height):
    if not isinstance(packs, dict):
        raise ValueError('packs must be an object mapping aliases to file paths')
    if not 1 <= len(packs) <= MAX_PACKS:
        raise ValueError(f'packs must contain 1..{MAX_PACKS} entries')
    material = {'width': width, 'height': height, 'packs': {}}
    for alias in sorted(packs):
        _ident(alias, 'packs alias')
        raw_path = packs[alias]
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f'packs.{alias} must be a non-empty file path')
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path(base_dir) / path
        try:
            document = load(path)
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValueError(f'packs.{alias}: cannot read pack {path}: {exc}') from exc
        try:
            pack_width, pack_height, catalog, frames = decode_pack(document)
        except ValueError as exc:
            raise ValueError(f'packs.{alias}: invalid pack: {exc}') from exc
        if (pack_width, pack_height) != (width, height):
            raise ValueError(
                f'packs.{alias}: pack is {pack_width}x{pack_height}, '
                f'period is {width}x{height}')
        # Every scene of every pack is decoded here, including scenes no
        # observation ever references: a damaged pack can never be first data.
        scenes = {}
        for entry, words in zip(catalog, frames):
            scenes[entry['id']] = _frame_bytes(words)
        material['packs'][alias] = {'path': raw_path, 'scenes': scenes}
    return material


def _material_sha(material):
    return hashlib.sha256(canonical_bytes(_material_to_json(material))).hexdigest()


def _lookup(material, pack, scene, label):
    if pack not in material['packs']:
        raise ValueError(f'{label}: unknown pack alias {pack!r}')
    scenes = material['packs'][pack]['scenes']
    if scene not in scenes:
        raise ValueError(f'{label}: pack {pack!r} has no scene {scene!r}')
    return scenes[scene]


def _parse_ref(ref, label, material):
    if not isinstance(ref, dict):
        raise ValueError(f'{label} must be an object with pack and scene')
    fields(ref, ('pack', 'scene'))
    pack = _ident(ref['pack'], label + '.pack')
    scene = _ident(ref['scene'], label + '.scene')
    _lookup(material, pack, scene, label)
    return pack, scene


# --- orders and evidence edits ---------------------------------------------

def _parse_orders(orders, material, period):
    array(orders, 0, MAX_ORDERS)
    parsed = []
    seen = set()
    for index, order in enumerate(orders):
        label = f'orders[{index}]'
        if not isinstance(order, dict):
            raise ValueError(f'{label} must be an object')
        fields(order, ('id', 'start_ms', 'end_ms', 'required_ms', 'expected'))
        order_id = _ident(order['id'], label + '.id')
        if order_id in seen:
            raise ValueError(f'{label}: duplicate order id {order_id}')
        seen.add(order_id)
        start, end = _interval(order, label, period)
        required = order['required_ms']
        if type(required) is not int or not 1 <= required <= end - start:
            raise ValueError(
                f'{label}.required_ms must be a positive integer no larger '
                f'than the window length')
        pack, scene = _parse_ref(order['expected'], label + '.expected', material)
        parsed.append({'id': order_id, 'start': start, 'end': end,
                       'required': required, 'pack': pack, 'scene': scene,
                       'frame': _lookup(material, pack, scene, label)})
    parsed.sort(key=lambda item: item['id'])
    return parsed


def _apply_changes(changes, evidence, material, period):
    array(changes, 0, MAX_CHANGES)
    seen = set()
    operations = []
    for index, change in enumerate(changes):
        label = f'changes[{index}]'
        if not isinstance(change, dict):
            raise ValueError(f'{label} must be an object')
        kind = change.get('type')
        if kind == 'put':
            fields(change, ('id', 'type', 'start_ms', 'end_ms', 'observed'))
            eid = _ident(change['id'], label + '.id')
            if eid in seen:
                raise ValueError(
                    f'{label}: evidence id {eid} appears more than once in this revision')
            seen.add(eid)
            start, end = _interval(change, label, period)
            pack, scene = _parse_ref(change['observed'],
                                     label + '.observed', material)
            operations.append(('put', eid, start, end, pack, scene))
        elif kind == 'drop':
            fields(change, ('id', 'type'))
            eid = _ident(change['id'], label + '.id')
            if eid in seen:
                raise ValueError(
                    f'{label}: evidence id {eid} appears more than once in this revision')
            seen.add(eid)
            if eid not in evidence:
                raise ValueError(f'{label}: cannot drop unknown evidence {eid}')
            operations.append(('drop', eid))
        else:
            raise ValueError(f'{label}.type must be "put" or "drop"')
    result = dict(evidence)
    for operation in operations:
        if operation[0] == 'put':
            _, eid, start, end, pack, scene = operation
            result[eid] = {'start': start, 'end': end,
                           'pack': pack, 'scene': scene}
        else:
            del result[operation[1]]
    if len(result) > MAX_EVIDENCE:
        raise ValueError(
            f'after this revision {len(result)} evidence intervals are active; '
            f'at most {MAX_EVIDENCE} are allowed')
    # An interval an observation names is decoded once, whatever pack alias
    # or scene spelling it came through, so a later drop can never depend on
    # the source file still being where it was.
    for eid, entry in result.items():
        _lookup(material, entry['pack'], entry['scene'], f'evidence {eid}')
    return result


# --- timeline analysis ------------------------------------------------------

def _coverage_parts(spans):
    parts = []
    for span in spans:
        ids = list(span['ids'])
        if parts and parts[-1]['end_ms'] == span['t0'] and parts[-1]['evidence_ids'] == ids:
            parts[-1]['end_ms'] = span['t1']
        else:
            parts.append({'start_ms': span['t0'], 'end_ms': span['t1'],
                          'evidence_ids': ids})
    return parts


def _collect(spans, kind, same_key=None):
    runs = []
    for span in spans:
        if span['kind'] != kind:
            continue
        if runs and runs[-1]['_spans'][-1]['t1'] == span['t0'] and (
                same_key is None or same_key(runs[-1], span)):
            runs[-1]['_spans'].append(span)
            runs[-1]['end_ms'] = span['t1']
        else:
            runs.append({'_spans': [span], 'start_ms': span['t0'],
                         'end_ms': span['t1']})
    for run in runs:
        spans_in_run = run.pop('_spans')
        run['coverage'] = _coverage_parts(spans_in_run)
        run['frame'] = spans_in_run[-1]['frame']
    return runs


def analyse(evidence, material, period):
    """Turn evidence intervals into maximal playback segments and gaps.

    Two evidence intervals prove the same picture only when their decoded
    frames are point-for-point identical; names, packs, offsets and digests
    never decide equality.  A millisecond watched under two different
    pictures is a conflict, and a millisecond with no evidence is missing.
    Neither can support a voucher and both cut a playback segment.
    """
    items = []
    for eid in evidence:
        entry = evidence[eid]
        frame = _lookup(material, entry['pack'], entry['scene'],
                        f'evidence {eid}')
        items.append((entry['start'], entry['end'], eid, frame))
    items.sort(key=lambda item: (item[0], item[1], item[2]))
    # The period boundaries always take part, so a stretch with no evidence
    # at all is still reported as missing rather than silently absent.
    points = sorted({value for start, end, _eid, _frame in items
                     for value in (start, end)} | {period[0], period[1]})
    spans = []
    for start, end in zip(points, points[1:]):
        active = [(eid, frame) for s, e, eid, frame in items if s <= start < e]
        if not active:
            spans.append({'t0': start, 't1': end, 'kind': 'missing',
                          'ids': (), 'frame': None})
            continue
        frames = {frame for _eid, frame in active}
        ids = tuple(sorted(eid for eid, _frame in active))
        if len(frames) == 1:
            spans.append({'t0': start, 't1': end, 'kind': 'confirmed',
                          'ids': ids, 'frame': next(iter(frames))})
        else:
            spans.append({'t0': start, 't1': end, 'kind': 'conflict',
                          'ids': ids, 'frame': None})

    segments = []
    for run in _collect(spans, 'confirmed',
                        same_key=lambda left, span: left['_spans'][-1]['frame'] == span['frame']):
        frame = run['frame']
        pixel_sha = _pixel_sha(frame)
        segments.append({
            'id': f"{run['start_ms']}:{run['end_ms']}:{pixel_sha}",
            'start_ms': run['start_ms'],
            'end_ms': run['end_ms'],
            'pixels_sha256': pixel_sha,
            'coverage': run['coverage'],
            'frame': frame,
        })
    missing = [{'start_ms': run['start_ms'], 'end_ms': run['end_ms']}
               for run in _collect(spans, 'missing')]
    conflicts = [{'start_ms': run['start_ms'], 'end_ms': run['end_ms'],
                  'coverage': run['coverage']}
                 for run in _collect(spans, 'conflict')]
    return segments, missing, conflicts


# --- allocation -------------------------------------------------------------

def _eligible(orders, segments):
    eligible = {}
    for order in orders:
        indexes = []
        for index, segment in enumerate(segments):
            if segment['frame'] != order['frame']:
                continue
            overlap = (min(segment['end_ms'], order['end'])
                       - max(segment['start_ms'], order['start']))
            if overlap >= order['required']:
                indexes.append(index)
        eligible[order['id']] = indexes
    return eligible


def _cardinality(order_ids, eligible, blocked):
    owner = {}

    def augment(order_id, seen):
        for index in eligible[order_id]:
            if index in blocked or index in seen:
                continue
            seen.add(index)
            if index not in owner or augment(owner[index], seen):
                owner[index] = order_id
                return True
        return False

    size = 0
    for order_id in order_ids:
        if augment(order_id, set()):
            size += 1
    return size


def allocate(orders, segments, eligible):
    """Maximum legal allocation, then the contract's lexicographic tie-break.

    The segment sequence of an order id is its matching segment sorted by
    (start, end); unmatched sorts after every segment.  Sequences are compared
    by ASCII order id, so the canonical allocation is found greedily per order
    while preserving maximum cardinality.
    """
    order_ids = [order['id'] for order in orders]
    total = _cardinality(order_ids, eligible, frozenset())
    pairs = {}
    used = set()
    for position, order_id in enumerate(order_ids):
        rest = order_ids[position + 1:]
        chosen = None
        for index in eligible[order_id]:
            if index in used:
                continue
            # The global maximum is fixed, so a candidate is only admissible
            # when it still allows exactly the remaining cardinality.  This
            # is what makes the per-order choice the lexicographic minimum.
            reachable = 1 + _cardinality(rest, eligible,
                                         frozenset(used | {index}))
            if len(pairs) + reachable == total:
                chosen = index
                break
        if chosen is not None:
            pairs[order_id] = chosen
            used.add(chosen)
    if len(pairs) != total:  # pragma: no cover - greedy invariant
        raise LedgerError('allocation search lost the maximum cardinality')
    return pairs


# --- statement and record ---------------------------------------------------

def _order_json(order):
    return {'id': order['id'],
            'window': {'start_ms': order['start'], 'end_ms': order['end']},
            'required_ms': order['required'],
            'expected': {'pack': order['pack'], 'scene': order['scene'],
                         'pixels_sha256': _pixel_sha(order['frame'])}}


def _deltas(previous_pairs, current_pairs):
    entries = []
    for order_id in sorted(set(previous_pairs) | set(current_pairs)):
        old = previous_pairs.get(order_id)
        new = current_pairs.get(order_id)
        if old == new:
            continue
        if old is not None:
            entries.append({'order_id': order_id, 'kind': 'revoke',
                            'segment_id': old, 'delta': -1})
        if new is not None:
            entries.append({'order_id': order_id, 'kind': 'grant',
                            'segment_id': new, 'delta': 1})
    return entries


def _build_statement(identity, revision, orders, segments, missing, conflicts,
                     evidence, material, pairs, previous_pairs):
    owner = {index: order_id for order_id, index in pairs.items()}
    billing = []
    for order in orders:
        entry = _order_json(order)
        chosen = pairs.get(order['id'])
        if chosen is not None:
            segment = segments[chosen]
            entry['status'] = MATCHED
            entry['segment'] = {
                'id': segment['id'],
                'start_ms': segment['start_ms'],
                'end_ms': segment['end_ms'],
                'overlap_ms': (min(segment['end_ms'], order['end'])
                               - max(segment['start_ms'], order['start'])),
            }
        else:
            entry['status'] = UNMATCHED
            indexes = _eligible([order], segments)[order['id']]
            entry['reason'] = NO_ELIGIBLE if not indexes else ALLOCATED_ELSEWHERE
            entry['eligible_segments'] = [
                {'id': segments[index]['id'],
                 'start_ms': segments[index]['start_ms'],
                 'end_ms': segments[index]['end_ms'],
                 'overlap_ms': (min(segments[index]['end_ms'], order['end'])
                                - max(segments[index]['start_ms'], order['start'])),
                 'allocated_to': owner.get(index)}
                for index in indexes
            ]
        billing.append(entry)

    deltas = _deltas(previous_pairs, {order_id: segments[index]['id']
                                      for order_id, index in pairs.items()})
    summary = {
        'matched': len(pairs),
        'unmatched': len(orders) - len(pairs),
        'granted': sum(1 for item in deltas if item['kind'] == 'grant'),
        'revoked': sum(1 for item in deltas if item['kind'] == 'revoke'),
        'net_delta': sum(item['delta'] for item in deltas),
    }
    evidence_out = []
    for eid in sorted(evidence):
        entry = evidence[eid]
        evidence_out.append({
            'id': eid,
            'start_ms': entry['start'],
            'end_ms': entry['end'],
            'pack': entry['pack'],
            'scene': entry['scene'],
            'pixels_sha256': _pixel_sha(_lookup(material, entry['pack'],
                                                entry['scene'],
                                                f'evidence {eid}')),
        })
    segments_out = [{key: segment[key] for key in
                     ('id', 'start_ms', 'end_ms', 'pixels_sha256', 'coverage')}
                    for segment in segments]
    return {
        'schema_version': SCHEMA_VERSION,
        'kind': STATEMENT_KIND,
        'panel_id': identity['panel_id'],
        'period_id': identity['period_id'],
        'revision': revision,
        'period': {'start_ms': identity['start'], 'end_ms': identity['end']},
        'width': identity['width'],
        'height': identity['height'],
        'billing': billing,
        'deltas': deltas,
        'summary': summary,
        'segments': segments_out,
        'missing': missing,
        'conflicts': conflicts,
        'evidence': evidence_out,
    }


def _record(identity, revision, material_sha, orders, evidence, pairs,
            statement_bytes):
    return {
        'schema_version': SCHEMA_VERSION,
        'kind': RECORD_KIND,
        'panel_id': identity['panel_id'],
        'period_id': identity['period_id'],
        'revision': revision,
        'period': {'start_ms': identity['start'], 'end_ms': identity['end']},
        'width': identity['width'],
        'height': identity['height'],
        'material_sha256': material_sha,
        'orders': [{key: order[key] for key in
                    ('id', 'start', 'end', 'required', 'pack', 'scene')}
                   for order in orders],
        'evidence': evidence,
        'pairs': pairs,
        'statement_sha256': hashlib.sha256(statement_bytes).hexdigest(),
    }


# --- revision builders ------------------------------------------------------

def _present(source, names):
    return isinstance(source, dict) and all(name in source for name in names)


def parse_identity(source):
    """Cheap identity extraction used to locate the band before locking."""
    if not isinstance(source, dict):
        raise ValueError('source must be a JSON object')
    panel = _ident(source.get('panel_id'), 'panel_id')
    period = _ident(source.get('period_id'), 'period_id')
    return panel, period


def build_first(source, base_dir):
    if not isinstance(source, dict):
        raise ValueError('source must be a JSON object')
    fields(source, FIRST_REQUIRED, optional=('changes',))
    identity = _identity(source)
    material = _parse_packs(source['packs'], base_dir,
                            identity['width'], identity['height'])
    orders = _parse_orders(source['orders'], material,
                           (identity['start'], identity['end']))
    evidence = _apply_changes(source.get('changes', []), {}, material,
                              (identity['start'], identity['end']))
    return identity, material, orders, evidence


def build_next(source, previous_identity, material, orders, evidence):
    if not isinstance(source, dict):
        raise ValueError('source must be a JSON object')
    fields(source, NEXT_REQUIRED, optional=('changes',))
    identity = _identity(source)
    for key in ('panel_id', 'period_id', 'width', 'height'):
        if identity[key] != previous_identity[key]:
            raise ValueError(
                f'{key} must not change after revision 1 '
                f'(was {previous_identity[key]!r}, got {identity[key]!r})')
    if (identity['start'], identity['end']) != (previous_identity['start'],
                                                previous_identity['end']):
        raise ValueError('period boundaries must not change after revision 1')
    evidence = _apply_changes(source.get('changes', []), evidence, material,
                              (identity['start'], identity['end']))
    return identity, evidence


def finalise(identity, revision, material, orders, evidence, previous_pairs):
    material_sha = _material_sha(material)
    segments, missing, conflicts = analyse(
        evidence, material, (identity['start'], identity['end']))
    eligible = _eligible(orders, segments)
    pairs = allocate(orders, segments, eligible)
    statement = _build_statement(identity, revision, orders, segments, missing,
                                 conflicts, evidence, material, pairs,
                                 previous_pairs)
    statement_bytes = canonical_bytes(statement)
    record = _record(identity, revision, material_sha, orders, evidence,
                     {order_id: segments[index]['id']
                      for order_id, index in pairs.items()},
                     statement_bytes)
    return statement_bytes, record


# --- on-disk band storage ---------------------------------------------------

RECORD_SUFFIX = '.record.json'
STATEMENT_SUFFIX = '.statement.json'
MATERIAL_NAME = 'material.json'
BAND_LOCK = '.band-lock'
ROOT_LOCK = '.lock'
TMP_SUFFIX = '.tmp'


def _band_key(panel_id, period_id):
    return (panel_id + '\0' + period_id).encode('utf-8').hex()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _record_name(revision):
    return f'{revision:08d}{RECORD_SUFFIX}'


def _statement_name(revision):
    return f'{revision:08d}{STATEMENT_SUFFIX}'


STOP_ENV = 'PIXEL_TYPE_TEST_STOP_AT'


def _stop_hook():
    """Deterministic fault injection for interruption tests.

    The environment variable names a commit boundary; the process exits
    there, so a test exercises a real process stop rather than a race with
    an asynchronous signal.
    """
    return os.environ.get(STOP_ENV) or None


def _atomic_replace(directory, name, payload):
    temporary = Path(directory) / f'.{name}.{os.getpid()}{TMP_SUFFIX}'
    with open(temporary, 'wb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, Path(directory) / name)
    _fsync_dir(directory)


class Ledger:
    """Append-only committed revisions for every ``(panel, period)`` band."""

    def __init__(self, directory):
        self.root = Path(directory)
        self.bands = self.root / 'bands'

    # -- layout ---------------------------------------------------------
    def _ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ROOT_LOCK).touch(exist_ok=True)

    def _band_dir(self, panel_id, period_id):
        return self.bands / _band_key(panel_id, period_id)

    def _ensure_band(self, panel_id, period_id):
        target = self._band_dir(panel_id, period_id)
        handle = open(self.root / ROOT_LOCK, 'a+b')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            target.mkdir(parents=True, exist_ok=True)
            (target / BAND_LOCK).touch(exist_ok=True)
        finally:
            handle.close()
        return target

    def _lock_band(self, band):
        handle = open(band / BAND_LOCK, 'a+b')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    # -- read side ------------------------------------------------------
    def _sweep(self, band):
        """Discard drafts from a process that stopped inside a commit.

        A record file is the commit point: a statement without a matching
        record is a draft and is removed, so a reader never sees half a
        revision and the next attempt may reuse the revision number.
        """
        for path in band.glob(f'*{TMP_SUFFIX}'):
            if path.is_file() or path.is_symlink():
                path.unlink()
        committed = set(self._revisions(band))
        for path in band.glob(f'*{STATEMENT_SUFFIX}'):
            stem = path.name[:-len(STATEMENT_SUFFIX)]
            if not stem.isdigit() or int(stem) not in committed:
                path.unlink()

    @staticmethod
    def _revisions(band):
        if not band.is_dir():
            return []
        found = []
        for path in band.glob(f'*{RECORD_SUFFIX}'):
            stem = path.name[:-len(RECORD_SUFFIX)]
            if stem.isdigit():
                found.append(int(stem))
        return sorted(found)

    def _head(self, band):
        revisions = self._revisions(band)
        return revisions[-1] if revisions else 0

    @staticmethod
    def _read_json(path, label):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise LedgerError(f'{label} is unreadable: {exc}') from exc
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerError(f'{label} is not valid JSON: {exc}') from exc

    def _load_record(self, band, revision):
        record = self._read_json(band / _record_name(revision),
                                 f'committed revision {revision} record')
        if (not isinstance(record, dict) or record.get('revision') != revision
                or record.get('kind') != RECORD_KIND):
            raise LedgerError(f'committed revision {revision} record is mismatched')
        statement_path = band / _statement_name(revision)
        try:
            statement_raw = statement_path.read_bytes()
        except OSError as exc:
            raise LedgerError(
                f'committed revision {revision} has no statement file') from exc
        if hashlib.sha256(statement_raw).hexdigest() != record.get('statement_sha256'):
            raise LedgerError(
                f'committed revision {revision} statement does not match its record')
        try:
            statement = json.loads(statement_raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerError(
                f'committed revision {revision} statement is not valid JSON: {exc}') from exc
        if (not isinstance(statement, dict)
                or statement.get('kind') != STATEMENT_KIND
                or statement.get('panel_id') != record.get('panel_id')
                or statement.get('period_id') != record.get('period_id')
                or statement.get('revision') != revision):
            raise LedgerError(
                f'committed revision {revision} statement is mismatched')
        return record, statement_raw

    def _load_material(self, band, record):
        document = self._read_json(band / MATERIAL_NAME, 'band material')
        try:
            material = _material_from_json(document)
        except (KeyError, ValueError) as exc:
            raise LedgerError(f'band material is invalid: {exc}') from exc
        if _material_sha(material) != record.get('material_sha256'):
            raise LedgerError('band material does not match the committed record')
        if (material['width'], material['height']) != (record.get('width'),
                                                        record.get('height')):
            raise LedgerError('band material dimensions do not match the record')
        return material

    def _load_state(self, band, revision):
        record, raw = self._load_record(band, revision)
        material = self._load_material(band, record)
        try:
            orders = [{'id': order['id'], 'start': order['start'],
                       'end': order['end'], 'required': order['required'],
                       'pack': order['pack'], 'scene': order['scene'],
                       'frame': _lookup(material, order['pack'], order['scene'],
                                        f'order {order["id"]}')}
                      for order in record['orders']]
        except (KeyError, TypeError) as exc:
            raise LedgerError(
                f'committed revision {revision} record is malformed: {exc}') from exc
        orders.sort(key=lambda item: item['id'])
        identity = {'panel_id': record['panel_id'],
                    'period_id': record['period_id'],
                    'width': record['width'], 'height': record['height'],
                    'start': record['period']['start_ms'],
                    'end': record['period']['end_ms']}
        return identity, material, orders, record, raw

    # -- public operations ---------------------------------------------
    def statement(self, panel_id, period_id, revision):
        """Return the exact committed statement bytes for one band revision."""
        _ident(panel_id, 'panel-id')
        _ident(period_id, 'period-id')
        if type(revision) is not int or revision < 1:
            raise LedgerError('revision must be a positive integer')
        band = self._band_dir(panel_id, period_id)
        if not band.is_dir():
            raise LedgerError(
                f'no committed revision {revision} for this panel and period')
        lock = self._lock_band(band)
        try:
            # Readers never clean up: a concurrent commit holds the same
            # lock, so the revision files are stable while we read them.
            head = self._head(band)
            if revision > head:
                raise LedgerError(
                    f'revision {revision} is not committed; latest is {head}')
            _record, raw = self._load_record(band, revision)
            return raw
        finally:
            lock.close()

    def commit(self, source, base_dir, revision, fail_at=None):
        """Commit the next revision of one band and return the statement bytes.

        ``fail_at`` is a test hook: it simulates the process stopping at a
        named point inside the commit.  A stopped commit leaves only swept
        drafts and never consumes the revision number.
        """
        if type(revision) is not int or revision < 1:
            raise LedgerError('revision must be a positive integer')
        panel_id, period_id = parse_identity(source)
        self._ensure_root()
        band = self._band_dir(panel_id, period_id)
        # A first revision is fully validated -- every pack in every alias,
        # even scenes nobody references -- before a band directory exists,
        # so a damaged source can never leave a half-born ledger behind.
        prebuilt = None
        if not band.is_dir():
            if revision != 1:
                raise LedgerError(
                    f'revision {revision} is not the next revision; no band '
                    f'is committed yet, expected 1')
            prebuilt = build_first(source, base_dir)
        band = self._ensure_band(panel_id, period_id)
        lock = self._lock_band(band)
        try:
            self._sweep(band)
            head = self._head(band)
            if head > 0 and _present(source, FULL_RESUBMISSION):
                # After revision 1 a full replacement is never accepted, even
                # if it happens to carry no changes list.
                raise LedgerError(
                    f'revision {revision} resubmits full packs/orders after '
                    f'revision {head} is committed; only changes may be sent')
            if revision != head + 1:
                raise LedgerError(
                    f'revision {revision} is not the next revision; '
                    f'committed head is {head}, expected {head + 1}')
            if head == 0:
                if prebuilt is None:
                    prebuilt = build_first(source, base_dir)
                identity, material, orders, evidence = prebuilt
                previous_pairs = {}
            else:
                prev_identity, material, orders, _previous, previous_raw = \
                    self._load_state(band, head)
                identity, evidence = build_next(source, prev_identity, material,
                                                orders, _previous['evidence'])
                # Previous grants come from the committed statement itself,
                # so the delta is always computed against the same state a
                # reader sees.
                previous_statement = json.loads(previous_raw.decode('utf-8'))
                previous_pairs = {
                    item['id']: item['segment']['id']
                    for item in previous_statement.get('billing', [])
                    if item.get('status') == MATCHED}
            if (identity['panel_id'], identity['period_id']) != (panel_id, period_id):
                raise LedgerError('source identity changed while locking')
            statement_bytes, record = finalise(
                identity, revision, material, orders, evidence, previous_pairs)

            stop = _stop_hook()
            boundary = fail_at if fail_at is not None else stop

            def at(name):
                if boundary == name:
                    if fail_at is not None:
                        raise RuntimeError(f'injected stop at {name}')
                    # Simulate abrupt termination: no finally blocks, no
                    # buffers flushed beyond what each write already fsynced.
                    os._exit(70)

            at('after_build')
            if head == 0:
                _atomic_replace(band, MATERIAL_NAME,
                                canonical_bytes(_material_to_json(material)))
                at('after_material')
            at('before_statement')
            # The statement is a draft until the record lands; the record
            # replacement is the single commit point.
            _atomic_replace(band, _statement_name(revision), statement_bytes)
            at('after_statement')
            at('before_record')
            _atomic_replace(band, _record_name(revision),
                            canonical_bytes(record))
            at('after_record')
            return statement_bytes
        finally:
            lock.close()
