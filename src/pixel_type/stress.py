"""离线压力样例生成器（可调用，不挂任何 CLI）。

生成满足装配清单契约的极限样例：

* 恰好 40000 个 32×32 节点，其中 2048 个原料节点（上限）；
* 全部 40000 个节点都能从本批 64 个绑定到达，不是靠无引用节点占数；
* 跨分支共用部件（同层部件被多个上层节点、底层被多个绑定重复引用）；
* 实际生效的裁窗、负位移/出界舍弃、带来源清除与透明透底；
* 未获批供稿只出现在被裁掉或被盖住的格里，交付可放行；
* 一条 64 节点（含两端）的最长引用链，贴合深度上限；
* 文字含非零字偶距与非零偏移，可直接驱动 v1 排版。

ID 使用紧凑写法（契约允许 1..32 位 ASCII 字母数字下划线/连字符），保证样例
本身落在 32 MiB 清单上限内。样例完全确定性：同参数生成逐字节一致。
"""

SIZE = 32
NODE_TOTAL = 40000
FRAGMENT_TOTAL = 2048
CHAIN_ASSEMBLIES = 63          # 加 1 个原料 = 路径恰好 64 个节点
SHARED_ASSEMBLIES = 256
ROOT_COUNT = 64                # 绑定数：链头 + GA + GB + 61 个森林根
STANDARD_ROOTS = ROOT_COUNT - 3
APPROVED = [f'S{i:02d}' for i in range(16)]
BAD_SUPPLIER = 'SX9'

# 森林各层（自根向下）规模：61 根，扇出 2，最底层为部分层并共享底层部件。
FOREST_LEVELS = [STANDARD_ROOTS, 122, 244, 488, 976, 1952, 3904,
                 7808, 15616, 6460]
# 2048 + 63 + 256 + 37414(森林) + GA + GB = 40000


def _fid(index):
    return f'f{index}'


def _hid(index):
    return f'h{index}'


def _fragment_rows(index):
    rows = []
    bad = index and index % 97 == 0      # f0 保持获批，供覆盖/裁剪特例使用
    for y in range(SIZE):
        cells = []
        for x in range(SIZE):
            if x == SIZE - 1 and y == SIZE - 1:
                # 未获批亮点：普通装配窗都不含坐标 31，特例节点负责盖住/裁掉。
                cells.append('1' if bad else '0')
            elif x == y:
                cells.append('.')               # 透明透底
            elif (x + y) % 5 == 0:
                cells.append('0')               # 带来源清除
            else:
                cells.append('1')
        rows.append(''.join(cells))
    return rows


def _fragments():
    nodes = []
    for index in range(FRAGMENT_TOTAL):
        supplier = BAD_SUPPLIER if (index and index % 97 == 0) \
            else f'S{index % 16:02d}'
        nodes.append({'id': _fid(index), 'kind': 'fragment',
                      'width': SIZE, 'height': SIZE,
                      'supplier': supplier, 'rows': _fragment_rows(index)})
    return nodes


def _chain():
    """k63 引用 f0，k1 为链头；路径含两端恰好 64 个节点。"""
    nodes = []
    for depth in range(CHAIN_ASSEMBLIES, 0, -1):
        if depth == CHAIN_ASSEMBLIES:
            ref, src, dst = _fid(0), [0, 0, 31, 31], [0, 0]
        else:
            ref = f'k{depth + 1}'
            # 每 7 个节点平移一次，制造持续生效的位移与出界舍弃。
            src, dst = [0, 0, 31, 31], ([1, 0] if depth % 7 == 0 else [0, 0])
        nodes.append({'id': f'k{depth}', 'kind': 'assembly',
                      'width': SIZE, 'height': SIZE,
                      'layers': [{'ref': ref, 'src': src, 'dst': dst}]})
    return nodes


def _shared():
    """256 个跨分支共用部件；h254 盖住未获批格，h255 把未获批格裁出画布。"""
    nodes = []
    for index in range(SHARED_ASSEMBLIES):
        if index == 254:
            layers = [
                {'ref': _fid(0), 'src': [0, 0, 32, 32], 'dst': [0, 0]},
                {'ref': _fid(1), 'src': [29, 29, 32, 32], 'dst': [29, 29]}]
        elif index == 255:
            # 单层全画布部件，供 GA/GB 经不同裁窗/位移共享；窗不含坐标 31，
            # 未获批坏格 (31,31) 被裁掉，不进入任何交付。
            layers = [
                {'ref': _fid(0), 'src': [0, 0, 31, 31], 'dst': [0, 0]}]
        else:
            dst = [28, 28] if index % 3 == 0 else [8, 8]   # 前者出界被裁
            layers = [
                {'ref': _fid((2 * index) % FRAGMENT_TOTAL),
                 'src': [0, 0, 31, 31], 'dst': [0, 0]},
                {'ref': _fid((2 * index + 1) % FRAGMENT_TOTAL),
                 'src': [4, 4, 20, 20], 'dst': dst}]
        nodes.append({'id': _hid(index), 'kind': 'assembly',
                      'width': SIZE, 'height': SIZE, 'layers': layers})
    return nodes


def _forest():
    """十层二叉森林，同层部件被多个父节点共享，全部节点自根可达。"""
    nodes = []
    windows = [([0, 0, 16, 16], [0, 0]), ([16, 16, 32, 32], [16, 16])]
    level_ids = [[f't{level}_{i}' for i in range(count)]
                 for level, count in enumerate(FOREST_LEVELS)]
    for level, ids in enumerate(level_ids):
        bottom = level == len(FOREST_LEVELS) - 1
        for ordinal, node_id in enumerate(ids):
            layers = []
            for branch, (src, dst) in enumerate(windows):
                use_src = list(src)
                if bottom:
                    if branch == 0:
                        # 256 个共享部件被底层节点跨分支重复引用（全部都用到）。
                        ref = _hid(ordinal % SHARED_ASSEMBLIES)
                    elif ordinal % 7 == 0:
                        # 全窗引用 h254：其第二层用获批亮点盖住 (31,31) 坏格，
                        # 保证右下角有效，同时覆盖“被盖住的未获批供稿不拦截”。
                        ref = _hid(254)
                    else:
                        # 直接引用全部 2048 个原料；窗不含坐标 31，
                        # 未获批坏格 (31,31) 随裁窗被舍弃，不进入交付。
                        ref = _fid((ordinal + branch) % FRAGMENT_TOTAL)
                        use_src = [16, 16, 31, 31]
                else:
                    child_ids = level_ids[level + 1]
                    ref = child_ids[(ordinal * 2 + branch) % len(child_ids)]
                use_dst = [-1, dst[1]] if ordinal % 5 == 0 and branch == 0 \
                    else list(dst)
                layers.append({'ref': ref, 'src': use_src, 'dst': use_dst})
            nodes.append({'id': node_id, 'kind': 'assembly',
                          'width': SIZE, 'height': SIZE, 'layers': layers})
    return nodes, level_ids


def _special_shared_users():
    """同一共享部件经不同裁窗/位移进入两个绑定，用来核对原始坐标不串位。

    h255 为单层部件（仅裁窗 + 位移，无二次覆盖），同一原料格经两个装配进入
    GA/GB 后必须仍指向相同的原料节点与原料原始坐标。
    """
    shared = _hid(255)
    return [
        {'id': 'GA', 'kind': 'assembly', 'width': SIZE, 'height': SIZE,
         'layers': [{'ref': shared, 'src': [0, 0, 16, 16],
                     'dst': [0, 0]}]},
        {'id': 'GB', 'kind': 'assembly', 'width': SIZE, 'height': SIZE,
         'layers': [{'ref': shared, 'src': [8, 8, 24, 24],
                     'dst': [4, 4]}]}]


def build_stress_manifest():
    """返回压力样例清单字典（40000 节点、64 绑定、含字偶距与偏移的文字）。"""
    nodes = _fragments() + _chain() + _shared()
    forest, level_ids = _forest()
    nodes.extend(forest)
    nodes.extend(_special_shared_users())
    if len(nodes) != NODE_TOTAL:
        raise AssertionError(f'压力样例节点数 {len(nodes)} != {NODE_TOTAL}')

    chars = ('ABCDEFGHIJKLMNOPQRSTUVWXYZ'
             'abcdefghijklmnopqrstuvwxyz0123456789!?')
    roots = ['k1', 'GA', 'GB'] + level_ids[0]
    if len(roots) != ROOT_COUNT or len(set(roots)) != ROOT_COUNT:
        raise AssertionError('压力样例根节点数量异常')
    bindings = [{'char': char, 'node': roots[index], 'advance': 8,
                 'offset': [index % 3, -(index % 2)]}
                for index, char in enumerate(chars)]

    text = chars * 3               # 192 个字符，全部绑定均被使用
    kerning = []
    seen_pairs = set()
    for left, right in zip(text, text[1:]):
        if (left, right) not in seen_pairs:
            seen_pairs.add((left, right))
            kerning.append({'left': left, 'right': right, 'adjust': -1})
    return {'nodes': nodes, 'bindings': bindings,
            'approved_suppliers': list(APPROVED), 'text': text,
            'kerning': kerning, 'line_width': 400, 'line_height': 40}
