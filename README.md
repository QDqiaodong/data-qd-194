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

实际7项测试通过，演示摘要：

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

## 当前能力与后续集成

自定义位图字体，逐码点贪心换行；无复杂文本整形、字体回退或单词折行。 合理新增需求可沿现有身份和结果接入多字体/连字处理、编辑命中测试和标牌导出；当前尚未实现这些扩展，不保证固定数量未来题目。
