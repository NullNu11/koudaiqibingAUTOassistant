# -*- coding: utf-8 -*-
"""全局搜索包含目标 UID 字节串的帧"""
import json

LOG = r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt"
UID = b"15411049238737"
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
        if UID in raw:
            print(f"{r['t']} {r['dir']} cmd={r.get('cmd')} fmt={r.get('fmt')} plen={r.get('plen')}")
            # 展示 UID 附近内容
            i = raw.find(UID)
            seg = raw[max(0, i - 60):i + 60]
            print("  附近:", seg)
