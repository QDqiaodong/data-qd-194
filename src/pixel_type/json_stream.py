"""流式 JSON 读取器：只保留当前解析路径上的对象，避免整份解析树同时存活。

标准库 json.loads 会把 32 MiB 清单膨胀到上百 MiB 的 Python 对象，使峰值 RSS
无法满足 128 MiB 预算。本模块提供事件式读取器 :class：`StreamingReader`，
在字节缓冲上按 JSON 语法增量产出值；装配规范化逻辑每读完一个节点就立即把它
压缩成紧凑表示并丢弃原始对象，因此同一时刻只有正在读取的单个节点占用对象内存。

支持完整 JSON 语法（对象/数组/字符串转义/数字/true/false/null，含 \\uXXXX 与
代理对）；重复键与非有限数由装配层的严格校验或本读取器的字面量检查拒绝。
"""

_READ_SIZE = 1 << 20


class JsonSyntaxError(ValueError):
    pass


class StreamingReader:
    def __init__(self, stream):
        self._stream = stream
        self._buf = b''
        self._pos = 0
        self._eof = False

    def _fill(self, need=1):
        """确保缓冲区从 _pos 起至少有 need 个未读字节（EOF 时尽力而为）。"""
        if self._pos > _READ_SIZE:
            self._buf = self._buf[self._pos:]
            self._pos = 0
        while not self._eof and len(self._buf) - self._pos < need:
            chunk = self._stream.read(_READ_SIZE)
            if not chunk:
                self._eof = True
                break
            self._buf += chunk

    def _peek(self):
        self._fill(1)
        if self._pos >= len(self._buf):
            raise JsonSyntaxError('JSON 提前结束')
        return self._buf[self._pos]

    def _skip_ws(self):
        while True:
            self._fill(1)
            if self._pos >= len(self._buf):
                return
            ch = self._buf[self._pos]
            if ch in (0x20, 0x09, 0x0A, 0x0D):
                self._pos += 1
            else:
                return

    def expect(self, byte):
        self._skip_ws()
        if self._peek() != byte:
            raise JsonSyntaxError(f'位置 {self._pos} 期望 {byte!r}')
        self._pos += 1

    def consume(self):
        """读取并返回下一个完整 JSON 值（标量直接返回，容器返回迭代句柄）。"""
        self._skip_ws()
        ch = self._peek()
        if ch == 0x7B:      # {
            self._pos += 1
            return ObjectCursor(self)
        if ch == 0x5B:      # [
            self._pos += 1
            return ArrayCursor(self)
        if ch == 0x22:      # "
            return self._read_string()
        if ch == 0x74 or ch == 0x66 or ch == 0x6E:   # true/false/null
            return self._read_literal()
        if ch == 0x2D or 0x30 <= ch <= 0x39:
            return self._read_number()
        raise JsonSyntaxError(f'位置 {self._pos} 非法 JSON 值起始字节 {ch!r}')

    def _read_literal(self):
        self._fill(5)
        fragment = self._buf[self._pos:self._pos + 5]
        # 显式拒绝 JSON 不允许的非有限数标记（标准库 parse_constant 口径）。
        if fragment[:3] == b'NaN' or fragment == b'Infini' or \
                self._buf[self._pos:self._pos + 4] == b'-Inf':
            raise JsonSyntaxError('NaN/Infinity 不是合法 JSON 值')
        if fragment[:4] == b'true':
            self._pos += 4
            return True
        if fragment[:4] == b'null':
            self._pos += 4
            return None
        if fragment == b'false':
            self._pos += 5
            return False
        raise JsonSyntaxError(f'位置 {self._pos} 非法字面量')

    def _read_number(self):
        # 逐字节收集，缓冲压缩不会使已收集的 token 失效。
        token = bytearray()
        self._fill(1)
        if self._buf[self._pos] == 0x2D:
            token.append(0x2D)
            self._pos += 1
        while True:
            self._fill(1)
            if self._pos >= len(self._buf):
                break
            ch = self._buf[self._pos]
            if 0x30 <= ch <= 0x39 or ch in (0x2E, 0x65, 0x45, 0x2B, 0x2D):
                token.append(ch)
                self._pos += 1
            else:
                break
        text = token.decode('ascii')
        if not text or text == '-':
            raise JsonSyntaxError(f'非法数字 {text!r}')
        try:
            if any(c in text for c in '.eE'):
                return float(text)
            return int(text)
        except ValueError as exc:
            raise JsonSyntaxError(f'非法数字 {text!r}') from exc

    def _read_string(self):
        if self._peek() != 0x22:
            raise JsonSyntaxError('字符串必须以引号开始')
        self._pos += 1
        out = bytearray()
        while True:
            self._fill(1)
            if self._pos >= len(self._buf):
                raise JsonSyntaxError('字符串未闭合')
            ch = self._buf[self._pos]
            if ch == 0x22:
                self._pos += 1
                return out.decode('utf-8')
            if ch < 0x20:
                raise JsonSyntaxError('字符串中出现未转义控制字符')
            if ch == 0x5C:      # 反斜杠转义
                self._pos += 1
                self._fill(1)
                esc = self._buf[self._pos]
                self._pos += 1
                simple = {0x22: b'"', 0x5C: b'\\', 0x2F: b'/',
                          0x62: b'\b', 0x66: b'\f', 0x6E: b'\n',
                          0x72: b'\r', 0x74: b'\t'}
                if esc in simple:
                    out += simple[esc]
                elif esc == 0x75:
                    code = self._read_hex4()
                    if 0xD800 <= code <= 0xDBFF:
                        self._fill(2)
                        if self._buf[self._pos] != 0x5C or \
                                self._buf[self._pos + 1] != 0x75:
                            raise JsonSyntaxError('高代理项后缺少低代理项')
                        self._pos += 2
                        low = self._read_hex4()
                        if not 0xDC00 <= low <= 0xDFFF:
                            raise JsonSyntaxError('非法代理对')
                        code = 0x10000 + ((code - 0xD800) << 10) + \
                            (low - 0xDC00)
                    elif 0xDC00 <= code <= 0xDFFF:
                        raise JsonSyntaxError('意外的低代理项')
                    out += chr(code).encode('utf-8')
                else:
                    raise JsonSyntaxError(f'非法转义 \\{chr(esc)}')
            else:
                # 普通 UTF-8 字节：逐字节前进，每次都先保证缓冲已填满，
                # 因此跨块边界的多字节序列也不会被截断。
                out.append(ch)
                self._pos += 1

    def _read_hex4(self):
        self._fill(4)
        token = self._buf[self._pos:self._pos + 4]
        if len(token) != 4:
            raise JsonSyntaxError('\\u 后需要 4 位十六进制')
        try:
            value = int(token, 16)
        except ValueError as exc:
            raise JsonSyntaxError('非法 \\u 转义') from exc
        self._pos += 4
        return value


class ObjectCursor:
    """事件式对象：逐键读取，键值仍按需流式返回。"""

    def __init__(self, reader):
        self._reader = reader
        self._first = True

    def next_key(self):
        """返回下一个键；对象结束返回 None。重复键由调用方自行拒绝。"""
        reader = self._reader
        reader._skip_ws()
        ch = reader._peek()
        if ch == 0x7D:
            reader._pos += 1
            return None
        if not self._first:
            if ch != 0x2C:
                raise JsonSyntaxError('对象成员之间缺少逗号')
            reader._pos += 1
            reader._skip_ws()
        self._first = False
        if reader._peek() != 0x22:
            raise JsonSyntaxError('对象键必须是字符串')
        return reader._read_string()

    def value(self):
        self._reader.expect(0x3A)
        return self._reader.consume()


class ArrayCursor:
    """事件式数组：逐元素读取，元素仍按需流式返回。"""

    def __init__(self, reader):
        self._reader = reader
        self._first = True

    def next_value(self):
        reader = self._reader
        reader._skip_ws()
        ch = reader._peek()
        if ch == 0x5D:
            reader._pos += 1
            return _END
        if not self._first:
            if ch != 0x2C:
                raise JsonSyntaxError('数组元素之间缺少逗号')
            reader._pos += 1
        self._first = False
        return reader.consume()


_END = object()
