# -*- coding: utf-8 -*-
"""
口袋奇兵 - 宝箱概率统计验证
阶段1: 等待用户手动开 1 个目标宝箱, 自动识别 itemId (cmd=815)
阶段2: 自动逐个开启 (amount=1, 间隔随机 1.5~3 秒), 记录每次结果的毫秒时间戳
结果写入 topwar_chest_results.jsonl

用法: python topwar_chest_test.py [最大开箱数] [itemid]
      传入 itemid 则跳过阶段1直接自动开箱
"""

import base64
import json
import os
import random
import socket
import struct
import sys
import time

from playwright.sync_api import sync_playwright

from topwar_checkin import (BASE, DEBUG_PORT, EDGE_CANDIDATES, GAME_URL,
                            PROFILE, WS_HOOK_JS, ensure_edge, port_open)

CMD_OPEN = 815          # 开宝箱
MAX_OPENS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
FIXED_ITEMID = int(sys.argv[2]) if len(sys.argv) > 2 else None
RESULT_FILE = os.path.join(BASE, "topwar_chest_results.jsonl")


def decrypt_raw(raw: bytes):
    """解密帧, 返回 (cmd, order, fmt, plain_body) 或 None"""
    if len(raw) < 13:
        return None
    cmd, o, ln = struct.unpack(">iii", raw[:12])
    if ln < 1 or len(raw) < 12 + ln:
        return None
    key = struct.pack(">i", 0x01010101 | o)
    fmt = raw[12] ^ key[0]
    body = bytes(b ^ key[(u + 1) % 4] for u, b in enumerate(raw[13:12 + ln]))
    return cmd, o, fmt, body


def extract_frames(raw: bytes):
    """从(可能粘包的)载荷中提取所有完整帧"""
    frames = []
    pos = 0
    while pos + 13 <= len(raw):
        d = decrypt_raw(raw[pos:])
        if d is None:
            break
        cmd, o, ln = struct.unpack(">iii", raw[pos:pos + 12])
        total = 12 + ln
        frames.append((cmd, o, d[2], d[3]))
        pos += total
    return frames


def main():
    if not ensure_edge():
        return

    results = []
    received = []      # [(ts, raw_bytes)]
    sent_seen = []     # [(ts, cmd, order, plain)]

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        context = browser.contexts[0]

        ws_url_map = {}

        def on_ws_created(params):
            ws_url_map[params.get("requestId")] = params.get("url", "")

        def add_frame(pd, direction):
            try:
                raw = base64.b64decode(pd)
            except Exception:
                return
            ts = time.time()
            received.append((ts, raw, direction))

        def on_recv(params):
            add_frame(params.get("response", {}).get("payloadData", ""), "recv")

        def on_sent(params):
            add_frame(params.get("response", {}).get("payloadData", ""), "sent")

        game_pages = [pg for pg in context.pages if "rivergame" in pg.url]
        for extra in game_pages[1:]:
            try:
                extra.close()
            except Exception:
                pass
        page = game_pages[0] if game_pages else context.new_page()
        page.add_init_script(WS_HOOK_JS)
        already = False
        try:
            already = page.evaluate("""() => !!window.__tw_hooked &&
                (window.__tw_sockets || []).some(
                    it => it.ws.readyState === 1 && /rivergame/i.test(it.url))""")
        except Exception:
            already = False
        if already:
            print("[OK] 页面已登录且 socket 可用, 直接复用")
        else:
            print("[..] 刷新游戏页面...")
            page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)

        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.on("Network.webSocketCreated", on_ws_created)
        cdp.on("Network.webSocketFrameReceived", on_recv)
        cdp.on("Network.webSocketFrameSent", on_sent)

        # 等待 socket 就绪
        print("[..] 等待游戏 socket 就绪(最多 180 秒)...")
        t0 = time.time()
        ready = False
        while time.time() - t0 < 180:
            time.sleep(2)
            try:
                ready = page.evaluate("""() => !!window.__tw_hooked &&
                    (window.__tw_sockets || []).some(
                        it => it.ws.readyState === 1 && /rivergame/i.test(it.url))""")
            except Exception:
                ready = False
            if ready:
                break
        if not ready:
            print("[!] socket 未就绪")
            return
        print("[OK] 已就绪")

        def list_sockets():
            return page.evaluate("""() => {
                if (!window.__tw_sockets) return [];
                const out = [];
                window.__tw_sockets.forEach((it, idx) => {
                    if (it.ws && it.ws.readyState === 1 && /rivergame/i.test(it.url)) {
                        out.push({url: it.url, idx: idx});
                    }
                });
                return out;
            }""")

        send_js = """(args) => {
            const {idx, bytes} = args;
            const it = window.__tw_sockets[idx];
            if (!it || it.ws.readyState !== 1) return false;
            it.ws.send(new Uint8Array(bytes).buffer);
            return true;
        }"""

        def encode_open(itemid, amount, order):
            body = json.dumps({"itemid": itemid, "amount": amount},
                              separators=(",", ":")).encode()
            key = struct.pack(">i", 0x01010101 | order)
            fmt = 0
            wire = bytes([fmt ^ key[0]])
            wire += bytes(b ^ key[(u + 1) % 4] for u, b in enumerate(body))
            return struct.pack(">iii", CMD_OPEN, order, len(wire)) + wire

        def wait_response(cmd, order, timeout=20, start_idx=0):
            t0 = time.time()
            while time.time() - t0 < timeout:
                time.sleep(0.5)
                for ts, raw, direction in received[start_idx:]:
                    if direction != "recv":
                        continue
                    for c, o, fmt, plain in extract_frames(raw):
                        if c == cmd and o == order:
                            return ts, fmt, plain
            return None

        # ---------- 阶段1: 识别 itemId ----------
        if FIXED_ITEMID:
            itemid = FIXED_ITEMID
            print(f"\n[OK] 使用命令行指定的 itemid={itemid}, 跳过手动识别")
        else:
            print("\n===== 阶段1: 请在游戏中手动开启 1 个目标宝箱 (最多等 5 分钟) =====")
            itemid = None
            t0 = time.time()
            while time.time() - t0 < 300 and itemid is None:
                time.sleep(1)
                for ts, raw, direction in list(received):
                    if direction != "sent":
                        continue
                    for c, o, fmt, plain in extract_frames(raw):
                        if fmt == 0:
                            try:
                                j = json.loads(plain.decode("utf-8", "replace"))
                            except Exception:
                                continue
                            if isinstance(j, dict) and "itemid" in j:
                                itemid = j["itemid"]
                                print(f"[OK] 识别到开箱命令: itemid={itemid} "
                                      f"(请求: {j})")
                                break
                    if itemid:
                        break

        if not itemid:
            print("[!] 未检测到开箱操作, 退出")
            return

        # ---------- 阶段2: 自动开箱 ----------
        print(f"\n===== 阶段2: 自动开启 itemid={itemid}, 最多 {MAX_OPENS} 次 =====")

        def refresh_sockets():
            socks = list_sockets()
            # 游戏 data 主连接优先: server-knight > 其他
            socks.sort(key=lambda c: 0 if "server-knight" in c["url"] else
                       (1 if "rivergame" in c["url"] else 2))
            return socks

        out_f = open(RESULT_FILE, "a", encoding="utf-8")
        opened = 0
        no_resp = 0
        good_idx = None  # 已验证可用的 socket idx
        for i in range(MAX_OPENS):
            order = 9_000_000 + int(time.time() * 1000) % 900_000
            frame = encode_open(itemid, 1, order)
            start_idx = len(received)
            send_ms = time.time()

            # 决定尝试的 socket 列表: 优先上次成功的
            socks = refresh_sockets()
            order_list = ([s for s in socks if s["idx"] == good_idx] +
                          [s for s in socks if s["idx"] != good_idx])
            r = None
            used_idx = None
            for cand in order_list:
                if cand["url"] == "?":
                    continue
                try:
                    ok = page.evaluate(send_js, {"idx": cand["idx"],
                                                 "bytes": list(frame)})
                except Exception:
                    continue
                if not ok:
                    continue
                r = wait_response(CMD_OPEN, order, timeout=8,
                                  start_idx=start_idx)
                if r:
                    used_idx = cand["idx"]
                    good_idx = used_idx
                    break
            if not r:
                no_resp += 1
                print(f"[{i+1}] 无响应 ({no_resp})")
                if no_resp >= 3:
                    print("[!] 连续无响应, 停止")
                    break
                continue
            no_resp = 0
            resp_ts, fmt, plain = r
            try:
                outer = json.loads(plain.decode("utf-8", "replace"))
            except Exception as e:
                print(f"[{i+1}] 响应解析失败: {e}")
                break
            s = outer.get("s")
            if s != 0:
                print(f"[{i+1}] 服务器返回错误 s={s}: {outer.get('d', '')[:100]}")
                break
            try:
                inner = json.loads(outer.get("d", "{}"))
            except Exception:
                inner = {}
            items = (inner.get("box") or {}).get("items", [])
            remaining = inner.get("amount")
            server_t = outer.get("t")
            rec = {
                "n": i + 1,
                "send_ms": round(send_ms * 1000),
                "order": order,
                "server_t": server_t,
                "remaining": remaining,
                "items": items,
            }
            results.append(rec)
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out_f.flush()
            opened += 1
            item_str = ", ".join(f"{it.get('itemId')}x{it.get('itemCount')}"
                                 for it in items) or "(空)"
            print(f"[{i+1}] 开出: {item_str} | 剩余:{remaining} | "
                  f"t={server_t} send={rec['send_ms']}")
            if remaining == 0:
                print("[OK] 宝箱已用完, 自动停止")
                break
            time.sleep(random.uniform(1.5, 3.0))

        out_f.close()
        print(f"\n[完成] 共开启 {opened} 次, 结果已保存: {RESULT_FILE}")
        # 不关闭浏览器


if __name__ == "__main__":
    main()
