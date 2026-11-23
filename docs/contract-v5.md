# 离线抽检卡与回执判读 v5

在保留 v1 排版、v2 `seal`/`reconcile`、v3 `compile-pack`/`unpack-frame`、v4 `intake`/`statement` 契约的前提下，新增两个离线子命令。不连接仪器、不点播、不下发屏幕写入；所有判断只依赖本次输入文件的字节与已冻结的素材包。

```
pixel-type inspect-plan   --source FILE --output CARD
pixel-type inspect-result --card CARD --readback FILE --output REPORT
```

两个命令都原子新建输出，不覆盖既有文件或符号链接；坏 JSON、坏结构、文件错误退出 2 且不产出结果。`not_guaranteed` 与四种证据判定都是正常业务结果，退出 0。

## 抽检任务（inspect-plan）

源 JSON 字段恰好为：`panel_id`、`width`、`height`、`packs`、`candidates`、`probe_tiles`、`max_reads`、`max_bad_reads`。

- `panel_id`、包别名、候选 `id` 均为 1–64 个 ASCII 字母、数字、下划线或连字符。
- `width`、`height` 为 8 的正整数倍，至多 256×128（与素材包一致）。块坐标 `(tx,ty)` 从屏幕左上角起算。
- `packs` 为 1–4 个别名到现有 `compile-pack` 输出文件路径的映射；相对路径以源文件所在目录为准。每个列出的包都完整通过 v3 整包校验且尺寸与源一致——即使损坏记录不在此次抽检名单中，也阻止制卡。
- `candidates` 为 1–16 项，每项恰好 `{id, pack, scene, allowed}`：`id` 唯一；`pack` 须为已列出的别名；`scene` 须存在于该包目录；`allowed` 为工单规定的布尔值。比较的是解包后相同坐标上的实际像素，与记录格式、场景名、引用基底无关；两个候选即使在全部可读块上像素相同，也分别保留身份与放行属性。
- `probe_tiles` 为 1–12 个互不重复的屏内 `{tx,ty}`；其余块本次不可读。一次抽检中屏幕保持不变，每个坐标至多取样一次。
- `max_reads` 为 0 到可读坐标数的整数，是整次抽检的回读总额度。
- `max_bad_reads` 为 0 或 1，是校准给出的整次容错上限：全部回读里至多这么多个块返回任意错误位图（不是每块一次）。

### 判定口径

给定已看到的回读，逐候选统计已读块中位图与该候选预期不同的块数；未读块不参与。不同数不超过 `max_bad_reads` 的候选为**仍可能**：

- 仍可能集合非空且全获准 → `cleared`；
- 非空且全未获准 → `rejected`；
- 获准与未获准都有 → `undecided`；
- 空集 → `inconsistent`。

不必精确识别场景：多个候选都获准即可放行。

### 保证

抽检卡判断是否**存在**合规策略能在 `max_reads` 内保证不留下 `undecided`：从空回读开始，每一步要读的块可由之前的回读值决定；任一名单画面、任一容错范围内的回读，都必须在额度内到达与证据相符的 `cleared` 或 `rejected`；若实际走到证据排除全部候选的路径，则停在 `inconsistent`。存在即标 `guaranteed` 并交付一套可执行策略；只有不存在时才标 `not_guaranteed`，不交付策略。固定块集不够、贪心找不到下一块、或仅无错时能判定，都不足以得出 `not_guaranteed`——判定由完整的与或博弈求解给出（仪器是存在方选下一块，屏与信道是全称方任选合法位图类别，含任何候选都不期望的值）。起始时已经能签同一结论的卡不必请求取样（策略为单个停止点）。

## 卡格式

卡为自包含 JSON（UTF-8、`sort_keys`、两空格缩进、末尾换行），同一输入产出相同字节：

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`kind` | `1`、`pixel-type.inspect.card` |
| `panel_id`、`width`、`height`、`tile_cols`、`tile_rows` | 面板身份与尺寸 |
| `max_reads`、`max_bad_reads` | 读数约束 |
| `probe_tiles` | 可读块坐标数组，顺序同源文件 |
| `candidates` | 每项 `{id, pack, scene, allowed, tiles}`；`tiles` 按 `probe_tiles` 顺序给出该候选在每一可读块的预期位图 |
| `guarantee` | `guaranteed` / `not_guaranteed` |
| `strategy` | 可执行策略；`not_guaranteed` 时为 `null` |

位图是 16 位小写十六进制字符串：8 字节，每像素行一字节，左端像素占最高位，与素材包单块编码相同。

### 策略表示

```json
"strategy": {
  "start": 0,
  "nodes": [
    {"id": 0, "action": "read", "tx": 0, "ty": 0,
     "branches": [{"bits": "0000000000000000", "next": 1},
                  {"bits": "8000000000000000", "next": 12}],
     "fallback": 19},
    {"id": 1, "action": "read", "...": "..."},
    {"id": 4, "action": "stop"}
  ]
}
```

仪器执行语义：当前节点从 `start` 开始。`stop` 节点：停止取样，把已得回读交验收判读。`read` 节点：请求回读 `(tx,ty)` 一块；返回值等于某条分支的 `bits` 时转到该分支的 `next`，否则（包括任何候选都不期望的合法位图）走 `fallback`。节点按路径不会重复读同一坐标，沿任一路径的读次数不超过 `max_reads`。停止节点不标注结论——判定永远由实际证据算出，不从策略节点标签推断。节点合并成有向无环图；所有停止状态共用一个停止点。

## 回执判读（inspect-result）

回读 JSON 恰好为 `{panel_id, card_sha256, reads}`：

- `card_sha256` 为卡文件原字节的 64 位小写十六进制 SHA-256；`panel_id` 须与卡一致。错绑退出 2。
- `reads` 为 `{tx,ty,bits}` 数组，可为空。坐标须为卡上可读块且唯一，总数不超过 `max_reads`；`bits` 为 16 位小写十六进制。任一不符退出 2，不留下报告。

验收员可能因仪器断开而改按可读清单手工补测：回执可以半途结束，也可以不走卡上的分支顺序。判读按**实际全部证据**计算上述四种判定：没走完策略不是拒收，曾抵达一个放行叶也不是最终放行。`not_guaranteed` 的卡同样判读实际回执。判读只打开卡与回执，不再打开源输入与素材包。

报告字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`kind` | `1`、`pixel-type.inspect.report` |
| `panel_id`、`card_sha256` | 报告绑定的面板与卡摘要 |
| `guarantee`、`max_reads`、`max_bad_reads` | 卡上的约束（仅供对照） |
| `reads`、`reads_used` | 实际回读与条数 |
| `verdict` | `cleared` / `rejected` / `undecided` / `inconsistent` |
| `possible_candidates` | 仍可能的候选 ID（按卡上顺序） |
| `candidates` | 逐候选 `{id, pack, scene, allowed, possible, mismatch_count, mismatches}`；`mismatches` 逐项给出不符块的 `{tx, ty, expected_bits, actual_bits}`，按 `(ty,tx)` 排序 |
| `premise` | 判定前提的明确表述 |

`premise` 写明：签放行以"屏幕确实显示名单内画面且整次错误块数未超 `max_bad_reads`"为前提，报告不证明名单以外的屏幕内容。验收员据此可分清是仍有相反候选（`undecided`）还是错码已超假设（`inconsistent`：全部候选的不符数都超过容错上限）。

## 测试与演示

```sh
PYTHONPATH=src python3 -m unittest tests.test_inspect tests.test_inspect_demo -v
```

`tests/test_inspect.py` 覆盖：72×8 四候选在九块可读、至多六读、容错一下的保证策略（并验证任意固定六块都不足以判定）；四真实画面在无错与一次错码（含另一候选的值与名单外合法位图）下沿卡分支的判定与读数；两候选两块容错一的不可保证与容错零的可保证；像素相同而放行属性相反的身份不可判定；源与素材移走后凭卡判读；证据不足不签放行、后续矛盾证据不被早先放行路线掩盖；错绑、重读、超额读数、坏卡、坏包（含未被工单引用的损坏记录）均退出 2 且不留结果。

`fixtures/inspect-demo/` 提供可手工复现的演示：

```sh
pixel-type compile-pack --source fixtures/inspect-demo/scenes.json --output run/demo-pack.json
pixel-type inspect-plan --source fixtures/inspect-demo/plan.json --output run/demo-card.json
pixel-type inspect-result --card run/demo-card.json \
  --readback fixtures/inspect-demo/readback-cleared.json --output run/demo-report.json
```

`readback-cleared.json` / `readback-rejected.json` 绑定已提交的 `card.json`（重新制卡字节相同，摘要不变），分别判 `cleared`（S1）与 `rejected`（S0）。

## 实现的明确取舍

- **策略交付形式**：节点合并为 DAG，所有停止状态共用一个 `stop` 汇点；停止节点不带结论标签。任一满足保证的策略都可交付，不要求最短；同一输入产出相同卡字节。
- **分支枚举**：`read` 节点只列出仍可能候选在该块的期望值，其余一切合法位图走 `fallback`（与"无一候选匹配"等效，因为已排除候选的状态不再变化）。
- **候选分组**：求解时在内部把全部可读块上像素相同的候选并为一组（组内只记获准/未获准计数），卡与报告仍逐候选列出身份。
- **卡校验**：判读时完整校验卡结构（版本、尺寸、候选位图覆盖、策略的图良构性：无环、不重读、不超额度），但不重新求解保证，也不重开素材包。
