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

实际全量测试通过（含 v1/v2/v3，共 72 项），v1 演示摘要：

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

## 离线素材编译器与取帧

新增两个离线子命令，把一个屏的整套画面装进终端素材空间并离线取帧，规则见 [素材契约](docs/contract-v3.md)：

```sh
.venv/bin/pixel-type compile-pack --source fixtures/pack-three-scenes.json --output run/pack.json
.venv/bin/pixel-type unpack-frame --pack run/pack.json --scene C --output run/frame-c.json
```

`compile-pack` 渲染每场景实际亮点（同点只亮一次、空字形不点灯、出屏即整包失败），在"整帧 / 以任一整帧为基础的异或掩码引用帧"之间做全局最优分配，取载荷字节最小者；等长时先按目录顺序比较记录格式（整帧在前），再比较引用帧基础序号，均取字典序最小。`direct_only` 场景强制整帧、可作他场景基础。载荷按 ASCII 场景 ID 的目录顺序无缝拼接为 Base64 自包含包，报告实占字节数；最小载荷超过预算退出 2 并在 stderr 报出所需与给定字节数，不产出部分包。

`unpack-frame` 仅凭包重建任一场景，原输入可不存在；输出含全暗块的完整 `tiles`。取帧前校验整包结构（版本/尺寸/目录顺序/记录边界/引用合法性/Base64 等），任一坏记录即使未被选中也使整包失败。两个命令都原子新建输出、不覆盖既有文件。确定性与打乱无关：`scenes` 顺序不影响成功包字节。验收三场景（A 暗、B 亮两处、C 再加一处）自由分配实占 100 字节；将 A 设为冷启动必读后为 120 字节。

自定义位图字体，逐码点贪心换行；无复杂文本整形、字体回退或单词折行。 合理新增需求可沿现有身份和结果接入多字体/连字处理、编辑命中测试和标牌导出；当前尚未实现这些扩展，不保证固定数量未来题目。
