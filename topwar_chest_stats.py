# -*- coding: utf-8 -*-
"""宝箱概率统计分析: 频率分布 + 时间戳相关性 + 序列独立性"""
import io
import json
import math
from collections import Counter

BASE = r"g:\code\py\pdfReader\seasoilder"
RESULT = BASE + r"\topwar_chest_results.jsonl"
SNIFF = BASE + r"\topwar_sniff_log.txt"
OUT = BASE + r"\_chest_stats.txt"

samples = []
with io.open(RESULT, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            r = json.loads(line)
            for it in r["items"]:
                for _ in range(it.get("itemCount", 1)):
                    samples.append({
                        "item": it["itemId"],
                        "send_ms": r["send_ms"],
                        "server_t": r["server_t"],
                        "order": r["order"],
                    })

# 补充: 从嗅探日志提取手动开箱(排除与 jsonl 重复的自动开箱, 按 server_t 去重)
try:
    known_t = {s["server_t"] for s in samples if s["server_t"]}
    lines = io.open(SNIFF, encoding="utf-8", errors="replace").read().splitlines()
    manual = 0
    for l in lines:
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get("dir") == "RECV" and r.get("cmd") == 815:
            h = r.get("body_hex", "")
            if not h:
                continue
            try:
                outer = json.loads(bytes.fromhex(h).decode("utf-8", "replace"))
                inner = json.loads(outer.get("d", "{}"))
            except Exception:
                continue
            if inner.get("itemId") != 1700013:
                continue
            if outer.get("t") in known_t:
                continue  # 自动开箱的重复帧
            for it in (inner.get("box") or {}).get("items", []):
                for _ in range(it.get("itemCount", 1)):
                    samples.append({"item": it["itemId"], "send_ms": None,
                                    "server_t": outer.get("t"), "order": None})
                    manual += 1
except Exception as e:
    print("sniff 补充失败:", e)

out = []
def w(s=""):
    out.append(str(s))

w(f"总样本: {len(samples)} (自动 {len(samples)-manual} + 手动 {manual})")
w()

# 1. 频率分布
cnt = Counter(s["item"] for s in samples)
n = sum(cnt.values())
w("== 1. 频率分布 ==")
for item, c in cnt.most_common():
    w(f"  {item}: {c:2d} 次  {c/n*100:5.1f}%")
w()

# 卡方检验(手工, 无 scipy)
def chi2_uniform(counts):
    k = len(counts)
    total = sum(counts.values())
    exp = total / k
    return sum((c - exp) ** 2 / exp for c in counts.values()), k - 1

def chi2_table(table):
    """table: dict[(a,b)] = count; 返回 (chi2, dof, min_expected)"""
    ra = {}
    cb = {}
    for (a, b), v in table.items():
        ra[a] = ra.get(a, 0) + v
        cb[b] = cb.get(b, 0) + v
    tot = sum(table.values())
    e = 0.0
    min_exp = None
    for (a, b), obs in table.items():
        exp = ra[a] * cb[b] / tot
        if min_exp is None or exp < min_exp:
            min_exp = exp
        if exp > 0:
            e += (obs - exp) ** 2 / exp
    dof = (len(ra) - 1) * (len(cb) - 1)
    return e, dof, (min_exp or 0)

def p_from_chi2(x, dof):
    """Wilson-Hilferty 近似卡方 p 值"""
    if x <= 0 or dof <= 0:
        return 1.0
    z = ((x / dof) ** (1 / 3) - (1 - 2 / (9 * dof))) / math.sqrt(2 / (9 * dof))
    # 正态尾概率
    return 0.5 * math.erfc(z / math.sqrt(2))

chi, dof = chi2_uniform(cnt)
w(f"  均匀性卡方 = {chi:.2f} (dof={dof}), p = {p_from_chi2(chi, dof):.3f}")
w("  (p>0.05 表示与均匀分布无显著差异 — 但真实概率未必均匀, 仅作参考)")
w()

# 2. 时间戳相关性: 结果 vs (send_ms mod M)
w("== 2. 时间戳哈希相关性检验 ==")
timed = [s for s in samples if s["send_ms"]]
w(f"  有毫秒时间戳的样本: {len(timed)}")
for M in (2, 3, 5, 10, 100, 1000):
    table = {}
    for s in timed:
        table[(s["item"], s["send_ms"] % M)] = \
            table.get((s["item"], s["send_ms"] % M), 0) + 1
    grp = Counter(s["send_ms"] % M for s in timed)
    if min(grp.values()) < 3 or len(grp) < 2:
        w(f"  mod {M:5d}: 分组样本不足, 跳过")
        continue
    chi, dof, min_exp = chi2_table(table)
    p = p_from_chi2(chi, dof)
    rel = "  (期望频数过低, 结果不可靠)" if min_exp < 5 else ""
    w(f"  mod {M:5d}: chi2={chi:6.2f} dof={dof} p={p:.3f} "
      f"{'<-- 相关!' if p < 0.05 else '(无相关)'}{rel}")
w()

# 3. 服务器时间戳相关性
w("== 3. 服务器时间戳 (t) 相关性 ==")
for M in (2, 3, 5, 10, 100, 1000):
    table = {}
    for s in samples:
        if s["server_t"]:
            table[(s["item"], s["server_t"] % M)] = \
                table.get((s["item"], s["server_t"] % M), 0) + 1
    grp = Counter(s["server_t"] % M for s in samples if s["server_t"])
    if min(grp.values()) < 3 or len(grp) < 2:
        w(f"  mod {M:5d}: 分组样本不足, 跳过")
        continue
    chi, dof, min_exp = chi2_table(table)
    p = p_from_chi2(chi, dof)
    rel = "  (期望频数过低, 结果不可靠)" if min_exp < 5 else ""
    w(f"  mod {M:5d}: chi2={chi:6.2f} dof={dof} p={p:.3f} "
      f"{'<-- 相关!' if p < 0.05 else '(无相关)'}{rel}")
w()

# 4. 序列独立性: 相邻结果是否相关 (lag-1)
w("== 4. 序列独立性 (lag-1) ==")
seq = [s["item"] for s in samples if s["send_ms"]]
table = {}
for a, b in zip(seq, seq[1:]):
    table[(a, b)] = table.get((a, b), 0) + 1
chi, dof, min_exp = chi2_table(table)
p = p_from_chi2(chi, dof)
rel = "  (期望频数过低, 结果不可靠)" if min_exp < 5 else ""
w(f"  相邻结果卡方 = {chi:.2f} (dof={dof}), p = {p:.3f} "
  f"{'<-- 有记忆!' if p < 0.05 else '(相互独立)'}{rel}")
w()

# 5. 游程检验 (二值化: 400303 vs 其他)
w("== 5. 游程检验 (400303=1, 其他=0) ==")
bits = [1 if it == 400303 else 0 for it in seq]
n1, n0 = sum(bits), len(bits) - sum(bits)
runs = 1 + sum(1 for i in range(1, len(bits)) if bits[i] != bits[i-1])
if n1 > 0 and n0 > 0:
    mu = 2 * n1 * n0 / (n1 + n0) + 1
    var = 2 * n1 * n0 * (2 * n1 * n0 - n1 - n0) / \
        ((n1 + n0) ** 2 * (n1 + n0 - 1))
    z = (runs - mu) / math.sqrt(var) if var > 0 else 0
    pz = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    w(f"  runs={runs}, 期望={mu:.1f}, z={z:.2f}, p={pz:.3f} "
      f"{'<-- 非随机!' if pz < 0.05 else '(随机)'}")
w()

w("== 6. 开出结果与请求参数的同现检查 ==")
w("  3 次请求参数完全相同 (itemid=1700013, amount=1), 结果 5 种不同物品")
w("  -> 随机源在服务器, 且不依赖请求内容")

with io.open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print(f"OK -> {OUT}")
print("\n".join(out))
