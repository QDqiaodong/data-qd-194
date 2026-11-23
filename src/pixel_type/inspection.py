"""Offline spot-check cards and readback verdicts for pre-delivery inspection.

A handheld inspector can only read back a few nominated 8x8 blocks per pass,
and up to ``max_bad_reads`` of those reads in the whole pass may return an
arbitrary wrong bitmap.  ``inspect-plan`` compiles a work order (panel,
material packs, candidate scenes with their approval flags, probe tiles and
read limits) into a self-contained card: the bitmap every candidate is
expected to show on every probe tile, plus -- when one exists -- an
executable strategy that is *guaranteed* to reach a decided verdict within
``max_reads`` reads under the premise that the panel shows a listed candidate
and the error budget holds.

``inspect-result`` judges one readback receipt against the card alone; the
source input and the material packs are never reopened.  Every candidate is
scored by how many read tiles contradict it; candidates within the error
budget stay possible, and the verdict over the whole evidence is ``cleared``
/ ``rejected`` / ``undecided`` / ``inconsistent``.  The verdict never comes
from strategy node labels or from the order the reads happened to arrive in.
"""

import os
import re

from .bitmap import TILE
from .checks import array, fields, integer
from .io import load
from .pack import bytes_to_word, decode_pack, word_to_bytes

ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
BITS_RE = re.compile(r'[0-9a-f]{16}')
SHA256_RE = re.compile(r'[0-9a-f]{64}')

MAX_WIDTH = 256
MAX_HEIGHT = 128
MAX_PACKS = 4
MAX_CANDIDATES = 16
MAX_PROBES = 12

SCHEMA_VERSION = 1
CARD_KIND = 'pixel-type.inspect.card'
REPORT_KIND = 'pixel-type.inspect.report'

GUARANTEED = 'guaranteed'
NOT_GUARANTEED = 'not_guaranteed'

CLEARED = 'cleared'
REJECTED = 'rejected'
UNDECIDED = 'undecided'
INCONSISTENT = 'inconsistent'

VERDICTS = (CLEARED, REJECTED, UNDECIDED, INCONSISTENT)

PREMISE = ('判定以前提为条件：屏幕确实显示候选名单内的画面，且整次回读中返回错误位图的块数不超过 '
           'max_bad_reads。cleared 表示在该前提下所有仍可能的候选均已获准；rejected 表示仍可能的'
           '候选均未获准；undecided 表示获准与未获准的候选都仍可能；inconsistent 表示证据已排除'
           '全部候选，前提本身不再成立。本报告不证明名单以外的屏幕内容。')


def _ident(value, label):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(
            f'{label} must be 1..64 ASCII letters, digits, underscore or hyphen')
    return value


def _word_bits(word):
    """Canonical block bitmap: 8 bytes, one per row, left pixel high bit."""
    return word_to_bytes(word).hex()


def _bits_word(bits):
    return bytes_to_word(bytes.fromhex(bits))


def _parse_probes(value, tile_cols, tile_rows):
    probes = []
    seen = set()
    for item in array(value, 1, MAX_PROBES):
        fields(item, ('tx', 'ty'))
        tx, ty = item['tx'], item['ty']
        if type(tx) is not int or type(ty) is not int:
            raise ValueError('probe tile coordinates must be integers')
        if not (0 <= tx < tile_cols and 0 <= ty < tile_rows):
            raise ValueError(f'probe tile ({tx},{ty}) is outside the panel')
        if (tx, ty) in seen:
            raise ValueError(f'duplicate probe tile ({tx},{ty})')
        seen.add((tx, ty))
        probes.append((tx, ty))
    return probes


# --- inspect-plan: source parsing -------------------------------------------

def _parse_plan_source(source):
    fields(source, ('panel_id', 'width', 'height', 'packs', 'candidates',
                    'probe_tiles', 'max_reads', 'max_bad_reads'))
    panel_id = _ident(source['panel_id'], 'panel_id')
    width = integer(source['width'], TILE, MAX_WIDTH)
    height = integer(source['height'], TILE, MAX_HEIGHT)
    if width % TILE or height % TILE:
        raise ValueError('width and height must be positive multiples of 8')
    packs = source['packs']
    if not isinstance(packs, dict) or not 1 <= len(packs) <= MAX_PACKS:
        raise ValueError('packs must map 1..4 aliases to pack file paths')
    pack_refs = {}
    for alias, path in packs.items():
        _ident(alias, 'pack alias')
        if not isinstance(path, str) or not path:
            raise ValueError('pack path must be a non-empty string')
        pack_refs[alias] = path
    probes = _parse_probes(source['probe_tiles'], width // TILE, height // TILE)
    max_reads = integer(source['max_reads'], 0, len(probes))
    max_bad = integer(source['max_bad_reads'], 0, 1)
    candidates = []
    seen = set()
    for cand in array(source['candidates'], 1, MAX_CANDIDATES):
        fields(cand, ('id', 'pack', 'scene', 'allowed'))
        cid = _ident(cand['id'], 'candidate id')
        if cid in seen:
            raise ValueError(f'duplicate candidate id {cid}')
        seen.add(cid)
        _ident(cand['pack'], 'candidate pack')
        if cand['pack'] not in pack_refs:
            raise ValueError(f'candidate {cid} names an unknown pack alias')
        _ident(cand['scene'], 'candidate scene')
        if type(cand['allowed']) is not bool:
            raise ValueError('candidate allowed must be a boolean')
        candidates.append({'id': cid, 'pack': cand['pack'],
                           'scene': cand['scene'], 'allowed': cand['allowed']})
    return {'panel_id': panel_id, 'width': width, 'height': height,
            'packs': pack_refs, 'candidates': candidates, 'probe_tiles': probes,
            'max_reads': max_reads, 'max_bad_reads': max_bad}


def _load_packs(pack_refs, base_dir, width, height):
    """Fully validate every listed pack and index its scene tile words.

    ``decode_pack`` rejects the whole pack for any bad record, including
    records no candidate references; sizes must match the work order.
    """
    frames = {}
    for alias in sorted(pack_refs):
        path = pack_refs[alias]
        if not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        pwidth, pheight, catalog, pack_frames = decode_pack(load(path))
        if (pwidth, pheight) != (width, height):
            raise ValueError(
                f'pack {alias} is {pwidth}x{pheight}, expected {width}x{height}')
        frames[alias] = {entry['id']: pack_frames[index]
                         for index, entry in enumerate(catalog)}
    return frames


# --- the guarantee game ------------------------------------------------------

class _Guarantee:
    """AND/OR solver: minimum reads a compliant strategy needs from a state.

    Candidates with identical expected bitmaps on every probe tile are
    indistinguishable, so the game is played over *groups* of alike
    candidates; a group only remembers whether it holds approved and/or
    rejected members.  A state is ``(read_mask, possible, ones)``: the tiles
    already sampled, the groups still within the error budget, and -- when
    ``max_bad_reads`` is 1 -- the groups sitting at exactly one mismatch.
    The instrument is the existential player (picks the next unread tile);
    the panel and channel are the universal one (pick any bitmap class,
    including a value no candidate expects).  A state needs 0 further reads
    when its evidence is already decided or already inconsistent; budgets
    above ``max_reads`` collapse into one "too late" value.
    """

    def __init__(self, values, approved, disapproved, max_bad, max_reads):
        self.values = values
        self.approved = approved
        self.disapproved = disapproved
        self.max_bad = max_bad
        self.max_reads = max_reads
        self.group_count = len(values)
        self.tile_count = len(values[0])
        self.limit = max_reads + 1
        self.memo = {}

    def needed(self, read_mask, possible, ones):
        key = (read_mask, possible, ones)
        hit = self.memo.get(key)
        if hit is None:
            hit = self._compute(read_mask, possible, ones)
            self.memo[key] = hit
        return hit

    def _step(self, possible, ones, match):
        stay = possible & match
        promoted = possible & ~ones & ~match
        if self.max_bad:
            return stay | promoted, (ones & match) | promoted
        return stay, 0

    def _outcomes(self, possible, tile):
        """Match sets: one per value any possible group expects, plus fallback."""
        matches = {}
        for group in range(self.group_count):
            if possible >> group & 1:
                matches.setdefault(self.values[group][tile], 0)
                matches[self.values[group][tile]] |= 1 << group
        return list(matches.values()) + [0]

    def _compute(self, read_mask, possible, ones):
        if not possible:
            return 0  # inconsistent: nothing left to prove
        if not possible & self.disapproved or not possible & self.approved:
            return 0  # already cleared or already rejected
        depth = bin(read_mask).count('1')
        if depth >= self.max_reads:
            return self.limit
        best = self.limit
        for tile in range(self.tile_count):
            bit = 1 << tile
            if read_mask & bit:
                continue
            worst = 0
            for match in self._outcomes(possible, tile):
                child_possible, child_ones = self._step(possible, ones, match)
                if not child_possible:
                    continue  # off-premise path: stops as inconsistent
                budget = self.needed(read_mask | bit, child_possible, child_ones)
                if budget > worst:
                    worst = budget
                    if worst + 1 >= best:
                        break
            else:
                best = worst + 1
                if best == 1:
                    break
        return best


def _group_candidates(candidates):
    """Merge candidates that are pixel-identical on every probe tile."""
    by_words = {}
    for cand in candidates:
        by_words.setdefault(cand['expected'], [0, 0])
        by_words[cand['expected']][0 if cand['allowed'] else 1] += 1
    groups = sorted(by_words.items())
    values = [list(words) for words, _counts in groups]
    approved = sum(1 << g for g, (_w, (yes, _no)) in enumerate(groups) if yes)
    disapproved = sum(1 << g for g, (_w, (_yes, no)) in enumerate(groups) if no)
    return values, approved, disapproved


def _build_strategy(solver, probes):
    """Materialise one optimal strategy as a DAG of read/stop nodes.

    Every read node lists one branch per bitmap any still-possible group
    expects on its tile; any other legal bitmap takes ``fallback``.  All
    decided and all inconsistent states share a single stop node -- the
    verdict is computed from the gathered evidence, never from node labels.
    """
    nodes = []
    ids = {}
    stop = [None]

    def emit(read_mask, possible, ones):
        key = (read_mask, possible, ones)
        if key in ids:
            return ids[key]
        decided = (not possible or not possible & solver.disapproved
                   or not possible & solver.approved)
        depth = bin(read_mask).count('1')
        if decided or depth >= solver.max_reads:
            if stop[0] is None:
                stop[0] = len(nodes)
                nodes.append({'id': stop[0], 'action': 'stop'})
            ids[key] = stop[0]
            return stop[0]
        target = solver.needed(read_mask, possible, ones)
        tile = None
        for candidate in range(solver.tile_count):
            bit = 1 << candidate
            if read_mask & bit:
                continue
            worst = 0
            for match in solver._outcomes(possible, candidate):
                child = solver._step(possible, ones, match)
                if not child[0]:
                    continue
                worst = max(worst, solver.needed(read_mask | bit, *child))
            if worst + 1 == target:
                tile = candidate
                break
        if tile is None:  # pragma: no cover - the solver always has one
            raise ValueError('internal: no optimal tile found')
        bit = 1 << tile
        node_id = len(nodes)
        nodes.append(None)
        ids[key] = node_id
        matches = {}
        for group in range(solver.group_count):
            if possible >> group & 1:
                matches.setdefault(solver.values[group][tile], 0)
                matches[solver.values[group][tile]] |= 1 << group
        branches = []
        for word in sorted(matches):
            child = solver._step(possible, ones, matches[word])
            branches.append({'bits': _word_bits(word),
                             'next': emit(read_mask | bit, *child)})
        fallback = emit(read_mask | bit, *solver._step(possible, ones, 0))
        tx, ty = probes[tile]
        nodes[node_id] = {'id': node_id, 'action': 'read', 'tx': tx, 'ty': ty,
                          'branches': branches, 'fallback': fallback}
        return node_id

    start = emit(0, (1 << solver.group_count) - 1, 0)
    return {'start': start, 'nodes': nodes}


def build_card(source, base_dir):
    """Validate a work order and compile the self-contained inspection card."""
    spec = _parse_plan_source(source)
    frames = _load_packs(spec['packs'], base_dir, spec['width'], spec['height'])
    tile_cols = spec['width'] // TILE
    probes = spec['probe_tiles']
    candidates = []
    for cand in spec['candidates']:
        scenes = frames[cand['pack']]
        if cand['scene'] not in scenes:
            raise ValueError(
                f"candidate {cand['id']} names unknown scene {cand['scene']} "
                f"in pack {cand['pack']}")
        words = scenes[cand['scene']]
        expected = tuple(words[ty * tile_cols + tx] for tx, ty in probes)
        candidates.append({**cand, 'expected': expected})
    card = {
        'schema_version': SCHEMA_VERSION,
        'kind': CARD_KIND,
        'panel_id': spec['panel_id'],
        'width': spec['width'],
        'height': spec['height'],
        'tile_cols': tile_cols,
        'tile_rows': spec['height'] // TILE,
        'max_reads': spec['max_reads'],
        'max_bad_reads': spec['max_bad_reads'],
        'probe_tiles': [{'tx': tx, 'ty': ty} for tx, ty in probes],
        'candidates': [
            {'id': cand['id'], 'pack': cand['pack'], 'scene': cand['scene'],
             'allowed': cand['allowed'],
             'tiles': [{'tx': tx, 'ty': ty, 'bits': _word_bits(word)}
                       for (tx, ty), word in zip(probes, cand['expected'])]}
            for cand in candidates],
    }
    values, approved, disapproved = _group_candidates(candidates)
    solver = _Guarantee(values, approved, disapproved,
                        spec['max_bad_reads'], spec['max_reads'])
    full = (1 << len(values)) - 1
    if solver.needed(0, full, 0) <= spec['max_reads']:
        card['guarantee'] = GUARANTEED
        card['strategy'] = _build_strategy(solver, probes)
    else:
        card['guarantee'] = NOT_GUARANTEED
        card['strategy'] = None
    return card


# --- inspect-result: card and receipt parsing --------------------------------

def _parse_card_candidates(value, probes):
    candidates = []
    seen = set()
    for cand in array(value, 1, MAX_CANDIDATES):
        fields(cand, ('id', 'pack', 'scene', 'allowed', 'tiles'))
        cid = _ident(cand['id'], 'candidate id')
        if cid in seen:
            raise ValueError(f'duplicate candidate id {cid}')
        seen.add(cid)
        _ident(cand['pack'], 'candidate pack')
        _ident(cand['scene'], 'candidate scene')
        if type(cand['allowed']) is not bool:
            raise ValueError('candidate allowed must be a boolean')
        tiles = array(cand['tiles'], len(probes), len(probes))
        expect = {}
        for entry in tiles:
            fields(entry, ('tx', 'ty', 'bits'))
            tx, ty = entry['tx'], entry['ty']
            if type(tx) is not int or type(ty) is not int:
                raise ValueError('candidate tile coordinates must be integers')
            if (tx, ty) in expect:
                raise ValueError(f'candidate {cid} repeats tile ({tx},{ty})')
            bits = entry['bits']
            if not isinstance(bits, str) or not BITS_RE.fullmatch(bits):
                raise ValueError('tile bits must be 16 lowercase hex characters')
            expect[(tx, ty)] = _bits_word(bits)
        if set(expect) != set(probes):
            raise ValueError(
                f'candidate {cid} must give exactly the probe tile bitmaps')
        candidates.append({'id': cid, 'pack': cand['pack'],
                           'scene': cand['scene'], 'allowed': cand['allowed'],
                           'expect': expect})
    return candidates


def _validate_strategy(strategy, probes, max_reads):
    if not isinstance(strategy, dict):
        raise ValueError('strategy must be an object')
    fields(strategy, ('start', 'nodes'))
    nodes = strategy['nodes']
    if not isinstance(nodes, list) or not nodes:
        raise ValueError('strategy nodes must be a non-empty list')
    table = {}
    for node in nodes:
        if not isinstance(node, dict) or type(node.get('id')) is not int:
            raise ValueError('strategy node must be an object with an integer id')
        if node['id'] in table:
            raise ValueError('duplicate strategy node id')
        action = node.get('action')
        if action == 'stop':
            fields(node, ('id', 'action'))
        elif action == 'read':
            fields(node, ('id', 'action', 'tx', 'ty', 'branches', 'fallback'))
        else:
            raise ValueError('unknown strategy node action')
        table[node['id']] = node
    if type(strategy['start']) is not int or strategy['start'] not in table:
        raise ValueError('strategy start must be a node id')
    probe_index = {tile: index for index, tile in enumerate(probes)}
    for node in nodes:
        if node['action'] != 'read':
            continue
        tx, ty = node['tx'], node['ty']
        if type(tx) is not int or type(ty) is not int:
            raise ValueError('strategy tile coordinates must be integers')
        if (tx, ty) not in probe_index:
            raise ValueError('strategy reads a tile outside the probe list')
        if type(node['fallback']) is not int or node['fallback'] not in table:
            raise ValueError('strategy fallback must be a node id')
        if not isinstance(node['branches'], list):
            raise ValueError('strategy branches must be a list')
        seen_bits = set()
        for branch in node['branches']:
            fields(branch, ('bits', 'next'))
            bits = branch['bits']
            if not isinstance(bits, str) or not BITS_RE.fullmatch(bits):
                raise ValueError('branch bits must be 16 lowercase hex characters')
            if bits in seen_bits:
                raise ValueError('duplicate branch bits on one strategy node')
            seen_bits.add(bits)
            if type(branch['next']) is not int or branch['next'] not in table:
                raise ValueError('branch next must be a node id')
    # Path sanity: acyclic, never re-samples a tile, never exceeds max_reads.
    done = set()

    def visit(node_id, used, depth, stack):
        if (node_id, used) in done:
            return
        if node_id in stack:
            raise ValueError('strategy contains a cycle')
        node = table[node_id]
        if node['action'] == 'stop':
            done.add((node_id, used))
            return
        tile_bit = 1 << probe_index[(node['tx'], node['ty'])]
        if used & tile_bit:
            raise ValueError('strategy samples one tile twice on a path')
        if depth + 1 > max_reads:
            raise ValueError('strategy exceeds max_reads on a path')
        stack.add(node_id)
        targets = [branch['next'] for branch in node['branches']]
        targets.append(node['fallback'])
        for target in targets:
            visit(target, used | tile_bit, depth + 1, stack)
        stack.discard(node_id)
        done.add((node_id, used))

    visit(strategy['start'], 0, 0, set())


def parse_card(document):
    """Validate a card file; sources and packs are never reopened."""
    if not isinstance(document, dict):
        raise ValueError('card must be a JSON object')
    fields(document, ('schema_version', 'kind', 'panel_id', 'width', 'height',
                      'tile_cols', 'tile_rows', 'max_reads', 'max_bad_reads',
                      'probe_tiles', 'candidates', 'guarantee', 'strategy'))
    if document['kind'] != CARD_KIND:
        raise ValueError('unknown card kind')
    version = document['schema_version']
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError('unsupported card schema version')
    panel_id = _ident(document['panel_id'], 'panel_id')
    width = integer(document['width'], TILE, MAX_WIDTH)
    height = integer(document['height'], TILE, MAX_HEIGHT)
    if width % TILE or height % TILE:
        raise ValueError('card width and height must be multiples of 8')
    if document['tile_cols'] != width // TILE:
        raise ValueError('tile_cols does not match card width')
    if document['tile_rows'] != height // TILE:
        raise ValueError('tile_rows does not match card height')
    probes = _parse_probes(document['probe_tiles'],
                           width // TILE, height // TILE)
    max_reads = integer(document['max_reads'], 0, len(probes))
    max_bad = integer(document['max_bad_reads'], 0, 1)
    candidates = _parse_card_candidates(document['candidates'], probes)
    guarantee = document['guarantee']
    if guarantee not in (GUARANTEED, NOT_GUARANTEED):
        raise ValueError('unknown guarantee marker')
    strategy = document['strategy']
    if guarantee == GUARANTEED:
        _validate_strategy(strategy, probes, max_reads)
    elif strategy is not None:
        raise ValueError('a not_guaranteed card must not carry a strategy')
    return {'panel_id': panel_id, 'width': width, 'height': height,
            'max_reads': max_reads, 'max_bad_reads': max_bad,
            'probe_tiles': probes, 'candidates': candidates,
            'guarantee': guarantee}


def _parse_reads(value, card):
    probe_set = set(card['probe_tiles'])
    reads = []
    seen = set()
    for read in array(value, 0, card['max_reads']):
        fields(read, ('tx', 'ty', 'bits'))
        tx, ty = read['tx'], read['ty']
        if type(tx) is not int or type(ty) is not int:
            raise ValueError('read coordinates must be integers')
        if (tx, ty) not in probe_set:
            raise ValueError(f'read ({tx},{ty}) is not a probe tile on the card')
        if (tx, ty) in seen:
            raise ValueError(f'duplicate read of tile ({tx},{ty})')
        seen.add((tx, ty))
        bits = read['bits']
        if not isinstance(bits, str) or not BITS_RE.fullmatch(bits):
            raise ValueError('read bits must be 16 lowercase hex characters')
        reads.append({'tx': tx, 'ty': ty, 'bits': bits,
                      'word': _bits_word(bits)})
    return reads


def build_report(card, card_sha256, readback):
    """Judge one readback receipt against a validated card.

    The verdict uses the whole receipt: an unfinished strategy walk is not a
    rejection, and a leaf once passed through is not a final clearance.
    """
    if not isinstance(readback, dict):
        raise ValueError('readback must be a JSON object')
    fields(readback, ('panel_id', 'card_sha256', 'reads'))
    if readback['panel_id'] != card['panel_id']:
        raise ValueError(
            f"readback panel_id {readback['panel_id']!r} does not match "
            f"card panel {card['panel_id']!r}")
    digest = readback['card_sha256']
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ValueError('card_sha256 must be 64 lowercase hex characters')
    if digest != card_sha256:
        raise ValueError('readback is bound to a different card')
    reads = _parse_reads(readback['reads'], card)
    max_bad = card['max_bad_reads']
    statuses = []
    for cand in card['candidates']:
        mismatches = []
        for read in reads:
            expected = cand['expect'][(read['tx'], read['ty'])]
            if read['word'] != expected:
                mismatches.append({'tx': read['tx'], 'ty': read['ty'],
                                   'expected_bits': _word_bits(expected),
                                   'actual_bits': read['bits']})
        mismatches.sort(key=lambda item: (item['ty'], item['tx']))
        count = len(mismatches)
        statuses.append({'id': cand['id'], 'pack': cand['pack'],
                         'scene': cand['scene'], 'allowed': cand['allowed'],
                         'possible': count <= max_bad,
                         'mismatch_count': count, 'mismatches': mismatches})
    possible = [status for status in statuses if status['possible']]
    if not possible:
        verdict = INCONSISTENT
    elif all(status['allowed'] for status in possible):
        verdict = CLEARED
    elif not any(status['allowed'] for status in possible):
        verdict = REJECTED
    else:
        verdict = UNDECIDED
    return {
        'schema_version': SCHEMA_VERSION,
        'kind': REPORT_KIND,
        'panel_id': card['panel_id'],
        'card_sha256': card_sha256,
        'guarantee': card['guarantee'],
        'max_reads': card['max_reads'],
        'max_bad_reads': max_bad,
        'reads_used': len(reads),
        'reads': [{'tx': read['tx'], 'ty': read['ty'], 'bits': read['bits']}
                  for read in reads],
        'verdict': verdict,
        'possible_candidates': [status['id'] for status in possible],
        'candidates': statuses,
        'premise': PREMISE,
    }
