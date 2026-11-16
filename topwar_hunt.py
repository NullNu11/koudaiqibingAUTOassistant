# -*- coding: utf-8 -*-
"""
口袋奇兵 - 自动打野引擎 (协议层)

协议流程 (2026-11-14 实测抓包):
  cmd 906  召唤/搜索野怪   {minLevel,maxLevel,groupType,pointType}
  cmd 905  行军配置(队列)  {type:8,ownerx,ownery,x,y,heroList,speed,...}
  cmd 999  部队编制        {armyListNew,heroList,formation,formationV2}
  cmd 1201 集结出征        {marchType:8,x,y,time:60,armyList,...}
  cmd 815  开启道具        {itemid:600001(10点体力包),amount:1}
  推送: 10163 战斗结果 / 10401 物品变动 / 10021 体力 / 10007 行军状态

体力规则: 上限 pointMax(当前95), 自然恢复 1点/360秒, 集结1次耗10点
"""

import base64
import collections
import json
import os
import struct
import threading
import time

from playwright.sync_api import sync_playwright

from topwar_checkin import (BASE, DEBUG_PORT, GAME_URL, WS_HOOK_JS,
                            ensure_edge)

# ---------------- 命令号(实测) ----------------
CMD_HEARTBEAT = 0
CMD_SUMMON = 906        # 召唤/搜索野怪
CMD_MARCH_CFG = 905     # 行军配置(选队列/英雄)
CMD_ARMY_CFG = 999      # 部队编制
CMD_MARCH_GO = 1201     # 集结出征
CMD_OPEN_ITEM = 815     # 开启道具(体力包/宝箱)
CMD_MARCH_STATE = 10007 # 行军状态推送
CMD_STAMINA = 10021     # 体力推送
CMD_BATTLE = 10163      # 战斗结果推送
CMD_ITEM_SYNC = 10401   # 物品变动推送

ITEM_STAMINA_PACK = 600001   # 10点体力包
GROUP_CLUBSTAR = 9100        # 俱星
STAMINA_PER_HUNT = 10        # 每次集结消耗体力
CMD_ATTACK = 902             # 普通野怪出征(5连打)
GROUP_NORMAL = 61            # 普通野怪
STAMINA_PER_NORMAL = 14      # 每次连打消耗体力 (实测 45->35->21)
NORMAL_SEARCH = {"minLevel": 104, "maxLevel": 104,
                 "groupType": GROUP_NORMAL, "pointType": 1}
CMD_REFUGEE_POINT = 10802    # 难民坐标推送(卡片使用后服务器主动下发)
CARD_REFUGEE = 1700039       # 难民召唤卡(2026-11-14 17:23 实测)
STAMINA_PER_REFUGEE = 10     # 每次集结攻打难民消耗体力
CMD_PRESET = 6514            # 攻打预设(队列1-10)拉取: SEND空体(8字节0) → RECV预设列表
CMD_MARCH_INFO = 10019       # 行军详情推送 (采集 state: 1行军 5采集中 3返程 4到家)
CMD_MARCH_PRESET = 11016     # 出征队列配置推送 (userPreMarch, 游戏内10个队列的真实组成)
                             # 触发: 登录推送 / 游戏内保存队列(1346)后

# ---------------- 采集田类型 (2026-11-15 17:08 抓包: 906 groupType=资源itemId) --
GATHER_TYPES = {
    "corn": (506, 5, "玉米田"),
    "oil":  (706, 5, "石油田"),
    "thor": (1603, 3, "雷神矿"),
}

# ---------------- 俱星出征模板 (2026-11-14 15:03~15:05 抓包) ----------------
TPL_DEFAULT = {
    "heroes": [234, 241, 115],          # 第一队列英雄
    "owner": {"x": 465, "y": 439},      # 城市坐标(迁城后需更新)
    "speed": 10000,
    "formation": 0,
    "formationV2": 1003,
    "armyList": [
        {"pos": 1,  "armyId": 20100,  "armyNum": 13},
        {"pos": 2,  "armyId": 504530, "armyNum": 13},
        {"pos": 3,  "armyId": 20100,  "armyNum": 13},
        {"pos": 11, "armyId": 20100,  "armyNum": 13},
        {"pos": 12, "armyId": 20100,  "armyNum": 13},
        {"pos": 13, "armyId": 20100,  "armyNum": 13},
        {"pos": 21, "armyId": 20100,  "armyNum": 13},
        {"pos": 22, "armyId": 20100,  "armyNum": 13},
        {"pos": 23, "armyId": 20100,  "armyNum": 12},
    ],
}

# ---------------- 普通野怪出征模板 (2026-11-14 16:31/16:37 抓包) ----------------
TPL_NORMAL = {
    "heroes": [115, 140, 204],          # 第二队列英雄
    "owner": {"x": 465, "y": 439},      # 城市坐标(迁城后需更新)
    "speed": 10000,
    "armyList": [
        {"pos": 1,  "armyId": 10098, "armyNum": 7},
        {"pos": 2,  "armyId": 10098, "armyNum": 7},
        {"pos": 3,  "armyId": 10098, "armyNum": 7},
        {"pos": 11, "armyId": 10098, "armyNum": 7},
        {"pos": 12, "armyId": 10098, "armyNum": 7},
        {"pos": 13, "armyId": 10098, "armyNum": 7},
        {"pos": 21, "armyId": 10098, "armyNum": 7},
        {"pos": 22, "armyId": 10098, "armyNum": 7},
        {"pos": 23, "armyId": 10098, "armyNum": 6},
    ],
}

CONFIG_FILE = os.path.join(BASE, "topwar_hunt_config.json")

# 出征队列配置全局缓存 (后端进程内所有任务引擎共享; 持久化到配置文件)
_MARCH_CACHE = {"presets": [], "evt": threading.Event()}

# ---------------- 难民出征模板 (2026-11-14 17:23 抓包, 与普通野怪同队) --------
TPL_REFUGEE = {
    "heroes": [115, 140, 204],          # 第二队列英雄
    "owner": {"x": 465, "y": 439},      # 城市坐标(迁城后需更新)
    "speed": 10000,
    "armyList": [
        {"pos": 1,  "armyId": 10098, "armyNum": 7},
        {"pos": 2,  "armyId": 10098, "armyNum": 7},
        {"pos": 3,  "armyId": 10098, "armyNum": 7},
        {"pos": 11, "armyId": 10098, "armyNum": 7},
        {"pos": 12, "armyId": 10098, "armyNum": 7},
        {"pos": 13, "armyId": 10098, "armyNum": 7},
        {"pos": 21, "armyId": 10098, "armyNum": 7},
        {"pos": 22, "armyId": 10098, "armyNum": 7},
        {"pos": 23, "armyId": 10098, "armyNum": 6},
    ],
}

SEND_JS = """(args) => {
    const {idx, bytes} = args;
    const it = window.__tw_sockets[idx];
    if (!it || it.ws.readyState !== 1) return false;
    it.ws.send(new Uint8Array(bytes).buffer);
    return true;
}"""

SOCKETS_JS = """() => {
    if (!window.__tw_sockets) return [];
    const out = [];
    window.__tw_sockets.forEach((it, idx) => {
        if (it.ws && it.ws.readyState === 1 && /rivergame/i.test(it.url)) {
            out.push({url: it.url, idx: idx});
        }
    });
    return out;
}"""

HOOKED_JS = """() => !!window.__tw_hooked &&
    (window.__tw_sockets || []).some(
        it => it.ws.readyState === 1 && /rivergame/i.test(it.url))"""

# 给页面内所有游戏 socket 挂 message 监听, 把收到的帧(base64)存入本连接专属 sink
# (每个引擎/任务一个 sink, 多任务并行时各取各的帧, 互不干扰)
CAPTURE_JS = """(id) => {
  if (!window.__tw_sockets) return false;
  window.__tw_sinks = window.__tw_sinks || [];
  if (window.__tw_sinks.some(s => s.id === id)) return true;
  // 发帧捕获: proto.send hook 一次性安装, 客户端发出的帧广播到所有 sink
  if (!window.__tw_send_hooked) {
    window.__tw_send_hooked = true;
    const proto = WebSocket.prototype;
    const origSend = proto.send;
    proto.send = function(data) {
      try {
        let u8 = null;
        if (data instanceof ArrayBuffer) u8 = new Uint8Array(data);
        else if (data && data.buffer instanceof ArrayBuffer)
          u8 = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
        if (u8 && window.__tw_sinks.length) {
          let s = "";
          for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
          const b64 = btoa(s);
          window.__tw_sinks.forEach(sk => {
            try { sk.q.push({d: b64, s: 1}); } catch (e) {}
          });
        }
      } catch (e) {}
      return origSend.call(this, data);
    };
  }
  if (!window.__tw_toB64) {
    window.__tw_toB64 = (u8) => {
      let s = "";
      for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
      return btoa(s);
    };
  }
  // 本连接专属收帧 sink: message 监听在 drain 时按需挂载
  window.__tw_sinks.push({id: id, q: [], done: new Set()});
  return true;
}"""

DRAIN_JS = """(id) => {
  const sink = (window.__tw_sinks || []).find(s => s.id === id);
  if (!sink) return [];
  (window.__tw_sockets || []).forEach(it => {
    const ws = it.ws;
    if (ws && ws.readyState === 1 && /rivergame/i.test(it.url)
        && !sink.done.has(ws)) {
      sink.done.add(ws);
      ws.addEventListener("message", (ev) => {
        try {
          const d = ev.data;
          if (d instanceof ArrayBuffer) {
            sink.q.push({d: window.__tw_toB64(new Uint8Array(d))});
          } else if (d && d.arrayBuffer) {
            d.arrayBuffer().then(ab =>
              sink.q.push({d: window.__tw_toB64(new Uint8Array(ab))}));
          }
        } catch (e) {}
      });
    }
  });
  const q = sink.q;
  sink.q = [];
  return q;
}"""

# 帧日志中跳过的噪声命令(心跳/行军状态/地图同步等高频帧)
NOISE_CMDS = {0, 10007, 10019, 10018, 12000, 12003, 12004, 10056}


def load_config():
    cfg = {"monster": "clubstar", "max_bottles": 5, "daily_limit": 10,
           "queue_clubstar": 1, "queue_normal": 1, "queue_refugee": 2,
           "names": {"hero": {}, "troop": {}},
           "march_presets": [],
           "tpl": TPL_DEFAULT, "tpl_normal": TPL_NORMAL,
           "tpl_refugee": TPL_REFUGEE}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        tpl = dict(TPL_DEFAULT)
        tpl.update(saved.get("tpl") or {})
        tpln = dict(TPL_NORMAL)
        tpln.update(saved.get("tpl_normal") or {})
        tplr = dict(TPL_REFUGEE)
        tplr.update(saved.get("tpl_refugee") or {})
        cfg.update(saved)
        cfg["tpl"] = tpl
        cfg["tpl_normal"] = tpln
        cfg["tpl_refugee"] = tplr
        if not isinstance(cfg.get("names"), dict):
            cfg["names"] = {}
        cfg["names"].setdefault("hero", {})
        cfg["names"].setdefault("troop", {})
    except Exception:
        pass
    if isinstance(cfg.get("march_presets"), list) and cfg["march_presets"]:
        _MARCH_CACHE["presets"] = cfg["march_presets"]
        _MARCH_CACHE["evt"].set()
    return cfg


def cache_march_presets(arr):
    """收到 11016 队列配置: 更新全局缓存并持久化"""
    if not isinstance(arr, list) or not arr:
        return
    _MARCH_CACHE["presets"] = arr
    _MARCH_CACHE["evt"].set()
    try:
        cfg = load_config()
        cfg["march_presets"] = arr
        save_config(cfg)
    except Exception:
        pass


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- 帧编解码 ----------------

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
        frames.append((cmd, o, d[2], d[3]))
        pos += 12 + ln
    return frames


def encode_raw_frame(cmd: int, order: int, fmt: int, body: bytes) -> bytes:
    """构造指定 fmt 的加密完整帧 (fmt=0: JSON, fmt=2: 二进制)"""
    key = struct.pack(">i", 0x01010101 | order)
    wire = bytes([fmt ^ key[0]])
    wire += bytes(b ^ key[(u + 1) % 4] for u, b in enumerate(body))
    return struct.pack(">iii", cmd, order, len(wire)) + wire


def encode_json_frame(cmd: int, order: int, obj) -> bytes:
    """构造 fmt=0(JSON) 的加密完整帧"""
    body = json.dumps(obj, separators=(",", ":")).encode()
    return encode_raw_frame(cmd, order, 0, body)


def extract_json_blobs(data: bytes):
    """从二进制帧体中提取所有平衡的 {...} JSON 对象 (6514 预设列表用)"""
    blobs = []
    i = data.find(b"{\"")
    while i >= 0:
        depth = 0; in_str = False; esc = False; j = i
        while j < len(data):
            c = data[j:j + 1]
            if in_str:
                if esc:
                    esc = False
                elif c == b"\\":
                    esc = True
                elif c == b'"':
                    in_str = False
            else:
                if c == b'"':
                    in_str = True
                elif c == b"{":
                    depth += 1
                elif c == b"}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if j >= len(data):
            break
        try:
            blobs.append(json.loads(data[i:j + 1].decode("utf-8")))
        except Exception:
            pass
        i = data.find(b"{\"", j + 1)
    return blobs


# ---------------- 打野引擎 ----------------

class HuntEngine:

    def __init__(self, log=print):
        self.log = log
        self.stop_evt = threading.Event()
        self.stop_reason = ""
        self.show_frames = True     # 是否在日志中输出每帧协议信息
        # 实时状态(由监听线程更新)
        self.stamina = None          # 当前体力
        self.stamina_max = None      # 体力上限
        self.bottles_left = None     # 背包体力包剩余
        self.last_battle = None      # (result, pveId)
        self.reward_items = []       # 最近的物品变动 [(itemId, amount)]
        self.server_t = None         # 服务器毫秒时间
        self.return_arrive = None    # 队列预计回家时间(服务器毫秒)
        self.march_out_secs = None   # 去程耗时(出征->战斗结果), 估计回程用
        self.my_marches = {}         # marchId -> {state, returnArriveTime}
        self.refugee_point = None    # 难民坐标 {x, y, ts} (10802 推送)
        self.queue_home_ts = None    # 最近一次侦听到队列回家的时间 (采集触发信号)
        self._last_march_log = ""
        self.hunts = 0               # 本次已召唤次数
        self.bottles_used = 0        # 本次已用体力包
        self._raw = []
        self._raw_lock = threading.Lock()
        self._acks = {}              # (cmd, order) -> outer dict
        self._ack_ts = {}            # (cmd, order) -> 收到时间
        self._recent = collections.deque(maxlen=300)  # (ts, cmd, order, s)
        self._pw = None
        self._page = None
        self.running = False
        self.presets = []            # 6514 攻打预设 [{index, heroList, armyList...}]
        self._preset_evt = threading.Event()
        self.march_presets = list(_MARCH_CACHE["presets"])  # 11016 队列配置
        # 页面内帧捕获 sink 标识 (多引擎并行时各取各的帧)
        self._cap_id = f"cap{int(time.time() * 1000)}{id(self) % 1000}"

    # ---------- 连接 ----------
    def connect(self, force_reload=False):
        if not ensure_edge():
            raise RuntimeError("无法启动 Edge 浏览器")
        self._pw = sync_playwright().start()
        try:
            browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{DEBUG_PORT}")
        except Exception as e:
            self.close()
            raise RuntimeError(f"连接浏览器失败: {e}")
        context = browser.contexts[0]
        pages = [pg for pg in context.pages if "rivergame" in pg.url]
        for extra in pages[1:]:
            try:
                extra.close()
            except Exception:
                pass
        self._page = pages[0] if pages else context.new_page()
        self._page.add_init_script(WS_HOOK_JS)
        already = False
        try:
            already = self._page.evaluate(HOOKED_JS)
        except Exception:
            already = False
        if already and not force_reload:
            self.log("[OK] 页面已连接, 直接复用")
        else:
            self.log("[..] 刷新游戏页面(注入 hook)...")
            self._page.goto(GAME_URL, wait_until="domcontentloaded",
                            timeout=60000)
        cdp = context.new_cdp_session(self._page)
        cdp.send("Network.enable")   # 仅为保持与旧版行为一致, 捕获改走页面内 hook

        self.log("[..] 等待游戏连接(最多 240 秒)...")
        t0 = time.time()
        while time.time() - t0 < 240:
            if self.stop_evt.is_set():
                self.close()
                raise RuntimeError("已停止")
            time.sleep(2)
            try:
                ok = self._page.evaluate(HOOKED_JS)
            except Exception:
                ok = False
            if ok:
                break
        else:
            self.close()
            raise RuntimeError("游戏 socket 未就绪(请确认已登录)")
        # 页面内挂帧捕获 (对已建立的 socket 立即生效)
        try:
            self._page.evaluate(CAPTURE_JS, self._cap_id)
        except Exception as e:
            self.log(f"[!] 帧捕获注入失败: {str(e)[:120]}")
        self.running = True
        self.log("[OK] 已连接游戏服务器")

    def close(self):
        self.stop_evt.set()
        self.running = False
        try:
            if self._page:
                self._page.evaluate(
                    "() => { window.__tw_sinks = (window.__tw_sinks || [])"
                    ".filter(s => s.id !== '" + self._cap_id + "'); }")
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._page = None

    # ---------- 收帧与监听 ----------
    # (收帧改为 _pwait 内轮询页面, 见 _drain_page)

    @staticmethod
    def _inner(outer):
        d = outer.get("d")
        if isinstance(d, str):
            try:
                return json.loads(d)
            except Exception:
                return {}
        return d if isinstance(d, dict) else {}

    def _process(self, ts, raw, outgoing=False):
        for cmd, order, fmt, plain in extract_frames(raw):
            if outgoing:
                # 引擎自己发的帧 order >= 9000000, send_json/send_raw 已记录
                if order >= 9_000_000 or cmd in NOISE_CMDS:
                    continue
                if fmt == 0:
                    try:
                        obj = json.loads(plain.decode("utf-8", "replace"))
                        self.log(f"[>>] CLIENT cmd={cmd} "
                                 f"{json.dumps(obj, ensure_ascii=False)[:300]}")
                    except Exception:
                        self.log(f"[>>] CLIENT cmd={cmd} <{len(plain)}B>")
                else:
                    self.log(f"[>>] CLIENT cmd={cmd} fmt={fmt} "
                             f"<{len(plain)}B binary>")
                continue
            if fmt == 2 and cmd == CMD_PRESET:
                blobs = extract_json_blobs(plain)
                seen = {}
                for b in blobs:
                    if isinstance(b, dict) and b.get("index"):
                        seen[b["index"]] = b
                if seen:
                    self.presets = [seen[k] for k in sorted(seen)]
                    self._preset_evt.set()
                    self.log(f"[OK] 收到攻打预设 {len(self.presets)} 个: "
                             + ", ".join(
                                 f"#{p['index']}(英雄{p.get('heroList')}, "
                                 f"编队{p.get('formationV2') or '-'})"
                                 for p in self.presets))
                continue
            if fmt != 0:
                if cmd not in NOISE_CMDS:
                    self.log(f"[<<] RECV cmd={cmd} fmt={fmt} "
                             f"<{len(plain)}B binary>")
                continue
            try:
                outer = json.loads(plain.decode("utf-8", "replace"))
            except Exception:
                continue
            if not isinstance(outer, dict):
                continue
            t = outer.get("t")
            if isinstance(t, (int, float)) and t > 1e12:
                self.server_t = t
            self._acks[(cmd, order)] = outer
            self._ack_ts[(cmd, order)] = ts
            self._recent.append((ts, cmd, order, outer.get("s")))
            if self.show_frames and cmd not in NOISE_CMDS:
                prev = json.dumps(self._inner(outer), ensure_ascii=False)
                self.log(f"[<<] RECV cmd={cmd} s={outer.get('s')} "
                         f"{prev[:220]}")
            inner = self._inner(outer)
            if not isinstance(inner, dict):
                continue
            if cmd == CMD_MARCH_PRESET and isinstance(
                    inner.get("userPreMarch"), list):
                arr = inner["userPreMarch"]
                self.march_presets = arr
                cache_march_presets(arr)
                self.log(f"[OK] 收到游戏出征队列配置 (11016): "
                         f"共 {len(arr)} 个队列")
                for ent in arr:
                    self.log("[队列] "
                             + json.dumps(ent, ensure_ascii=False)[:900])
            elif cmd == CMD_STAMINA and "point" in inner:
                old = self.stamina
                self.stamina = inner.get("point")
                self.stamina_max = inner.get("pointMax")
                if old != self.stamina:
                    self.log(f"[体力] {self.stamina}/{self.stamina_max} "
                             f"(原因: {inner.get('reason', '?')})")
            elif cmd == CMD_BATTLE and inner.get("marchType") in (8, 42):
                self.last_battle = (inner.get("result"), inner.get("pveId"),
                                    inner.get("marchType"))
                self.log(f"[战斗] marchType={inner.get('marchType')} "
                         f"result={inner.get('result')} "
                         f"pveId={inner.get('pveId')} 奖励已自动入包")
            elif cmd == CMD_ITEM_SYNC and inner.get("itemId") is not None:
                self.reward_items.append(
                    (inner.get("itemId"), inner.get("amount")))
                if inner.get("itemId") == ITEM_STAMINA_PACK:
                    self.bottles_left = inner.get("amount")
            elif cmd == CMD_REFUGEE_POINT and "x" in inner:
                self.refugee_point = {"x": inner.get("x"), "y": inner.get("y"),
                                      "ts": ts}
            elif cmd in (CMD_MARCH_STATE, CMD_MARCH_INFO):
                mi = inner.get("marchInfo") if cmd == CMD_MARCH_INFO else inner
                if not isinstance(mi, dict):
                    continue
                ra = mi.get("returnArriveTime")
                if isinstance(ra, (int, float)) and ra > 0:
                    self.return_arrive = max(self.return_arrive or 0, ra)
                # 我方 march 的状态追踪: 1出征 5采集中 13战斗中 14战斗结束 3返程 4到家
                mid = mi.get("marchId")
                rec = self.my_marches.get(str(mid)) if mid is not None else None
                if rec is not None:
                    st = mi.get("state")
                    if st != rec.get("state"):
                        rec["state"] = st
                        self.log(f"[行军] march=...{str(mid)[-8:]} state={st}")
                    if isinstance(ra, (int, float)) and ra > 0:
                        rec["returnArriveTime"] = ra
                # 队列回家事件 (0/4=到家): 自动采集的触发信号
                if mi.get("state") in (0, 4):
                    self.queue_home_ts = time.time()
                    self.log("[事件] 侦听到队列回家")

    # ---------- 发送 ----------
    def _send_bytes(self, frame):
        """把完整加密帧写入第一个可用的游戏 socket"""
        socks = []
        try:
            socks = self._page.evaluate(SOCKETS_JS) or []
        except Exception:
            pass
        socks.sort(key=lambda c: 0 if "server-knight" in c["url"] else 1)
        for c in socks:
            try:
                ok = self._page.evaluate(SEND_JS, {"idx": c["idx"],
                                                   "bytes": list(frame)})
            except Exception as e:
                self.log(f"[!] socket[{c['idx']}] 发送异常: {str(e)[:120]}")
                continue
            if ok:
                return c
        raise RuntimeError("发送失败: 无可用游戏 socket")

    def send_json(self, cmd, obj):
        order = 9_000_000 + int(time.time() * 1000) % 900_000
        frame = encode_json_frame(cmd, order, obj)
        c = self._send_bytes(frame)
        if self.show_frames:
            self.log(f"[>>] SEND cmd={cmd} sock[{c['idx']}]"
                     f"{c['url'][:45]} "
                     f"{json.dumps(obj, ensure_ascii=False)[:240]}")
        return order

    def send_raw(self, cmd, fmt, body):
        """发送 fmt=2 二进制帧 (6514 预设拉取等)"""
        order = 9_000_000 + int(time.time() * 1000) % 900_000
        c = self._send_bytes(encode_raw_frame(cmd, order, fmt, body))
        if self.show_frames:
            self.log(f"[>>] SEND cmd={cmd} fmt={fmt} sock[{c['idx']}]"
                     f"{c['url'][:45]} <{len(body)}B binary>")
        return order

    def _diag_recent(self, since_ts, max_n=12):
        """诊断: 发送之后收到的帧列表"""
        rows = [r for r in self._recent if r[0] >= since_ts][-max_n:]
        if not rows:
            return "(发送后未收到任何帧!)"
        return " ".join(f"{c}/{o}" for _, c, o, _ in rows)

    def _pwait(self, ms):
        """等待并轮询页面收帧。
        关键: Playwright sync API 不能跨线程, 收帧必须在调用方线程完成;
        所有等待都经过这里, 顺带完成帧轮询 (每 300ms 一次)。"""
        end = time.time() + ms / 1000.0
        while True:
            now = time.time()
            if now >= end:
                return
            try:
                self._drain_page()
            except Exception:
                pass
            rest = end - time.time()
            if rest <= 0:
                return
            # 用 playwright 等待以保持事件派发, 单段最长 300ms
            step = min(rest, 0.3)
            try:
                self._page.wait_for_timeout(int(step * 1000))
            except Exception:
                time.sleep(step)

    def _drain_page(self):
        """从页面 hook 拉取新帧并解密处理 (只能在创建 playwright 的线程调用)"""
        frames = self._page.evaluate(DRAIN_JS, self._cap_id) or []
        for f in frames:
            try:
                raw = base64.b64decode(f.get("d", ""))
            except Exception:
                continue
            try:
                self._process(time.time(), raw, outgoing=bool(f.get("s")))
            except Exception:
                pass

    def _window_match(self, cmd, since_ts):
        """时间窗口兜底: 找发送之后到达的同一 cmd 的最新响应"""
        best = None
        for (c, o), ts in list(self._ack_ts.items()):
            if c == cmd and ts >= since_ts:
                if best is None or ts > best[1]:
                    best = (o, ts)
        return best

    def request(self, cmd, obj, timeout=20):
        """发送并等待响应: 先精确匹配 order, 2 秒后按时间窗口降级匹配"""
        order = self.send_json(cmd, obj)
        t0 = time.time()
        while time.time() - t0 < timeout:
            outer = self._acks.get((cmd, order))
            if outer is not None:
                return outer
            self._pwait(300)
            if time.time() - t0 >= 2:
                best = self._window_match(cmd, t0 - 0.5)
                if best is not None:
                    self.log(f"[!] cmd={cmd} 响应 order 不匹配 "
                             f"(发出 {order}/实际 {best[0]}), 按时间窗口匹配")
                    return self._acks[(cmd, best[0])]
        self.log(f"[诊断] cmd={cmd} 无响应, 发送后收帧: "
                 f"{self._diag_recent(t0)}")
        return None

    # ---------- 攻打预设 (队列 1-10) ----------
    def pull_presets(self, timeout=15):
        """拉取游戏内的攻打预设 (6514), 返回 [{index, heroList, armyList...}]"""
        self.presets = []
        self._preset_evt.clear()
        self.send_raw(CMD_PRESET, 2, b"\x00" * 8)
        t0 = time.time()
        # 轮询等待 (收帧发生在 _pwait 内, 不能用 Event.wait 阻塞)
        while time.time() - t0 < timeout and not self.presets:
            self._pwait(300)
        if not self.presets:
            self.log(f"[!] 未收到攻打预设 (6514), 超时 {timeout}s")
        return self.presets

    def _march_preset_indexes(self):
        """列出各队列英雄组成, 便于核对"""
        out = []
        for i, ent in enumerate(self.march_presets):
            hs = "/".join(str(h) for h in (ent.get("heroIds") or []))
            out.append(f"#{i + 1}(英雄{hs})")
        return out

    def _find_march_preset(self, queue):
        """队列号 N = 11016 userPreMarch 数组第 N 个 (游戏队列槽位顺序)"""
        if 1 <= queue <= len(self.march_presets):
            return self.march_presets[queue - 1]
        return None

    def preset_tpl(self, queue, base_tpl):
        """用游戏内出征队列 (11016, 共10个, 用户在游戏中配置的真实组成)
        生成出征模板; 队列配置未获取时等待, 超时回退 base_tpl"""
        if not self.march_presets and _MARCH_CACHE["presets"]:
            self.march_presets = list(_MARCH_CACHE["presets"])
        if not self.march_presets:
            self.log("[..] 未获取到游戏队列配置 (11016)")
            self.log("[..] 请在游戏中随便编辑并保存一个队列, "
                     "或刷新游戏页面 (最长等待 90 秒)...")
            t0 = time.time()
            while time.time() - t0 < 90 and not self.march_presets:
                self._pwait(500)
                if not self.march_presets and _MARCH_CACHE["presets"]:
                    self.march_presets = list(_MARCH_CACHE["presets"])
        if not self.march_presets:
            self.log("[!] 仍未获取到队列配置, 使用原配置模板")
            tpl = dict(base_tpl)
            tpl.setdefault("heroes", [])
            tpl.setdefault("armyList", [])
            tpl.setdefault("formation", 0)
            tpl.setdefault("formationV2", 0)
            return tpl
        p = self._find_march_preset(queue)
        if p is None:
            self.log(f"[!] 队列 #{queue} 未找到 (现有配置: "
                     f"{self._march_preset_indexes()}), 使用原配置模板")
            tpl = dict(base_tpl)
            tpl.setdefault("heroes", [])
            tpl.setdefault("armyList", [])
            tpl.setdefault("formation", 0)
            tpl.setdefault("formationV2", 0)
            return tpl
        tpl = dict(base_tpl)
        tpl["heroes"] = list(p.get("heroIds") or [])
        # armyInfo: pos 从 0 计 (999/902 的 pos 从 1 计), num→armyNum
        tpl["armyList"] = [{"pos": (a.get("pos") or 0) + 1,
                            "armyId": a.get("armyId"),
                            "armyNum": a.get("num")}
                           for a in (p.get("armyInfo") or [])]
        tpl["formation"] = p.get("formation", 0)
        tpl["formationV2"] = p.get("formationV2", 0)
        self.log(f"[OK] 使用游戏队列 #{queue}: 英雄 {tpl['heroes']}, "
                 f"兵表 {[(a['armyId'], a['armyNum']) for a in tpl['armyList']]}, "
                 f"编队{tpl['formationV2'] or '-'}")
        return tpl

    # ---------- 工具 ----------
    def _sleep(self, secs):
        end = time.time() + secs
        while time.time() < end and not self.stop_evt.is_set():
            self._pwait(min(1.0, end - time.time()))

    def ensure_stamina(self, need=STAMINA_PER_HUNT, max_bottles=0):
        """体力不足时开体力包; 返回是否成功"""
        if self.stamina is None:
            # 体力未知: 直接尝试, 出征失败/体力推送会暴露真实体力
            return True
        if self.stamina >= need:
            return True
        while True:
            if self.bottles_used >= max_bottles:
                self.stop_reason = (f"体力包配额已用完 "
                                    f"(上限 {max_bottles} 个)")
                return False
            outer = self.request(CMD_OPEN_ITEM,
                                 {"itemid": ITEM_STAMINA_PACK, "amount": 1})
            if outer is None:
                self.stop_reason = "开启体力包无响应"
                return False
            if outer.get("s") != 0:
                self.stop_reason = (f"体力包开启失败 s={outer.get('s')} "
                                    f"(背包可能没有体力包了)")
                return False
            self.bottles_used += 1
            self.log(f"[OK] 开启10点体力包 (第 {self.bottles_used}/{max_bottles}"
                     f" 个, 背包剩余 {self.bottles_left})")
            t0 = time.time()
            while time.time() - t0 < 8:
                if self.stamina is not None and self.stamina >= need:
                    return True
                self._pwait(500)
            if self.stamina is None:
                self.stop_reason = "无法获取体力状态"
                return False
        return False

    @staticmethod
    def fail_detail(outer):
        """提取失败响应中除 s 之外的详细信息 (err/m 等错误描述)"""
        if not outer:
            return ""
        extra = {k: v for k, v in outer.items() if k != "s"}
        return (" 详细: " + json.dumps(extra, ensure_ascii=False)[:260]) \
            if extra else ""

    def wait_queue_home_est(self, default_wait=15):
        """出征失败后的智能等待: 有服务器回家时间则等到家再重试, 否则默认等待"""
        try:
            if self.return_arrive and self.server_t:
                delta = (self.return_arrive - self.server_t) / 1000.0
                if 0 < delta < 300:
                    self.log(f"[..] 队列预计 {int(delta)} 秒后回家, "
                             f"等待后再重试")
                    t0 = time.time()
                    while (time.time() - t0 < delta
                           and not self.stop_evt.is_set()):
                        self._pwait(1000)
                    return
        except Exception:
            pass
        self._pwait(default_wait * 1000)

    def wait_battle(self, timeout=420, match_type=None):
        """等待战斗结果推送; match_type: 只匹配指定 marchType"""
        t0 = time.time()
        # 软停止: 战斗已出征, 必须等结果, 不受 stop_evt 影响
        while time.time() - t0 < timeout:
            if self.last_battle is not None:
                r = self.last_battle
                if match_type is None or (len(r) > 2 and r[2] == match_type):
                    self.last_battle = None
                    return r
            self._pwait(1000)
        return None

    def wait_home(self, march_id=None, cap=420):
        """等待队列回家
        带 march_id: 事件驱动, 监听 10007 状态 (0/4=到家, 3=返程含到家时间)
        不带: 旧逻辑 (returnArriveTime > 去程耗时估计 > 默认 90 秒)"""
        if march_id:
            rec = self.my_marches.get(str(march_id))
            t0 = time.time()
            while time.time() - t0 < cap and not self.stop_evt.is_set():
                if rec is not None:
                    if rec.get("state") in (0, 4):
                        self.log("[OK] 队列已到家")
                        return
                    ra = rec.get("returnArriveTime")
                    if ra and self.server_t:
                        delta = (ra - self.server_t) / 1000.0
                        if 0 < delta < 300:
                            self.log(f"[..] 等待队列回家 ~{int(delta)} 秒 "
                                     f"(服务器推送)")
                            self._sleep(min(delta + 3, cap))
                            continue
                self._pwait(1000)
            self.log("[..] 等待回家超时, 继续下一轮")
            return
        if self.return_arrive and self.server_t:
            delta = (self.return_arrive - self.server_t) / 1000.0
            if delta > 0:
                self.log(f"[..] 等待队列回家 ~{int(delta)} 秒 (服务器推送)")
                self._sleep(min(delta + 5, cap))
                return
        if self.march_out_secs:
            w = min(self.march_out_secs + 10, cap)
            self.log(f"[..] 等待队列回家 ~{int(w)} 秒 (按去程耗时估计)")
            self._sleep(w)
            return
        self.log("[..] 等待队列回家 (默认 90 秒)")
        self._sleep(min(90, cap))

    # ---------- 打野: 俱星 ----------
    def hunt_clubstar(self, max_bottles, daily_limit=10, queue=1):
        tpl = self.preset_tpl(queue, load_config()["tpl"])
        self.log(f"===== 自动打野: 俱星5级 | 体力瓶上限 {max_bottles} | "
                 f"召唤上限 {daily_limit} 次 =====")
        self.hunts = 0
        self.bottles_used = 0
        while not self.stop_evt.is_set():
            if self.hunts >= daily_limit:
                self.log(f"[停止] 已达到每日召唤上限 ({daily_limit} 次)")
                break
            # 体力预检(已知不足则先开包)
            if self.stamina is not None and self.stamina < STAMINA_PER_HUNT:
                if not self.ensure_stamina(STAMINA_PER_HUNT, max_bottles):
                    break

            # 1) 召唤
            outer = self.request(CMD_SUMMON,
                                 {"minLevel": 5, "maxLevel": 5,
                                  "groupType": GROUP_CLUBSTAR,
                                  "pointType": 9})
            if outer is None:
                self.log("[!] 召唤无响应, 30 秒后重试")
                self._sleep(30)
                continue
            if outer.get("s") != 0:
                self.log(f"[停止] 召唤失败 s={outer.get('s')} "
                         f"(可能已达今日召唤次数上限)")
                break
            point = self._inner(outer).get("point") or {}
            x, y = point.get("x"), point.get("y")
            ox, oy = tpl["owner"]["x"], tpl["owner"]["y"]
            self.log(f"[OK] 召唤成功: 5级俱星 @ ({x},{y}) "
                     f"[第 {self.hunts + 1}/{daily_limit} 次]")

            # 2) 编队(选队列)
            self.request(CMD_MARCH_CFG,
                         {"type": 8, "ownerx": ox, "ownery": oy,
                          "x": x, "y": y, "heroList": tpl["heroes"],
                          "speed": tpl["speed"], "ownerRid": 0, "rid": 0})
            self.request(CMD_ARMY_CFG,
                         {"armyListNew": tpl["armyList"],
                          "heroList": tpl["heroes"],
                          "formation": tpl["formation"],
                          "formationV2": tpl["formationV2"]})

            # 3) 集结出征(失败自动重试: 体力不足开包 / 队列未回家则等待)
            go = {"marchType": 8, "x": x, "y": y, "time": 60,
                  "armyList": tpl["armyList"], "armyListNew": tpl["armyList"],
                  "heroList": tpl["heroes"], "trapList": [],
                  "formation": tpl["formation"],
                  "formationV2": tpl["formationV2"]}
            t_march = time.time()
            outer = None
            for attempt in range(1, 6):
                outer = self.request(CMD_MARCH_GO, go, timeout=30)
                if outer is not None and outer.get("s") == 0:
                    break
                s = outer.get("s") if outer else "无响应"
                self.log(f"[..] 出征未成功 (s={s}){self.fail_detail(outer)}, "
                         f"重试 {attempt}/5 (英雄不在家/等待队列回家/"
                         f"补充体力)...")
                if not self.ensure_stamina(STAMINA_PER_HUNT, max_bottles):
                    break
                self.wait_queue_home_est()
                outer = None
            if outer is None or outer.get("s") != 0:
                s = outer.get("s") if outer else "无响应"
                self.log(f"[停止] 出征连续失败 (s={s})"
                         f"{self.fail_detail(outer)}"
                         + " ← 若为英雄不在家, 请在游戏中等该队列回城"
                           "或换一个预设队列")
                break
            self.log("[OK] 已出征, 等待战斗...")
            self.hunts += 1

            # 4) 等战斗结果
            self.last_battle = None
            self.return_arrive = None
            r = self.wait_battle(420)
            if r is not None:
                # 去程耗时 = 出征到战斗结果; 回程与之对称, 用于估计回家时间
                self.march_out_secs = time.time() - t_march
            if r is None:
                self.log("[!] 等待战斗结果超时, 继续下一轮")
            elif r[0] != 1:
                self.log(f"[!] 战斗未胜 (result={r[0]}), 继续下一轮")

            # 5) 等队列回家
            self.wait_home()

        self.log(f"[结束] 本次召唤 {self.hunts} 次, 使用体力包 "
                 f"{self.bottles_used} 个"
                 + (f" ({self.stop_reason})" if self.stop_reason else ""))

    # ---------- 打野: 普通野怪 (marchType=42, 5连打, 无次数限制) ----------
    def hunt_normal(self, max_bottles, queue=1):
        tpl = self.preset_tpl(queue, load_config()["tpl_normal"])
        need = STAMINA_PER_NORMAL
        self.log(f"===== 自动打野: 普通野怪(5连打, 每次 {need} 体力) | "
                 f"体力瓶上限 {max_bottles} | 体力耗尽且瓶子用完即停 =====")
        count = 0
        self.bottles_used = 0
        while not self.stop_evt.is_set():
            # 体力预检
            if self.stamina is not None and self.stamina < need:
                if not self.ensure_stamina(need, max_bottles):
                    break

            # 1) 搜索野怪 (906, 与召唤共用命令)
            outer = self.request(CMD_SUMMON, NORMAL_SEARCH)
            if outer is None:
                self.log("[!] 搜索无响应, 30 秒后重试")
                self._sleep(30)
                continue
            if outer.get("s") != 0:
                self.log(f"[停止] 搜索失败 s={outer.get('s')}")
                break
            point = self._inner(outer).get("point") or {}
            x, y = point.get("x"), point.get("y")
            lv = (point.get("m") or {}).get("level")
            self.log(f"[OK] 搜索到野怪: {lv}级 @ ({x},{y}) [第 {count + 1} 次]")

            # 2) 编队 (905/999)
            ox, oy = tpl["owner"]["x"], tpl["owner"]["y"]
            self.request(CMD_MARCH_CFG,
                         {"type": 42, "ownerx": ox, "ownery": oy,
                          "x": x, "y": y, "heroList": tpl["heroes"],
                          "speed": tpl["speed"], "ownerRid": 0, "rid": 0})
            self.request(CMD_ARMY_CFG,
                         {"armyListNew": tpl["armyList"],
                          "heroList": tpl["heroes"],
                          "formation": tpl["formation"],
                          "formationV2": tpl["formationV2"]})

            # 3) 攻打 (902, 失败自动重试: 队列未回家则等待)
            go = {"marchType": 42, "x": x, "y": y,
                  "armyList": tpl["armyList"], "armyListNew": tpl["armyList"],
                  "heroList": tpl["heroes"], "trapList": [], "break": 0,
                  "formation": tpl["formation"],
                  "formationV2": tpl["formationV2"], "extraArmy": {}}
            outer = None
            for attempt in range(1, 6):
                outer = self.request(CMD_ATTACK, go, timeout=30)
                if outer is not None and outer.get("s") == 0:
                    break
                s = outer.get("s") if outer else "无响应"
                self.log(f"[..] 攻打未成功 (s={s}){self.fail_detail(outer)}, "
                         f"重试 {attempt}/5 (英雄不在家/等待队列回家)...")
                if not self.ensure_stamina(need, max_bottles):
                    break
                self.wait_queue_home_est()
                outer = None
            if outer is None or outer.get("s") != 0:
                s = outer.get("s") if outer else "无响应"
                self.log(f"[停止] 攻打连续失败 (s={s})"
                         f"{self.fail_detail(outer)}"
                         + " ← 若为英雄不在家, 请在游戏中等该队列回城"
                           "或换一个预设队列")
                break
            mi = self._inner(outer).get("marchInfo") or {}
            march_id = str(mi.get("marchId") or "")
            if march_id:
                self.my_marches[march_id] = {"state": mi.get("state")}
            self.log(f"[OK] 已出征 (连打约 4 分钟)"
                     + (f", march=...{march_id[-8:]}" if march_id else ""))
            count += 1

            # 4) 等战斗结果 (10163, marchType=42, 5份战报+奖励同时到)
            self.last_battle = None
            r = self.wait_battle(600, match_type=42)
            if r is None:
                self.log("[!] 等待战斗结果超时, 继续下一轮")
            elif r[0] != 1:
                self.log(f"[!] 战斗未胜 (result={r[0]}), 继续下一轮")

            # 5) 等队列回家 (事件驱动, 精确到秒)
            self.wait_home(march_id)

        self.log(f"[结束] 本次攻打 {count} 次, 使用体力包 {self.bottles_used} 个"
                 + (f" ({self.stop_reason})" if self.stop_reason else ""))

    # ---------- 打野: 难民 (卡片召唤 + 集结攻打, 无次数限制) ----------
    def hunt_refugee(self, max_bottles, queue=2):
        tpl = self.preset_tpl(queue, load_config()["tpl_refugee"])
        need = STAMINA_PER_REFUGEE
        self.log(f"===== 自动打野: 难民(卡片召唤, 集结攻打, 每次 {need} 体力) | "
                 f"体力瓶上限 {max_bottles} | 体力耗尽且瓶子用完即停 =====")
        count = 0
        self.bottles_used = 0
        while not self.stop_evt.is_set():
            # 体力预检
            if self.stamina is not None and self.stamina < need:
                if not self.ensure_stamina(need, max_bottles):
                    break

            # 1) 使用难民召唤卡 (815), 服务器随即推送 10802 难民坐标
            self.refugee_point = None
            outer = self.request(CMD_OPEN_ITEM,
                                 {"itemid": CARD_REFUGEE, "amount": 1})
            if outer is None:
                self.log("[!] 卡片使用无响应, 30 秒后重试")
                self._sleep(30)
                continue
            if outer.get("s") != 0:
                self.log(f"[停止] 卡片使用失败 s={outer.get('s')} "
                         f"(难民卡可能已用完)")
                break
            cards_left = self._inner(outer).get("amount")

            # 2) 等待难民坐标推送 (10802)
            pt = None
            t0 = time.time()
            while time.time() - t0 < 10:
                if self.refugee_point is not None:
                    pt = self.refugee_point
                    break
                self._pwait(300)
            if pt is None:
                self.log("[!] 未收到难民坐标推送(10802), 30 秒后重试")
                self._sleep(30)
                continue
            x, y = pt.get("x"), pt.get("y")
            self.log(f"[OK] 召唤成功: 难民 @ ({x},{y}) [第 {count + 1} 次]"
                     + (f", 卡片剩余 {cards_left}"
                        if cards_left is not None else ""))

            # 3) 编队 (905/999, 集结 marchType=8)
            ox, oy = tpl["owner"]["x"], tpl["owner"]["y"]
            self.request(CMD_MARCH_CFG,
                         {"type": 8, "ownerx": ox, "ownery": oy,
                          "x": x, "y": y, "heroList": tpl["heroes"],
                          "speed": tpl["speed"], "ownerRid": 0, "rid": 0})
            self.request(CMD_ARMY_CFG,
                         {"armyListNew": tpl["armyList"],
                          "heroList": tpl["heroes"],
                          "formation": tpl["formation"],
                          "formationV2": tpl["formationV2"]})

            # 4) 集结出征 (1201, 失败自动重试: 体力/队列未回家)
            go = {"marchType": 8, "x": x, "y": y, "time": 60,
                  "armyList": tpl["armyList"], "armyListNew": tpl["armyList"],
                  "heroList": tpl["heroes"], "trapList": [],
                  "formation": tpl["formation"],
                  "formationV2": tpl["formationV2"]}
            outer = None
            for attempt in range(1, 6):
                outer = self.request(CMD_MARCH_GO, go, timeout=30)
                if outer is not None and outer.get("s") == 0:
                    break
                s = outer.get("s") if outer else "无响应"
                self.log(f"[..] 攻打未成功 (s={s}){self.fail_detail(outer)}, "
                         f"重试 {attempt}/5 (英雄不在家/等待队列回家/"
                         f"补充体力)...")
                if not self.ensure_stamina(need, max_bottles):
                    break
                self.wait_queue_home_est()
                outer = None
            if outer is None or outer.get("s") != 0:
                s = outer.get("s") if outer else "无响应"
                self.log(f"[停止] 攻打连续失败 (s={s})"
                         f"{self.fail_detail(outer)}"
                         + " ← 若为英雄不在家, 请在游戏中等该队列回城"
                           "或换一个预设队列")
                break
            mi = self._inner(outer).get("marchInfo") or {}
            march_id = str(mi.get("marchId") or "")
            if march_id:
                self.my_marches[march_id] = {"state": mi.get("state")}
            self.log("[OK] 已集结出征, 等待战斗..."
                     + (f" (march=...{march_id[-8:]})" if march_id else ""))
            count += 1

            # 5) 等战斗结果 (10163, marchType=8, "拯救难民战报")
            self.last_battle = None
            r = self.wait_battle(600, match_type=8)
            if r is None:
                self.log("[!] 等待战斗结果超时, 继续下一轮")
            elif r[0] != 1:
                self.log(f"[!] 战斗未胜 (result={r[0]}), 继续下一轮")

            # 6) 等队列回家 (事件驱动)
            self.wait_home(march_id)

        self.log(f"[结束] 本次攻打难民 {count} 次, 使用体力包 "
                 f"{self.bottles_used} 个"
                 + (f" ({self.stop_reason})" if self.stop_reason else ""))

    # ---------- 自动采集 (2026-11-15 17:08 抓包) ----------
    @staticmethod
    def _in_window(win_start, win_end):
        """监听时间段判断; 留空=全天, 支持跨午夜 (如 22:00~08:00)"""
        def pmin(s):
            h, m = s.split(":")[:2]
            return int(h) * 60 + int(m)
        try:
            if not win_start or not win_end:
                return True
            lt = time.localtime()
            c = lt.tm_hour * 60 + lt.tm_min
            s, e = pmin(win_start), pmin(win_end)
            if s <= e:
                return s <= c < e
            return c >= s or c < e
        except Exception:
            return True

    def wait_any_march_home(self, cap=7200):
        """等待所有已跟踪的行军回家 (采集出征失败重试前用)"""
        t0 = time.time()
        while time.time() - t0 < cap and not self.stop_evt.is_set():
            active = [r for r in self.my_marches.values()
                      if r.get("state") in (1, 2, 5, 3, 13, 14)]
            if not active:
                return
            self._pwait(1000)

    def _gather_once(self, res_key, queue):
        """采集一轮: 搜索田地 → 编队 → 出征采集 → 等待采集结束回家
        返回是否成功完成一次完整采集"""
        gid, lv, name = GATHER_TYPES[res_key]
        base_tpl = load_config()["tpl_normal"]

        # 1) 搜索田地 (906, groupType=资源itemId, pointType=2)
        outer = self.request(CMD_SUMMON, {"minLevel": lv, "maxLevel": lv,
                                          "groupType": gid, "pointType": 2})
        if outer is None:
            self.log(f"[!] 搜索{name}无响应")
            return False
        if outer.get("s") != 0:
            self.log(f"[!] 搜索{name}失败 s={outer.get('s')}"
                     f"{self.fail_detail(outer)} (附近可能没有 {lv} 级田地)")
            return False
        point = self._inner(outer).get("point") or {}
        x, y = point.get("x"), point.get("y")
        self.log(f"[OK] 搜索到{lv}级{name} @ ({x},{y})")

        # 2) 编队 (905 type=3 / 999, 用攻打预设队列的英雄+兵表)
        tpl = self.preset_tpl(queue, base_tpl)
        ox, oy = tpl["owner"]["x"], tpl["owner"]["y"]
        self.request(CMD_MARCH_CFG,
                     {"type": 3, "ownerx": ox, "ownery": oy, "x": x, "y": y,
                      "heroList": tpl["heroes"], "speed": tpl["speed"],
                      "ownerRid": 0, "rid": 0})
        self.request(CMD_ARMY_CFG,
                     {"armyListNew": tpl["armyList"], "heroList": tpl["heroes"],
                      "formation": tpl["formation"],
                      "formationV2": tpl["formationV2"]})

        # 3) 出征采集 (902 marchType=3; 失败重试, 多次失败疑似超负重则缩减兵力)
        scale = 1.0
        outer = None
        for attempt in range(1, 7):
            army = tpl["armyList"]
            if scale < 0.999:
                army = [dict(a, armyNum=max(1, int(round(
                    a.get("armyNum", 0) * scale)))) for a in army]
            go = {"marchType": 3, "x": x, "y": y, "armyList": army,
                  "armyListNew": army, "heroList": tpl["heroes"],
                  "trapList": [], "formation": tpl["formation"],
                  "formationV2": tpl["formationV2"], "extraArmy": {}}
            outer = self.request(CMD_ATTACK, go, timeout=30)
            if outer is not None and outer.get("s") == 0:
                break
            s = outer.get("s") if outer else "无响应"
            self.log(f"[..] 采集出征未成功 (s={s}){self.fail_detail(outer)}, "
                     f"重试 {attempt}/6 (队列未回家则等待)")
            active = [r for r in self.my_marches.values()
                      if r.get("state") in (1, 2, 5, 3, 13, 14)]
            if active:
                self.log("[..] 检测到队列在外, 等待回家后重试...")
                self.wait_any_march_home()
            if attempt >= 3:
                scale *= 0.6
                self.log(f"[..] 多次失败疑似超出负重, 缩减兵力至 "
                         f"{int(scale * 100)}% 重试")
            outer = None
        if outer is None or outer.get("s") != 0:
            s = outer.get("s") if outer else "无响应"
            self.log(f"[!] 采集出征连续失败 (s={s}){self.fail_detail(outer)}"
                     + " ← 若为英雄不在家, 请在游戏中等待该队列回城")
            return False
        mi = self._inner(outer).get("marchInfo") or {}
        march_id = str(mi.get("marchId") or "")
        if march_id:
            self.my_marches[march_id] = {"state": mi.get("state")}
        self.log("[OK] 已出征采集" +
                 (f" (march=...{march_id[-8:]})" if march_id else "") +
                 ", 等待采集结束回家...")

        # 4) 等待采集结束并回家 (采集可能持续数小时)
        self.wait_home(march_id, cap=14400)
        self.log("[OK] 采集队列已回家")
        return True

    def hunt_gather(self, res_key, queue=1, win_start="", win_end=""):
        """自动采集主循环: 时间段内持续 搜索→采集→回家→再采集"""
        keys = list(GATHER_TYPES) if res_key == "all" else [res_key]
        names = "→".join(GATHER_TYPES[k][2] for k in keys)
        win = f"{win_start}~{win_end}" if win_start and win_end else "全天"
        self.log(f"===== 自动采集: {names} | 预设队列 #{queue} | "
                 f"监听时间段: {win} =====")
        count = 0
        idx = 0
        while not self.stop_evt.is_set():
            if not self._in_window(win_start, win_end):
                if idx == 0:
                    self.log(f"[..] 当前不在监听时间段 ({win}), "
                             f"每 30 秒检查一次...")
                self._sleep(30)
                continue
            key = keys[idx % len(keys)]
            if self._gather_once(key, queue):
                count += 1
            else:
                self._sleep(20)   # 搜索失败/队列在外, 稍后换目标重试
            idx += 1
        self.log(f"[结束] 本次采集出征 {count} 次")

    # ---------- 抓包监听 (演示采集流程用) ----------
    def capture_mode(self):
        """监听模式: 记录页面上所有收/发的协议帧, 供演示采集流程抓包分析
        注意: 必须强制刷新游戏页面, 发帧 hook 才能在游戏代码运行前生效"""
        self.log("===== 抓包监听模式已启动 =====")
        self.log("[..] 游戏页面将自动刷新以注入发帧 hook, 请等待游戏重新加载完成")
        self.log("加载完成后请演示: 搜索并采集玉米田 → 石油田 → 雷神矿")
        self.log("演示完成后回到网页点「停止任务」, 协议帧全部保留在日志中")
        count = 0
        while not self.stop_evt.is_set():
            self._pwait(500)
        self.log(f"[结束] 抓包监听已停止 (累计处理帧 {count})")
