# -*- coding: utf-8 -*-
import io, json
lines = io.open(r"g:\code\py\pdfReader\seasoilder\topwar_sniff_log.txt",
                encoding="utf-8", errors="replace").read().splitlines()
print("total", len(lines))
for l in lines[-12:]:
    try:
        r = json.loads(l)
        print(r.get("t"), r.get("dir"), "cmd=", r.get("cmd"), "plen=", r.get("plen"))
    except Exception:
        print(l[:150])
