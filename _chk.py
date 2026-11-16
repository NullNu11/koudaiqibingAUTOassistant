# -*- coding: utf-8 -*-
import os
import time

BASE = r"g:\code\py\pdfReader\seasoilder"
log = os.path.join(BASE, "topwar_sniff_log.txt")
out = os.path.join(BASE, "_sniff_out.log")
err = os.path.join(BASE, "_sniff_err.log")

print("now:", time.strftime("%H:%M:%S"))
if os.path.exists(log):
    st = os.stat(log)
    print(f"sniff_log: {st.st_size} bytes, mtime {time.strftime('%H:%M:%S', time.localtime(st.st_mtime))}")
    with open(log, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    print("last lines:")
    for ln in lines[-5:]:
        print("  ", ln.strip()[:150])
else:
    print("sniff_log 不存在")
for name in (out, err):
    if os.path.exists(name):
        print(f"--- {os.path.basename(name)} tail ---")
        print(open(name, encoding="utf-8", errors="replace").read()[-800:])
