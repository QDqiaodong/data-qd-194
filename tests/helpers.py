"""Deterministic v1 input builders for the offline freeze tests."""

import hashlib
import json


def glyph(char, rows, advance=None, offset=(0, 0)):
    return {'char': char, 'advance': advance if advance is not None else 8,
            'offset': list(offset), 'rows': list(rows)}


def v1_input(glyphs, text, line_width=256, line_height=8, kerning=()):
    return {'glyphs': list(glyphs), 'kerning': list(kerning), 'text': text,
            'line_width': line_width, 'line_height': line_height}


def dot_glyph(char, dots, advance=8):
    """A glyph whose '1' pixels are the given (x, y) coordinates inside 8x8."""
    rows = [['0'] * 8 for _ in range(8)]
    for x, y in dots:
        rows[y][x] = '1'
    return glyph(char, [''.join(row) for row in rows], advance=advance)


def source(panel_id, width, height, limit, old, new, windows):
    return {'panel_id': panel_id, 'width': width, 'height': height,
            'max_tiles_per_write': limit, 'old': old, 'new': new,
            'protected_windows': windows}


def window(wid, x0, y0, x1, y1):
    return {'id': wid, 'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1}


def write_bytes(path, document):
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True,
                     indent=2).encode('utf-8')
    with open(path, 'wb') as handle:
        handle.write(raw)
    return raw


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def dump_source(path, document):
    raw = write_bytes(path, document)
    return sha256_bytes(raw)


def tile_rows_from_pixels(pixels, tx, ty):
    rows = [['0'] * 8 for _ in range(8)]
    for x, y in pixels:
        if x >> 3 == tx and y >> 3 == ty:
            rows[y & 7][x & 7] = '1'
    return [''.join(row) for row in rows]


def readback(panel_id, tile_rows):
    """tile_rows: iterable of (tx, ty, rows)."""
    return {'panel_id': panel_id,
            'tiles': [{'tx': tx, 'ty': ty, 'rows': rows} for tx, ty, rows in tile_rows]}


def screen_tiles(plan, which):
    return {(t['tx'], t['ty']): t['rows'] for t in plan['screens'][which]['tiles']}
