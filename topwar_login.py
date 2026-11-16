# -*- coding: utf-8 -*-
"""
口袋奇兵(TopWar) H5 - 微信扫码登录 + 协议抓取 (v3)

v3 方案: 不用 Playwright 启动浏览器(会带自动化参数, 微信二维码拒绝加载),
而是以普通方式启动 Edge(--remote-debugging-port), 再通过 CDP 连接监听网络。
浏览器行为与手动打开完全一致, 二维码可正常显示。

用法: python topwar_login.py
- 若之前的自动化浏览器窗口还开着, 请先关闭它
- 脚本会打开一个正常的 Edge 窗口, 用微信扫码登录即可
"""

import json
import os
import socket
import subprocess
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

GAME_URL = "https://warh5.rivergame.net/webgame/index.html"
BASE = os.path.dirname(os.path.abspath(__file__))
WS_LOG_FILE = os.path.join(BASE, "topwar_ws_log.txt")
HTTP_LOG_FILE = os.path.join(BASE, "topwar_api_log.txt")
LS_FILE = os.path.join(BASE, "topwar_state_localstorage.json")
PROFILE = os.path.join(BASE, "topwar_profile")
DEBUG_PORT = 9222

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

NOISE_HOSTS = ("bi-tracker", "log.aliyuncs.com", "aihelp", "pingpp", "umeng", "cnzz")


def is_noise(url: str) -> bool:
    return any(h in url for h in NOISE_HOSTS)


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_edge_debugging():
    """以普通方式启动 Edge(带远程调试端口), 返回是否成功"""
    if port_open(DEBUG_PORT):
        print(f"[OK] 检测到端口 {DEBUG_PORT} 已有浏览器在监听, 直接连接。")
        return True
    edge = next((e for e in EDGE_CANDIDATES if os.path.exists(e)), None)
    if not edge:
        print("[!] 未找到 Edge, 请手动启动: msedge.exe --remote-debugging-port=9222 --user-data-dir=topwar_profile")
        return False
    subprocess.Popen([
        edge,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
    ])
    for _ in range(20):
        time.sleep(0.5)
        if port_open(DEBUG_PORT):
            print("[OK] Edge 已启动(普通模式 + 调试端口)。")
            return True
    print("[!] Edge 启动失败。")
    return False


def main():
    ws_log = []
    http_log = []

    if not ensure_edge_debugging():
        return

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        context = browser.contexts[0]

        # ---------- WebSocket 捕获 ----------
        def on_websocket(ws):
            print(f"[WS] 新连接: {ws.url[:120]}")
            ws.on("framesent", lambda payload: ws_log.append(
                (datetime.now().strftime("%H:%M:%S"), ws.url, "SEND", str(payload))))
            ws.on("framereceived", lambda payload: ws_log.append(
                (datetime.now().strftime("%H:%M:%S"), ws.url, "RECV", str(payload))))

        context.on("websocket", on_websocket)

        # ---------- HTTP 捕获(过滤噪音) ----------
        def on_response(response):
            try:
                url = response.url
                if is_noise(url):
                    return
                if url.split("?")[0].lower().endswith(
                        (".js", ".css", ".png", ".jpg", ".ico", ".mp3",
                         ".json.gz", ".plist", ".fnt", ".atlas")):
                    return
                body = ""
                try:
                    body = response.text()
                except Exception:
                    pass
                http_log.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "method": response.request.method,
                    "url": url,
                    "status": response.status,
                    "post_data": (response.request.post_data or "")[:3000],
                    "response_body": body[:3000],
                })
                print(f"[HTTP] {response.request.method} {url[:100]} -> {response.status}")
            except Exception:
                pass

        context.on("response", on_response)

        # 打开游戏页面
        pages = context.pages
        page = pages[-1] if pages else context.new_page()
        if "rivergame" not in (page.url or ""):
            page = context.new_page()
        print("正在打开游戏页面...")
        page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)

        print("=" * 60)
        print(">>> 请在浏览器窗口中用微信扫码登录 <<<")
        print(">>> 检测到游戏服务器 WebSocket 数据后自动继续 <<<")
        print("=" * 60)

        logged_in = False
        for _ in range(900):  # 最多 15 分钟
            time.sleep(2)
            game_ws = [e for e in ws_log if not is_noise(e[1])]
            if game_ws:
                logged_in = True
                print(f"\n[OK] 检测到游戏服务器 WebSocket 通信!")
                break

        if logged_in:
            print("[..] 继续抓包 120 秒, 期间可在游戏里手动点开'签到'页面以抓取签到接口...")
            for _ in range(60):
                time.sleep(2)

            try:
                ls = page.evaluate(
                    "() => { const o={}; for(let i=0;i<localStorage.length;i++){"
                    "const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }")
                with open(LS_FILE, "w", encoding="utf-8") as f:
                    json.dump(ls, f, ensure_ascii=False, indent=2)
                print(f"[OK] localStorage 已保存: {LS_FILE}")
            except Exception as e:
                print(f"[!] localStorage 导出失败: {e}")

        with open(WS_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"抓取时间: {datetime.now()}  登录成功: {logged_in}\n")
            f.write(f"共 {len(ws_log)} 帧\n" + "=" * 80 + "\n\n")
            last_url = ""
            for t, url, direction, payload in ws_log:
                if url != last_url:
                    f.write(f"### WebSocket: {url}\n")
                    last_url = url
                f.write(f"[{t}] {direction}: {payload[:3000]}\n")
                f.write("-" * 60 + "\n")
        print(f"[OK] WebSocket 日志已保存: {WS_LOG_FILE} (共 {len(ws_log)} 帧)")

        with open(HTTP_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"抓取时间: {datetime.now()}  登录成功: {logged_in}\n")
            f.write(f"共 {len(http_log)} 条(已过滤统计噪音)\n" + "=" * 80 + "\n\n")
            for e in http_log:
                f.write(f"[{e['time']}] {e['method']} {e['url']}  ({e['status']})\n")
                if e["post_data"]:
                    f.write(f"  PostData: {e['post_data']}\n")
                if e["response_body"]:
                    f.write(f"  Response: {e['response_body'][:2000]}\n")
                f.write("-" * 80 + "\n")
        print(f"[OK] HTTP 日志已保存: {HTTP_LOG_FILE} (共 {len(http_log)} 条)")

        if logged_in:
            print("\n[完成] 登录态保留在浏览器 profile 中, 下次可直接复用。")
            print("下一步: 分析 topwar_ws_log.txt / topwar_api_log.txt 找签到接口。")
        else:
            print("\n[!] 超时未检测到游戏服务器通信, 请确认已完成扫码登录。")
        # 不关闭浏览器, 保留登录态


if __name__ == "__main__":
    main()
