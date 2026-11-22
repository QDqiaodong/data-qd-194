"""Physical 8x8 tile bitmaps and screen-wide ink sets."""

TILE = 8


def bit_index(x, y):
    """Flat index inside an 8x8 tile, row-major from the top-left corner."""
    return y * TILE + (x & 7)


def tile_of(x, y):
    return (x >> 3, y >> 3)


def row_strings(bits):
    """Serialize a tile's 64 bits as eight 01 strings read left to right."""
    return [''.join('1' if bits[y * TILE + x] else '0' for x in range(TILE)) for y in range(TILE)]


class Screen:
    """Ink state of one physical panel stored as one 64-bit word per 8x8 tile."""

    __slots__ = ('width', 'height', 'cols', 'rows', 'tiles')

    def __init__(self, width, height, pixels=()):
        if width <= 0 or width % TILE or height <= 0 or height % TILE:
            raise ValueError('screen width and height must be positive multiples of 8')
        self.width = width
        self.height = height
        self.cols = width // TILE
        self.rows = height // TILE
        self.tiles = [0] * (self.cols * self.rows)
        for x, y in pixels:
            self.set_pixel(x, y)

    def set_pixel(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f'ink pixel ({x},{y}) falls outside the physical screen')
        self.tiles[(y >> 3) * self.cols + (x >> 3)] |= 1 << bit_index(x, y)

    def tile_records(self):
        """Deterministic, self-contained tile dump used inside frozen plans."""
        records = []
        for ty in range(self.rows):
            for tx in range(self.cols):
                word = self.tiles[ty * self.cols + tx]
                records.append({'tx': tx, 'ty': ty, 'rows': row_strings(_word_to_bits(word))})
        return records


def _word_to_bits(word):
    return [(word >> i) & 1 for i in range(TILE * TILE)]


def word_to_rows(word):
    return row_strings(_word_to_bits(word))


def rows_to_word(rows):
    """Parse eight 8-character 01 rows back into the canonical tile word."""
    if not isinstance(rows, list) or len(rows) != TILE or not all(isinstance(r, str) for r in rows):
        raise ValueError('tile rows must be eight 01 strings')
    word = 0
    for y, row in enumerate(rows):
        if len(row) != TILE or any(c not in '01' for c in row):
            raise ValueError('tile rows must be eight 8-bit 01 strings')
        for x, bit in enumerate(row):
            if bit == '1':
                word |= 1 << bit_index(x, y)
    return word
