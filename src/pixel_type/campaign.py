"""Offline batch fit of approved signage messages to fixed display types.

Implements the v6 ``campaign-fit`` contract: every approved message is
typeset onto every display under the v1 advance/kerning/offset rules, but
line breaking is chosen globally per message x display combination instead
of by the v1 greedy wrap.  Everything is computed offline from the source
file bytes; nothing here touches devices, screens, packs or ledgers.
"""

import re

from .checks import array, fields, integer
from .model import character

ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')

MAX_LINE_HEIGHT = 1000
MAX_GLYPHS = 256
MAX_KERN_PAIRS = 2000
MAX_DISPLAYS = 16
MAX_MESSAGES = 32
MAX_MESSAGE_CODEPOINTS = 2000
MAX_TOTAL_CODEPOINTS = 8000
MAX_LINES = 128
MAX_WIDTH = 256
MAX_HEIGHT = 128
PIXEL_BUDGET = 200000  # the v1 render budget, applied per message

REASON_LIMITS = 'line_or_width_limit_exceeded'
REASON_INK = 'ink_out_of_bounds'

KIND = 'pixel-type.campaign-fit.report'


# ---------------------------------------------------------------- source validation

def _ident(value, label):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f'{label} must use 1..64 ASCII letters, digits, underscore or hyphen')
    return value


def _glyphs(records):
    """Validate glyph records under the exact v1 rules."""
    glyphs = {}
    for glyph in array(records, 1, MAX_GLYPHS):
        fields(glyph, ('char', 'advance', 'offset', 'rows'))
        ch = character(glyph['char'])
        if ch in glyphs:
            raise ValueError(f'duplicate glyph for {ch!r}')
        integer(glyph['advance'], 1, 128)
        for v in array(glyph['offset'], 2, 2):
            integer(v, -128, 128)
        rows = array(glyph['rows'], 0, 32)
        width = None
        for row in rows:
            if not isinstance(row, str) or not 1 <= len(row) <= 32 or any(c not in '01' for c in row):
                raise ValueError('bitmap row must be 1..32 bits')
            if width is not None and width != len(row):
                raise ValueError('bitmap rows must share width')
            width = len(row)
        glyphs[ch] = glyph
    return glyphs


def _kerning(records, glyphs):
    """Validate kerning records under the exact v1 rules."""
    kerning = {}
    for pair in array(records, 0, MAX_KERN_PAIRS):
        fields(pair, ('left', 'right', 'adjust'))
        left = character(pair['left'])
        right = character(pair['right'])
        if left not in glyphs or right not in glyphs or (left, right) in kerning:
            raise ValueError('unknown or duplicate kern pair')
        adjust = integer(pair['adjust'], -127, 127)
        if glyphs[right]['advance'] + adjust <= 0:
            raise ValueError('kerned pen advance must remain positive')
        kerning[(left, right)] = adjust
    return kerning


def _displays(records):
    displays = []
    seen = set()
    for record in array(records, 1, MAX_DISPLAYS):
        fields(record, ('id', 'width', 'height', 'safe_rect', 'max_lines'))
        did = _ident(record['id'], 'display id')
        if did in seen:
            raise ValueError(f'duplicate display id {did}')
        seen.add(did)
        width = integer(record['width'], 8, MAX_WIDTH)
        height = integer(record['height'], 8, MAX_HEIGHT)
        if width % 8 or height % 8:
            raise ValueError(f'display {did}: width and height must be positive multiples of 8')
        rect = record['safe_rect']
        fields(rect, ('x', 'y', 'width', 'height'))
        x = integer(rect['x'], 0, MAX_WIDTH)
        y = integer(rect['y'], 0, MAX_HEIGHT)
        w = integer(rect['width'], 1, MAX_WIDTH)
        h = integer(rect['height'], 1, MAX_HEIGHT)
        if x + w > width or y + h > height:
            raise ValueError(f'display {did}: safe_rect must be a non-empty rectangle inside the screen')
        max_lines = integer(record['max_lines'], 1, MAX_LINES)
        displays.append({'id': did, 'width': width, 'height': height,
                         'safe_rect': {'x': x, 'y': y, 'width': w, 'height': h},
                         'max_lines': max_lines})
    return displays


def _spans(records, text, mid):
    spans = []
    for span in array(records, 0, len(text)):
        fields(span, ('start', 'end'))
        start = integer(span['start'], 0, len(text))
        end = integer(span['end'], 0, len(text))
        if not start < end:
            raise ValueError(f'message {mid}: protected span must be a non-empty half-open interval')
        if '\n' in text[start:end]:
            raise ValueError(f'message {mid}: protected span must not contain a newline')
        spans.append((start, end))
    spans.sort()
    for (_, end_before), (start_after, _) in zip(spans, spans[1:]):
        if start_after < end_before:
            raise ValueError(f'message {mid}: protected spans must not overlap')
    return spans


def _messages(records, glyphs):
    messages = []
    seen = set()
    total = 0
    for record in array(records, 1, MAX_MESSAGES):
        fields(record, ('id', 'text', 'protected_spans'))
        mid = _ident(record['id'], 'message id')
        if mid in seen:
            raise ValueError(f'duplicate message id {mid}')
        seen.add(mid)
        text = record['text']
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_MESSAGE_CODEPOINTS:
            raise ValueError(f'message {mid}: 1..{MAX_MESSAGE_CODEPOINTS} text codepoints required')
        for ch in text:
            if ch != '\n':
                character(ch)
        if any(ch != '\n' and ch not in glyphs for ch in text):
            raise ValueError(f'message {mid}: missing glyph; no implicit fallback')
        total += len(text)
        if total > MAX_TOTAL_CODEPOINTS:
            raise ValueError(f'total message text over {MAX_TOTAL_CODEPOINTS} codepoints')
        spans = _spans(record['protected_spans'], text, mid)
        # The v1 render pixel budget, applied to each message independently.
        drawn = sum(sum(row.count('1') for row in glyphs[ch]['rows'])
                    for ch in text if ch != '\n')
        if drawn > PIXEL_BUDGET:
            raise ValueError(f'message {mid}: render pixel budget exceeded')
        messages.append({'id': mid, 'text': text, 'protected_spans': spans})
    return messages


def validate_source(data):
    """Validate a campaign-fit source document; raises ValueError on any defect."""
    fields(data, ('line_height', 'glyphs', 'kerning', 'displays', 'messages'))
    line_height = integer(data['line_height'], 1, MAX_LINE_HEIGHT)
    glyphs = _glyphs(data['glyphs'])
    kerning = _kerning(data['kerning'], glyphs)
    displays = _displays(data['displays'])
    messages = _messages(data['messages'], glyphs)
    return line_height, glyphs, kerning, displays, messages


# ---------------------------------------------------------------- derived message data

def _ink(glyph):
    """The actual '1' pixels of one glyph as (dx, dy) plus their extents."""
    dots = tuple((x, y) for y, row in enumerate(glyph['rows'])
                 for x, bit in enumerate(row) if bit == '1')
    if not dots:
        return None
    xs = [d[0] for d in dots]
    ys = [d[1] for d in dots]
    return {'dots': dots, 'x0': min(xs), 'x1': max(xs) + 1,
            'y0': min(ys), 'y1': max(ys) + 1}


def _derived(message, glyphs, kerning):
    """Precompute per-codepoint layout data shared by every display."""
    text = message['text']
    n = len(text)
    adv = [0] * n
    kern = [0] * n       # kern between i-1 and i; 0 across a newline boundary
    ox = [0] * n
    oy = [0] * n
    ink = [None] * n     # per-codepoint ink extents and dots; None when empty
    for i, ch in enumerate(text):
        if ch == '\n':
            continue
        glyph = glyphs[ch]
        adv[i] = glyph['advance']
        ox[i], oy[i] = glyph['offset']
        ink[i] = _ink(glyph)
        if i and text[i - 1] != '\n':
            kern[i] = kerning.get((text[i - 1], ch), 0)
    padv = [0] * (n + 1)
    pkern = [0] * (n + 1)
    for i in range(n):
        padv[i + 1] = padv[i] + adv[i]
        pkern[i + 1] = pkern[i] + kern[i]
    next_nl = [0] * (n + 1)
    next_nl[n] = n
    for p in range(n - 1, -1, -1):
        next_nl[p] = p if text[p] == '\n' else next_nl[p + 1]
    # An auto-break may sit between adjacent non-newline codepoints, but
    # never inside a protected span.  break_ok[i] allows a break after i.
    break_ok = [False] * n
    for i in range(n - 1):
        break_ok[i] = text[i] != '\n' and text[i + 1] != '\n'
    for start, end in message['protected_spans']:
        for i in range(start, end - 1):
            break_ok[i] = False
    # A line may end at e when e is the text end, a newline (explicit break),
    # or an allowed auto-break position.
    ok_end = [False] * (n + 1)
    for e in range(1, n + 1):
        ok_end[e] = e == n or text[e] == '\n' or break_ok[e - 1]
    prev_ok = [0] * (n + 1)
    last = 0
    for e in range(1, n + 1):
        if ok_end[e]:
            last = e
        prev_ok[e] = last
    return {'text': text, 'n': n, 'adv': adv, 'kern': kern, 'ox': ox, 'oy': oy,
            'ink': ink, 'padv': padv, 'pkern': pkern, 'next_nl': next_nl,
            'break_ok': break_ok, 'prev_ok': prev_ok}


def _line_advance(d, p, e):
    """Pen advance of the line covering codepoints [p, e)."""
    return (d['padv'][e] - d['padv'][p]) + (d['pkern'][e] - d['pkern'][p + 1])


def _min_lines(d, width):
    """Minimum line count with every line advance <= width, or None.

    Greedy farthest-reach is optimal: any legal breaking's first line ends no
    later than the greedy one, and the remainder is a suffix problem.
    """
    text = d['text']
    n = d['n']
    p = 0
    lines = 0
    while True:
        if p == n:
            return lines + 1
        if text[p] == '\n':
            p += 1
            lines += 1
            continue
        nl = d['next_nl'][p]
        lo, hi = p, nl
        while lo < hi:  # farthest end with advance <= width (monotone)
            mid = (lo + hi + 1) // 2
            if _line_advance(d, p, mid) <= width:
                lo = mid
            else:
                hi = mid - 1
        e = d['prev_ok'][lo]
        if e <= p:
            return None
        lines += 1
        if e == nl:
            if nl == n:
                return lines
            p = nl + 1
        else:
            p = e


def _min_widest(d):
    """Minimum over all legal breakings of the widest line advance."""
    lo = 0
    hi = d['padv'][d['n']] + d['pkern'][d['n']]  # every segment on one line
    while lo < hi:
        mid = (lo + hi) // 2
        if _min_lines(d, mid) is not None:
            hi = mid
        else:
            lo = mid + 1
    return lo


# ---------------------------------------------------------------- breaking search

def _relax(best, parents, q, cand_ragged, pred_seq, tail, ptr):
    """Keep the lexicographically smaller (raggedness, break sequence)."""
    cur = best[q]
    if cur is not None and cand_ragged > cur[0]:
        return
    cand_seq = pred_seq if tail is None else pred_seq + (tail,)
    if cur is not None and cand_ragged == cur[0] and cand_seq >= cur[1]:
        return
    best[q] = (cand_ragged, cand_seq)
    parents[q] = ptr


def _relax_done(done, cand_ragged, cand_seq, ptr):
    if done[0] is None or cand_ragged < done[0] or \
            (cand_ragged == done[0] and cand_seq < done[1]):
        done[0] = cand_ragged
        done[1] = cand_seq
        done[2] = ptr


def _fit(d, display, line_height):
    """The optimal fully legal breaking, or None when none exists.

    Level-by-level dynamic program: level l holds, for every text position,
    the best (raggedness, break sequence) way to typeset up to that position
    in exactly l lines with every line advance inside the safe width and
    every actual ink pixel inside the safe rectangle.  Ink bounds depend on
    the absolute line index, so the line count is part of the state.  The
    first level that completes the text is the minimum-line answer; ties
    break on raggedness, then on the break-index sequence.  Returns the line
    ranges as (start, end, kind) triples, or None.
    """
    text = d['text']
    n = d['n']
    safe = display['safe_rect']
    sw = safe['width']
    sh = safe['height']
    max_lines = display['max_lines']
    adv = d['adv']
    kern = d['kern']
    ink = d['ink']
    ox = d['ox']
    oy = d['oy']
    next_nl = d['next_nl']
    break_ok = d['break_ok']

    # Per position, the feasible line ends with their line-index windows:
    # (end, advance, min_level, max_level, tail), where tail is the
    # auto-break boundary index or None for a segment end.  Advance and the
    # horizontal ink span are level-independent, so they are scanned once
    # and cached; only the vertical ink window constrains the level.
    ends_cache = [None] * (n + 1)

    def line_ends(q):
        cached = ends_cache[q]
        if cached is not None:
            return cached
        nl = next_nl[q]
        advance = 0
        min_rx = max_rx = min_ry = max_ry = 0
        has_ink = False
        out = []
        for e in range(q + 1, nl + 1):
            t = e - 1
            advance += adv[t] + (kern[t] if t > q else 0)
            if advance > sw:
                break
            g = ink[t]
            if g is not None:
                pen = advance - adv[t]
                rx0 = pen + ox[t] + g['x0']
                rx1 = pen + ox[t] + g['x1']
                ry0 = oy[t] + g['y0']
                ry1 = oy[t] + g['y1']
                if has_ink:
                    if rx0 < min_rx:
                        min_rx = rx0
                    if rx1 > max_rx:
                        max_rx = rx1
                    if ry0 < min_ry:
                        min_ry = ry0
                    if ry1 > max_ry:
                        max_ry = ry1
                else:
                    min_rx, max_rx, min_ry, max_ry = rx0, rx1, ry0, ry1
                    has_ink = True
                # Every bound is monotone in e, so the first violation
                # ends the scan for this position.
                if min_rx < 0 or max_rx > sw:
                    break
            if has_ink:
                lmin = 0 if min_ry >= 0 else (-min_ry + line_height - 1) // line_height
                lmax = (sh - max_ry) // line_height
            else:
                lmin = 0
                lmax = max_lines  # no ink: the line index is unconstrained
            if e == nl:
                out.append((e, advance, lmin, lmax, None))
            elif break_ok[t]:
                out.append((e, advance, lmin, lmax, t))
        ends_cache[q] = out
        return out

    best = [None] * (n + 1)      # per position: (raggedness, break sequence)
    best[0] = (0, ())
    levels_parents = [[None] * (n + 1)]
    level = 0
    while level < max_lines:
        nxt = [None] * (n + 1)
        nxt_parent = [None] * (n + 1)
        done = [None, None, None]  # raggedness, sequence, parent pointer
        for q in range(n + 1):
            cur = best[q]
            if cur is None:
                continue
            rq, sq = cur
            if q == n:
                # Trailing empty segment: one more empty line finishes.
                _relax_done(done, rq, sq, (q, n, n, 'empty'))
                continue
            if text[q] == '\n':
                _relax(nxt, nxt_parent, q + 1, rq, sq, None, (q, q, q, 'empty'))
                continue
            for e, advance, lmin, lmax, tail in line_ends(q):
                if level < lmin or level > lmax:
                    continue
                if tail is None:
                    if e == n:
                        _relax_done(done, rq, sq, (q, q, n, 'segend'))
                    else:
                        _relax(nxt, nxt_parent, e + 1, rq, sq, None,
                               (q, q, e, 'segend'))
                else:
                    slack = sw - advance
                    _relax(nxt, nxt_parent, e, rq + slack * slack, sq, tail,
                           (q, q, e, 'auto'))
        if done[0] is not None:
            ranges = []
            ptr = done[2]
            lvl = level
            while ptr is not None:
                pred, start, end, kind = ptr
                ranges.append((start, end, kind))
                ptr = levels_parents[lvl][pred]
                lvl -= 1
            ranges.reverse()
            return ranges
        if all(v is None for v in nxt):
            return None
        levels_parents.append(nxt_parent)
        best = nxt
        level += 1
    return None


def _advance_layout(d, width):
    """The canonical advance-only optimal breaking, or None.

    Same selection rule as the eligible result -- minimum line count, then
    minimum raggedness, then lexicographically smallest break sequence --
    but ignoring ink bounds.  Used to explain ink failures with the exact
    layout the advance rules alone would have chosen.  Returns line ranges
    as (start, end, kind) triples, or None when no breaking keeps every
    line advance within width.
    """
    text = d['text']
    n = d['n']
    done = n + 1
    best = [None] * (n + 2)      # per position: (lines, raggedness, sequence)
    parents = [None] * (n + 2)
    best[0] = (0, 0, ())

    def relax(q, cand_lines, cand_ragged, pred_seq, tail, ptr):
        cur = best[q]
        if cur is not None and (cand_lines, cand_ragged) > (cur[0], cur[1]):
            return
        cand_seq = pred_seq if tail is None else pred_seq + (tail,)
        if cur is not None and (cand_lines, cand_ragged) == (cur[0], cur[1]) \
                and cand_seq >= cur[2]:
            return
        best[q] = (cand_lines, cand_ragged, cand_seq)
        parents[q] = ptr

    for q in range(n + 1):
        cur = best[q]
        if cur is None:
            continue
        lines_q, rag_q, seq_q = cur
        if q == n:
            relax(done, lines_q + 1, rag_q, seq_q, None, (q, n, n, 'empty'))
            continue
        if text[q] == '\n':
            relax(q + 1, lines_q + 1, rag_q, seq_q, None, (q, q, q, 'empty'))
            continue
        nl = d['next_nl'][q]
        advance = 0
        for e in range(q + 1, nl + 1):
            t = e - 1
            advance += d['adv'][t] + (d['kern'][t] if t > q else 0)
            if advance > width:
                break
            if e == nl:
                if nl == n:
                    relax(done, lines_q + 1, rag_q, seq_q, None, (q, q, n, 'segend'))
                else:
                    relax(nl + 1, lines_q + 1, rag_q, seq_q, None, (q, q, nl, 'segend'))
            elif d['break_ok'][t]:
                slack = width - advance
                relax(e, lines_q + 1, rag_q + slack * slack, seq_q, t, (q, q, e, 'auto'))
    if best[done] is None:
        return None
    ranges = []
    ptr = parents[done]
    while ptr is not None:
        pred, start, end, kind = ptr
        ranges.append((start, end, kind))
        ptr = parents[pred]
    ranges.reverse()
    return ranges


# ---------------------------------------------------------------- layout and ink

def _breaks_of(ranges):
    """Auto-break boundary indices: the codepoint before each chosen break."""
    return [end - 1 for start, end, kind in ranges if kind == 'auto']


def _layout(d, ranges, display, line_height):
    """Placements, per-line advances and the ink set of a chosen breaking."""
    safe = display['safe_rect']
    sx = safe['x']
    sy = safe['y']
    lines = []
    placements = []
    pixels = set()
    for li, (start, end, _kind) in enumerate(ranges):
        pen = 0
        indices = []
        for t in range(start, end):
            k = d['kern'][t] if t > start else 0
            x = pen + k
            origin = [sx + x + d['ox'][t], sy + li * line_height + d['oy'][t]]
            placements.append({'character_index': t, 'char': d['text'][t],
                               'line': li, 'pen_x': sx + x,
                               'applied_kerning': k, 'ink_origin': origin,
                               'advance': d['adv'][t]})
            g = d['ink'][t]
            if g is not None:
                for dx, dy in g['dots']:
                    pixels.add((origin[0] + dx, origin[1] + dy))
            pen = x + d['adv'][t]
            indices.append(t)
        lines.append({'advance': pen, 'character_indices': indices})
    if pixels:
        bbox = {'min': [min(x for x, y in pixels), min(y for x, y in pixels)],
                'max_exclusive': [max(x for x, y in pixels) + 1,
                                  max(y for x, y in pixels) + 1]}
    else:
        bbox = None
    return lines, placements, pixels, bbox


def _violations(d, ranges, display, line_height):
    """Out-of-bounds ink pixels of a breaking, in a deterministic order."""
    safe = display['safe_rect']
    sx = safe['x']
    sy = safe['y']
    sw = safe['width']
    sh = safe['height']
    width = display['width']
    height = display['height']
    bad = []
    for li, (start, end, _kind) in enumerate(ranges):
        pen = 0
        for t in range(start, end):
            g = d['ink'][t]
            if g is not None:
                k = d['kern'][t] if t > start else 0
                x = pen + k
                for dx, dy in g['dots']:
                    px = sx + x + d['ox'][t] + dx
                    py = sy + li * line_height + d['oy'][t] + dy
                    if not (sx <= px < sx + sw and sy <= py < sy + sh):
                        outside = 'safe_rect'
                        if not (0 <= px < width and 0 <= py < height):
                            outside = 'physical_screen'
                        bad.append({'character_index': t, 'x': px, 'y': py,
                                    'outside': outside})
            pen = pen + (d['kern'][t] if t > start else 0) + d['adv'][t]
    bad.sort(key=lambda entry: (entry['character_index'], entry['x'], entry['y']))
    return bad


# ---------------------------------------------------------------- report

def _fit_combination(d, display, line_height, widest):
    safe = display['safe_rect']
    min_lines = _min_lines(d, safe['width'])
    if min_lines is None or min_lines > display['max_lines']:
        return {'eligible': False,
                'reason': REASON_LIMITS,
                'details': {
                    'max_lines': display['max_lines'],
                    'safe_width': safe['width'],
                    'min_lines_within_safe_width': min_lines,
                    'min_widest_line_advance': widest()}}
    ranges = _fit(d, display, line_height)
    if ranges is None:
        advance_ranges = _advance_layout(d, safe['width'])
        return {'eligible': False,
                'reason': REASON_INK,
                'details': {
                    'breaks': _breaks_of(advance_ranges),
                    'out_of_bounds_ink': _violations(d, advance_ranges, display,
                                                   line_height)}}
    lines, placements, pixels, bbox = _layout(d, ranges, display, line_height)
    return {'eligible': True,
            'breaks': _breaks_of(ranges),
            'lines': lines,
            'placements': placements,
            'ink_bbox': bbox,
            'unique_ink_pixels': len(pixels)}


def build_report(data):
    """Validate the source and build the deterministic campaign-fit report."""
    line_height, glyphs, kerning, displays, messages = validate_source(data)
    results = []
    for message in messages:
        d = _derived(message, glyphs, kerning)
        widest = None

        def lazy_widest(d=d):
            nonlocal widest
            if widest is None:
                widest = _min_widest(d)
            return widest

        for display in displays:
            result = _fit_combination(d, display, line_height, lazy_widest)
            result['message_id'] = message['id']
            result['display_id'] = display['id']
            results.append(result)
    results.sort(key=lambda r: (r['message_id'], r['display_id']))
    eligible = sum(1 for r in results if r['eligible'])
    return {
        'schema_version': 1,
        'kind': KIND,
        'line_height': line_height,
        'displays': sorted(({'id': dsp['id'], 'width': dsp['width'],
                             'height': dsp['height'], 'safe_rect': dsp['safe_rect'],
                             'max_lines': dsp['max_lines']} for dsp in displays),
                           key=lambda dsp: dsp['id']),
        'messages': sorted(({'id': msg['id'], 'text': msg['text'],
                             'protected_spans': [{'start': s, 'end': e}
                                                 for s, e in msg['protected_spans']]}
                            for msg in messages),
                           key=lambda msg: msg['id']),
        'results': results,
        'summary': {'combinations': len(results), 'eligible': eligible,
                    'ineligible': len(results) - eligible},
    }
