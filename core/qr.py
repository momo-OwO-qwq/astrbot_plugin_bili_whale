"""纯标准库二维码生成器（byte 模式，纠错等级 L/M，版本 1~10）。

为什么自己写：AstrBot 那台机器不一定装了 `qrcode`/Pillow，而扫码登录必须
在群里直接甩出一张二维码图片。这里只用 zlib + struct 手写 PNG，零依赖。

结构：
    make(text, ec="M") -> list[list[int]]      # 模块矩阵，1=黑
    to_png(text, path, ec="M", scale=8)        # 直接落盘
"""
from __future__ import annotations

# 移植自 astrbot_plugin_bili_whale（MIT）；算法未改，只调整了模块位置。
import struct
import zlib

# ---------------------------------------------------------------- GF(256)
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_ecc(data: list[int], nsym: int) -> list[int]:
    """Reed-Solomon 纠错码字（生成多项式用 GF(256) 的 0x11D）。"""
    gen = [1]
    for i in range(nsym):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _gf_mul(g, _EXP[i])
        gen = nxt
    msg = list(data) + [0] * nsym
    for i in range(len(data)):
        coef = msg[i]
        if coef:
            for j, g in enumerate(gen):
                msg[i + j] ^= _gf_mul(g, coef)
    return msg[len(data):]


# ------------------------------------------------ 版本/纠错 表（版本 1~10）
# 每条：(每块总码字, 每块数据码字) × 块数；写法沿用 QR 规范
# 形式 (块数, 总码字, 数据码字[, 块数, 总码字, 数据码字])
_RS = {
    ("L", 1): [(1, 26, 19)],
    ("M", 1): [(1, 26, 16)],
    ("L", 2): [(1, 44, 34)],
    ("M", 2): [(1, 44, 28)],
    ("L", 3): [(1, 70, 55)],
    ("M", 3): [(1, 70, 44)],
    ("L", 4): [(1, 100, 80)],
    ("M", 4): [(2, 50, 32)],
    ("L", 5): [(1, 134, 108)],
    ("M", 5): [(2, 67, 43)],
    ("L", 6): [(2, 86, 68)],
    ("M", 6): [(4, 43, 27)],
    ("L", 7): [(2, 98, 78)],
    ("M", 7): [(4, 49, 31)],
    ("L", 8): [(2, 121, 97)],
    ("M", 8): [(2, 60, 38), (2, 61, 39)],
    ("L", 9): [(2, 146, 116)],
    ("M", 9): [(3, 58, 36), (2, 59, 37)],
    ("L", 10): [(2, 86, 68), (2, 87, 69)],
    ("M", 10): [(4, 69, 43), (1, 70, 44)],
}

_ALIGN = {
    2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30),
    6: (6, 34), 7: (6, 22, 38), 8: (6, 24, 42),
    9: (6, 26, 46), 10: (6, 28, 50),
}
_EC_BITS = {"L": 0b01, "M": 0b00}


def _blocks(ec: str, version: int) -> list[tuple[int, int]]:
    out = []
    for spec in _RS[(ec, version)]:
        if len(spec) == 3:
            n, total, data = spec
            out += [(total, data)] * n
        else:
            n1, total1, data1, n2, total2, data2 = spec
            out += [(total1, data1)] * n1 + [(total2, data2)] * n2
    return out


def _data_codewords(ec: str, version: int) -> int:
    return sum(d for _, d in _blocks(ec, version))


def _bit_capacity(ec: str, version: int) -> int:
    return _data_codewords(ec, version) * 8


def _pick_version(nbytes: int, ec: str) -> int | None:
    for v in range(1, 11):
        cci = 8 if v < 10 else 16
        need = 4 + cci + nbytes * 8
        if need <= _bit_capacity(ec, v):
            return v
    return None


def _encode_payload(data: bytes, ec: str, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                                  # byte 模式
    put(len(data), 8 if version < 10 else 16)       # 字符计数
    for b in data:
        put(b, 8)

    cap = _bit_capacity(ec, version)
    bits += [0] * min(4, cap - len(bits))           # 终止符
    while len(bits) % 8:
        bits.append(0)

    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    i = 0
    while len(cw) < _data_codewords(ec, version):
        cw.append(pad[i % 2])
        i += 1

    # 分块 + 纠错 + 交织
    blocks = _blocks(ec, version)
    data_blocks, ecc_blocks = [], []
    pos = 0
    for total, dlen in blocks:
        chunk = cw[pos:pos + dlen]
        pos += dlen
        data_blocks.append(chunk)
        ecc_blocks.append(_rs_ecc(chunk, total - dlen))

    out: list[int] = []
    for i in range(max(len(b) for b in data_blocks)):
        for b in data_blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(max(len(b) for b in ecc_blocks)):
        for b in ecc_blocks:
            if i < len(b):
                out.append(b[i])
    return out


def _bch(data: int, poly: int, poly_bits: int) -> int:
    while data.bit_length() >= poly_bits:
        data ^= poly << (data.bit_length() - poly_bits)
    return data


def _format_bits(ec: str, mask: int) -> int:
    fmt = (_EC_BITS[ec] << 3) | mask
    rem = _bch(fmt << 10, 0x537, 11)
    return ((fmt << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    return (version << 12) | _bch(version << 12, 0x1F25, 13)


def _mask_hit(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def _penalty(m: list[list[bool]]) -> int:
    n = len(m)
    score = 0
    # 规则 1：连续同色
    for line in list(m) + [list(col) for col in zip(*m)]:
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    # 规则 2：2x2 同色
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    # 规则 3：1:1:3:1:1 加 4 白
    pat_a = [True, False, True, True, True, False, True, False, False, False, False]
    pat_b = pat_a[::-1]
    for line in list(m) + [list(col) for col in zip(*m)]:
        for i in range(n - 10):
            seg = line[i:i + 11]
            if seg == pat_a or seg == pat_b:
                score += 40
    # 规则 4：黑白比例
    dark = sum(sum(1 for v in row if v) for row in m)
    pct = dark * 100 // (n * n)
    score += 10 * (min(abs(pct - 50), 50) // 5)
    return score


def _build(data: bytes, ec: str, version: int) -> tuple[list[list[int]], list[list[bool]]]:
    n = 17 + 4 * version
    m = [[False] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def setf(r: int, c: int, v: bool) -> None:
        m[r][c] = v
        fixed[r][c] = True

    # 三个定位图案 + 分隔带
    for (r0, c0) in ((0, 0), (0, n - 7), (n - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < n and 0 <= c < n):
                    continue
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                ring = inside and (dr in (0, 6) or dc in (0, 6))
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                setf(r, c, bool(ring or core))

    # 定时图案
    for i in range(n):
        if not fixed[6][i]:
            setf(6, i, i % 2 == 0)
        if not fixed[i][6]:
            setf(i, 6, i % 2 == 0)

    # 校正图案：与三个定位图案重叠的三处才跳过
    pos = _ALIGN.get(version, ())
    for i, r0 in enumerate(pos):
        for j, c0 in enumerate(pos):
            if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    setf(r0 + dr, c0 + dc, max(abs(dr), abs(dc)) in (0, 2))

    # 暗模块 + 预留格式/版本信息区
    setf(n - 8, 8, True)
    for i in range(9):
        for (r, c) in ((8, i), (i, 8)):
            if not fixed[r][c]:
                setf(r, c, False)
    for i in range(8):
        setf(8, n - 1 - i, False)
        setf(n - 1 - i, 8, False)
    if version >= 7:
        for i in range(6):
            for j in range(3):
                setf(i, n - 11 + j, False)
                setf(n - 11 + j, i, False)

    # 数据区（右下角起 Z 字形）
    cw = _encode_payload(data, ec, version)
    bits: list[int] = []
    for byte in cw:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    idx = 0
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for r in rows:
            for c in (col, col - 1):
                if not fixed[r][c]:
                    bit = bits[idx] if idx < len(bits) else 0
                    idx += 1
                    m[r][c] = bool(bit)
        upward = not upward
        col -= 2

    return m, fixed


def _with_format(m: list[list[int]], ec: str, mask: int, version: int) -> list[list[int]]:
    n = len(m)
    out = [row[:] for row in m]
    fmt = _format_bits(ec, mask)
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        # 左上竖向
        if i < 6:
            out[i][8] = bit
        elif i < 8:
            out[i + 1][8] = bit
        else:
            out[n - 15 + i][8] = bit
        # 左上横向
        if i < 8:
            out[8][n - 1 - i] = bit
        elif i < 9:
            out[8][15 - i - 1 + 1] = bit
        else:
            out[8][15 - i - 1] = bit
    if version >= 7:
        vb = _version_bits(version)
        for i in range(18):
            bit = bool((vb >> i) & 1)
            out[n - 11 + i % 3][i // 3] = bit
            out[i // 3][n - 11 + i % 3] = bit
    return out


def _apply_mask(m: list[list[int]], fixed: list[list[bool]], mask: int) -> list[list[int]]:
    n = len(m)
    out = [row[:] for row in m]
    for r in range(n):
        for c in range(n):
            if not fixed[r][c] and _mask_hit(mask, r, c):
                out[r][c] = not out[r][c]
    return out


def make(text: str, ec: str = "M") -> list[list[int]]:
    """返回二维码模块矩阵（True=黑）。文本太长（>~200 字节）会抛 ValueError。"""
    data = text.encode("utf-8")
    version = _pick_version(len(data), ec)
    if version is None:
        raise ValueError(f"内容太长，放不进二维码（{len(data)} 字节，最大约 {_data_codewords(ec, 10)} 字节）")

    n = 17 + 4 * version
    base, fixed = _build(data, ec, version)

    best, best_score = None, None
    for mask in range(8):
        cand = _apply_mask(base, fixed, mask)
        cand = _with_format(cand, ec, mask, version)
        s = _penalty(cand)
        if best_score is None or s < best_score:
            best, best_score = cand, s
    assert best is not None and len(best) == n
    return best


def to_png(text: str, path: str, ec: str = "M", scale: int = 8, border: int = 4) -> str:
    """把二维码写成灰度 PNG，返回路径。"""
    m = make(text, ec)
    n = len(m)
    dim = (n + border * 2) * scale
    rows = []
    for y in range(dim):
        my = y // scale - border
        line = bytearray()
        for x in range(dim):
            mx = x // scale - border
            dark = 0 <= my < n and 0 <= mx < n and m[my][mx]
            line.append(0 if dark else 255)
        rows.append(b"\x00" + bytes(line))
    raw = b"".join(rows)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
    return path
