# 离线批量适配报告 v6

在保留 v1 排版、v2 `seal`/`reconcile`、v3 `compile-pack`/`unpack-frame`、v4 `intake`/`statement`、v5 `inspect-plan`/`inspect-result` 契约的前提下，新增一个离线子命令，把一批已批准的站内标牌消息逐消息、逐屏型做排版适配，给出可发布性结论。不接设备、不改屏幕、不写素材包或台账；所有判断只依赖本次源文件字节。

```
pixel-type campaign-fit --source FILE --output REPORT
```

报告原子新建，不覆盖既有文件或符号链接；坏 JSON、重复键、坏结构、文件错误退出 2 且不产出报告。任一组合无解是正常业务结果，退出 0，不影响其它组合。

## 源文件

源 JSON 字段恰好为：`line_height`、`glyphs`、`kerning`、`displays`、`messages`。

- `line_height` 为 1–1000 整数，是全部组合共用的行距。
- `glyphs`、`kerning` 的记录结构、字符规则、位图规则与字偶距规则完全沿用 v1：1–256 个字形（`char` 为单个可显示 Unicode 标量、`advance` 1–128、`offset` 两个 -128..128 整数、`rows` 为 0..32 行等宽 1..32 位 01 串，允许空 `rows` 的空白字形）；0–2000 条字偶距（字符对已注册且唯一、`right.advance + adjust > 0`）。不提供隐式缺字回退。
- `displays` 为 1–16 项，每项恰好 `{id, width, height, safe_rect, max_lines}`：
  - `id` 唯一，1–64 个 ASCII 字母、数字、下划线或连字符（message `id` 同规则）。
  - 物理 `width`、`height` 为 8 的正整数倍，至多 256×128。
  - `safe_rect` 恰好 `{x, y, width, height}`，是屏内非空矩形（`x,y >= 0`，`width,height >= 1`，整体不越出物理屏）。其宽度是本屏的排版行宽；矩形之外不得有任何实际亮点。
  - `max_lines` 为 1–128 整数。
- `messages` 为 1–32 项，每项恰好 `{id, text, protected_spans}`：
  - `text` 为 1–2000 个 Unicode codepoint；全部消息合计不超过 8000 个。字符规则沿用 v1（除 LF 换行外禁 ASCII 控制、DEL、代理项及 U+FFFE/U+FFFF）；每个非换行字符必须有字形。每条消息独立套用 v1 的 200000 绘制像素预算。
  - `protected_spans` 为原始文本的零基 codepoint 半开区间 `{start, end}` 数组：非空（`start < end`）、互不重叠、不包含换行符。跨度表达不可拆开的站名、出口编号或应急短语。

任一结构或记录非法则整批失败，不生成部分报告。

## 排版与断行语义

逐 codepoint 排版，沿用 v1 的 advance、kerning 与字形偏移：行内首字 kern 为 0，之后每字 pen 位置 = 前一 pen + 字偶距；换行（显式或自动）后 pen 与前一字都重置。每行从 `safe_rect.x` 开始，第 0 行原点纵坐标为 `safe_rect.y`，之后每行按共享 `line_height` 下移。空格是普通字形，不 trim、不重排、不删除任何字符。

换行符（LF）是固定断行，保留空行与尾随空行。自动断行可发生在任意两个相邻非换行 codepoint 之间，但不得落在任一受保护跨度内部（跨度 `[start,end)` 禁止边界 `start .. end-2`；边界 `i` 表示在第 `i` 与第 `i+1` 个 codepoint 之间断开）。自动断行不改变源文本；报告中的字符索引始终指向原始 `text`，显式换行也计入索引。

## 适配与选择

对每个 message × display 组合独立寻找所有满足下列条件的合法断行方案：

1. 每行的 pen advance 不超过 `safe_rect.width`；
2. 总行数不超过 `max_lines`；
3. 按字形偏移绘出的每一个实际墨点都在该 display 的 `safe_rect` 内（安全矩形在物理屏内，故同时保证在屏内）。只检查 advance 或名义行高不够；出界墨点不裁掉。墨迹为空的文本仍可适配；相互重叠的墨点只占一个实际屏幕坐标，但字符 placement 与原始字符身份仍保留。

在可行方案中按以下顺序选唯一结果：

1. 总行数最少；
2. 除每个显式换行段末行以外，各行的 `(safe_rect.width - line_advance)²` 之和最小；
3. 仍并列时，全部自动断行的原文边界索引序列（升序）字典序最小。

每个组合的结果与输入数组顺序无关：报告整体按 ID 排序规范化，源文件中 glyphs、kerning、displays、messages、protected_spans 的书写顺序不影响报告字节。

## 报告

报告为确定性 JSON（UTF-8、`sort_keys`、两空格缩进、末尾换行），同一输入产出相同字节：

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`kind` | `1`、`pixel-type.campaign-fit.report` |
| `line_height` | 共用行距 |
| `displays` | 全部 display 身份（`id`、物理尺寸、`safe_rect`、`max_lines`），按 `id` 排序 |
| `messages` | 全部 message 身份（`id`、`text`、规范化排序后的 `protected_spans`），按 `id` 排序 |
| `results` | 每个组合一条，按 `(message_id, display_id)` 排序 |
| `summary` | `{combinations, eligible, ineligible}` 计数，同时打印到 stdout |

可行组合给出：

- `eligible: true`
- `breaks`：自动断行边界索引（断行前一个 codepoint 的原始索引，升序）
- `lines`：逐行 `{advance, character_indices}`（空行为 advance 0、空索引）
- `placements`：逐字符 `{character_index, char, line, pen_x, applied_kerning, ink_origin, advance}`，坐标为屏上绝对坐标（pen 起点 `safe_rect.x`，行原点 `safe_rect.y + line * line_height`），按原文顺序
- `ink_bbox`：实际墨点边界 `{min, max_exclusive}`，无墨点为 `null`
- `unique_ink_pixels`：唯一亮点数（重叠墨点只计一次）

不可行组合给出 `eligible: false` 与可定位的原因：

- `reason: "line_or_width_limit_exceeded"` —— 所有合法断行都超过 `max_lines` 或行宽。`details` 含 `max_lines`、`safe_width`、`min_lines_within_safe_width`（行宽内最少行数，无任何 advance 可行断行时为 `null`）与 `min_widest_line_advance`（所有合法断行中最宽行 advance 的最小值，与具体屏宽无关）。
- `reason: "ink_out_of_bounds"` —— 存在 advance 与行数都可行的断行，但每一个都因实际墨迹越出物理屏或安全矩形而失败。`details` 含 `breaks`（仅按 advance 规则选出的规范断行：行数最少、raggedness 最小、边界字典序最小）和 `out_of_bounds_ink`：该断行下全部越界墨点，按 `(character_index, x, y)` 排序，每项含 `outside`（`physical_screen` 或 `safe_rect`）区分越出的是物理屏还是安全矩形。

不会因为先遇到的候选失败就把组合误判为无解：判定建立在对全部合法断行的完整搜索上。

## 测试与演示

```sh
PYTHONPATH=src python3 -m unittest tests.test_campaign tests.test_campaign_demo -v
```

`tests/test_campaign.py` 覆盖：真实 v1 字形/kerning 下两个屏宽的全局断行选择（证明 v1 贪心不是题定最优，逐项核对 tie-break 与原始字符索引）；受保护跨度禁止更优断行；显式/连续换行与首尾空格保留；输入顺序打乱后报告字节相同；负偏移/高位字形墨迹越界的精确诊断与较大屏的可行交付；墨迹贴边、空白字形、重叠墨点、名义行溢出但墨迹安全；唯一墨迹安全方案的搜索完整性；行数/行宽与墨迹失败的原因区分；缺字、非法跨度、ID 重复、JSON 重复键、输出已存在/符号链接的失败行为。

`fixtures/campaign-demo/` 提供可手工复现的演示（两屏型、四消息，4 可行 / 4 不可行，两种原因齐全）：

```sh
pixel-type campaign-fit --source fixtures/campaign-demo/source.json --output run/campaign.json
```

输出与提交的 `fixtures/campaign-demo/report.json` 字节相同。

## 实现的明确取舍

- **搜索结构**：行数最小是首要目标，而墨迹垂直边界依赖绝对行号，故按行数分层做动态规划；每层内按 `(raggedness, 断行序列)` 字典序保留每位置最优。水平墨迹与 advance 与行号无关，按位置预处理一次并缓存。
- **诊断基准**：`ink_out_of_bounds` 的越界墨点取自"仅按 advance 规则选出的规范断行"，与可行选择的排序规则一致，只是忽略墨迹约束；该布局必然有越界点，否则组合本可行。
- **行数/行宽诊断**：`min_lines_within_safe_width` 由最远可达贪心给出（对固定行宽求最少行数最优）；`min_widest_line_advance` 与屏宽无关，按消息缓存。
