#!/usr/bin/env python3
"""压力样例离线测量入口（独立 Linux CPython 进程，标准库，无新增 CLI 命令）。

用法：
  python -I bench/stress_bench.py generate --output run/stress-manifest.json
  python -I bench/stress_bench.py run --source run/stress-manifest.json \
      --delivery run/stress-delivery.json

run 子进程内部用 resource.getrusage(RUSAGE_SELF).ru_maxrss 报告峰值 RSS
（Linux 单位 KiB），并对交付内容做自动断言；失败时退出码非零。
"""

import argparse
import json
import resource
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from pixel_type.assembly import (assemble_file, cell_source,
                                 iter_cells, normalize_stream)
from pixel_type.service import process
from pixel_type.stress import (APPROVED, FOREST_LEVELS,
                               FRAGMENT_TOTAL, NODE_TOTAL, ROOT_COUNT,
                               build_stress_manifest)
from pixel_type.store import canonical_bytes


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _check_manifest_stream(source):
    """结构断言走装配端的流式规范化：不实例化整份 JSON 解析树。"""
    with open(source, 'rb') as stream:
        (records, dims, id_to_index, fragment_order, fragment_index,
         bindings, kerning, text, line_width, line_height,
         approved_set) = normalize_stream(stream)
    kind, suppliers, fragment_rows, layers = records
    _assert(len(kind) == NODE_TOTAL, '节点数必须为 40000')
    _assert(sum(1 for k in kind if k == 1) == FRAGMENT_TOTAL,
            '原料节点必须为 2048')
    _assert(len(bindings) == ROOT_COUNT, '绑定数必须为 64')
    _assert(len(id_to_index) == NODE_TOTAL, '节点 ID 必须唯一')
    roots = sorted({b['node_index'] for b in bindings})
    # 全部节点从绑定可达（在紧凑图上迭代 DFS）。
    seen = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if kind[node] == 2:
            stack.extend(layer[0] for layer in layers[node])
    _assert(seen == set(range(NODE_TOTAL)),
            '全部 40000 节点必须从本批绑定可达')
    # 最长路径恰好 64 个节点（含两端）。
    depth = {}

    def measure(node):
        if node in depth:
            return depth[node]
        if kind[node] == 1:
            depth[node] = 1
            return 1
        longest = 1 + max(measure(layer[0]) for layer in layers[node])
        depth[node] = longest
        return longest

    for root in roots:
        measure(root)
    _assert(max(depth.values()) == 64, '必须存在恰好 64 节点的引用路径')
    _assert(FOREST_LEVELS[0] == 61 and len(FOREST_LEVELS) == 10, '森林层级异常')
    return {'bindings': bindings, 'approved_suppliers': approved_set,
            'text': text, 'id_to_index': id_to_index}


def _check_delivery(info, delivery):
    layout_input = delivery['layout_input']
    glyphs = {g['char']: g for g in layout_input['glyphs']}
    _assert(len(glyphs) == ROOT_COUNT, '交付字形数必须为 64')

    # 未获批供稿只允许出现在被裁掉/盖住的格里：所有来源均须在批准名单。
    approved = set(info['approved_suppliers'])
    for char in glyphs:
        for cell in iter_cells(delivery, char):
            _assert(cell['supplier'] in approved,
                    f"未获批供稿方 {cell['supplier']} 出现在交付格 {char}")
    # 森林根（第 4 个绑定）在 (31,31) 必须可追溯到已获批原料且坐标在 0..31。
    root_char = info['bindings'][3]['char']
    corner = cell_source(delivery, root_char, 31, 31)
    _assert(corner is not None and corner['supplier'] in APPROVED,
            '右下角格必须是有效且获批的来源')
    _assert(0 <= corner['fragment_x'] <= 31 and 0 <= corner['fragment_y'] <= 31,
            '原料原始坐标必须落在 0..31')

    # 共享单层部件 h255 经不同裁窗/位移进入 GA/GB，原料原始坐标不得串位。
    index_to_id = {index: node_id
                   for node_id, index in info['id_to_index'].items()}
    node_of_char = {b['char']: index_to_id[b['node_index']]
                    for b in info['bindings']}
    char_ga = next(c for c, node in node_of_char.items() if node == 'GA')
    char_gb = next(c for c, node in node_of_char.items() if node == 'GB')
    # h255 是 f0 的 31×31 单层拷贝：GA 窗 (0..15)，GB 窗 (8..23)+位移 (4,4)，
    # 即 GB 画布上 (4..19) 才是窗口区域。
    ga_cell = cell_source(delivery, char_ga, 1, 2)
    gb_cell = cell_source(delivery, char_gb, 5, 6)
    _assert(ga_cell is not None and gb_cell is not None,
            'GA/GB 保留格必须有来源')
    _assert(ga_cell['fragment'] == gb_cell['fragment'] == 'f0',
            '两个绑定必须追溯到同一共享原料节点')
    _assert((ga_cell['fragment_x'], ga_cell['fragment_y']) == (1, 2)
            and (gb_cell['fragment_x'], gb_cell['fragment_y']) == (9, 10),
            '必须保留原料原始坐标，不能写成裁窗内相对坐标或装配坐标')
    # 同一块共享部件、不同位移：GB(5,6) 与 GA(9,10) 指向同一原料格。
    ga_equiv = cell_source(delivery, char_ga, 9, 10)
    _assert(ga_equiv is not None
            and (gb_cell['fragment_x'], gb_cell['fragment_y'])
            == (ga_equiv['fragment_x'], ga_equiv['fragment_y']),
            '位移后的同一原料格来源必须一致')
    # GB 锚点 (4,4) 对应源窗起点 (8,8)；该原料格恰为透明对角，故取 (4,5)
    # 对应原料 (8,9)。
    gb_origin = cell_source(delivery, char_gb, 4, 5)
    _assert(gb_origin is not None and gb_origin['fragment_x'] == 8
            and gb_origin['fragment_y'] == 9,
            'GB 锚点附近必须追溯到源窗起点对应的原料原始坐标')
    # GB(1,2) 在窗口上方，透明且无来源（位移出界/未覆盖格可核查）。
    _assert(cell_source(delivery, char_gb, 1, 2) is None,
            '窗口未覆盖的格必须透明且无来源')
    # 坐标外查询返回 None。
    _assert(cell_source(delivery, char_ga, 32, 0) is None,
            '坐标外查询必须返回 None')
    # 带来源清除格必须可按局部坐标查到状态 0 与原料信息。
    cleared = next(cell for cell in iter_cells(delivery, char_ga)
                   if cell['state'] == '0')
    _assert(cleared['supplier'] in approved and cleared['fragment'],
            '清除格必须带获批原料来源')
    for glyph in layout_input['glyphs']:
        _assert(all(set(row) <= {'0', '1'} for row in glyph['rows']),
                'v1 位图只能包含 0/1')

    # 原样取出 layout_input 调用现有 v1 排版（含非零字偶距与偏移）。
    report = process(layout_input)
    _assert(report['summary']['placed_glyphs'] == len(info['text']),
            'v1 必须为每个非换行字符产生 placement')
    _assert(any(p['applied_kerning'] != 0
                for p in report['placements']), '文字必须实际应用非零字偶距')
    _assert(any(g['offset'] != [0, 0] for g in layout_input['glyphs']),
            '必须存在非零偏移字形')
    _assert(report['summary']['unique_ink_pixels'] > 0, '必须有实际墨迹')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog='stress_bench')
    sub = parser.add_subparsers(dest='command', required=True)
    gen = sub.add_parser('generate')
    gen.add_argument('--output', required=True)
    run = sub.add_parser('run')
    run.add_argument('--source', required=True)
    run.add_argument('--delivery', required=True)
    args = parser.parse_args(argv)

    if args.command == 'generate':
        manifest = build_stress_manifest()
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_bytes(manifest))
        print(json.dumps({'nodes': len(manifest['nodes']),
                          'bytes': path.stat().st_size}, sort_keys=True))
        return 0

    # 全程流式：先结构断言（紧凑图，不实例化整份解析树），再装配落盘，
    # 最后只基于交付与紧凑信息做结果断言，使峰值 RSS 真实反映装配端开销。
    info = _check_manifest_stream(args.source)
    delivery = assemble_file(args.source, args.delivery)
    report = _check_delivery(info, delivery)
    peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({'peak_rss_mib': round(peak_kib / 1024, 2),
                      'budget_mib': 128,
                      'within_budget': peak_kib < 128 * 1024,
                      'v1_placed': report['summary']['placed_glyphs'],
                      'v1_unique_ink': report['summary']['unique_ink_pixels'],
                      'sourced_cells': delivery['summary']['sourced_cells']},
                     sort_keys=True))
    if peak_kib >= 128 * 1024:
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
