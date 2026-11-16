# -*- coding: utf-8 -*-
"""统计指定时间后各 cmd 出现次数(分方向)"""
import collections
import json
import sys

LOG = r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt"
since = sys.argv[1] if len(sys.argv) > 1 else "17:45:50"
cnt = collections.Counter()
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("t", "") < since:
            continue
        cnt[(r.get("dir"), r.get("cmd"))] += 1
for (d, c), n in sorted(cnt.items(), key=lambda kv: -kv[1]):
    print(f"{d} cmd={c} x{n}")
