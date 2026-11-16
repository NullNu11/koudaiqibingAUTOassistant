# -*- coding: utf-8 -*-
"""搜索 varint 编码的目标 UID"""
import json

LOG = r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt"


def varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


target = varint(15411049238737)
print("varint:", target.hex())
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("t", "") < "17:40:00":
            continue
        raw = bytes.fromhex(r.get("body_hex", ""))
        i = raw.find(target)
        if i >= 0:
            print(f"{r['t']} {r['dir']} cmd={r.get('cmd')} fmt={r.get('fmt')} plen={r.get('plen')} pos={i}")
            print("  附近:", raw[max(0, i - 50):i + 50])
