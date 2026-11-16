# -*- coding: utf-8 -*-
"""检查聊天帧: 256/954 的原始数据 + 是否含指定 uid"""
import json
import sys

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
        if r.get("t", "") < "17:45:50":
            continue
        if r.get("cmd") not in (256, 954, 4394):
            continue
        hx = r.get("body_hex", "")
        raw = bytes.fromhex(hx)
        mark = "  <<< 含目标UID!" if UID in raw else ""
        print(f"{r['t']} {r['dir']} cmd={r.get('cmd')} fmt={r.get('fmt')} plen={r.get('plen')}{mark}")
        print("  hex head:", hx[:400])
