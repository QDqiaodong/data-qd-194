"""Turn two validated v1 layouts into the minimal atomic tile-write plan."""

import re

from .bitmap import TILE, Screen, tile_of, word_to_rows
from .checks import array, fields, integer

ID_PATTERN = r'[A-Za-z0-9_-]{1,64}'
MAX_WINDOWS = 128
MAX_PANEL = (256, 128)
_ID_RE = re.compile(ID_PATTERN)


def _release_ident(value, label):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f'{label} must use 1..64 ASCII letters, digits, underscore or hyphen')
    return value


def _validate_windows(windows, width, height):
    result = []
    array(windows, 0, MAX_WINDOWS)
    seen = set()
    for window in windows:
        fields(window, ('id', 'x0', 'y0', 'x1', 'y1'))
        wid = window['id']
        if not isinstance(wid, str) or not _ID_RE.fullmatch(wid):
            raise ValueError('window id must use 1..64 ASCII letters, digits, underscore or hyphen')
        if wid in seen:
            raise ValueError('duplicate protected window id')
        seen.add(wid)
        x0, y0, x1, y1 = (integer(window[name], 0, max(width, height)) for name in ('x0', 'y0', 'x1', 'y1'))
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError(f'window {wid} must be a non-empty rectangle inside the panel')
        result.append({'id': wid, 'rect': [x0, y0, x1, y1]})
    return result


def render_pixels(v1_input):
    """Real ink coordinates produced by the v1 layout/render pipeline.

    Reuses the original validation, kerning, wrapping and offset rules; the
    pixel set is a union, so drawing the same point twice lights one lamp and
    blank glyphs contribute no pixels.
    """
    from .layout import layout
    from .model import validate
    from .render import render
    glyphs, kerning, text = validate(v1_input)
    lines, placements = layout(glyphs, kerning, text, v1_input['line_width'], v1_input['line_height'])
    _bbox, _overflow, pixels, _svg = render(
        glyphs, placements, lines, v1_input['line_width'], v1_input['line_height'], text)
    return set(pixels)


def build_plan(source, release_id):
    """Validate the freeze request and construct the immutable write plan.

    Raises ValueError for any contract violation, including oversized groups.
    """
    fields(source, ('panel_id', 'width', 'height', 'max_tiles_per_write', 'old', 'new', 'protected_windows'))
    panel_id = _release_ident(source['panel_id'], 'panel_id')
    _release_ident(release_id, 'release_id')
    width = integer(source['width'], TILE, MAX_PANEL[0])
    height = integer(source['height'], TILE, MAX_PANEL[1])
    if width % TILE or height % TILE:
        raise ValueError('panel width and height must be positive multiples of 8')
    tile_count = (width // TILE) * (height // TILE)
    limit = integer(source['max_tiles_per_write'], 1, tile_count)
    if not isinstance(source['old'], dict) or not isinstance(source['new'], dict):
        raise ValueError('old and new must be complete v1 typesetting inputs')
    # Both layouts independently pass the original v1 validation.
    old_pixels = render_pixels(source['old'])
    new_pixels = render_pixels(source['new'])
    windows = _validate_windows(source['protected_windows'], width, height)
    old_screen = Screen(width, height, old_pixels)
    new_screen = Screen(width, height, new_pixels)

    changed = []
    old_map = {}
    new_map = {}
    for index, (old_word, new_word) in enumerate(zip(old_screen.tiles, new_screen.tiles)):
        tx, ty = index % old_screen.cols, index // old_screen.cols
        if old_word != new_word:
            changed.append((tx, ty))
            old_map[(tx, ty)] = old_word
            new_map[(tx, ty)] = new_word
    changed_set = set(changed)

    # A window joins the changed tiles whose real flipped pixels lie inside it.
    # A tile merely intersecting the window, or ink that stays lit in both
    # states, does not create a join.
    window_tiles = []
    join = {tile: set() for tile in changed}
    for window in windows:
        x0, y0, x1, y1 = window['rect']
        affected = set()
        for x, y in old_pixels.symmetric_difference(new_pixels):
            if x0 <= x < x1 and y0 <= y < y1:
                tile = tile_of(x, y)
                if tile in changed_set:
                    affected.add(tile)
        affected = sorted(affected)
        window_tiles.append({'id': window['id'], 'rect': window['rect'], 'tiles': affected})
        for tile in affected:
            join[tile].add(window['id'])

    # Union-find over the co-write constraints. Each connected component is a
    # minimal non-splittable write group; independent groups stay separate.
    parent = {tile: tile for tile in changed}

    def find(tile):
        root = tile
        while parent[root] != root:
            root = parent[root]
        while parent[tile] != root:
            parent[tile], tile = root, parent[tile]
        return root

    for item in window_tiles:
        members = item['tiles']
        if members:
            anchor = find(members[0])
            for tile in members[1:]:
                root = find(tile)
                if root != anchor:
                    parent[root] = anchor

    components = {}
    for tile in changed:
        components.setdefault(find(tile), []).append(tile)
    groups = [sorted(tiles) for tiles in components.values()]
    groups.sort()

    oversized = []
    for tiles in groups:
        if len(tiles) > limit:
            window_ids = sorted({wid for tile in tiles for wid in join[tile]})
            oversized.append({'tiles': tiles, 'window_ids': window_ids, 'size': len(tiles)})
    if oversized:
        oversized.sort(key=lambda item: item['tiles'])
        detail = '; '.join(
            f"tiles {item['tiles']} size {item['size']} > {limit} via windows {item['window_ids']}"
            for item in oversized)
        raise ValueError(f'write group exceeds max_tiles_per_write={limit}: {detail}')

    plan_groups = []
    for index, tiles in enumerate(groups):
        window_ids = sorted({wid for tile in tiles for wid in join[tile]})
        plan_groups.append({
            'group_id': f'G{index + 1:04d}',
            'tiles': [{'tx': tx, 'ty': ty,
                       'old_rows': word_to_rows(old_map[(tx, ty)]),
                       'new_rows': word_to_rows(new_map[(tx, ty)])} for tx, ty in tiles],
            'window_ids': window_ids,
        })

    return {
        'schema_version': 1,
        'kind': 'pixel-type.seal.plan',
        'release': {'release_id': release_id},
        'panel': {'panel_id': panel_id, 'width': width, 'height': height,
                  'tile_cols': width // TILE, 'tile_rows': height // TILE},
        'device': {'max_tiles_per_write': limit},
        'changed_tiles': [{'tx': tx, 'ty': ty} for tx, ty in changed],
        'protected_windows': window_tiles,
        'groups': plan_groups,
        'screens': {
            'old': {'tiles': old_screen.tile_records()},
            'new': {'tiles': new_screen.tile_records()},
        },
    }
