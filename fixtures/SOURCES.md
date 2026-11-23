# 样例来源

全部样例为作者自制合成数据，采用本仓MIT许可。遵循本仓docs/contract.md明确的有界模型，不来自客户、真实样本或生产作业。

`fixtures/intake-demo/` 的素材包（`pack-main.json`、`pack-alt.json`）由本仓 `compile-pack` 从内嵌在生成脚本中的 v1 排版合成；`rev1.json`–`rev3.json` 只引用这些别名。两包刻意让 `alt/zz` 的整帧与 `main/aa` 逐点相同而场景名不同，并让 `alt/aa` 成为同名异画，用于台账验收测试。均为自制数据，不来自客户或生产作业。

`fixtures/inspect-demo/` 的素材包（`pack.json`）由本仓 `compile-pack` 从 `scenes.json` 的 v1 排版合成：72×8 屏上 A 全暗、B 亮 G 组三块、C 亮 G+Y 两组、D 亮 X 组三块（单点亮块为块内左上角像素，编码 `8000000000000000`）。`plan.json` 把四个画面登记为候选 R0/R1/S0/S1（仅 R0、S1 获准），九块全部可读、至多读六块、容错一次。`card.json` 为 `inspect-plan` 的对应输出；`readback-cleared.json`、`readback-rejected.json` 绑定该卡摘要，分别对应真实画面 S1 与 S0。均为自制合成数据。

`fixtures/campaign-demo/` 的 `source.json` 为手工编写的两屏型（GATE-64、PIER-32）四消息批量适配输入，字形/字偶距为自制合成站台字体（含一个 14 行高的 '7' 与两条负字偶距）；`report.json` 为本仓 `campaign-fit` 对该源的对应输出（4 可行 / 4 不可行，两种失败原因齐全）。均为自制合成数据，不来自客户或生产作业。
