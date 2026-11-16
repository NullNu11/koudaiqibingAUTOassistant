# -*- coding: utf-8 -*-
"""输出指定时间/cmd 的原始 body_hex (SEND 和 RECV 都要)"""
import json
import sys

LOG = r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt"
t0 = sys.argv[1]
cmd_want = int(sys.argv[2])
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("t", "") < t0 or r.get("cmd") != cmd_want:
            continue
        print(f"{r['t']} {r['dir']} cmd={cmd_want} order={r.get('order')} fmt={r.get('fmt')} plen={r.get('plen')}")
        print("  hex:", r.get("body_hex", r.get("raw_hex", "")))
