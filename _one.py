# -*- coding: utf-8 -*-
"""输出指定时间之后指定 cmd 的完整帧"""
import json
import sys

LOG = r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt"
t0 = sys.argv[1]
cmd_want = int(sys.argv[2])
mode = sys.argv[3] if len(sys.argv) > 3 else "RECV"
n = 0
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("dir") != mode or r.get("t", "") < t0 or r.get("cmd") != cmd_want:
            continue
        txt = bytes.fromhex(r.get("body_hex", "")).decode("utf-8", "replace")
        print(f"--- {r['t']} {mode} cmd={cmd_want} order={r.get('order')} ---")
        print(txt[:1500])
        n += 1
        if n >= 6:
            break
