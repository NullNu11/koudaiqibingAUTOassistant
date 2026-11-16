# -*- coding: utf-8 -*-
"""
口袋奇兵 - 奖励领取协议嗅探
用户在浏览器里点击各类"领取奖励"按钮, 本脚本实时记录:
  - 客户端发出的每一帧 (谁决定数量? 请求里有没有数量字段?)
  - 服务器返回的每一帧 (响应里的数量/道具明细)
所有帧解密后写入 topwar_sniff_log.txt (JSONL), 供后续分析。

用法: python topwar_sniff.py   (Ctrl+C 或超时 3600 秒结束)
"""

import base64
import json
import os
import socket
import struct
import subprocess
import time

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(BASE, "topwar_profile")
DEBUG_PORT = 9222
GAME_URL = "https://warh5.rivergame.net/webgame/index.html"
LOG = os.path.join(BASE, "topwar_sniff_log.txt")
DURATION = 3600  # 最长运行 1 小时

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

WS_HOOK_JS = """
(function(){
  // 追加式钩子: 兼容页面已注入的其他 hook, 保证 __tw_sent 始终可用
  window.__tw_sockets = window.__tw_sockets || [];
  window.__tw_sent = window.__tw_sent || [];
  window.__tw_hooked = true;
  const proto = WebSocket.prototype;
  if (!proto.__tw_send_hooked) {
    proto.__tw_send_hooked = true;
    const origSend = proto.send;
    proto.send = function(data) {
      try {
        let buf = null;
        if (data instanceof ArrayBuffer) buf = new Uint8Array(data);
        else if (data && data.buffer instanceof ArrayBuffer)
          buf = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
        if (buf) {
          let s = '';
          for (let i = 0; i < buf.length; i++) s += String.fromCharCode(buf[i]);
          window.__tw_sent.push(btoa(s));
        }
      } catch(e) {}
      return origSend.call(this, data);
    };
  }
  if (window.__tw_ctor_hooked) return;
  window.__tw_ctor_hooked = true;
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


def decrypt_body(raw: bytes):
    """按协议解密帧体, 返回 (cmd, order, fmt, plain_hex) 或 None"""
    if len(raw) < 13:
        return None
    try:
        cmd, o, ln = struct.unpack(">iii", raw[:12])
    except Exception:
        return None
    if ln < 1 or len(raw) < 12 + ln:
        return None
    key = struct.pack(">i", 0x01010101 | o)
    fmt = raw[12] ^ key[0]
    body = bytes(b ^ key[(u + 1) % 4] for u, b in enumerate(raw[13:12 + ln]))
    return cmd, o, fmt, body


def main():
    if not ensure_edge():
        return
    logf = open(LOG, "a", encoding="utf-8")

    def emit(rec):
        rec["t"] = time.strftime("%H:%M:%S")
        logf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logf.flush()
        print(f"[{rec['t']}] {rec['dir']} cmd={rec.get('cmd')} "
              f"order={rec.get('order')} len={rec.get('plen')}")

    received = []

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        context = browser.contexts[0]

        ws_url_map = {}

        def on_ws_created(params):
            ws_url_map[params.get("requestId")] = params.get("url", "")

        def on_frame(params):
            url = ws_url_map.get(params.get("requestId", ""), "?")
            pd = params.get("response", {}).get("payloadData", "")
            try:
                raw = base64.b64decode(pd)
            except Exception:
                return
            received.append((time.time(), raw, url))

        # 找游戏页, 刷新使 socket 全部重建(保证 CDP 能抓到所有帧)
        game_pages = [pg for pg in context.pages if "rivergame" in pg.url]
        for extra in game_pages[1:]:
            try:
                extra.close()
            except Exception:
                pass
        page = game_pages[0] if game_pages else context.new_page()
        page.add_init_script(WS_HOOK_JS)
        print("[..] 刷新游戏页面(注入 hook)...")
        page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)

        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.on("Network.webSocketCreated", on_ws_created)
        cdp.on("Network.webSocketFrameReceived", on_frame)

        # 等待游戏 socket 就绪
        print("[..] 等待游戏连接(最多 240 秒)...")
        t0 = time.time()
        while time.time() - t0 < 240:
            time.sleep(3)
            try:
                ok = page.evaluate("""() => !!window.__tw_hooked &&
                    (window.__tw_sockets || []).some(
                        it => it.ws.readyState === 1 && /rivergame/i.test(it.url))""")
            except Exception:
                ok = False
            if ok:
                break
        else:
            print("[!] socket 未就绪, 退出")
            return
        # 等登录流量结束
        for _ in range(30):
            time.sleep(2)
            n = len(received)
            time.sleep(8)
            if len(received) == n and n > 0:
                break
        print(f"[OK] 游戏已就绪, 开始嗅探 (请开始点击领取, 最长 {DURATION} 秒)")
        print(f"[OK] 日志: {LOG}")

        t_end = time.time() + DURATION
        sent_idx = 0
        recv_idx = 0
        while time.time() < t_end:
            time.sleep(1)
            try:
                sent = page.evaluate("() => window.__tw_sent.splice(0)")
            except Exception:
                sent = []
            for b64 in sent:
                try:
                    raw = base64.b64decode(b64)
                except Exception:
                    continue
                d = decrypt_body(raw)
                if d:
                    cmd, o, fmt, body = d
                    emit({"dir": "SEND", "cmd": cmd, "order": o, "fmt": fmt,
                          "plen": len(raw), "body_hex": body.hex()})
                else:
                    emit({"dir": "SEND", "plen": len(raw), "raw_hex": raw.hex()})
            while recv_idx < len(received):
                ts, raw, url = received[recv_idx]
                recv_idx += 1
                d = decrypt_body(raw)
                if d:
                    cmd, o, fmt, body = d
                    emit({"dir": "RECV", "cmd": cmd, "order": o, "fmt": fmt,
                          "plen": len(raw), "body_hex": body.hex()})
                else:
                    # 可能粘包/文本帧, 记录原始
                    emit({"dir": "RECV", "plen": len(raw),
                          "raw_hex": raw.hex()[:400], "url": url[:60]})

    logf.close()
    print("[OK] 嗅探结束")


if __name__ == "__main__":
    main()
