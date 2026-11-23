# data-qd-194 · 点阵字形排版与标牌

位图字形、字偶间距与行宽联合决定逐字符排版；真实墨迹、溢出证据及SVG均从相同placement生成。

layout.py 处理原始字符索引和换行；render.py 从实际字位生成像素、墨迹边界和自包含SVG。 详细字段、单位和失败边界见 [数据契约](docs/contract.md)。全部样例为自制合成数据，许可见 [来源](fixtures/SOURCES.md)。

## 独立安装与演示

Python >=3.11，本批实际使用CPython3.12.14，运行/测试仅标准库。每仓建立独立.venv，不共享其他项目运行环境；复用公共构建缓存，锁定setuptools80.9.0/wheel0.45.1。以下命令已在本机已有uv、Python与公共缓存时实际离线执行通过；其他机器首次缺缓存时需准备依赖。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/pixel-type --source fixtures/input.json --output run/report-initial.json
```

实际全量测试通过（含 v1/v2/v3/v4/v5/v6，共 165 项），v1 演示摘要：

```json
{
  "drawn_pixels": 48,
  "lines": 3,
  "overflow_glyphs": 0,
  "placed_glyphs": 6,
  "unique_ink_pixels": 48
}
```

报告原子新建，不覆盖旧文件或符号链接。演示路径已存在时请改用新输出名；输入结构/约束或文件错误退出2，正常业务诊断见契约。已实际核验重复JSON键拒绝且不生成结果、同名输出字节不变及符号链接不覆盖。

## 离线换版：冻结与回读续作

新增两个离线子命令，不接真实设备、不发送写入，规则见 [换版契约](docs/contract-v2.md)：

```sh
.venv/bin/pixel-type seal --store run/store --release-id REL-1 \
  --request-sha256 $(sha256sum run/change.json | cut -d' ' -f1) \
  --source run/change.json
.venv/bin/pixel-type reconcile --store run/store --release-id REL-1 \
  --request-sha256 <同一摘要> --readback run/readback.json --output run/report.json
```

`seal` 把审核过的 `old`/`new` 两份 v1 排版输入冻结成自包含写入方案（最小不可拆原子组、保护窗口联结、旧新位图与核对条件）；同 ID 同摘要重取字节完全一致且不再依赖源文件，同 ID 异摘要冲突退出 2。`reconcile` 仅凭冻结方案与一次设备回读，逐组给出 `conflict`/`unconfirmed`/`ready`/`applied`，只有 `ready` 组附下一条原子写入及旧态前置条件；报告原子新建、不覆盖旧文件，坏输入退出 2。

## 离线播出履约台账

新增两个离线子命令，不接设备、不规划换版、不改素材包格式，规则见 [台账契约](docs/contract-v4.md)：

```sh
.venv/bin/pixel-type intake --ledger run/ledger --source run/rev1.json --revision 1
.venv/bin/pixel-type statement --ledger run/ledger --panel-id PANEL-1 \
  --period-id P2026-11 --revision 1
```

`intake` 分期冻结首版屏尺寸、素材包全部场景像素与派单，之后每版只接受对证据的 `put`/`drop` 编辑；每版取核销数量最大的合法配对，并按派单 ID 词典序打破并列。`statement` 以确定性 JSON 返回完整配对、与上一版相比的撤销/新增差额（同一单换段成对记账）、缺采/冲突区间、最大连续播出段及可追溯的覆盖证据。历史账在源文件与素材文件移走后仍返回原字节；同一版本只会入账一次，提交点前后中断可确定性复现与重跑。

```sh
.venv/bin/python -I -m unittest tests.test_ledger tests.test_ledger_demo -v
```

演示数据见 `fixtures/intake-demo/`（两包三版，可直接照上面命令跑通）。

## 离线批量适配报告

新增一个离线子命令，把一批已批准的站内标牌消息逐消息、逐屏型做排版适配并说明是否可发布；不接设备、不改屏幕、不写素材包或台账，规则见 [适配契约](docs/contract-v6.md)：

```sh
.venv/bin/pixel-type campaign-fit --source fixtures/campaign-demo/source.json --output run/campaign.json
```

`campaign-fit` 在共享的 v1 字形/字偶距/行距上，对每个 message × display 组合完整搜索全部合法断行（每行 advance 不越安全行宽、行数不越 `max_lines`、每一个实际墨点都落在 `safe_rect` 内，受保护跨度不可拆开），按"行数最少 → 各行剩余宽度平方和最小 → 断行边界字典序最小"选出唯一排版，报告逐行 advance、原始字符索引对应的 placement、实际墨点边界与唯一亮点数。不可行组合给出可定位原因（行数/行宽不可行，或 advance 可行的断行全部墨迹越界，并列出越界墨点的原始字符索引与屏幕坐标）。报告确定性、与输入记录顺序无关，原子新建不覆盖既有文件；坏输入整批失败退出 2，单个组合无解不影响其它组合。

```sh
.venv/bin/python -I -m unittest tests.test_campaign tests.test_campaign_demo -v
```

演示数据见 `fixtures/campaign-demo/`（两屏型、四消息，4 可行 / 4 不可行，两种原因齐全，可直接照上面命令跑通）。

## 离线抽检卡与回执判读

新增两个离线子命令，不连接仪器、不点播、不下发屏幕写入，规则见 [抽检契约](docs/contract-v5.md)：

```sh
.venv/bin/pixel-type inspect-plan --source fixtures/inspect-demo/plan.json --output run/card.json
.venv/bin/pixel-type inspect-result --card run/card.json \
  --readback fixtures/inspect-demo/readback-cleared.json --output run/report.json
```

`inspect-plan` 把抽检工单（面板、1–4 个素材包、1–16 个带放行属性的候选画面、可读块、读数额度与整次容错上限）编译成自包含抽检卡：每个候选在每一可读块的预期位图，以及——在存在合规策略时——一套保证在额度内必达 `cleared`/`rejected` 的可执行分支策略（完整与或博弈求解，不因固定块集不够或贪心失败而误判不可保证）；不存在时只标 `not_guaranteed`。`inspect-result` 仅凭卡与回执判读：逐候选统计不符块数，按全部实际证据给出 `cleared`/`rejected`/`undecided`/`inconsistent`，报告绑定面板与卡摘要、列明每个候选的可能/排除依据与判定前提，不再打开源输入与素材包。卡与报告都原子新建，不覆盖既有路径；坏输入退出 2。

```sh
.venv/bin/python -I -m unittest tests.test_inspect tests.test_inspect_demo -v
```

演示数据见 `fixtures/inspect-demo/`（72×8 屏、四候选、九块可读、至多读六块容错一次，可直接照上面命令跑通）。

## 离线素材编译器与取帧

新增两个离线子命令，把一个屏的整套画面装进终端素材空间并离线取帧，规则见 [素材契约](docs/contract-v3.md)：

```sh
.venv/bin/pixel-type compile-pack --source fixtures/pack-three-scenes.json --output run/pack.json
.venv/bin/pixel-type unpack-frame --pack run/pack.json --scene C --output run/frame-c.json
```

`compile-pack` 渲染每场景实际亮点（同点只亮一次、空字形不点灯、出屏即整包失败），在"整帧 / 以任一整帧为基础的异或掩码引用帧"之间做全局最优分配，取载荷字节最小者；等长时先按目录顺序比较记录格式（整帧在前），再比较引用帧基础序号，均取字典序最小。`direct_only` 场景强制整帧、可作他场景基础。载荷按 ASCII 场景 ID 的目录顺序无缝拼接为 Base64 自包含包，报告实占字节数；最小载荷超过预算退出 2 并在 stderr 报出所需与给定字节数，不产出部分包。

`unpack-frame` 仅凭包重建任一场景，原输入可不存在；输出含全暗块的完整 `tiles`。取帧前校验整包结构（版本/尺寸/目录顺序/记录边界/引用合法性/Base64 等），任一坏记录即使未被选中也使整包失败。两个命令都原子新建输出、不覆盖既有文件。确定性与打乱无关：`scenes` 顺序不影响成功包字节。验收三场景（A 暗、B 亮两处、C 再加一处）自由分配实占 100 字节；将 A 设为冷启动必读后为 120 字节。

自定义位图字体，逐码点贪心换行；无复杂文本整形、字体回退或单词折行。 合理新增需求可沿现有身份和结果接入多字体/连字处理、编辑命中测试和标牌导出；当前尚未实现这些扩展，不保证固定数量未来题目。

## 离线字形装配（无新增 CLI，Python 数据处理入口）

多供稿字形原料（fragment）与手工合成的装配节点（assembly）经一个纯 Python 入口
装配为可直接送 v1 的交付，规则见 [字形装配契约](docs/contract-v7.md)。不新增任何
CLI 子命令、不接 UI 或屏幕，原有 `pixel-type` 调用与全部子命令保持原行为：

```python
from pixel_type.assembly import assemble, assemble_file, cell_source

delivery = assemble(manifest_dict)                 # 字典 → 交付字典
assemble_file('manifest.json', 'run/delivery.json')  # 流式读 + 原子新建
cell_source(delivery, 'A', 1, 0)                   # 按字符/局部坐标查逐格来源
```

三态语义：`1` 带来源亮点、`0` 带来源清除、`.` 透明不写入；交付 v1 时透明与清除
都变 `0`，但只有有效格带逐格来源（供稿方、原料节点 ID、原料原始坐标）。未获批
供稿方的有效格（即使是看起来与透明相同的清除）拒绝交付并给出可定位诊断；被盖住、
裁掉或未被绑定用到的未获批供稿不拦截。大清单走事件式流式解析，整份 JSON 解析树
从不同时驻留，绑定级三态画布处理完即释放。

极限压力样例（40000 个全部可达的 32×32 节点、2048 原料、跨分支共用、裁窗/位移/
清除/透底、64 节点深链）与峰值 RSS 测量由独立进程驱动，不挂 CLI：

```sh
python -I bench/stress_bench.py generate --output run/stress-manifest.json
python -I bench/stress_bench.py run --source run/stress-manifest.json \
    --delivery run/stress-delivery.json
```

测量在独立 Linux CPython 进程内用 `resource.getrusage(RUSAGE_SELF).ru_maxrss`
报告峰值 RSS，预算 128 MiB，超限退出码 3；样例结果与来源由自动断言核对，不只看
进程退出。

```sh
python -I -c "import sys; sys.path.insert(0,'src'); \
import unittest; unittest.TextTestRunner(verbosity=1).run( \
unittest.defaultTestLoader.loadTestsFromNames( \
['tests.test_assembly','tests.test_stress_sample']))"
```
