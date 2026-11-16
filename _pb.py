# -*- coding: utf-8 -*-
"""极简 protobuf 解码器 + 解析 256 聊天响应"""
import json
import sys

LOG = r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt"


def rd_varint(b, i):
    v = 0
    sh = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << sh
        if not (c & 0x80):
            return v, i
        sh += 7


def parse(b, depth=0, max_depth=6):
    """返回 [(field, wire, value)] ; wire2 的 value 若可解析为 pb 则递归"""
    out = []
    i = 0
    while i < len(b):
        try:
            key, i = rd_varint(b, i)
        except Exception:
            break
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = rd_varint(b, i)
            out.append((f, 0, v))
        elif w == 2:
            ln, i = rd_varint(b, i)
            seg = b[i:i + ln]
            i += ln
            out.append((f, 2, seg))
        elif w == 5:
            out.append((f, 5, b[i:i + 4]))
            i += 4
        elif w == 1:
            out.append((f, 1, b[i:i + 8]))
            i += 8
        else:
            break
    return out


def show(b, depth=0, max_depth=5):
    pad = "  " * depth
    for f, w, v in parse(b):
        if w == 2:
            txt = None
            try:
                s = v.decode("utf-8")
                if all(32 <= ord(c) < 0x10000 or c in "\n\r\t" for c in s):
                    txt = s
            except Exception:
                pass
            if txt is not None and len(txt) > 0:
                print(f"{pad}f{f} str({len(v)}) {txt[:120]!r}")
                if depth < max_depth and len(v) > 24 and any(c < 0x20 for c in v):
                    show(v, depth + 1, max_depth)
            else:
                sub = parse(v)
                ok = bool(sub) and all(1 <= f2 <= 20 for f2, _, _ in sub)
                if ok and depth < max_depth and len(v) > 2:
                    print(f"{pad}f{f} pb({len(v)}) {{")
                    show(v, depth + 1, max_depth)
                    print(f"{pad}}}")
                else:
                    print(f"{pad}f{f} bytes({len(v)}) {v[:48].hex()}")
        else:
            print(f"{pad}f{f} v{w} = {v}")


# 256 响应体: 去掉前缀 00000006 10<varint> 0000???? 后是 protobuf
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("dir") != "RECV" or r.get("cmd") != 256 or r.get("t", "") < "17:47:00":
            continue
        raw = bytes.fromhex(r.get("body_hex", ""))
        print(f"===== {r['t']} cmd=256 len={len(raw)} =====")
        # 尝试从第 13 字节开始解 (跳过 4+varint+4 结构, 自动探测 0a 开头)
        for off in range(8, 20):
            if raw[off] == 0x0A:
                break
        show(raw[off:])
