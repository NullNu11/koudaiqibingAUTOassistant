# -*- coding: utf-8 -*-
"""解析嗅探日志: 指定时间后的 RECV 帧 (解码 JSON), 过滤噪声"""
import json
import sys

LOG = r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt"
since = sys.argv[1] if len(sys.argv) > 1 else "17:45:00"
skip = {0, 10007, 12000, 12003, 12004, 10056}

with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("dir") != "RECV" or r.get("t", "") < since:
            continue
        if r.get("cmd") in skip:
            continue
        body = r.get("body_hex", "")
        try:
            txt = bytes.fromhex(body).decode("utf-8", errors="replace")
        except Exception:
            txt = f"<hex {len(body)//2}B>"
        print(f"{r['t']} cmd={r.get('cmd')} order={r.get('order')} len={r.get('plen')} {txt[:300]}")
