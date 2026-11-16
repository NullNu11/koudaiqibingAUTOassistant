# 口袋奇兵助手 (TopWar H5 自动化工具) — 开发文档

> 更新日期: 2026-11-15 · 控制台版本 `2026.11.15-6`
> 本文档面向后续维护 / 补充开发的 AI 或人类开发者，完整记录已实现功能、协议逆向结论与设计逻辑。

---

## 1. 项目概述

本项目通过 **浏览器注入 + WebSocket 协议帧直发** 的方式自动化操作《口袋奇兵》H5 网页版
(`https://warh5.rivergame.net/webgame/index.html`)。

核心思路：

1. 用带远程调试端口(`--remote-debugging-port=9222`)的 Edge 启动游戏（复用已登录 profile）；
2. 通过 Playwright CDP 连接该浏览器，向游戏页面注入 WebSocket Hook；
3. Hook 捕获游戏收发的**原始加密协议帧**，由 Python 端解密分析；
4. Python 端构造加密帧直接写入游戏 socket，实现"不点 UI 直接发协议"的自动化；
5. 本地 Web 控制台（`127.0.0.1:8765`）提供 UI、日志与多任务管理。

### 文件清单

| 文件 | 作用 |
|---|---|
| `topwar_checkin.py` | 基础库（Edge 启动、WS Hook 注入、帧编解码）+ 自动签到（cmd 5684/5685） |
| `topwar_hunt.py` | 核心协议引擎 `HuntEngine`：打野（俱星/普通野怪/难民）、自动采集、抓包监听、队列配置缓存 |
| `topwar_web.py` | 本地 Web 控制台（纯标准库 HTTP 服务 + 内嵌单页 HTML/JS），多任务管理 |
| `topwar_login.py` | 微信扫码登录 + WS/HTTP 抓取（v3，普通方式启动 Edge 避免自动化参数被拒） |
| `topwar_sniff.py` | 独立协议嗅探工具（收/发帧全记录到 JSONL） |
| `topwar_ui.py` | 早期 tk 控制台 UI（已被 `topwar_web.py` 取代） |
| `topwar_chest_stats.py` | 宝箱/奖励统计脚本（基于 sniff 日志） |
| `topwar_hunt_config.json` | 持久化配置（出征模板、体力参数、队列缓存 `march_presets`、用户命名等） |
| `口袋奇兵助手.cmd` / `口袋奇兵签到.cmd` | 一键启动入口 |

运行环境：Windows + Edge，Python 依赖 `playwright`（sync API），其余均为标准库。

---

## 2. 浏览器 / 页面接入层

### 2.1 Edge 启动与连接 (`topwar_checkin.py`)

- `ensure_edge()`：探测 `127.0.0.1:9222`，未监听则用 `subprocess.Popen` 启动 Edge
  （profile 固定为 `seasoilder/topwar_profile/`，保持微信登录态）。
- 引擎通过 `chromium.connect_over_cdp("http://127.0.0.1:9222")` 连接，
  取 `contexts[0]` 中 url 含 `rivergame` 的页面（多余的游戏页自动关闭）。

### 2.2 WebSocket Hook (`WS_HOOK_JS`, add_init_script 注入)

- 替换 `window.WebSocket` 构造器，把所有新建 socket 记入 `window.__tw_sockets = [{ws, url}]`。
- 帧捕获过滤条件：`ws.readyState === 1 && /rivergame/i.test(url)`。

### 2.3 收发帧捕获（多任务并行关键设计）

**收帧**：每个引擎实例持有一个页面内 sink（`window.__tw_sinks = [{id, q, done}]`）。
`DRAIN_JS(id)` 首次调用时给每个游戏 socket 挂 `message` 监听，把收到的帧 base64 后
push 进 `q`，随后取出并清空队列返回给 Python。

**发帧**：`CAPTURE_JS` 中一次性 hook `WebSocket.prototype.send`（proto 级），
把客户端发出的帧广播到**所有** sink（`{d: b64, s: 1}`，s=1 标记为发帧）。

> **重要坑**：必须在 proto 级 hook `send`，不能 hook 实例 —— 游戏代码会缓存 `ws.send`
> 引用，页面加载后再包装实例不生效。因此抓包监听任务必须
> `connect(force_reload=True)` **强制刷新游戏页面**，让 hook 在游戏代码运行前注入
> （`add_init_script` 保证时序）。

sink id 形如 `cap{毫秒}{id(self)%1000}`，每个任务唯一；`close()` 时从页面移除本 sink。

### 2.4 线程模型（Playwright sync API 限制）

Playwright sync API **不能跨线程**：页面操作只能在创建它的线程里做。
所以引擎所有等待都经过 `_pwait(ms)` —— 边倒计时边以 300ms 节奏轮询
`_drain_page()`（evaluate `DRAIN_JS` 拉帧 → `_process()` 解密分发）。
不允许用 `threading.Event.wait()` 长阻塞收帧线程，否则帧会堆积不处理。

---

## 3. 协议层

### 3.1 帧格式与加密（已完全逆向）

```
帧头: [cmd int32 BE][order int32 BE][bodyLen int32 BE]  共 12 字节
载荷: [format u8][加密 body]
密钥: key = struct.pack(">i", 0x01010101 | order)  # 4 字节
  format ^= key[0]
  body[u] ^= key[(u + 1) % 4]
```

- `format`(fmt) 0 = JSON（绝大多数命令），2 = 二进制（protobuf / 混合）。
- `order`：响应与请求的 order 对应；引擎自己发的帧用 `order = 9_000_000 + ...`
  以便与游戏客户端的帧区分。
- `decrypt_raw()` 解密、`extract_frames()` 处理粘包（一次 WebSocket 载荷可含多帧）。
- `encode_json_frame()` / `encode_raw_frame()` / `send_json()` / `send_raw()` 构造与发送。

### 3.2 响应匹配 (`request(cmd, obj, timeout)`)

1. 发送后等待 `_acks[(cmd, order)]`（order 精确匹配）；
2. 2 秒后降级为时间窗口匹配（`_window_match`：取发送后到达的同 cmd 最新响应）；
3. 超时返回 None 并打印收帧诊断。

响应结构：外层 `{cmd, s, t, d}`，`s=0` 成功；`d` 为内层业务 JSON（`_inner()`）。
`t > 1e12` 视为服务器毫秒时间戳（维护 `self.server_t`）。

### 3.3 已知协议命令表（实测）

| cmd | 名称/含义 | 关键字段 |
|---|---|---|
| 0 | 心跳 | — |
| 906 | 搜索/召唤点（野怪与田地共用） | `minLevel,maxLevel,groupType,pointType`；野怪 pointType=1，田地 pointType=2，俱星 pointType=9 |
| 905 | 行军配置（编队第一步） | `type`(8集结/42连打/3采集), `ownerx,ownery,x,y,heroList,speed` |
| 999 | 部队编制（编队第二步） | `armyListNew:[{pos,armyId,armyNum}],heroList,formation,formationV2` |
| 1201 | 集结出征 (marchType=8) | `x,y,time:60,armyList,heroList,...` |
| 902 | 普通出征（连打/采集, marchType=42/3） | 同上结构，含 `extraArmy:{}` |
| 815 | 开启道具 | `itemid, amount`（600001=10点体力包, 1700039=难民召唤卡） |
| 6514 | 攻打预设拉取（旧方案，已弃用） | 发 8 字节 0 的 fmt=2 帧 → 返回二进制混合帧（`extract_json_blobs` 解析） |
| 1346 | 游戏内保存队列（客户端发） | 触发服务器推送 11016 |
| 10007 | 行军状态推送（旧） | `marchId, state, returnArriveTime` |
| 10019 | 行军详情推送 | `marchInfo:{marchId,state,returnArriveTime}`；采集 state: 1行军/5采集中/3返程/4到家（另有 13战斗中/14战斗结束） |
| 10021 | 体力推送 | `point, pointMax, reason` |
| 10163 | 战斗结果推送 | `result, pveId, marchType` |
| 10401 | 物品变动推送 | `itemId, amount` |
| 10802 | 难民坐标推送 | `x, y` |
| **11016** | **出征队列配置推送 (`userPreMarch`)** | 见 3.4 |

groupType 速查：9100=俱星、61=普通野怪；田地 = 资源 itemId：
**506=玉米田、706=石油田、1603=雷神矿**。

### 3.4 出征队列配置 (cmd 11016) —— 队列识别的正确姿势

游戏内 10 个出征队列（英雄+兵表+编队）的**唯一真实来源**：

- 服务器在 **登录时** 和 **游戏内保存队列(1346)后** 主动推送 11016，
  `data.userPreMarch` 为队列条目数组；
- **无法主动拉取**（直接发 11016 返回 s=125 命令不存在）；
- ⚠️ **数组顺序 = 队列号**（第 N 项即游戏中的队列 N）。条目里的 `icon` 字段
  与队列编号无关（icon=2 会重复出现），不能用作编号；
- 空槽位不出现在数组里（曾出现只返回 8 个队列的情况）。

`_MARCH_CACHE = {"presets": [], "evt": threading.Event()}` 为后端进程级全局缓存，
`cache_march_presets(arr)` 收到推送后更新缓存并持久化到 `topwar_hunt_config.json`
的 `march_presets` 字段（重启后由 `topwar_web.py` 启动时 `load_config()` 恢复）。

`preset_tpl(queue, base_tpl)`：等待/读取队列配置（最长 90 秒，期间提示用户在游戏内
编辑保存一个队列或刷新页面），按序号取 `march_presets[queue-1]`，转换为出征模板：

- `heroIds` → `heroes`
- `armyInfo: [{pos(从0计), armyId, num}]` → `armyList: [{pos(+1), armyId, armyNum}]`
- `formation / formationV2` 直接沿用；坐标沿用 base_tpl 的 `owner`（见第 7 节坑）

取不到配置时回退 base_tpl（内置硬编码模板 `TPL_DEFAULT/TPL_NORMAL/TPL_REFUGEE`）。

---

## 4. 功能模块

### 4.1 自动签到 (`topwar_checkin.py`)

- cmd 5684 查询可领任务 → cmd 5685 一键领取（ids=[1..6]）。
- fmt=2 PROTOBUFFV2 body：`[int32 d][d字节头][int32 n][n字节 protobuf]`。
- `main()` 可被 web 控制台 `contextlib.redirect_stdout` 捕获日志调用。

### 4.2 自动打野 (`HuntEngine`, topwar_hunt.py)

三种模式，均为循环：**搜索/召唤 → 编队(905/999) → 出征 → 等战斗结果(10163) → 等回家**。

| 模式 | 入口 | 要点 |
|---|---|---|
| 俱星 | `hunt_clubstar(max_bottles, daily_limit, queue)` | 906 召唤(pointType=9) → 1201(marchType=8)；每日召唤上限；每轮耗 10 体力 |
| 普通野怪 | `hunt_normal(max_bottles, queue)` | 906 搜索(level 104, groupType=61) → 902(marchType=42) 5连打，每次约 14 体力 |
| 难民 | `hunt_refugee(max_bottles, queue)` | 815 用难民卡(1700039) → 等 10802 坐标 → 905(type=8)/999 → 1201；每次 10 体力 |

通用机制：

- **体力管理**：`ensure_stamina()` 监听 10021；体力不足时自动开体力包(815, 600001)，
  包数达上限即软停止（`stop_reason`）。体力规则：上限 95，1 点/6 分钟自然恢复。
- **出征失败重试**（5 次）：失败后 `ensure_stamina` + `wait_queue_home_est()`
  （优先用服务器 `returnArriveTime` 精确等待回家，否则默认 15 秒）。
- **等待回家**：`wait_home(march_id)` 事件驱动 —— 出征响应里的 `marchId` 记入
  `self.my_marches`，监听 10007/10019 的 state 变化（0/4=到家），辅以
  `returnArriveTime` 推算。去程耗时记录在 `march_out_secs` 作兜底估计。
- **软停止语义**：`stop_evt` 只在轮与轮之间生效；已出征的队列必须等战斗结果
  （`wait_battle` 不受 stop 影响），防止部队悬在外面。

### 4.3 自动采集 (`hunt_gather`, v2026.11.15-3)

- `GATHER_TYPES = {"corn": (506, 5, "玉米田"), "oil": (706, 5, "石油田"),
  "thor": (1603, 3, "雷神矿")}`；`"all"` 三种轮流（玉米→石油→雷神）。
- `_gather_once(res_key, queue)` 流程：
  1. 906 搜索田地（`pointType:2`, groupType=资源 itemId, 指定等级）；
  2. 905(`type:3`)/999 编队（复用攻打预设队列的英雄+兵表）；
  3. 902 `marchType:3` 出征采集 —— 失败重试 6 次：检测到队列在外则
     `wait_any_march_home()`；连续 ≥3 次失败视为**超出负重**，兵力 `scale × 0.6`
     递减重试；
  4. `wait_home(march_id, cap=14400)` 等待采集结束（可达数小时）。
- 主循环：不在监听时间段内每 30 秒检查；`_in_window(win_start, win_end)`
  支持**跨午夜**时间段（如 22:00~08:00），留空=全天。
- 采集不消耗体力。

### 4.4 抓包监听 (`capture_mode`)

独立任务：`connect(force_reload=True)` 强制刷新页面（保证 proto.send hook 生效），
然后循环 `_pwait` 把所有收/发帧打进日志（`[>>] CLIENT` / `[<<] RECV`），
供人工演示游戏操作后做协议分析。发帧日志会过滤引擎自身 order ≥ 9e6 的帧和噪声命令。

噪声命令集合（不打日志）：`NOISE_CMDS = {0, 10007, 10019, 10018, 12000, 12003, 12004, 10056}`。

---

## 5. Web 控制台 (`topwar_web.py`)

纯标准库 `ThreadingHTTPServer`，只监听 `127.0.0.1:8765`，启动自动开浏览器。
`Server.allow_reuse_address = False` 防止新旧两个后端实例同时存在。

### 5.1 多任务模型

```
STATE.tasks = { tid: {"type": 签到/打野/采集/抓包, "info": 描述, "engine": HuntEngine|None} }
```

- 每个任务一个独立线程（`start_thread`，daemon）+ 一个独立 `HuntEngine` 实例；
- 引擎 `log=lambda m: STATE.log(tag, m)` 把日志写进全局环形缓冲
  （`STATE.logs`，带自增序号，前端增量拉取）；
- 各任务帧通道按 sink id 隔离，互不干扰 —— **可同时跑多个采集任务用不同队列采同类田**；
- `_MARCH_CACHE`（11016 队列配置）为所有引擎共享的全局数据。

### 5.2 API

| 接口 | 说明 |
|---|---|
| `GET /` | 控制台单页 |
| `GET /api/status` | 任务列表 + 各引擎实时状态（体力/瓶子/次数/停止原因） |
| `GET /api/logs?since=N&frame=1` | 增量日志；`frame=1` 只取协议帧 |
| `GET /api/queues` | 返回 11016 队列缓存：`[{slot, heroes, armyCnt, formationV2}]`（slot=数组下标+1） |
| `POST /api/checkin` | 启动签到（互斥，不重复） |
| `POST /api/hunt` | `{monster, max_bottles, daily_limit, queue}` 启动打野，配置持久化 |
| `POST /api/gather` | `{res, queue, win_start, win_end}` 启动采集，配置持久化 |
| `POST /api/capture` | 启动抓包（**独占**：要求先停止所有任务，因为要强制刷新页面） |
| `POST /api/stop` | `{id}` 停止指定任务；不带 id 停全部。实现：`engine.stop_evt.set()` |

### 5.3 UI 要点

- 按钮：①签到 ②自动打野(展开表单) ③抓包监听 ④自动采集(展开表单) ⑤全部停止；
- 「运行中任务」卡片：每个任务一行，**独立停止按钮**（打野/采集为软停止）；
- 日志双 Tab：运行日志 / 协议帧（前端按 `[>>]`/`[<<]` 前缀分流）；
- 队列下拉框经 `loadQueues()` 从 `/api/queues` 生成，显示
  `队列N · 英雄a/b/c · 编队x`；无缓存时回退"预设队列 1~10"；
- 启动时显式 `load_config()` 恢复持久化的队列缓存（否则重启后 `/api/queues` 为空）。

---

## 6. 配置文件 (`topwar_hunt_config.json`)

由 `load_config()/save_config()` 管理，含默认值合并逻辑：

- `monster / max_bottles / daily_limit`：打野参数
- `queue_clubstar / queue_normal / queue_refugee / queue_gather`：各功能上次使用的队列号
- `gather_type / gather_win_start / gather_win_end`：采集参数
- `tpl / tpl_normal / tpl_refugee`：内置出征模板（可被用户配置覆盖）
- **`march_presets`**：11016 队列配置数组（自动持久化）
- `names.hero / names.troop`：英雄/兵种 ID → 名称的映射（展示用，可人工补充）

---

## 7. 已知坑与注意事项（开发必读）

1. **发帧 hook 时序**：实例级包装 `ws.send` 无效（游戏缓存引用），必须 proto 级
   hook 且在游戏代码运行前注入 → 需要发帧捕获时必须刷新游戏页面。
2. **Playwright sync API 单线程**：所有页面操作必须在创建 playwright 的线程；
   一切等待走 `_pwait()`（内部轮询收帧）。
3. **11016 只能被动接收**：不能主动拉取；等登录推送或提示用户在游戏内编辑保存队列。
   **队列号 = 数组顺序**，与 `icon` 字段无关。
4. **owner 坐标**：模板里的 `owner:{x:465,y:439}` 是硬编码的城市坐标，
   **迁城后需更新**（905 的 ownerx/ownery）。这是尚未自动化的点。
5. **多开**：多任务并行依赖独立 sink + 独立引擎实例；但抓包监听因要刷新页面而独占。
6. **软停止**：打野/采集的"停止"在当前轮（战斗/回家）结束后才退出，UI 有相应提示。
7. **粘包**：一个 WebSocket 载荷可能包含多帧，必须用 `extract_frames()` 循环解。
8. **60 岁诊断**：`request()` 超时会打印发送后收到的帧列表，便于排查协议问题。
9. 游戏客户端发的帧与引擎发的帧靠 order 段位区分（引擎 ≥ 9_000_000）。

---

## 8. 补充开发指南：如何新增一个自动化功能

以"采集"功能的标准开发流程为参考：

1. **抓包演示**：在控制台点「抓包监听」（会刷新页面注入发帧 hook），
   在游戏里完整手动操作一遍目标功能；
2. **分析协议**：从日志/`topwar_sniff_log.txt` 里找 `[>>] CLIENT` 帧确定：
   搜索/配置/执行三段命令的 cmd、关键字段、marchType/type 取值；
   对照 `[<<] RECV` 确定响应结构与后续状态推送（如 10019）；
3. **实现**：在 `HuntEngine` 里加常量（cmd 号、groupType 等）与
   `xxx_once()` + `hunt_xxx()` 主循环，复用
   `request()`（响应匹配）、`preset_tpl()`（队列模板）、
   `ensure_stamina()`（体力）、`wait_home()`（回家等待）等基础设施；
4. **接入控制台**：`topwar_web.py` 加 `run_xxx(tid, cfg)` 任务函数与
   `/api/xxx` 接口，复用 `STATE.add_task/del_task` 与停止机制；
   UI 加表单卡片；把新参数持久化进 config；
5. **验证**：注意观察失败重试路径（s≠0 的错误码、英雄不在家、体力不足、超负重）。

### 可扩展方向（尚未实现）

- 城市坐标自动获取（906 响应或某登录推送中应含本城坐标，替代硬编码 owner）；
- 英雄/兵种 ID → 名称映射自动化（目前 `cfg["names"]` 半人工维护）；
- 打野采集的任务调度/定时启动；
- 采集资源满载提前回家的精确处理；
- 更多野怪类型 / 活动玩法的协议逆向（同样走抓包流程）。
