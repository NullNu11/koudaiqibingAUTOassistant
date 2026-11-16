# -*- coding: utf-8 -*-
"""从嗅探日志提取完整的 SEND 帧载荷(JSON), 生成打野模板"""
import io
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "topwar_sniff_log.txt")

lines = io.open(LOG, encoding="utf-8", errors="replace").read().splitlines()
seen = set()
for l in lines:
    try:
        r = json.loads(l)
    except Exception:
        continue
    if r.get("dir") != "SEND":
        continue
    c = r.get("cmd")
    if c not in (906, 905, 999, 1201):
        continue
    h = r.get("body_hex", "")
    if not h:
        continue
    try:
        plain = bytes.fromhex(h).decode("utf-8")
        j = json.loads(plain)
    except Exception:
        continue
    key = (c, json.dumps(j, sort_keys=True)[:60])
    if key in seen:
        continue
    seen.add(key)
    print(f"===== cmd={c} t={r.get('t')} =====")
    print(json.dumps(j, ensure_ascii=False, indent=1)[:2000])
    print()
