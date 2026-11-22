# 点阵字形排版 v1

标牌设计人员提供1..256个glyphs，每个char为单个可显示Unicode标量、advance为1..128整数、offset为两个-128..128整数，rows为0..32行等宽1..32位01像素串。空rows允许空白字形，不自动补空格。位置单位为像素，坐标y向下，所有字形放在行顶+offset处；无baseline或字体文件格式推断。

kerning给既有字符对left/right和整数adjust；每字符对唯一，right.advance+adjust必须>0以保证排版笔向前。text长度1..2000，除LF换行外禁ASCII控制、DEL、代理项及XML禁止的U+FFFE/U+FFFF；所有非换行字符必须有字形，不隐式字体回退。每个Unicode代码点是一个索引单位，无连字、组合字符整形、双向文字或emoji序列聚类。line_width=1..10000、line_height=1..1000。

逐字符贪心换行。新字笔位置=当前pen+前字/当前字kern，以笔位置+advance是否超line_width决定；若行已有字且超宽则换新行、清空前字和kern。单个超宽字仍放在空行并标overflow，不无限换行。LF强制新行（保留空行和尾随空行）；空格按普通字形保留，可能独占下一行，不做单词折行、修剪或两端对齐。

pen控制换行，真实像素覆盖另算：ink_origin=笔位置+offset，像素坐标来自位图1的位置。超当前行水平/垂直名义矩形或笔跨度越界时，将原始character_index列入overflow，不裁掉像素。summary.drawn_pixels保留重复绘制次数，unique_ink_pixels为空间并集数量；ink_bbox覆盖真实像素，空画面为null。字体像素总绘制预算≤200000。

svg_text置于同一JSON，所有rect由实际placement产生，标题文本XML转义；viewBox包含名义画布与所有真实墨迹。无外部字体/资源/脚本。字形重叠允许，用并集数量诊断，不自动调整。成功先布局渲染再原子新建JSON，拒绝已有目标/链接；结构错误退出2，排版溢出是正常业务结果退出0。
