"""离线字形装配（pixel-type 数据处理入口）。

输入一份装配清单（fragment 原料节点 + assembly 装配节点 + 绑定/排版字段），
输出单个自包含交付 JSON：

* ``layout_input`` 可原样送入 v1 排版（pixel_type.service.process）；
* ``provenance`` 记录交付字形中每个最终有效格（亮点或带来源清除）的状态、
  供稿方、原料节点 ID 与原料原始坐标，解读不依赖输入清单。

三态语义：``1`` 亮点、``0`` 带来源清除、``.`` 透明（无来源，不写入）。
装配按层从底到顶合成，只有非透明格覆盖下层；交付 v1 时透明与清除都转为 ``0``。

内存口径：大清单走 :mod:`pixel_type.json_stream` 流式解析，整份 JSON 解析树
从不同时驻留；每个绑定根私有三态画布缓存，处理完立即整体释放。

本模块只提供 Python 可调用入口（assemble / assemble_file / assemble_stream /
cell_source / iter_cells），不新增任何 CLI 子命令，原有排版接口与子命令行为
不变。
"""

import base64
import re

from .checks import array, fields, integer
from .io import parse_json, publish_bytes
from .model import character, validate as validate_v1
from .store import canonical_bytes

MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_NODES = 40000
MAX_FRAGMENTS = 2048
MAX_BINDINGS = 256
MAX_LAYERS = 4
MAX_PATH_NODES = 64
ID_RE = re.compile(r'[A-Za-z0-9_-]{1,32}\Z')

TRANSPARENT = 0
LIT = 1
CLEAR = 2

# 来源字（32 位小端，位段定义同时写入交付的 provenance.encoding）：
#   位  0-4  字形局部 x（5 位）
#   位  5-9  字形局部 y（5 位）
#   位 10    状态：1=亮点，0=带来源清除
#   位 11-15 原料节点内原始 x（5 位）
#   位 16-20 原料节点内原始 y（5 位）
#   位 21-31 原料表下标（11 位，0..2047）
MASK5 = 0x1F
MASK11 = 0x7FF
WORD_Y_SHIFT = 5
WORD_STATE_BIT = 10
WORD_FX_SHIFT = 11
WORD_FY_SHIFT = 16
WORD_FRAG_SHIFT = 21


class AssemblyError(ValueError):
    """清单结构、引用、放行或 v1 校验失败。"""


def _ident(value):
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise AssemblyError(
            '节点/供稿方 ID 须为 1-32 位 ASCII 字母、数字、下划线或连字符')
    return value


def _point2(value):
    if isinstance(value, list):
        array(value, 2, 2)
    else:
        value = _scalar_array(value, 2)
    return integer(value[0], -128, 128), integer(value[1], -128, 128)


def _window(value):
    if isinstance(value, list):
        array(value, 4, 4)
    else:
        value = _scalar_array(value, 4)
    x0, y0, x1, y1 = (integer(v, -32, 32) for v in
                      (value[0], value[1], value[2], value[3]))
    if not (x0 < x1 and y0 < y1):
        raise AssemblyError('src 必须是非空半开窗')
    return x0, y0, x1, y1


def _dst(value):
    if isinstance(value, list):
        array(value, 2, 2)
    else:
        value = _scalar_array(value, 2)
    return integer(value[0], -31, 31), integer(value[1], -31, 31)


def _scalar_array(cursor, expected):
    """把流式数组游标排空为标量列表（src/dst/offset 等小数组）。"""
    from .json_stream import ArrayCursor, ObjectCursor, _END
    if not isinstance(cursor, ArrayCursor):
        raise AssemblyError('期望数组值')
    result = []
    while True:
        value = cursor.next_value()
        if value is _END:
            break
        if isinstance(value, (ObjectCursor, ArrayCursor)):
            raise AssemblyError('嵌套容器不能出现在坐标数组中')
        result.append(value)
    if len(result) != expected:
        raise AssemblyError(f'数组长度必须恰好为 {expected}')
    return result


# ---------------------------------------------------------------------------
# 规范化（紧凑并行数组）
#
# kind[i]             1=fragment 2=assembly
# dims[i]             (width, height)
# fragment_supplier[i] / fragment_rows[i]
# assembly_layers[i]  ((ref 下标,x0,y0,x1,y1,dx,dy), ...)
# ---------------------------------------------------------------------------

def _empty_storage():
    return (bytearray(MAX_NODES), [None] * MAX_NODES, [None] * MAX_NODES,
            [None] * MAX_NODES, [None] * MAX_NODES, {})


def _store_fragment(storage, index, node_id, width, height, supplier, rows):
    kind, dims, f_supplier, f_rows, _layers, id_to_index = storage
    kind[index] = 1
    dims[index] = (width, height)
    f_supplier[index] = supplier
    if len(rows) != height:
        raise AssemblyError(f'fragment {node_id} 行数必须恰好等于 height')
    packed = bytearray(width * height)
    for row_index, row in enumerate(rows):
        if len(row) != width:
            raise AssemblyError(
                f'fragment {node_id} 行宽必须恰好等于 width')
        packed[row_index * width:(row_index + 1) * width] = row
    f_rows[index] = bytes(packed)


def _store_assembly(storage, index, width, height, layers):
    kind, dims, _fs, _fr, a_layers, _id_map = storage
    kind[index] = 2
    dims[index] = (width, height)
    a_layers[index] = layers


def _normalize(manifest):
    """已解析字典 → 紧凑结构；用于小清单、单元测试与直接 API 调用。"""
    if not isinstance(manifest, dict):
        raise AssemblyError('清单顶层必须是对象')
    fields(manifest, ('nodes', 'bindings', 'approved_suppliers', 'text',
                      'kerning', 'line_width', 'line_height'))

    approved_set = _approved_from_list(manifest['approved_suppliers'])
    raw_nodes = array(manifest['nodes'], 1, MAX_NODES)
    storage = _empty_storage()
    id_to_index = storage[5]
    fragment_count = 0
    for index, node in enumerate(raw_nodes):
        fields(node, ('id', 'width', 'height', 'kind'),
               ('supplier', 'rows', 'layers'))
        node_id = _ident(node['id'])
        if node_id in id_to_index:
            raise AssemblyError(f'节点 ID 重复: {node_id}')
        id_to_index[node_id] = index
        width = integer(node['width'], 1, 32)
        height = integer(node['height'], 1, 32)
        if node['kind'] == 'fragment':
            fragment_count += 1
            if fragment_count > MAX_FRAGMENTS:
                raise AssemblyError(f'原料节点超过 {MAX_FRAGMENTS} 个')
            if 'layers' in node or 'supplier' not in node or 'rows' not in node:
                raise AssemblyError('fragment 只能含 supplier/rows')
            supplier = _ident(node['supplier'])
            rows = node['rows']
            if not isinstance(rows, list):
                raise AssemblyError(f'fragment {node_id} rows 必须是数组')
            encoded = []
            for row in rows:
                if not isinstance(row, str) or any(c not in '01.' for c in row):
                    raise AssemblyError(
                        f'fragment {node_id} 每行必须是 width 个 0/1/. 字符')
                encoded.append(row.encode('ascii'))
            _store_fragment(storage, index, node_id, width, height,
                            supplier, encoded)
        elif node['kind'] == 'assembly':
            if 'supplier' in node or 'rows' in node or 'layers' not in node:
                raise AssemblyError('assembly 只能含 layers')
            layers = []
            for layer in array(node['layers'], 1, MAX_LAYERS):
                fields(layer, ('ref', 'src', 'dst'))
                layers.append((_ident(layer['ref']),)
                              + _window(layer['src']) + _dst(layer['dst']))
            _store_assembly(storage, index, width, height, layers)
        else:
            raise AssemblyError(f'节点 {node_id} 的 kind 只能是 fragment/assembly')
    node_count = len(raw_nodes)

    return _finalize(
        storage, node_count,
        _normalize_bindings(manifest['bindings'], id_to_index),
        manifest['kerning'], manifest['text'], manifest['line_width'],
        manifest['line_height'], approved_set)


def _normalize_bindings(raw_bindings, id_to_index):
    raw_bindings = array(raw_bindings, 1, MAX_BINDINGS)
    bindings = []
    bound = set()
    for binding in raw_bindings:
        fields(binding, ('char', 'node', 'advance', 'offset'))
        ch = character(binding['char'])
        if ch in bound:
            raise AssemblyError(f'绑定字符重复: {ch!r}')
        bound.add(ch)
        advance = integer(binding['advance'], 1, 128)
        ox, oy = _point2(binding['offset'])
        bindings.append({'char': ch, 'node_ref': _ident(binding['node']),
                         'advance': advance, 'offset': [ox, oy]})
    return bindings


def _approved_from_list(value):
    approved = array(value, 0, MAX_NODES)
    approved_set = set()
    for supplier in approved:
        _ident(supplier)
        if supplier in approved_set:
            raise AssemblyError('approved_suppliers 存在重复供稿方')
        approved_set.add(supplier)
    return approved_set


def _finalize(storage, node_count, raw_bindings, kerning, text, line_width,
              line_height, approved_set):
    """ref 解析、图校验、绑定/文字/v1 前置校验与原料排序，两条路径共用。"""
    kind, dims, f_supplier, f_rows, a_layers, id_to_index = storage[:6]
    for index in range(node_count):
        if kind[index] != 2:
            continue
        resolved = []
        for ref, x0, y0, x1, y1, dx, dy in a_layers[index]:
            ref_index = id_to_index.get(ref)
            if ref_index is None:
                raise AssemblyError(f'装配节点下标 {index} 缺少引用: {ref}')
            ref_width, ref_height = dims[ref_index]
            if not (0 <= x0 and 0 <= y0 and x1 <= ref_width and y1 <= ref_height):
                raise AssemblyError(
                    f'节点 {ref} 的裁窗越界: [{x0},{y0},{x1},{y1}]，'
                    f'引用画布为 {ref_width}x{ref_height}')
            resolved.append((ref_index, x0, y0, x1, y1, dx, dy))
        a_layers[index] = tuple(resolved)

    records = (kind[:node_count], f_supplier[:node_count],
               f_rows[:node_count], a_layers[:node_count])
    dims = dims[:node_count]
    _validate_graph(records)

    if not isinstance(text, str) or not 1 <= len(text) <= 2000:
        raise AssemblyError('text 须为 1..2000 个字符的字符串')
    used_chars = {ch for ch in text if ch != '\n'}
    for ch in used_chars:
        character(ch)

    # 两条规范化路径都已把绑定压成 {'char','node_ref','advance','offset'}。
    array(raw_bindings, 1, MAX_BINDINGS)
    bindings = []
    bound_chars = set()
    for binding in raw_bindings:
        ch = binding['char']
        if ch in bound_chars:
            raise AssemblyError(f'绑定字符重复: {ch!r}')
        bound_chars.add(ch)
        node_index = id_to_index.get(binding['node_ref'])
        if node_index is None:
            raise AssemblyError(
                f"绑定 {ch!r} 引用了不存在的节点: {binding['node_ref']}")
        bindings.append({'char': ch, 'node_index': node_index,
                         'advance': binding['advance'],
                         'offset': binding['offset']})
    missing = used_chars - bound_chars
    extra = bound_chars - used_chars
    if missing or extra:
        raise AssemblyError(
            f'绑定须与 text 非换行字符一一对应；缺绑 {sorted(missing)}，'
            f'多绑 {sorted(extra)}')

    integer(line_width, 1, 10000)
    integer(line_height, 1, 1000)
    if not isinstance(kerning, list) or not 0 <= len(kerning) <= 2000:
        raise AssemblyError('kerning 须为 0..2000 条记录的数组')

    index_to_id = {pos: node_id for node_id, pos in id_to_index.items()}
    fragment_order = sorted(
        (index for index in range(node_count) if records[0][index] == 1),
        key=lambda index: index_to_id[index])
    fragment_index = {index: pos for pos, index in enumerate(fragment_order)}

    return (records, dims, id_to_index, fragment_order, fragment_index,
            bindings, kerning, text, line_width, line_height, approved_set)


def normalize_stream(stream):
    """流式读取清单字节并直接紧凑化，整份解析树从不同时驻留内存。

    顶层各节按出现顺序即时排空（容器游标不跨键保留），因此任意时刻只有
    正在读取的单个节点以解析对象形式存在。
    """
    from .json_stream import ArrayCursor, ObjectCursor, StreamingReader
    reader = StreamingReader(stream)
    top = reader.consume()
    if not isinstance(top, ObjectCursor):
        raise AssemblyError('清单顶层必须是对象')

    storage = _empty_storage()
    id_to_index = storage[5]
    section_seen = set()
    approved_set = None
    bindings = None
    kerning = None
    text = line_width = line_height = None
    fragment_count = 0
    node_count = 0

    while True:
        key = top.next_key()
        if key is None:
            break
        if key in section_seen:
            raise AssemblyError(f'清单顶层重复键: {key}')
        section_seen.add(key)
        cursor = top.value()
        if key == 'approved_suppliers':
            if not isinstance(cursor, ArrayCursor):
                raise AssemblyError('approved_suppliers 必须是数组')
            approved_set = _drain_approved(cursor)
        elif key == 'nodes':
            if not isinstance(cursor, ArrayCursor):
                raise AssemblyError('nodes 必须是数组')
            node_count, fragment_count = _drain_nodes(
                cursor, storage, fragment_count)
        elif key == 'bindings':
            if not isinstance(cursor, ArrayCursor):
                raise AssemblyError('bindings 必须是数组')
            bindings = _read_bindings(cursor, id_to_index)
        elif key == 'kerning':
            kerning = _read_kerning(cursor)
        elif key == 'text':
            text = cursor
        elif key == 'line_width':
            line_width = cursor
        elif key == 'line_height':
            line_height = cursor
        else:
            raise AssemblyError(f'清单顶层未知字段: {key}')

    required = {'nodes', 'bindings', 'approved_suppliers', 'text',
                'kerning', 'line_width', 'line_height'}
    if section_seen != required:
        raise AssemblyError(f'清单顶层字段须恰好为 {sorted(required)}')
    if node_count < 1:
        raise AssemblyError('nodes 至少 1 个节点')

    return _finalize(storage, node_count, bindings, kerning, text,
                     line_width, line_height, approved_set)


def _drain_approved(cursor):
    from .json_stream import _END
    approved_set = set()
    while True:
        supplier = cursor.next_value()
        if supplier is _END:
            break
        _ident(supplier)
        if supplier in approved_set:
            raise AssemblyError('approved_suppliers 存在重复供稿方')
        approved_set.add(supplier)
    if len(approved_set) > MAX_NODES:
        raise AssemblyError(f'approved_suppliers 至多 {MAX_NODES} 项')
    return approved_set


def _drain_nodes(cursor, storage, fragment_count):
    from .json_stream import ObjectCursor, _END
    id_to_index = storage[5]
    node_count = 0
    while True:
        node_cursor = cursor.next_value()
        if node_cursor is _END:
            return node_count, fragment_count
        if node_count >= MAX_NODES:
            raise AssemblyError(f'节点数超过 {MAX_NODES}')
        if not isinstance(node_cursor, ObjectCursor):
            raise AssemblyError('nodes 元素必须是对象')
        index = node_count
        node_count += 1
        node_fields = {}
        while True:
            node_key = node_cursor.next_key()
            if node_key is None:
                break
            if node_key in node_fields:
                raise AssemblyError(f'节点重复键: {node_key}')
            value_cursor = node_cursor.value()
            # 容器值必须在读下一个键前排空；直接紧凑化，不保留解析对象。
            if node_key == 'rows':
                node_fields[node_key] = _read_rows(value_cursor, '<stream>')
            elif node_key == 'layers':
                node_fields[node_key] = _read_layers(value_cursor)
            else:
                node_fields[node_key] = value_cursor
        pending = node_fields
        if not set(pending) >= {'id', 'width', 'height', 'kind'}:
            raise AssemblyError('节点缺少 id/width/height/kind')
        node_id = _ident(pending['id'])
        if node_id in id_to_index:
            raise AssemblyError(f'节点 ID 重复: {node_id}')
        id_to_index[node_id] = index
        width = integer(pending['width'], 1, 32)
        height = integer(pending['height'], 1, 32)
        node_kind = pending['kind']
        if node_kind == 'fragment':
            fragment_count += 1
            if fragment_count > MAX_FRAGMENTS:
                raise AssemblyError(f'原料节点超过 {MAX_FRAGMENTS} 个')
            if set(pending) != {'id', 'width', 'height', 'kind',
                                'supplier', 'rows'}:
                raise AssemblyError('fragment 字段不合法')
            _store_fragment(
                storage, index, node_id, width, height,
                _ident(pending['supplier']), pending['rows'])
        elif node_kind == 'assembly':
            if set(pending) != {'id', 'width', 'height', 'kind', 'layers'}:
                raise AssemblyError('assembly 字段不合法')
            _store_assembly(storage, index, width, height, pending['layers'])
        else:
            raise AssemblyError(
                f'节点 {node_id} 的 kind 只能是 fragment/assembly')


def _read_rows(cursor, node_id):
    from .json_stream import ArrayCursor, _END
    if not isinstance(cursor, ArrayCursor):
        raise AssemblyError(f'fragment {node_id} rows 必须是数组')
    rows = []
    while True:
        row = cursor.next_value()
        if row is _END:
            return rows
        if not isinstance(row, str) or any(c not in '01.' for c in row):
            raise AssemblyError(
                f'fragment {node_id} 每行必须是仅含 0/1/. 的字符串')
        rows.append(row.encode('ascii'))


def _read_layers(cursor):
    from .json_stream import ArrayCursor, ObjectCursor, _END
    if not isinstance(cursor, ArrayCursor):
        raise AssemblyError('layers 必须是数组')
    layers = []
    while True:
        layer_cursor = cursor.next_value()
        if layer_cursor is _END:
            break
        if not isinstance(layer_cursor, ObjectCursor):
            raise AssemblyError('layer 必须是对象')
        seen = set()
        values = {}
        while True:
            layer_key = layer_cursor.next_key()
            if layer_key is None:
                break
            if layer_key in seen:
                raise AssemblyError(f'layer 重复键: {layer_key}')
            seen.add(layer_key)
            value_cursor = layer_cursor.value()
            if layer_key == 'src':
                values[layer_key] = _scalar_array(value_cursor, 4)
            elif layer_key == 'dst':
                values[layer_key] = _scalar_array(value_cursor, 2)
            else:
                values[layer_key] = value_cursor
        if seen != {'ref', 'src', 'dst'}:
            raise AssemblyError('layer 必须恰好含 ref/src/dst')
        if len(layers) >= MAX_LAYERS:
            raise AssemblyError(f'每个装配至多 {MAX_LAYERS} 个层')
        layers.append((_ident(values['ref']),) + _window(values['src'])
                      + _dst(values['dst']))
    if not layers:
        raise AssemblyError('assembly 至少 1 个层')
    return layers


def _read_bindings(cursor, id_to_index):
    from .json_stream import ArrayCursor, ObjectCursor, _END
    if not isinstance(cursor, ArrayCursor):
        raise AssemblyError('bindings 必须是数组')
    bindings = []
    while True:
        binding_cursor = cursor.next_value()
        if binding_cursor is _END:
            break
        if not isinstance(binding_cursor, ObjectCursor):
            raise AssemblyError('binding 必须是对象')
        seen = set()
        values = {}
        while True:
            binding_key = binding_cursor.next_key()
            if binding_key is None:
                break
            if binding_key in seen:
                raise AssemblyError(f'binding 重复键: {binding_key}')
            seen.add(binding_key)
            value_cursor = binding_cursor.value()
            if binding_key == 'offset':
                values[binding_key] = _scalar_array(value_cursor, 2)
            else:
                values[binding_key] = value_cursor
        if seen != {'char', 'node', 'advance', 'offset'}:
            raise AssemblyError('binding 必须恰好含 char/node/advance/offset')
        ch = character(values['char'])
        advance = integer(values['advance'], 1, 128)
        ox, oy = _point2(values['offset'])
        # 清单允许前向引用，节点 ID 在 _finalize 拿到完整映射后再解析。
        bindings.append({'char': ch, 'node_ref': _ident(values['node']),
                         'advance': advance, 'offset': [ox, oy]})
    return bindings


def _read_kerning(cursor):
    from .json_stream import ArrayCursor, ObjectCursor, _END
    if not isinstance(cursor, ArrayCursor):
        raise AssemblyError('kerning 必须是数组')
    kerning = []
    while True:
        pair_cursor = cursor.next_value()
        if pair_cursor is _END:
            break
        if not isinstance(pair_cursor, ObjectCursor):
            raise AssemblyError('kerning 元素必须是对象')
        seen = set()
        values = {}
        while True:
            pair_key = pair_cursor.next_key()
            if pair_key is None:
                break
            if pair_key in seen:
                raise AssemblyError(f'kerning 重复键: {pair_key}')
            seen.add(pair_key)
            values[pair_key] = pair_cursor.value()
        if seen != {'left', 'right', 'adjust'}:
            raise AssemblyError('kerning 元素必须恰好含 left/right/adjust')
        kerning.append({'left': character(values['left']),
                        'right': character(values['right']),
                        'adjust': values['adjust']})
    return kerning


# ---------------------------------------------------------------------------
# 图校验与三态合成
# ---------------------------------------------------------------------------

def _validate_graph(records):
    """全量迭代 DFS：查环与最长路径（含两端最多 64 个节点）。"""
    kind, _suppliers, _rows, assembly_layers = records
    node_count = len(kind)
    color = bytearray(node_count)
    depth = [0] * node_count
    for start in range(node_count):
        if color[start] != 0:
            continue
        stack = [(start, 0)]
        while stack:
            node, step = stack.pop()
            if kind[node] == 1:
                color[node] = 2
                depth[node] = 1
                continue
            layers = assembly_layers[node]
            if step == 0:
                if color[node] != 0:
                    continue
                color[node] = 1
                stack.append((node, 1))
                for layer in layers:
                    ref = layer[0]
                    if color[ref] == 1:
                        raise AssemblyError(f'装配图存在环，涉及节点下标 {ref}')
                    if color[ref] == 0:
                        stack.append((ref, 0))
            else:
                longest = 1 + max(depth[layer[0]] for layer in layers)
                if longest > MAX_PATH_NODES:
                    raise AssemblyError(
                        f'节点下标 {node} 的引用路径超过 {MAX_PATH_NODES} 个节点')
                depth[node] = longest
                color[node] = 2


def _reachable_order(records, roots):
    """从绑定根出发的迭代后序（图已证明是 DAG），仅含可达节点。"""
    kind, _suppliers, _rows, assembly_layers = records
    seen = set()
    queued = set(roots)
    order = []
    stack = [(root, False) for root in roots]
    while stack:
        node, expanded = stack.pop()
        if expanded:
            if node in seen:
                continue
            seen.add(node)
            order.append(node)
            continue
        stack.append((node, True))
        if kind[node] == 2:
            for layer in reversed(assembly_layers[node]):
                ref = layer[0]
                if ref not in queued:
                    queued.add(ref)
                    stack.append((ref, False))
    return order


def _fragment_raster(packed):
    return bytearray(
        TRANSPARENT if cell == 46 else LIT if cell == 49 else CLEAR
        for cell in packed)


def _build_fragment_cache(records):
    """一次性物化全部原料三态画布（最多 2048 × 1024 字节 ≈ 2 MiB）。

    原料画布只读，跨所有绑定共享，避免同一原料在不同字形中被重复解码；
    装配画布仍是各绑定私有、处理完即释放。
    """
    kind, _suppliers, fragment_rows, _layers = records
    return [_fragment_raster(fragment_rows[i]) if kind[i] == 1 else None
            for i in range(len(kind))]


def _evaluate(records, dims, node, states, fragment_cache):
    """深度优先按需合成 node 的三态画布，同一绑定内共享节点只合成一次。"""
    raster = states[node]
    if raster is not None:
        return raster
    kind, _suppliers, _fragment_rows, assembly_layers = records
    if kind[node] == 1:
        # 原料只读共享；绑定私有槽仍指向同一对象，释放时只清槽不破坏缓存。
        states[node] = fragment_cache[node]
        return fragment_cache[node]
    width, height = dims[node]
    canvas = bytearray(width * height)
    for ref_index, x0, y0, x1, y1, dx, dy in assembly_layers[node]:
        source = _evaluate(records, dims, ref_index, states, fragment_cache)
        ref_width, _ref_height = dims[ref_index]
        for sy in range(y0, y1):
            cy = dy + (sy - y0)
            if cy < 0 or cy >= height:
                continue
            src_row = sy * ref_width
            dst_row = cy * width
            for sx in range(x0, x1):
                value = source[src_row + sx]
                if value == TRANSPARENT:
                    continue
                cx = dx + (sx - x0)
                if 0 <= cx < width:
                    canvas[dst_row + cx] = value
    states[node] = canvas
    return canvas


def _release(states):
    """绑定根处理完后清空其私有装配画布槽（原料缓存独立，不受影响）。"""
    for index in range(len(states)):
        states[index] = None


def _trace(records, dims, states, fragment_cache, node, x, y):
    """把非透明格追溯到原料，返回 (状态, 原料节点下标, fx, fy)。

    来源指到最后实际写入的原料节点及原始坐标，而非中间节点或裁窗相对坐标。
    """
    kind, _suppliers, _rows, assembly_layers = records
    if kind[node] == 1:
        width, _height = dims[node]
        return fragment_cache[node][y * width + x], node, x, y
    for ref_index, x0, y0, x1, y1, dx, dy in reversed(assembly_layers[node]):
        lx = x - dx
        ly = y - dy
        if not (0 <= lx < x1 - x0 and 0 <= ly < y1 - y0):
            continue
        sx = x0 + lx
        sy = y0 + ly
        ref_width, _ref_height = dims[ref_index]
        source = (fragment_cache[ref_index] if kind[ref_index] == 1
                  else states[ref_index])
        if source[sy * ref_width + sx] != TRANSPARENT:
            return _trace(records, dims, states, fragment_cache,
                          ref_index, sx, sy)
    raise AssemblyError('内部错误：透明格不应进入来源追溯')


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def _assemble_normalized(normalized):
    (records, dims, id_to_index, fragment_order, fragment_index, bindings,
     kerning, text, line_width, line_height, approved_set) = normalized
    kind, fragment_supplier, _fragment_rows, _layers = records
    node_count = len(kind)
    index_to_id = {index: node_id for node_id, index in id_to_index.items()}
    roots = sorted({binding['node_index'] for binding in bindings})
    _reachable_order(records, roots)

    fragment_table = [
        {'id': index_to_id[index], 'supplier': fragment_supplier[index]}
        for index in fragment_order]

    glyphs_v1 = []
    provenance_glyphs = {}
    total_sourced = total_lit = total_clear = 0
    first_violation = None
    states = [None] * node_count
    fragment_cache = _build_fragment_cache(records)

    for binding in sorted(bindings, key=lambda item: item['char']):
        ch = binding['char']
        node = binding['node_index']
        width, height = dims[node]
        raster = _evaluate(records, dims, node, states, fragment_cache)
        rows = []
        words = bytearray()
        for y in range(height):
            row_chars = []
            base = y * width
            for x in range(width):
                value = raster[base + x]
                row_chars.append('1' if value == LIT else '0')
                if value == TRANSPARENT:
                    continue
                state, frag_node, fx, fy = _trace(
                    records, dims, states, fragment_cache, node, x, y)
                supplier = fragment_supplier[frag_node]
                total_sourced += 1
                if state == LIT:
                    total_lit += 1
                else:
                    total_clear += 1
                if first_violation is None and supplier not in approved_set:
                    first_violation = {
                        'char': ch, 'x': x, 'y': y,
                        'state': '1' if state == LIT else '0',
                        'supplier': supplier,
                        'fragment': index_to_id[frag_node],
                        'fragment_x': fx, 'fragment_y': fy}
                word = (x | (y << WORD_Y_SHIFT)
                        | ((1 if state == LIT else 0) << WORD_STATE_BIT)
                        | (fx << WORD_FX_SHIFT) | (fy << WORD_FY_SHIFT)
                        | (fragment_index[frag_node] << WORD_FRAG_SHIFT))
                words.extend(word.to_bytes(4, 'little'))
            rows.append(''.join(row_chars))
        glyphs_v1.append({'char': ch, 'advance': binding['advance'],
                          'offset': list(binding['offset']), 'rows': rows})
        provenance_glyphs[ch] = {
            'width': width, 'height': height,
            'cells': base64.b64encode(bytes(words)).decode('ascii')}
        _release(states)

    if first_violation is not None:
        bad = first_violation
        raise AssemblyError(
            f"未获批供稿方 {bad['supplier']!r} 的格在交付中生效：字符 "
            f"{bad['char']!r} 字形坐标 ({bad['x']},{bad['y']}) 状态 "
            f"{bad['state']}，原料节点 {bad['fragment']!r} 原始坐标 "
            f"({bad['fragment_x']},{bad['fragment_y']})")

    layout_input = {'glyphs': glyphs_v1, 'kerning': kerning, 'text': text,
                    'line_width': line_width, 'line_height': line_height}
    validate_v1(layout_input)

    return {
        'assembly_version': 1,
        'layout_input': layout_input,
        'provenance': {
            'encoding': {
                'format': 'base64-uint32le',
                'word_bytes': 4,
                'bits': {'x': '0-4 字形局部 x',
                         'y': '5-9 字形局部 y',
                         'state': '10 状态，1=亮点 0=带来源清除',
                         'fragment_x': '11-15 原料节点内原始 x',
                         'fragment_y': '16-20 原料节点内原始 y',
                         'fragment_index': '21-31 provenance.fragments 下标'},
                'cell_order': 'row_major（按 y 再按 x 升序）',
                'transparent': '透明格无来源，不出现在 cells 中',
                'alphabet': 'standard base64 (RFC 4648)'},
            'fragments': fragment_table,
            'glyphs': provenance_glyphs},
        'summary': {'nodes': node_count,
                    'fragments': len(fragment_order),
                    'assemblies': node_count - len(fragment_order),
                    'bindings': len(bindings),
                    'approved_suppliers': sorted(approved_set),
                    'sourced_cells': total_sourced,
                    'lit_cells': total_lit,
                    'clear_cells': total_clear}}


def assemble(manifest):
    """装配清单字典 → 自包含交付字典（纯内存，不做文件 IO）。

    结构/引用/放行/v1 校验的任何失败都抛 :class:`AssemblyError`。
    """
    return _assemble_normalized(_normalize(manifest))


def assemble_stream(stream):
    """从字节流流式装配，用于大清单的低峰值 RSS 路径。"""
    return _assemble_normalized(normalize_stream(stream))


def _decode_word(word):
    return {
        'x': word & MASK5,
        'y': (word >> WORD_Y_SHIFT) & MASK5,
        'state': '1' if (word >> WORD_STATE_BIT) & 1 else '0',
        'fragment_x': (word >> WORD_FX_SHIFT) & MASK5,
        'fragment_y': (word >> WORD_FY_SHIFT) & MASK5,
        'fragment_index': (word >> WORD_FRAG_SHIFT) & MASK11}


def iter_cells(delivery, char):
    """遍历某交付字形的全部有效格来源（透明格不产生），只读交付文件内容。"""
    glyph = delivery['provenance']['glyphs'][char]
    fragments = delivery['provenance']['fragments']
    raw = base64.b64decode(glyph['cells'])
    for offset in range(0, len(raw), 4):
        decoded = _decode_word(int.from_bytes(raw[offset:offset + 4], 'little'))
        fragment = fragments[decoded['fragment_index']]
        yield {'char': char,
               'x': decoded['x'], 'y': decoded['y'], 'state': decoded['state'],
               'supplier': fragment['supplier'], 'fragment': fragment['id'],
               'fragment_x': decoded['fragment_x'],
               'fragment_y': decoded['fragment_y']}


def cell_source(delivery, char, x, y):
    """按字符与字形局部坐标查逐格来源；透明格或坐标外返回 None。"""
    glyph = delivery['provenance']['glyphs'].get(char)
    if glyph is None or not (0 <= x < glyph['width'] and
                             0 <= y < glyph['height']):
        return None
    for cell in iter_cells(delivery, char):
        if cell['x'] == x and cell['y'] == y:
            return cell
    return None


def load_manifest(path):
    """读取 ≤32 MiB 清单并严格解析（重复 JSON 键拒绝、非有限数拒绝）。"""
    with open(path, 'rb') as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise AssemblyError('清单超过 32 MiB 上限')
    return parse_json(raw)


def assemble_file(source_path, output_path):
    """读清单 → 流式装配 → 原子新建交付文件，绝不覆盖已有同名文件/符号链接。

    坏输入、不能放行或写入失败时不留下半份交付，也不改动已有同名文件。
    返回交付字典（与落盘字节同源）。
    """
    import os
    size = os.path.getsize(source_path)
    if size > MAX_MANIFEST_BYTES:
        raise AssemblyError('清单超过 32 MiB 上限')
    with open(source_path, 'rb') as stream:
        delivery = assemble_stream(stream)
    publish_bytes(output_path, canonical_bytes(delivery))
    return delivery
