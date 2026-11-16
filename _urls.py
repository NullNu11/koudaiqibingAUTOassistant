# -*- coding: utf-8 -*-
"""统计嗅探日志中的 WebSocket URL 与 cmd=None 帧的 url 字段"""
import json
from collections import Counter

LOG = r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt"
urls = Counter(); none_urls = Counter()
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        u = r.get("url")
        if u:
            if r.get("cmd") is None:
                none_urls[u] += 1
            else:
                urls[u] += 1
print("=== cmd=None frame urls ===")
for u, n in none_urls.most_common():
    print(f"  {n:5d}  {u}")
