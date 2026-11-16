# -*- coding: utf-8 -*-
"""搜索第二个分享者 UID (ASCII 与 varint)"""
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
            return out + bytes([b])


uids = {"ASCII11981908053603": b"11981908053603",
        "VARINT11981908053603": varint(11981908053603)}
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("t", "") < "17:47:00":
            continue
        raw = bytes.fromhex(r.get("body_hex", ""))
        for name, pat in uids.items():
            i = raw.find(pat)
            if i >= 0:
                print(f"{r['t']} {r['dir']} cmd={r.get('cmd')} fmt={r.get('fmt')} plen={r.get('plen')} [{name}] pos={i}")
                print("  附近:", raw[max(0, i - 70):i + 70])
                break
