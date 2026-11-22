"""Offline multi-scene signage pack compiler and single-frame extractor.

A pack stores one record per scene in catalog order (ASCII scene id order).
A record is either a *full frame* (every 8x8 block of the panel) or a
*reference frame* (a base catalog index plus XOR masks for changed blocks).
Only full frames may act as bases, so a frame is always reconstructible from
one self-contained record plus deltas -- never from residual screen pixels.
"""

import base64
import re

from .bitmap import TILE, Screen, word_to_rows
from .checks import array, fields, integer
from .planner import render_pixels

ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
MAX_WIDTH = 256
MAX_HEIGHT = 128
MAX_BUDGET = 524288
MAX_SCENES = 16

FORMAT_FULL = 'full'
FORMAT_REFERENCE = 'reference'
PACK_KIND = 'pixel-type.pack'
FRAME_KIND = 'pixel-type.frame'
SCHEMA_VERSION = 1


class BudgetExceeded(ValueError):
    """The smallest legal payload does not fit the terminal budget."""

    def __init__(self, required, budget):
        self.required = required
        self.budget = budget
        super().__init__(
            f'minimum payload {required} bytes exceeds budget {budget} bytes')


# --- byte-level tile codec -------------------------------------------------
#
# A block is 8 bytes, one byte per pixel row from top to bottom; the leftmost
# pixel of a row is the most significant bit.  Block order is row-major:
# block = ty * tile_cols + tx.

def word_to_bytes(word):
    out = bytearray(TILE)
    for y in range(TILE):
        byte = 0
        for x in range(TILE):
            if word & (1 << (y * TILE + x)):
                byte |= 1 << (TILE - 1 - x)
        out[y] = byte
    return bytes(out)


def bytes_to_word(raw):
    if len(raw) != TILE:
        raise ValueError('one block must be exactly 8 bytes')
    word = 0
    for y, byte in enumerate(raw):
        for x in range(TILE):
            if byte & (1 << (TILE - 1 - x)):
                word |= 1 << (y * TILE + x)
    return word


# --- compile ---------------------------------------------------------------

def _scene_pixels(scene_id, layout):
    try:
        return render_pixels(layout)
    except ValueError as exc:
        raise ValueError(f'scene {scene_id}: {exc}') from exc


def _parse_source(source):
    fields(source, ('width', 'height', 'payload_budget', 'scenes'))
    width = integer(source['width'], TILE, MAX_WIDTH)
    height = integer(source['height'], TILE, MAX_HEIGHT)
    if width % TILE or height % TILE:
        raise ValueError('width and height must be positive multiples of 8')
    budget = integer(source['payload_budget'], 0, MAX_BUDGET)
    scenes = array(source['scenes'], 1, MAX_SCENES)
    parsed = []
    seen = set()
    for scene in scenes:
        if not isinstance(scene, dict):
            raise ValueError('every scene must be an object')
        fields(scene, ('id', 'direct_only', 'layout'))
        scene_id = scene['id']
        if not isinstance(scene_id, str) or not ID_RE.fullmatch(scene_id):
            raise ValueError('scene id must be 1..64 ASCII letters, digits, underscore or hyphen')
        if scene_id in seen:
            raise ValueError(f'duplicate scene id {scene_id}')
        seen.add(scene_id)
        if type(scene['direct_only']) is not bool:
            raise ValueError('direct_only must be a boolean')
        if not isinstance(scene['layout'], dict):
            raise ValueError(f'scene {scene_id}: layout must be a complete v1 object')
        pixels = _scene_pixels(scene_id, scene['layout'])
        try:
            screen = Screen(width, height, pixels)
        except ValueError as exc:
            raise ValueError(f'scene {scene_id}: {exc}') from exc
        parsed.append({'id': scene_id, 'direct_only': scene['direct_only'],
                       'words': screen.tiles})
    # Catalog order is the ASCII order of scene ids; every later decision is
    # taken in this order so a shuffled ``scenes`` list is irrelevant.
    parsed.sort(key=lambda item: item['id'])
    return width, height, budget, parsed


def _block_diffs(words):
    count = len(words)
    diff = [[0] * count for _ in range(count)]
    for p in range(count):
        left = words[p]
        for q in range(count):
            if p == q:
                continue
            right = words[q]
            diff[p][q] = sum(1 for a, b in zip(left, right) if a != b)
    return diff


def _optimise(words, direct_flags):
    """Global minimum payload, then the contract's lexicographic tie-break.

    Enumerates every set of full-frame scenes (all ``direct_only`` scenes are
    forced in) because a scene may cheaply act as the base of several others,
    and a mostly-different scene may be cheaper stored in full than as a
    delta.  For a fixed set, each referencing scene independently picks the
    base with the fewest changed blocks, breaking equal counts by catalog
    index -- which is exactly the lexicographic base rule.
    """
    count = len(words)
    block_count = len(words[0])
    full_cost = block_count * TILE
    diff = _block_diffs(words)
    # Per scene: candidate bases ordered by (changed blocks, catalog index).
    ranked = [sorted((diff[p][q], q) for q in range(count) if q != p)
              for p in range(count)]
    mandatory = 0
    free_bits = []
    for p in range(count):
        if direct_flags[p]:
            mandatory |= 1 << p
        else:
            free_bits.append(1 << p)

    best = None
    for subset in range(1 << len(free_bits)):
        full_mask = mandatory
        for index, bit in enumerate(free_bits):
            if subset >> index & 1:
                full_mask |= bit
        if full_mask == 0:
            continue
        cost = full_mask.bit_count() * full_cost
        bases = []
        feasible = True
        for p in range(count):
            if full_mask >> p & 1:
                continue
            for changed, base in ranked[p]:
                if full_mask >> base & 1:
                    cost += 3 + 10 * changed
                    bases.append(base)
                    break
            else:
                feasible = False
                break
            if best is not None and cost > best[0]:
                # Cost only grows; this subset can no longer win on length.
                feasible = False
                break
        if not feasible:
            continue
        formats = tuple(0 if full_mask >> p & 1 else 1 for p in range(count))
        candidate = (cost, formats, tuple(bases))
        if best is None or candidate < best:
            best = candidate
            best_mask = full_mask
    if best is None:  # pragma: no cover - a full-frame set always exists
        raise ValueError('no legal frame assignment exists')
    cost, _formats, _bases = best
    assignment = []
    base_iter = iter(_bases)
    for p in range(count):
        assignment.append(None if best_mask >> p & 1 else next(base_iter))
    return cost, assignment


def _build_payload(parsed, assignment):
    payload = bytearray()
    catalog = []
    for scene, base in zip(parsed, assignment):
        offset = len(payload)
        if base is None:
            record = b''.join(word_to_bytes(word) for word in scene['words'])
            entry = {'id': scene['id'], 'direct_only': scene['direct_only'],
                     'format': FORMAT_FULL, 'offset': offset,
                     'length': len(record)}
        else:
            changed = [block for block, (a, b)
                       in enumerate(zip(scene['words'], parsed[base]['words']))
                       if a != b]
            record = bytearray([base])
            record += len(changed).to_bytes(2, 'little')
            for block in changed:
                record += block.to_bytes(2, 'little')
                record += word_to_bytes(scene['words'][block]
                                        ^ parsed[base]['words'][block])
            record = bytes(record)
            entry = {'id': scene['id'], 'direct_only': scene['direct_only'],
                     'format': FORMAT_REFERENCE, 'offset': offset,
                     'length': len(record)}
        payload += record
        catalog.append(entry)
    return bytes(payload), catalog


def compile_pack(source):
    """Validate a pack source and return the self-contained pack document.

    Raises ``BudgetExceeded`` when the globally minimal payload is larger than
    ``payload_budget``; raises ``ValueError`` for any structural violation.
    """
    width, height, budget, parsed = _parse_source(source)
    tile_cols = width // TILE
    tile_rows = height // TILE
    direct_flags = [scene['direct_only'] for scene in parsed]
    required, assignment = _optimise([scene['words'] for scene in parsed],
                                     direct_flags)
    if required > budget:
        raise BudgetExceeded(required, budget)
    payload, catalog = _build_payload(parsed, assignment)
    if len(payload) != required:  # pragma: no cover - internal invariant
        raise ValueError('internal payload size mismatch')
    return {
        'schema_version': SCHEMA_VERSION,
        'kind': PACK_KIND,
        'width': width,
        'height': height,
        'tile_cols': tile_cols,
        'tile_rows': tile_rows,
        'payload_budget': budget,
        'payload_bytes': len(payload),
        'payload_base64': base64.b64encode(payload).decode('ascii'),
        'catalog': catalog,
    }


# --- unpack ----------------------------------------------------------------

def _require_int(value, label, low=None, high=None):
    if type(value) is not int:
        raise ValueError(f'{label} must be an integer')
    if low is not None and value < low:
        raise ValueError(f'{label} is out of range')
    if high is not None and value > high:
        raise ValueError(f'{label} is out of range')
    return value


def _check_pack_fields(document):
    required = {'schema_version', 'width', 'height', 'payload_bytes',
                'payload_base64', 'catalog'}
    if not isinstance(document, dict):
        raise ValueError('pack must be a JSON object')
    missing = required - document.keys()
    if missing:
        # "At least" these fields: unknown extras are tolerated, but every
        # required field must be present.
        raise ValueError(f'pack is missing required fields {sorted(missing)}')
    if document.get('kind', PACK_KIND) != PACK_KIND:
        raise ValueError('unknown pack kind')
    version = document['schema_version']
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError('unsupported pack schema version')
    width = _require_int(document['width'], 'width', TILE, MAX_WIDTH)
    height = _require_int(document['height'], 'height', TILE, MAX_HEIGHT)
    if width % TILE or height % TILE:
        raise ValueError('pack width and height must be multiples of 8')
    if 'tile_cols' in document and document['tile_cols'] != width // TILE:
        raise ValueError('tile_cols does not match width')
    if 'tile_rows' in document and document['tile_rows'] != height // TILE:
        raise ValueError('tile_rows does not match height')
    if 'payload_budget' in document:
        _require_int(document['payload_budget'], 'payload_budget', 0, MAX_BUDGET)
    return width, height


def _decode_payload(document, width, height):
    encoded = document['payload_base64']
    if not isinstance(encoded, str):
        raise ValueError('payload_base64 must be a string')
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'invalid base64 payload: {exc}') from exc
    declared = _require_int(document['payload_bytes'], 'payload_bytes', 0)
    if declared != len(payload):
        raise ValueError(
            f'declared payload_bytes {declared} does not match {len(payload)} decoded bytes')
    return payload


def _parse_catalog(document):
    catalog = document['catalog']
    if not isinstance(catalog, list) or not catalog:
        raise ValueError('catalog must be a non-empty list')
    seen = set()
    previous = None
    for entry in catalog:
        if not isinstance(entry, dict):
            raise ValueError('catalog entries must be objects')
        required = ('id', 'direct_only', 'format', 'offset', 'length')
        if set(required) - entry.keys():
            raise ValueError('catalog entry is missing required fields')
        scene_id = entry['id']
        if not isinstance(scene_id, str) or not ID_RE.fullmatch(scene_id):
            raise ValueError('catalog scene id is invalid')
        if scene_id in seen:
            raise ValueError(f'duplicate catalog id {scene_id}')
        seen.add(scene_id)
        if previous is not None and not previous < scene_id:
            raise ValueError('catalog is not in ASCII scene-id order')
        previous = scene_id
        if type(entry['direct_only']) is not bool:
            raise ValueError('catalog direct_only must be a boolean')
        if entry['format'] not in (FORMAT_FULL, FORMAT_REFERENCE):
            raise ValueError('unknown record format')
        _require_int(entry['offset'], 'record offset', 0)
        _require_int(entry['length'], 'record length', 0)
        if entry['format'] == FORMAT_REFERENCE and entry['direct_only']:
            raise ValueError('a cold-start (direct_only) scene must be a full frame')
    return catalog


def _parse_records(document, width, height):
    tile_cols = width // TILE
    tile_rows = height // TILE
    block_count = tile_cols * tile_rows
    payload = _decode_payload(document, width, height)
    catalog = _parse_catalog(document)
    count = len(catalog)
    cursor = 0
    records = []
    for index, entry in enumerate(catalog):
        offset = entry['offset']
        length = entry['length']
        if offset != cursor:
            raise ValueError(f'catalog record {entry["id"]} has a hole or overlap at offset {offset}')
        if cursor + length > len(payload):
            raise ValueError(f'catalog record {entry["id"]} overruns the payload')
        chunk = payload[cursor:cursor + length]
        if entry['format'] == FORMAT_FULL:
            if length != block_count * TILE:
                raise ValueError(f'full frame {entry["id"]} has an invalid length')
            words = [bytes_to_word(chunk[i * TILE:(i + 1) * TILE])
                     for i in range(block_count)]
            records.append({'format': FORMAT_FULL, 'words': words})
        else:
            if length < 3:
                raise ValueError(f'reference frame {entry["id"]} is too short')
            base = chunk[0]
            changed = int.from_bytes(chunk[1:3], 'little')
            if length != 3 + 10 * changed:
                raise ValueError(f'reference frame {entry["id"]} has an invalid length')
            if base >= count:
                raise ValueError(f'reference frame {entry["id"]} names a missing base')
            masks = []
            previous = -1
            position = 3
            for _ in range(changed):
                block = int.from_bytes(chunk[position:position + 2], 'little')
                mask = chunk[position + 2:position + 10]
                if block >= block_count:
                    raise ValueError(f'reference frame {entry["id"]} changes a block outside the panel')
                if block <= previous:
                    raise ValueError(f'reference frame {entry["id"]} has unordered or duplicate blocks')
                if not any(mask):
                    raise ValueError(f'reference frame {entry["id"]} has an all-zero mask')
                previous = block
                masks.append((block, bytes_to_word(mask)))
                position += 10
            records.append({'format': FORMAT_REFERENCE, 'base': base, 'masks': masks})
        cursor += length
    if cursor != len(payload):
        raise ValueError('payload has trailing bytes not covered by the catalog')
    # A base must itself be a full frame; reference-to-reference is a chain.
    for entry, record in zip(catalog, records):
        if record['format'] == FORMAT_REFERENCE:
            if records[record['base']]['format'] != FORMAT_FULL:
                raise ValueError(
                    f'reference frame {entry["id"]} depends on another reference frame')
    words = []
    for record in records:
        if record['format'] == FORMAT_FULL:
            words.append(list(record['words']))
        else:
            base_words = records[record['base']]['words']
            frame = list(base_words)
            for block, mask in record['masks']:
                frame[block] = base_words[block] ^ mask
            words.append(frame)
    return width, height, catalog, words


def decode_pack(document):
    """Fully validate a pack and materialise every frame.

    Returns ``(width, height, catalog, frames)`` where ``frames`` holds one
    list of 64-bit tile words per catalog entry.  Any bad record -- even one
    no caller is interested in -- rejects the whole pack.
    """
    width, height = _check_pack_fields(document)
    return _parse_records(document, width, height)


def unpack_frame(document, scene_id):
    """Validate the whole pack and return one fully materialised frame."""
    width, height, catalog, frames = decode_pack(document)
    if not isinstance(scene_id, str):
        raise ValueError('scene id must be a string')
    for index, entry in enumerate(catalog):
        if entry['id'] == scene_id:
            break
    else:
        raise ValueError(f'unknown scene {scene_id}')
    tile_cols = width // TILE
    tile_rows = height // TILE
    tiles = []
    for ty in range(tile_rows):
        for tx in range(tile_cols):
            block = ty * tile_cols + tx
            tiles.append({'tx': tx, 'ty': ty,
                          'rows': word_to_rows(frames[index][block])})
    return {'schema_version': SCHEMA_VERSION, 'kind': FRAME_KIND,
            'id': scene_id, 'width': width, 'height': height, 'tiles': tiles}
