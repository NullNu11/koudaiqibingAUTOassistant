# -*- coding: utf-8 -*-
"""
口袋奇兵(TopWar) H5 - 自动一键领取奖励 (自动签到)

原理:
- 复用已登录的浏览器 profile (topwar_profile), 页面自动恢复登录态
- 注入 WebSocket Hook 获取游戏 socket, 直接发送加密的二进制协议帧
- cmd 5684 查询可领取任务 (TASK_RELAX_ONE_CLICK_GET_INFO)
- cmd 5685 一键领取奖励   (TASK_RELAX_ONE_CLICK_GET_REWARD)

协议:
  帧头: [cmd int32 BE][order int32 BE][bodyLen int32 BE][format u8][加密载荷]
  加密: key32 = (0x01010101|order) 的 BE 4字节
        format ^= key[0]; body[u] ^= key[(u+1)%4]
  PROTOBUFFV2 body: [int32 d][d字节头][int32 n][n字节 protobuf]

用法: python topwar_checkin.py
"""

import base64
import os
import re
import socket
import struct
import subprocess
import time

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(BASE, "topwar_profile")
DEBUG_PORT = 9222
GAME_URL = "https://warh5.rivergame.net/webgame/index.html"

CMD_QUERY = 5684    # TASK_RELAX_ONE_CLICK_GET_INFO  (PBReqEmpty)
CMD_CLAIM = 5685    # TASK_RELAX_ONE_CLICK_GET_REWARD (ids=[1..6])
CLAIM_IDS = [1, 2, 3, 4, 5, 6]
FORMAT_PB_V2 = 2

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

WS_HOOK_JS = """
(function(){
  if (window.__tw_hooked) return;
  window.__tw_hooked = true;
  window.__tw_sockets = [];
  const Orig = window.WebSocket;
  function HookedWS(url, protocols) {
    const ws = protocols === undefined ? new Orig(url) : new Orig(url, protocols);
    try { window.__tw_sockets.push({ws: ws, url: String(url)}); } catch(e) {}
    return ws;
  }
  HookedWS.prototype = Orig.prototype;
  ['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(k => { try { HookedWS[k] = Orig[k]; } catch(e){} });
  window.WebSocket = HookedWS;
})();
"""


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_edge():
    if port_open(DEBUG_PORT):
        return True
    edge = next((e for e in EDGE_CANDIDATES if os.path.exists(e)), None)
    if not edge:
        print("[!] 未找到 Edge")
        return False
    subprocess.Popen([edge, f"--remote-debugging-port={DEBUG_PORT}",
                      f"--user-data-dir={PROFILE}",
                      "--no-first-run", "--no-default-browser-check",
                      "--start-maximized"])
    for _ in range(30):
        time.sleep(0.5)
        if port_open(DEBUG_PORT):
            return True
    return False


# ---------------- 协议编解码 ----------------

def encode_frame(cmd: int, order: int, body_plain: bytes) -> bytes:
    """构造加密后的完整帧"""
    key = struct.pack(">i", 0x01010101 | order)
    fmt = FORMAT_PB_V2
    wire = bytes([fmt ^ key[0]])
    wire += bytes(b ^ key[(u + 1) % 4] for u, b in enumerate(body_plain))
    return struct.pack(">iii", cmd, order, len(wire)) + wire


def decode_frame(raw: bytes):
    """解密收到的帧, 返回 (cmd, order, fmt, header_bytes, data_bytes)"""
    if len(raw) < 13:
        return None
    cmd, o, ln = struct.unpack(">iii", raw[:12])
    if ln < 1 or len(raw) < 12 + ln:
        return None
    key = struct.pack(">i", 0x01010101 | o)
    fmt = raw[12] ^ key[0]
    body = bytes(b ^ key[(u + 1) % 4] for u, b in enumerate(raw[13:12 + ln]))
    d = struct.unpack(">i", body[0:4])[0] if len(body) >= 4 else 0
    header = body[4:4 + d] if d >= 0 and len(body) >= 4 + d else b""
    n = struct.unpack(">i", body[4 + d:8 + d])[0] if len(body) >= 8 + d else -1
    data = body[8 + d:8 + d + n] if n >= 0 else b""
    return cmd, o, fmt, header, data


def parse_ack_header(header: bytes):
    """PBDataAckHeader: field1=s(状态), field2=ts(时间戳)"""
    s = None
    ts = None
    i = 0
    while i < len(header):
        tag = header[i]
        i += 1
        field, wire = tag >> 3, tag & 7
        if wire != 0:
            break
        v, shift = 0, 0
        while i < len(header):
            b = header[i]
            i += 1
            v |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        if field == 1:
            s = v
        elif field == 2:
            ts = v
    return s, ts


def parse_task_ids(data: bytes):
    """PBAckTaskRelaxOneClickGetInfo: repeated {field1 varint=id, ...}"""
    ids = []
    i = 0
    while i < len(data):
        if data[i] == 0x0A and i + 1 < len(data):
            ln = data[i + 1]
            group = data[i + 2:i + 2 + ln]
            if group and group[0] == 0x08:
                ids.append(group[1])
            i += 2 + ln
        else:
            break
    return ids


def read_varint(buf, i):
    v, shift = 0, 0
    while i < len(buf):
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return v, i


def parse_claim_result(data: bytes):
    """PBAckTaskRelaxOneClickGetReward: repeated {id:1, count:2, item:3?}"""
    out = []
    i = 0
    while i < len(data):
        if data[i] == 0x0A and i + 1 < len(data):
            ln, i = data[i + 1], i + 2
            g = data[i:i + ln]
            i += ln
            tid = cnt = None
            j = 0
            while j < len(g):
                t = g[j]
                j += 1
                field, wire = t >> 3, t & 7
                if wire == 0:
                    v, j = read_varint(g, j)
                    if field == 1:
                        tid = v
                    elif field == 2:
                        cnt = v
                elif wire == 2:
                    v, j = read_varint(g, j)
                    j += v  # 跳过嵌套道具结构
                else:
                    break
            if tid is not None:
                out.append((tid, cnt))
        else:
            break
    return out


def pb_packed_ints(ids):
    """packed repeated int32: tag 0x0a, len, 值"""
    body = b"".join(struct.pack("B", v) for v in ids)
    return bytes([0x0A, len(body)]) + body


def build_v2_body(data: bytes) -> bytes:
    """PROTOBUFFV2 body: [int32 d=0][int32 n][data]"""
    return struct.pack(">ii", 0, len(data)) + data


# ---------------- 主流程 ----------------

def main():
    if not ensure_edge():
        return

    received = []  # [(ts, raw_bytes)]

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        context = browser.contexts[0]

        # CDP 抓帧
        ws_url_map = {}   # requestId -> url
        ws_stats = {}     # url -> [recv_count, last_ts]

        def on_ws_created(params):
            rid = params.get("requestId")
            url = params.get("url", "")
            ws_url_map[rid] = url
            ws_stats.setdefault(url, [0, 0])

        def on_frame(params):
            rid = params.get("requestId", "")
            url = ws_url_map.get(rid, "?")
            st = ws_stats.setdefault(url, [0, 0])
            st[0] += 1
            st[1] = time.time()
            pd = params.get("response", {}).get("payloadData", "")
            try:
                raw = base64.b64decode(pd)
                received.append((time.time(), raw))
            except Exception:
                pass  # 文本帧(心跳/chatpush)忽略

        # 打开(或复用)游戏页, 注入 hook 后刷新
        game_pages = [pg for pg in context.pages if "rivergame" in pg.url]
        for extra in game_pages[1:]:
            try:
                extra.close()
                print("[..] 已关闭多余的重复标签页")
            except Exception:
                pass
        page = game_pages[0] if game_pages else context.new_page()

        page.add_init_script(WS_HOOK_JS)
        # 若当前页面 hook 已生效且有可用 socket, 直接复用, 不刷新
        already = False
        try:
            already = page.evaluate("""() => !!window.__tw_hooked &&
                (window.__tw_sockets || []).some(
                    it => it.ws.readyState === 1 && /rivergame/i.test(it.url))""")
        except Exception:
            already = False
        if already:
            print("[OK] 页面已登录且 socket 可用, 直接复用(不刷新)")
        else:
            print("[..] 刷新游戏页面(复用登录态, 注入 socket hook)...")
            page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)

        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.on("Network.webSocketCreated", on_ws_created)
        cdp.on("Network.webSocketFrameReceived", on_frame)

        # 等待 hook 生效 且 游戏 socket 已连接, 且登录数据流完成 (最多 240 秒)
        print("[..] 等待游戏登录并连接服务器(最多 240 秒)...")
        t0 = time.time()
        ready = False
        while time.time() - t0 < 240:
            time.sleep(3)
            try:
                ready = page.evaluate("""() => {
                    if (!window.__tw_hooked) return false;
                    if (!window.__tw_sockets || !window.__tw_sockets.length) return false;
                    return window.__tw_sockets.some(
                        it => it.ws.readyState === 1 && /rivergame/i.test(it.url));
                }""")
            except Exception:
                ready = False
            if ready:
                # 刚刷新的页面需等登录数据流; 复用模式直接就绪
                if already or len(received) >= 30:
                    print(f"[OK] 游戏 socket 已就绪 (耗时 {time.time()-t0:.0f} 秒, "
                          f"收到 {len(received)} 帧)")
                    break
        if not ready:
            print("[!] 超时: socket 未就绪, 请确认登录状态。")
            return

        # 等待网络空闲(10 秒无新帧), 确保登录流程彻底结束(复用模式跳过)
        if not already:
            print("[..] 等待网络空闲...")
            for _ in range(30):
                time.sleep(2)
                n = len(received)
                time.sleep(8)
                if len(received) == n and n > 0:
                    break
            print(f"[OK] 网络已空闲 (共收到 {len(received)} 帧)")
        print("[OK] 各 socket 流量统计:")
        for url, (cnt, last) in ws_stats.items():
            print(f"     {cnt:5d} 帧  {url[:80]}")
        received.clear()

        # 收集所有可用的 socket 候选(按优先级排序)
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

        candidates = None
        for _ in range(20):
            candidates = list_sockets()
            if candidates:
                break
            time.sleep(1)
        if not candidates:
            print("[!] 未找到可用的游戏 WebSocket")
            return
        print(f"[OK] 找到 {len(candidates)} 个游戏 socket:")
        for c in candidates:
            print(f"     [{c['idx']}] {c['url'][:90]}")
        # 记录流量最大的 socket (游戏数据主连接), 发送时优先尝试
        main_url = max(ws_stats.items(), key=lambda kv: kv[1][0])[0] if ws_stats else ""
        print(f"[OK] 游戏 data 主连接: {main_url[:80]}")

        send_js = """(args) => {
            const {idx, bytes} = args;
            const it = window.__tw_sockets[idx];
            if (!it || it.ws.readyState !== 1) return false;
            it.ws.send(new Uint8Array(bytes).buffer);
            return true;
        }"""

        def extract_frame(raw: bytes, cmd: int, order: int):
            """在(可能粘包的)载荷中定位并解析目标帧"""
            head = struct.pack(">ii", cmd, order)
            pos = 0
            while True:
                pos = raw.find(head, pos)
                if pos < 0 or pos + 12 > len(raw):
                    return None
                ln = struct.unpack(">i", raw[pos + 8:pos + 12])[0]
                if 0 < ln <= len(raw) - pos - 12:
                    return decode_frame(raw[pos:pos + 12 + ln])
                pos += 4

        sent_orders = []  # [(cmd, order)] 本轮已发送的全部请求

        def scan_for(cmd):
            """在收到的帧里搜索任意一个匹配 order 的响应"""
            for ts, raw in received:
                for c, o in sent_orders:
                    if c != cmd:
                        continue
                    fr = extract_frame(raw, cmd, o)
                    if fr:
                        return fr
            return None

        def send_and_wait(cmd, body_plain, per_socket_wait=40, max_sends=2):
            """向各 socket 依次发送; 记录所有 order, 全程扫描迟到响应"""
            socks = list_sockets()
            socks.sort(key=lambda c: (
                0 if ("socket.io" in c["url"] or "group-push" in c["url"]) else 1))
            socks = socks[:max_sends]
            for c in socks:
                order = 9_000_000 + int(time.time() * 1000) % 900_000
                frame = encode_frame(cmd, order, body_plain)
                sent_orders.append((cmd, order))
                try:
                    ok = page.evaluate(send_js, {"idx": c["idx"], "bytes": list(frame)})
                except Exception as e:
                    print(f"[..] socket[{c['idx']}] 发送异常: {str(e)[:80]}")
                    continue
                if not ok:
                    continue
                print(f"[>>] 发送 cmd={cmd} order={order} -> socket[{c['idx']}] "
                      f"({c['url'][:50]})")
                t0 = time.time()
                while time.time() - t0 < per_socket_wait:
                    time.sleep(2)
                    fr = scan_for(cmd)
                    if fr:
                        print(f"[<<] 收到匹配响应 (等待 {time.time()-t0:.0f} 秒)")
                        return fr
            fr = scan_for(cmd)
            if fr:
                print("[<<] 收到匹配响应 (延迟送达)")
            return fr

        # 1) 查询(带重试)
        print("\n===== 步骤1: 查询可领取任务 (cmd 5684) =====")
        r = None
        for attempt in range(3):
            r = send_and_wait(CMD_QUERY, build_v2_body(b""))
            if r:
                break
            print(f"[..] 第 {attempt+1} 次查询无响应, 10 秒后重试...")
            time.sleep(10)
        if not r:
            # 最后全量扫一遍
            r = scan_for(CMD_QUERY)
        if not r:
            print("[!] 查询无响应")
            return
        cmd, o, fmt, header, data = r
        s, ts = parse_ack_header(header)
        ids = parse_task_ids(data)
        print(f"[<<] 响应: 状态s={s} 任务列表={ids}")

        # 2) 一键领取(带重试)
        print("\n===== 步骤2: 一键领取 (cmd 5685) =====")
        body = build_v2_body(pb_packed_ints(CLAIM_IDS))
        r = None
        for attempt in range(3):
            r = send_and_wait(CMD_CLAIM, body)
            if r:
                break
            print(f"[..] 第 {attempt+1} 次领取无响应, 10 秒后重试...")
            time.sleep(10)
        if not r:
            print("[!] 领取无响应")
            return
        cmd, o, fmt, header, data = r
        s, ts = parse_ack_header(header)
        if s not in (0, None):
            print(f"[!] 服务器返回错误状态 s={s} (可能今天已领过)")
            return
        rewards = parse_claim_result(data)
        if rewards:
            print("[OK] 领取成功! 奖励明细:")
            for tid, cnt in rewards:
                print(f"     任务{tid}: {'+' + str(cnt) if cnt is not None else '(道具奖励)'}")
            print(f"[OK] 共领取 {len(rewards)} 项任务奖励")
        else:
            print("[..] 响应无奖励明细 (可能已领过或无可领取任务)")

        print("\n[完成] 自动领取流程结束。")
        # 不关闭浏览器, 保持登录态


if __name__ == "__main__":
    main()
