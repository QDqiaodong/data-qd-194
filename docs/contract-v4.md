# 离线播出履约台账 v4

在保留 v1 排版、v2 `seal`/`reconcile`、v3 `compile-pack`/`unpack-frame` 契约的前提下，新增两个离线子命令。不连接设备、不规划换版、不改变素材包格式。所有判断只依赖已提交的台账数据与本次输入的字节。

```
pixel-type intake    --ledger DIR --source FILE --revision N
pixel-type statement --ledger DIR --panel-id ID --period-id ID --revision N
```

`intake` 成功时只在 stdout 打印 `{"committed_revision": N}`；账单不从终端输出读取。
`statement` 把该期第 N 版的账单原文写到 stdout，退出码 0。任何坏输入、版本不可入账、版本不存在或 I/O 错误退出 2，并在 stderr 给出可定位信息。重复、跳号、全量覆盖旧账、同一版本第二次入账都退出 2，不覆盖旧账、不消耗版本号。

台账目录可同时管理多个 `(panel_id, period_id)`。各期互相独立加锁，同一期同一新版本只会有一个进程成功。

## 输入

业务 ID（`panel_id`、`period_id`、派单 `id`、证据 `id`、包别名、场景 ID）为 1–64 个 ASCII 字母、数字、下划线或连字符。时间为 0–2^53−1 的整数毫秒，开区间左闭右开且非空，单个时段不超过 24 小时。业务 ID 直接用于命令行与 JSON 键，不另行转义。

第 1 版（首版）字段：

```json
{
  "panel_id": "P1", "period_id": "2026-11",
  "width": 64, "height": 8,
  "start_ms": 0, "end_ms": 40,
  "packs": {"one": "pack1.json", "two": "packs/pack2.json"},
  "orders": [ ... ],
  "changes": [ ... ]
}
```

- `width`、`height` 为 8 的正整数倍，上限 256×128（与 v3 素材包相同）。
- 第 1 版必须提供 `packs` 与 `orders`；`changes` 可缺省表示空列表。
- `packs` 为 1–8 个别名到现有 `compile-pack` 输出文件路径的映射。相对路径相对输入文件所在目录解释。首版会校验每个包的**整包**结构、每个场景的记录与尺寸（包括没有被任何证据引用的场景）；任一坏记录使整版失败。此后台账只依赖已入库的像素，不再读这些文件。
- `orders` 至多 24 张派单，`id` 唯一，每张含 `start_ms`/`end_ms`（落在期内）、`required_ms`（正整数且不超过窗口长度）与 `expected`（`{pack, scene}`）。
- `changes` 至多 128 条，是对证据 ID 的编辑，每个 ID 一次版本内至多出现一次：
  - `{"type":"put","id":...,"start_ms":...,"end_ms":...,"observed":{"pack":...,"scene":...}}` 新增或整体替换该证据；
  - `{"type":"drop","id":...}` 撤回当前存在的证据，撤回未知 ID 非法。

第 2 版起的字段：`panel_id`、`period_id`、`width`、`height`、`start_ms`、`end_ms`、`changes`。屏、时段身份与边界必须与第 1 版完全一致；`packs` 与 `orders` 不得重新提交（重新定义尺寸、派单或素材都会拒收）。后续版本的引用只能使用已入库的别名和场景，源文件与素材文件被移动或删除都不影响。

所有引用与结构在任何计算之前完整校验，任何一条坏数据拒绝整版，已提交版本不受影响。

## 核销口径

每个证据区间表示采集器保证连续看到 `observed` 画面的时间；没有证据的时间是缺采，**不**当作全暗，也不当作任何画面。

1. **逐点还原。** 把期内在每个毫秒上覆盖它的证据按画面比较：画面以取帧后的整屏逐点像素为身份。同名场景在不同包中没有共享身份；不同别名、不同场景名、不同记录偏移、不同编码、不同摘要但像素一致，就是同一画面。互不相同的画面使该毫秒成为**冲突**。
2. **缺采、冲突都切断。** 没有证据的时间为缺采区间；有互相矛盾画面的时间为冲突区间。两者都不能证明任何派单，也都会切断连续的播出段。
3. **最大连续段。** 画面一致且无冲突、无缺采的相邻毫秒合并成最大连续播出段。即使段足够长，它也只是一张凭证，只能被一张派单核销。
4. **资格。** 段与派单画面像素相同，且段与派单窗口的交集长度不小于 `required_ms` 时才有资格。
5. **最优配对。** 每版取核销数量最大的合法配对，不按先来先得。并列时按派单 ID 的 ASCII 次序比较各单配到的段：段按起点、再按终点升序；未配排在所有段之后；取整个序列字典序最小者。
6. **段身份。** 段 ID 为 `"<start_ms>:<end_ms>:<像素 SHA-256>"`，只由起止时间与逐点像素决定，与证据顺序、包别名、场景名无关。
7. **可追溯。** 每个确认段、缺采段、冲突段都附 `coverage`，把区间切成最细的片段，每片列出当时覆盖它的证据 ID。这样即使证据来源在一个最大段中间改变，也不会声称任一证据覆盖了整段。

## 账单（statement）

`statement` 输出确定性 JSON（UTF-8、`sort_keys`、两空格缩进、末尾换行）。字段契约：

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`kind` | `1`、`pixel-type.intake.statement` |
| `panel_id`、`period_id`、`revision` | 身份与版本 |
| `period` | `{start_ms, end_ms}` |
| `width`、`height` | 屏尺寸 |
| `billing` | 每张派单一项，按 ID 的 ASCII 次序 |
| `deltas` | 与上一版配对相比的差额明细，按派单 ID 与新增/撤销次序 |
| `summary` | `matched`、`unmatched`、`granted`、`revoked`、`net_delta` |
| `segments` | 本期全部最大连续段 |
| `missing` | 缺采区间 |
| `conflicts` | 冲突区间 |
| `evidence` | 本版生效的全部证据 |

`billing` 每项含 `id`、`window`、`required_ms`、`expected`（含 `pixels_sha256`），以及：

- 已配：`status="matched"` 与 `segment`（`id`、`start_ms`、`end_ms`、`overlap_ms`）；
- 未核销：`status="unmatched"`、`reason` 及 `eligible_segments`。`reason` 为 `"no_eligible_segment"` 或 `"eligible_segments_allocated_elsewhere"`；后者说明该单有资格但没有拿到段。`eligible_segments` 逐条给出段、`overlap_ms` 与 `allocated_to`（该段归属的派单或 `null`）。

`segments` 每项含 `id`、`start_ms`、`end_ms`、`pixels_sha256`、`coverage`。`missing` 每项含 `start_ms`、`end_ms`。`conflicts` 每项含 `start_ms`、`end_ms`、`coverage`。`coverage` 为 `[{start_ms, end_ms, evidence_ids}]`，`evidence_ids` 按 ASCII 排序。`evidence` 每项含 `id`、`start_ms`、`end_ms`、`pack`、`scene`、`pixels_sha256`。

`deltas` 每项为 `{order_id, kind, segment_id, delta}`：`kind="revoke"`、`delta=-1` 表示撤销旧配对；`kind="grant"`、`delta=1` 表示新增配对。同一单换段记两条（一条 -1、一条 +1），即使净核销量为 0。配对未变的单不记。第 1 版以空账为前态，所以第 1 版的差额即全部新增。`net_delta` 为本版差额之和。

## 入账与查账

- 同一期从第 1 版开始，每次只接受当前已提交版本的下一个号。第 N 版以已提交的第 N−1 版的**证据、配对账单**为前态；差额对同一真实前版计算。证据区间被改写后，原来未直接受编辑影响的派单也可能换段或被撤销，因此每版都重新整体求解，而不只检查直接受影响的派单。
- 不接收全量覆盖，不接受重定义尺寸、派单或素材。
- 一个版本只入账一次；重复或跳号退出 2，调用方用 `statement` 查询结果。
- 每版的生效证据、完整配对账单与差额作为一次提交一起可见。

## 存储与中断

台账目录布局（实现自定，仅为说明）：

```
DIR/
  .lock                       期目录创建锁
  bands/<hex(panel\0period)>/
    .band-lock                该期的互斥锁（flock）
    material.json             第 1 版冻结的别名、场景像素（base64 整屏）
    NNNNNNNN.statement.json   第 N 版账单原文
    NNNNNNNN.record.json      第 N 版记录（证据、派单、配对、校验和）
```

- 证据、派单与原素材在**第 1 版**一次性解码成逐屏像素并冻结入库；此后不再打开输入包、源文件或素材文件。历史账单在源文件、素材文件被移动或替换后仍返回原字节。
- 提交写入使用临时文件 + `fsync` + `os.replace`，`.record.json` 的出现是唯一提交点。读者与下一次 `intake` 只认已提交的 `.record.json`；只有账单草稿没有记录的版本会被当作未提交清理，从而不出现“账单已出现但下一版不能用”的状态。
- 两个进程争同一期同一新版本时，`flock` 串行化：胜者在锁内读取已提交前态计算结果；败者在见到新版本头后按版本校验退出 2。胜者的差额必然基于真正的第 N−1 版。
- 进程终止（`SIGKILL`、崩溃）不需要模拟断电：只要终止，下一次 `intake` 在锁内清理临时文件即可重试同一版本。已提交版本在重启后完整可查，并可作为下一版前态。

## 测试与演示

测试全部使用标准库与合成数据，可离线运行：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tests/test_ledger.py` 覆盖验收场景：

1. 用现有 v1 排版与 `compile-pack` 产出两包：一包的引用帧与另一包的整帧像素相同、场景名不同；再构造同名异画。第 1 版确认 `[0,10)`（由 `[0,6)` 与 `[4,10)` 两条异名同画证据组成）与 `[20,30)`；A 配 `[20,30)`、B 配 `[0,10)`；`coverage` 证明没有任何一条证据被说成覆盖整段。
2. 第 2 版在 `[23,27)` 补传同名异画证据，报告冲突，不把两侧 3+3 ms 拼起来，只能核销 A→`[0,10)`，B 有资格但该段归 A；A 撤旧补新、B 冲销，`net_delta=-1`；第 1 版字节不变；第 3 版撤回冲突后恢复第 1 版配对并补账。另有一组“两单互换段、净额为零”的成对差额。
3. 移动输入与包文件后仍可查旧账并提交后续更正；被篡改的未选场景所在包不能让台账以首版入账；两个进程争同一版本只有一个成功；在 `PIXEL_TYPE_TEST_STOP_AT` 指定的提交边界终止进程（`after_build`、`before_statement`、`after_statement`、`before_record`、`after_record`），检查账单与下一版所见前态一致，失败结果不妨碍重跑。

中断边界用环境变量确定性地注入，而不是碰巧杀死进程：`_stop_hook()` 读 `PIXEL_TYPE_TEST_STOP_AT`，在提交代码走到命名边界时 `os._exit(70)`，因此测试固定覆盖每一个边界（`after_build` 到 `after_record`），且每次都在同一个可复现的写点停下。提交点之前退出时版本不可见、版本号不消耗、可原样重跑；提交点之后退出时版本已完整可见，重跑同号被拒。

手工演示：

```sh
mkdir -p run/demo && cp fixtures/intake-demo/*.json run/demo/
pixel-type intake --ledger run/demo/ledger --source run/demo/rev1.json --revision 1
pixel-type statement --ledger run/demo/ledger --panel-id PANEL-1 \
  --period-id P2026-11 --revision 1
pixel-type intake --ledger run/demo/ledger --source run/demo/rev2.json --revision 2
pixel-type statement --ledger run/demo/ledger --panel-id PANEL-1 \
  --period-id P2026-11 --revision 2
```

`fixtures/intake-demo/` 提供两包与两版输入（含 `rev3.json` 撤回更正）；`tests/test_ledger_demo.py` 直接以这些文件跑完整流程并断言关键结论。

## 实现的明确取舍

契约未逐字规定、实现必须选定一项的地方：

- **`packs` 的规模**：1–8 个别名（与素材包场景数量同阶，便于一次首版冻结）。空 `packs` 视为坏输入。
- **`changes` 可省略**：第 1 版省略等于没有任何证据；后续版本省略等于不修改，用于验证“无变化版本”仍会重算（通常不产生差额）。第 1 版有 `packs`/`orders` 但没有任何证据区间时仍可入账，账单会把整期报为缺采。
- **尺寸归属**：`width`/`height` 由输入给出，且必须与每个包的尺寸一致；首版把它们冻结进台账，后续版本必须复述相同值。
- **重复段身份**：同一像素在同一期出现两个不相连的最大段时，ID 因起止时间不同而不同；同起止时间、同像素必然是同一段。
- **`coverage` 粒度**：按“参与证据集合 + 画面类别”切分，而不是按原始区间切分；相邻且集合相同的片段合并，避免把同一证据来源重复列出。
- **`deltas` 顺序**：先按派单 ID 的 ASCII 次序，再在同一单内先 `revoke` 后 `grant`，保证字节稳定。
- **`eligible_segments`**：只对未核销单输出；列为全部资格段（按起点、终点升序）及当前归属，不列出所有段。
- **冲突证据**：冲突区间同样附 `coverage`，因此可追到具体证据 ID；冲突本身不归属任何单。
- **时段与证据上限**：`required_ms` 必须为正整数；证据上限在每次编辑后按生效集合计数（替换不增加计数，撤回减少计数，新增超限整版拒绝）。

## 运行与排错要点

- 台账目录可以不存在；`intake` 会在校验首版全部输入**之后**才创建期目录，坏首版不会留下半成品目录。`statement` 对从未入账的期同样退出 2。
- 同一期并发时用该期的 `flock` 串行化：后来者在锁内看到已提交的新版本头，按版本校验退出 2；它不会读到胜者的草稿，也不会覆盖胜者的文件。
- 提交只追加 `NNNNNNNN.record.json`（提交点）与其账单；旧版本文件不被改写或删除，因此 `statement` 的历史字节不随后续更正变化。
- 台账自身被截断或篡改（记录与账单摘要不符、素材摘要与记录不符）视为无法入账，退出 2 并指出期与版本，不静默重建。
- 证据画面在每次 `intake` 都按已冻结像素重新解析；因此即使后续版本只 `drop` 一条证据，也不依赖任何源文件仍在原处。
