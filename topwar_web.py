# -*- coding: utf-8 -*-
"""
口袋奇兵助手 - 本地网页版控制台

架构: 本机 Python 后端 (纯标准库, 只监听 127.0.0.1) + 内嵌单页网页
  GET  /              控制台页面
  GET  /api/status    任务状态 + 打野引擎实时数据
  GET  /api/logs?since=N   增量日志(含协议帧)
  POST /api/checkin   启动签到
  POST /api/hunt      启动打野 {monster, max_bottles, daily_limit}
  POST /api/stop      停止打野

用法: python topwar_web.py  (自动打开浏览器)
"""

import contextlib
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import topwar_checkin
from topwar_hunt import HuntEngine, load_config, save_config, _MARCH_CACHE

HOST, PORT = "127.0.0.1", 8765
VERSION = "2026.11.15-6"    # 页面标题栏会显示, 用于确认运行的是新代码

MONSTERS = [
    ("clubstar", "俱星 5级 (每日限10次)"),
    ("normal",   "普通野怪 5连打 (无限制 · 体力耗尽为止)"),
    ("refugee",  "难民 卡片召唤 (无限制 · 体力耗尽为止)"),
]


# ---------------- 全局状态 ----------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.logs = []          # [{i,tag,msg,t}]
        self.log_i = 0
        self.tasks = {}         # tid -> {"type", "info", "engine"}
        self._tid = 0

    def log(self, tag, msg):
        with self.lock:
            self.log_i += 1
            self.logs.append({"i": self.log_i, "tag": tag, "msg": msg,
                              "t": time.strftime("%H:%M:%S")})
            if len(self.logs) > 4000:
                del self.logs[:2000]

    def since(self, idx):
        with self.lock:
            return [e for e in self.logs if e["i"] > idx]

    def add_task(self, ttype, info=""):
        with self.lock:
            self._tid += 1
            tid = self._tid
            self.tasks[tid] = {"type": ttype, "info": info, "engine": None}
        return tid

    def set_engine(self, tid, engine):
        with self.lock:
            if tid in self.tasks:
                self.tasks[tid]["engine"] = engine

    def del_task(self, tid):
        with self.lock:
            self.tasks.pop(tid, None)

    def status(self):
        with self.lock:
            ts = []
            for tid, t in self.tasks.items():
                e = t["engine"]
                eng = None
                if e is not None:
                    eng = {"running": e.running,
                           "stamina": e.stamina,
                           "staminaMax": e.stamina_max,
                           "bottlesUsed": e.bottles_used,
                           "bottlesLeft": e.bottles_left,
                           "hunts": e.hunts,
                           "stopReason": e.stop_reason}
                ts.append({"id": tid, "type": t["type"],
                           "info": t["info"], "engine": eng})
            return {"tasks": ts, "version": VERSION}


STATE = State()
load_config()   # 启动时载入持久化的出征队列配置缓存 (11016)


class LogWriter:
    """重定向 print 输出到网页日志"""
    def __init__(self, tag):
        self.tag = tag

    def write(self, s):
        for line in s.rstrip("\n").split("\n"):
            if line.strip():
                STATE.log(self.tag, line)

    def flush(self):
        pass


# ---------------- 任务 ----------------
def run_checkin(tid):
    STATE.log("sys", "—— 签到任务开始 ——")
    try:
        with contextlib.redirect_stdout(LogWriter("checkin")):
            topwar_checkin.main()
    except Exception as e:
        STATE.log("checkin", f"[错误] {e}")
    finally:
        STATE.del_task(tid)
        STATE.log("sys", "—— 签到任务结束 ——")


def run_hunt(tid, cfg):
    engine = HuntEngine(log=lambda m: STATE.log("hunt", m))
    STATE.set_engine(tid, engine)
    STATE.log("sys", f"—— 打野任务开始 (类型: {cfg['monster']}, "
                     f"体力瓶上限: {cfg['max_bottles']}) ——")
    try:
        engine.connect()
        if cfg["monster"] == "clubstar":
            engine.hunt_clubstar(cfg["max_bottles"],
                                 cfg.get("daily_limit", 10),
                                 cfg.get("queue_clubstar", 1))
        elif cfg["monster"] == "normal":
            engine.hunt_normal(cfg["max_bottles"],
                               cfg.get("queue_normal", 1))
        elif cfg["monster"] == "refugee":
            engine.hunt_refugee(cfg["max_bottles"],
                                cfg.get("queue_refugee", 2))
        else:
            engine.log("[!] 该野怪类型的协议尚未实现: "
                       "请先在游戏中演示一次完整攻打流程供抓包分析。")
    except Exception as e:
        engine.log(f"[错误] {e}")
    finally:
        engine.close()
        STATE.del_task(tid)
        STATE.log("sys", "—— 打野任务结束 ——")


def run_gather(tid, cfg):
    engine = HuntEngine(log=lambda m: STATE.log("gather", m))
    STATE.set_engine(tid, engine)
    STATE.log("sys", "—— 自动采集任务开始 ——")
    try:
        engine.connect()
        engine.hunt_gather(cfg.get("gather_type", "corn"),
                           cfg.get("queue_gather", 1),
                           cfg.get("gather_win_start", ""),
                           cfg.get("gather_win_end", ""))
    except Exception as e:
        engine.log(f"[错误] {e}")
    finally:
        engine.close()
        STATE.del_task(tid)
        STATE.log("sys", "—— 自动采集任务结束 ——")


def run_capture(tid):
    engine = HuntEngine(log=lambda m: STATE.log("hunt", m))
    STATE.set_engine(tid, engine)
    STATE.log("sys", "—— 抓包监听开始 ——")
    try:
        engine.connect(force_reload=True)
        engine.capture_mode()
    except Exception as e:
        engine.log(f"[错误] {e}")
    finally:
        engine.close()
        STATE.del_task(tid)
        STATE.log("sys", "—— 抓包监听结束 ——")


def start_thread(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


# ---------------- 页面 ----------------
PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>口袋奇兵助手</title>
<style>
:root{--bg:#0f1420;--card:#1a2233;--line:#2a3550;--txt:#dce3f2;
      --dim:#8a94ad;--acc:#4a8cff;--ok:#3ecf6f;--warn:#e8a13c;--err:#ff6b6b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);
     font-family:"Microsoft YaHei UI",system-ui,sans-serif;padding:20px}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px}
.sub{color:var(--dim);font-size:12px;margin-bottom:18px}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;
       font-size:12px;background:var(--line);margin-left:8px}
.badge.busy{background:#7a5a17;color:#ffd88a}
.badge.idle{background:#1d3a2a;color:#8ee6ae}
.row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
button{border:0;border-radius:8px;padding:10px 22px;font-size:14px;
       cursor:pointer;color:#fff;background:var(--acc);transition:.15s}
button:hover{filter:brightness(1.15)}
button:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:var(--line)}
button.stop{background:var(--err)}
.card{background:var(--card);border:1px solid var(--line);
      border-radius:12px;padding:16px;margin-bottom:14px}
.card h2{font-size:14px;color:var(--dim);margin-bottom:10px}
.stats{display:flex;gap:18px;flex-wrap:wrap}
.stat .k{font-size:12px;color:var(--dim)}
.stat .v{font-size:20px;font-weight:600;margin-top:2px}
form .f{display:flex;align-items:center;gap:10px;margin-bottom:10px}
form label{width:150px;color:var(--dim);font-size:13px}
select,input{background:#111827;border:1px solid var(--line);color:var(--txt);
             border-radius:6px;padding:7px 10px;font-size:13px}
.tip{font-size:12px;color:var(--dim);line-height:1.7;margin-top:8px}
.tabs{display:flex;gap:6px;margin-bottom:8px}
.tab{padding:5px 14px;border-radius:6px;font-size:13px;cursor:pointer;
     background:var(--line);color:var(--dim)}
.tab.on{background:var(--acc);color:#fff}
.logbox{background:#0b0f19;border:1px solid var(--line);border-radius:8px;
        height:340px;overflow-y:auto;padding:10px;font-size:12px;
        font-family:Consolas,monospace;line-height:1.65}
.ln{white-space:pre-wrap;word-break:break-all}
.ln .t{color:var(--dim);margin-right:6px}
.tag-checkin{color:var(--ok)} .tag-hunt{color:#7ab3ff}
.tag-gather{color:#5fd0a8}
.tag-sys{color:var(--warn)} .tag-frame{color:#6f7a92}
.taskrow{display:flex;align-items:center;gap:10px;padding:8px 12px;
         border:1px solid var(--line);border-radius:8px;margin-bottom:8px}
.taskrow .tt{flex:1;font-size:13px}
.taskrow .ts{font-size:12px;color:var(--dim);max-width:260px;
             overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.taskrow button{padding:5px 14px;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>口袋奇兵助手
    <span id="badge" class="badge idle">空闲</span>
  </h1>
  <div class="sub">本地控制台 v<span id="ver">-</span> · 仅监听 127.0.0.1 ·
     关闭后端 cmd 窗口即关闭服务</div>

  <div class="row">
    <button id="btnCheckin" onclick="api('/api/checkin')">① 开始签到</button>
    <button id="btnHunt" onclick="toggleHunt()">② 自动打野 ▾</button>
    <button id="btnCap" onclick="api('/api/capture')">③ 抓包监听</button>
    <button id="btnGather" onclick="toggleGather()">④ 自动采集 ▾</button>
    <button id="btnStopAll" class="stop" onclick="api('/api/stop')">全部停止</button>
  </div>

  <div class="card" id="huntCard" style="display:none">
    <h2>打野详情设置</h2>
    <form onsubmit="startHunt(event)">
      <div class="f">
        <label>野怪类型</label>
        <select id="monster"></select>
      </div>
      <div class="f">
        <label>体力瓶使用上限</label>
        <input id="bottles" type="number" min="0" max="999" value="5">
        <span class="tip">配额用完后自动停止打野</span>
      </div>
      <div class="f">
        <label>攻打预设队列</label>
        <select id="queue"></select>
        <span class="tip">使用游戏内攻打预设(1~10)的英雄+兵表, 启动时日志会显示所选预设详情</span>
      </div>
      <div class="f">
        <label>俱星每日召唤上限</label>
        <input id="limit" type="number" min="1" max="10" value="10">
      </div>
      <div class="tip">· 每次集结攻打消耗 10 点体力，体力不足时自动开启 10 点体力包<br>
          · 体力上限 95，自然恢复 1 点 / 6 分钟<br>
          · 打完一轮后自动等待队列回家，再召唤下一只</div>
      <div style="margin-top:12px">
        <button type="submit">启动打野</button>
      </div>
    </form>
  </div>

  <div class="card" id="gatherCard" style="display:none">
    <h2>自动采集设置</h2>
    <form onsubmit="startGather(event)">
      <div class="f">
        <label>田地类型</label>
        <select id="gres">
          <option value="corn">玉米田 5级</option>
          <option value="oil">石油田 5级</option>
          <option value="thor">雷神矿 3级</option>
          <option value="all">三种轮流 (玉米→石油→雷神)</option>
        </select>
      </div>
      <div class="f">
        <label>采集预设队列</label>
        <select id="gqueue"></select>
        <span class="tip">使用游戏内攻打预设(1~10)的英雄+兵表出征采集</span>
      </div>
      <div class="f">
        <label>监听时间段</label>
        <input id="gstart" type="time">
        <span class="tip">至</span>
        <input id="gend" type="time">
        <span class="tip">留空 = 全天; 时间段内队列回家后自动再次采集</span>
      </div>
      <div class="tip">· 采集不消耗体力, 自动搜索附近指定类型/等级田地<br>
        · 采集中等待队列回家(可能数小时), 回家后自动采下一趟<br>
        · 出征失败会自动等待队列回家并重试, 兵力超负重时自动缩减</div>
      <div style="margin-top:12px">
        <button type="submit">启动采集</button>
      </div>
    </form>
  </div>

  <div class="card" id="tasksCard" style="display:none">
    <h2>运行中任务 <span class="tip">各任务独立运行互不影响; 可启动多个采集任务用不同队列采同类田</span></h2>
    <div id="taskList"></div>
  </div>

  <div class="card">
    <h2>实时状态</h2>
    <div class="stats">
      <div class="stat"><div class="k">运行任务</div><div class="v" id="sTasks">0</div></div>
      <div class="stat"><div class="k">体力</div><div class="v" id="sStam">-</div></div>
    </div>
  </div>

  <div class="card">
    <div class="tabs">
      <div class="tab on" id="tabLog" onclick="setTab('log')">运行日志</div>
      <div class="tab" id="tabFrame" onclick="setTab('frame')">协议帧</div>
    </div>
    <div class="logbox" id="logbox"></div>
  </div>
</div>

<script>
let since=0, tab='log', polling=false;
const $=id=>document.getElementById(id);
const MON={clubstar:"俱星 5级 (每日限10次)", normal:"普通野怪 5连打 (无限制 · 体力耗尽为止)", refugee:"难民 卡片召唤 (无限制 · 体力耗尽为止)"};
$("monster").innerHTML=Object.entries(MON).map(([k,v])=>`<option value="${k}">${v}</option>`).join("");
$("queue").innerHTML=Array.from({length:10},(_,i)=>`<option value="${i+1}">预设队列 ${i+1}</option>`).join("");
const QDEF={clubstar:1,normal:1,refugee:2};
$("queue").value=QDEF[$("monster").value];
$("monster").onchange=()=>{$("queue").value=QDEF[$("monster").value];};

function toggleHunt(){const c=$("huntCard");c.style.display=c.style.display==="none"?"block":"none";if(c.style.display==="block")loadQueues();}
function toggleGather(){const c=$("gatherCard");c.style.display=c.style.display==="none"?"block":"none";if(c.style.display==="block")loadQueues();}
$("gqueue").innerHTML=Array.from({length:10},(_,i)=>`<option value="${i+1}">预设队列 ${i+1}</option>`).join("");

function startGather(ev){
  ev.preventDefault();
  api("/api/gather",{res:$("gres").value,
                     queue:+$("gqueue").value,
                     win_start:$("gstart").value,
                     win_end:$("gend").value});
  $("gatherCard").style.display="none";
}

function setTab(t){tab=t;$("tabLog").classList.toggle("on",t==="log");
  $("tabFrame").classList.toggle("on",t==="frame");
  $("logbox").innerHTML="";since=(tab==="log")?logSince:frameSince;}

let logSince=0, frameSince=0;

function color(tag){return tag==="checkin"?"tag-checkin":tag==="hunt"?"tag-hunt":tag==="gather"?"tag-gather":tag==="sys"?"tag-sys":"tag-frame";}

function stopTask(id){api("/api/stop",{id:id});}

async function loadQueues(){
  try{
    const r=await(await fetch("/api/queues")).json();
    const qs=r.queues||[];
    const opt=qs.length
      ? qs.map(q=>`<option value="${q.slot}">队列${q.slot} · 英雄${q.heroes.join("/")}${q.formationV2?` · 编队${q.formationV2}`:""}</option>`).join("")
      : Array.from({length:10},(_,i)=>`<option value="${i+1}">预设队列 ${i+1}</option>`).join("");
    $("queue").innerHTML=opt;
    $("gqueue").innerHTML=opt;
    $("queue").value=QDEF[$("monster").value]||1;
  }catch(e){}
}

async function api(path, body){
  const opt=body?{method:"POST",headers:{"Content-Type":"application/json"},
                  body:JSON.stringify(body)}:{method:"POST"};
  const r=await fetch(path,opt); return r.json();
}

function startHunt(ev){
  ev.preventDefault();
  api("/api/hunt",{monster:$("monster").value,
                   max_bottles:+$("bottles").value,
                   daily_limit:+$("limit").value,
                   queue:+$("queue").value});
  $("huntCard").style.display="none";
}

async function poll(){
  if(polling)return; polling=true;
  try{
    const st=await(await fetch("/api/status")).json();
    $("ver").textContent=st.version||"?";
    const ts=st.tasks||[];
    const b=$("badge");
    b.textContent=ts.length?(ts.length+" 个任务运行中"):"空闲";
    b.className="badge "+(ts.length?"busy":"idle");
    $("sTasks").textContent=ts.length;
    let stam="-";
    for(const t of ts){
      if(t.engine&&t.engine.stamina!=null){stam=t.engine.stamina+"/"+t.engine.staminaMax;break;}
    }
    $("sStam").textContent=stam;
    $("tasksCard").style.display=ts.length?"block":"none";
    $("taskList").innerHTML=ts.map(t=>{
      const e=t.engine;
      const stt=e?(e.running?"运行中":"启动中…"):"";
      const extra=e&&e.stopReason?(" · "+e.stopReason):"";
      return `<div class="taskrow"><span class="tt">${(t.info||t.type).replace(/</g,"&lt;")}</span>`+
             `<span class="ts">${stt}${extra}</span>`+
             (e?`<button class="stop" onclick="stopTask(${t.id})">停止</button>`:"")+
             `</div>`;
    }).join("");
    // 日志
    const cur=tab;
    const r=await(await fetch("/api/logs?since="+(cur==="log"?logSince:frameSince)
                              +(cur==="frame"?"&frame=1":""))).json();
    if(r.items.length){
      const box=$("logbox");
      for(const it of r.items){
        const d=document.createElement("div");
        d.className="ln "+color(it.tag);
        const isFrame=it.msg.startsWith("[>>]")||it.msg.startsWith("[<<]");
        if(cur==="log"&&isFrame)continue;
        if(cur==="frame"&&!isFrame)continue;
        d.innerHTML=`<span class="t">${it.t}</span>${it.msg.replace(/</g,"&lt;")}`;
        box.appendChild(d);
      }
      box.scrollTop=box.scrollHeight;
      if(cur==="log")logSince=r.next; else frameSince=r.next;
    }
  }catch(e){}
  polling=false;
  setTimeout(poll,1000);
}
poll();
</script>
</body>
</html>
"""


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _page(self):
        data = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._page()
        elif self.path.startswith("/api/status"):
            self._json(STATE.status())
        elif self.path.startswith("/api/logs"):
            idx = 0
            frame_only = "frame=1" in self.path
            try:
                idx = int(self.path.split("since=")[1].split("&")[0])
            except Exception:
                pass
            items = STATE.since(idx)
            if frame_only:
                items = [e for e in items
                         if e["msg"].startswith(("[>>]", "[<<]"))]
            self._json({"items": items,
                        "next": items[-1]["i"] if items else idx})
        elif self.path.startswith("/api/queues"):
            qs = []
            for i, ent in enumerate(_MARCH_CACHE["presets"]):
                qs.append({"slot": i + 1,
                           "heroes": ent.get("heroIds") or [],
                           "armyCnt": len(ent.get("armyInfo") or []),
                           "formationV2": ent.get("formationV2", 0)})
            self._json({"queues": qs})
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                body = {}

        if self.path == "/api/checkin":
            if any(t["type"] == "签到" for t in
                   list(STATE.tasks.values())):
                self._json({"ok": False, "err": "签到任务已在运行"})
                return
            tid = STATE.add_task("签到")
            start_thread(run_checkin, tid)
            self._json({"ok": True})
        elif self.path == "/api/hunt":
            cfg = load_config()
            cfg["monster"] = body.get("monster", "clubstar")
            cfg["max_bottles"] = max(0, int(body.get("max_bottles", 5)))
            cfg["daily_limit"] = max(1, int(body.get("daily_limit", 10)))
            try:
                q = min(10, max(1, int(body.get("queue", 1))))
            except Exception:
                q = 1
            cfg[f"queue_{cfg['monster']}"] = q
            save_config(cfg)
            label = dict(MONSTERS).get(cfg["monster"], cfg["monster"])
            tid = STATE.add_task("打野", f"打野 · {label} · 队列{q}")
            start_thread(run_hunt, tid, cfg)
            self._json({"ok": True})
        elif self.path == "/api/gather":
            cfg = load_config()
            cfg["gather_type"] = body.get("res", "corn")
            try:
                q = min(10, max(1, int(body.get("queue", 1))))
            except Exception:
                q = 1
            cfg["queue_gather"] = q
            ws_ = (body.get("win_start") or "").strip()
            we_ = (body.get("win_end") or "").strip()
            cfg["gather_win_start"] = ws_
            cfg["gather_win_end"] = we_
            save_config(cfg)
            gname = {"corn": "玉米田5级", "oil": "石油田5级",
                     "thor": "雷神矿3级", "all": "三种轮流"}.get(
                         cfg["gather_type"], cfg["gather_type"])
            win = f"{ws_}~{we_}" if ws_ and we_ else "全天"
            tid = STATE.add_task("采集", f"采集 · {gname} · 队列{q} · {win}")
            start_thread(run_gather, tid, cfg)
            self._json({"ok": True})
        elif self.path == "/api/capture":
            if STATE.tasks:
                self._json({"ok": False,
                            "err": "抓包监听会刷新游戏页面, 请先停止所有任务"})
                return
            tid = STATE.add_task("抓包")
            start_thread(run_capture, tid)
            self._json({"ok": True})
        elif self.path == "/api/stop":
            try:
                tid = int(body.get("id", 0))
            except Exception:
                tid = 0
            with STATE.lock:
                if tid:
                    targets = [(tid, dict(STATE.tasks[tid]))] \
                        if tid in STATE.tasks else []
                else:
                    targets = list(STATE.tasks.items())
            stopped = 0
            for _, t in targets:
                e = t.get("engine")
                if e is not None:
                    e.stop_evt.set()
                    if not e.stop_reason:
                        e.stop_reason = "手动停止"
                    stopped += 1
            if stopped:
                STATE.log("sys", "[..] 已请求停止任务 "
                          f"(当前轮打完/回家后退出)")
                self._json({"ok": True})
            else:
                self._json({"ok": False, "err": "该任务没有可停止的引擎"})
        else:
            self.send_error(404)

    def log_message(self, *a):  # 关闭访问日志
        pass


class Server(ThreadingHTTPServer):
    # 禁止 SO_REUSEADDR: 防止多个后端实例同时绑定 8765 导致新旧代码混淆
    allow_reuse_address = False


def main():
    try:
        srv = Server((HOST, PORT), Handler)
    except OSError:
        print("[X] 端口 8765 已被占用: 已有一个控制台实例在运行。")
        print("    请勿重复双击启动; 如需重启, 先关闭旧的 python 进程。")
        raise SystemExit(1)
    url = f"http://{HOST}:{PORT}"
    print(f"[OK] 控制台已启动: {url}  (版本 {VERSION}, Ctrl+C 退出)")
    threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出] 服务已停止")


if __name__ == "__main__":
    main()
