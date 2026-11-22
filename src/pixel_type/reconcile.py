"""Reconcile one device readback against a frozen release plan."""

from .bitmap import TILE, rows_to_word
from .checks import fields

EVIDENCE_SAMPLE_LIMIT = 16
STATUS_ORDER = ('conflict', 'unconfirmed', 'ready', 'applied')


def parse_readback(data, panel_id, tile_cols, tile_rows):
    """Validate one readback sample and index its tile words.

    Partial reads are legal; unread tiles simply stay absent from the map.
    Raises ValueError on panel mismatch, duplicates, out-of-range or bad bitmaps.
    """
    fields(data, ('panel_id', 'tiles'))
    if data['panel_id'] != panel_id:
        raise ValueError(
            f"readback panel_id {data['panel_id']!r} does not match frozen panel {panel_id!r}")
    if not isinstance(data['tiles'], list):
        raise ValueError('readback tiles must be a list')
    read = {}
    for tile in data['tiles']:
        fields(tile, ('tx', 'ty', 'rows'))
        if type(tile['tx']) is not int or type(tile['ty']) is not int:
            raise ValueError('tile coordinates must be integers')
        tx, ty = tile['tx'], tile['ty']
        if not (0 <= tx < tile_cols and 0 <= ty < tile_rows):
            raise ValueError(f'readback tile ({tx},{ty}) is outside the panel')
        if (tx, ty) in read:
            raise ValueError(f'duplicate readback tile ({tx},{ty})')
        read[(tx, ty)] = rows_to_word(tile['rows'])
    return read


def parse_screen_plans(plan):
    def index(records):
        return {(record['tx'], record['ty']): rows_to_word(record['rows'])
                for record in records}

    return (plan['panel']['tile_cols'],
            index(plan['screens']['old']['tiles']),
            index(plan['screens']['new']['tiles']))


def _scope_tiles(group, window_rects):
    """Whole changed tiles plus the whole rectangle of every joining window."""
    tiles = {(item['tx'], item['ty']) for item in group['tiles']}
    for wid in group['window_ids']:
        x0, y0, x1, y1 = window_rects[wid]
        for ty in range(y0 // TILE, (y1 - 1) // TILE + 1):
            for tx in range(x0 // TILE, (x1 - 1) // TILE + 1):
                tiles.add((tx, ty))
    return sorted(tiles)


def _tile_evidence(tx, ty, read_word, old_word, new_word):
    """Pixels of one read tile that neither frozen interpretation can explain."""
    old_ok = read_word == old_word
    new_ok = read_word == new_word
    exclusions = []
    for bit in range(TILE * TILE):
        mask = 1 << bit
        observed = bool(read_word & mask)
        old_bit = bool(old_word & mask)
        new_bit = bool(new_word & mask)
        if observed != old_bit and observed != new_bit:
            exclusions.append({
                'x': tx * TILE + (bit & 7), 'y': ty * TILE + (bit >> 3),
                'observed': int(observed), 'frozen_old': int(old_bit),
                'frozen_new': int(new_bit),
                'reason': 'stable-pixel-drift' if old_bit == new_bit else 'mixed-state',
            })
    return old_ok, new_ok, exclusions


def build_report(plan, readback):
    _cols, old_map, new_map = parse_screen_plans(plan)
    read = parse_readback(readback, plan['panel']['panel_id'],
                          plan['panel']['tile_cols'], plan['panel']['tile_rows'])
    window_rects = {item['id']: item['rect'] for item in plan['protected_windows']}
    groups = []
    for group in plan['groups']:
        scope = _scope_tiles(group, window_rects)
        missing = []
        evidence = []
        state_evidence = []
        old_possible = True
        new_possible = True
        for tx, ty in scope:
            old_word = old_map[(tx, ty)]
            new_word = new_map[(tx, ty)]
            if (tx, ty) not in read:
                # An unread tile is unknown evidence, never an assumed blank.
                missing.append({'tx': tx, 'ty': ty, 'missing_pixels': TILE * TILE})
                continue
            old_ok, new_ok, exclusions = _tile_evidence(
                tx, ty, read[(tx, ty)], old_word, new_word)
            old_possible &= old_ok
            new_possible &= new_ok
            if exclusions:
                evidence.append({
                    'tx': tx, 'ty': ty, 'excluded_pixels': len(exclusions),
                    'samples': exclusions[:EVIDENCE_SAMPLE_LIMIT],
                })
            elif old_ok != new_ok:
                # A clean tile that matches exactly one frozen interpretation
                # is evidence of which state that part of the screen is in.
                state_evidence.append({
                    'tx': tx, 'ty': ty,
                    'matches': 'old' if old_ok else 'new'})
        if not old_possible and not new_possible:
            status = 'conflict'
            # When every read pixel individually matches old or new but no
            # single interpretation covers the whole scope (mixed states), the
            # per-tile matches are the locatable conflict evidence.
            if not evidence and state_evidence:
                evidence.append({'reason': 'mixed-state-tiles',
                                 'matching_tiles': state_evidence})
        elif missing:
            status = 'unconfirmed'
        elif new_possible:
            status = 'applied'
        else:
            status = 'ready'
        entry = {
            'group_id': group['group_id'],
            'status': status,
            'tiles': [{'tx': item['tx'], 'ty': item['ty']} for item in group['tiles']],
            'window_ids': list(group['window_ids']),
            'scope_tiles': [{'tx': tx, 'ty': ty} for tx, ty in scope],
            'missing_reads': missing,
            'excluding_evidence': evidence,
        }
        if status == 'ready':
            entry['next_write'] = {
                'tiles': [
                    {'tx': item['tx'], 'ty': item['ty'],
                     'target_rows': item['new_rows'],
                     'precondition_rows': item['old_rows']}
                    for item in group['tiles']
                ],
                'tile_count': len(group['tiles']),
            }
        groups.append(entry)

    return {
        'schema_version': 1,
        'kind': 'pixel-type.reconcile.report',
        'release': {'release_id': plan['release']['release_id'],
                    'request_sha256': plan['request']['sha256']},
        'panel': dict(plan['panel']),
        'readback': {'panel_id': readback['panel_id'],
                     'tiles_read': len(readback['tiles']),
                     'tiles_total': plan['panel']['tile_cols'] * plan['panel']['tile_rows']},
        'group_count': len(groups),
        'status_counts': {status: sum(1 for g in groups if g['status'] == status)
                          for status in STATUS_ORDER},
        'groups': groups,
    }
