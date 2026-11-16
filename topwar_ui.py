# -*- coding: utf-8 -*-
"""
口袋奇兵助手 - 图形界面
  ① 开始签到 : 运行一键领取奖励 (topwar_checkin)
  ② 自动打野 : 协议自动化打怪, 详情设置中配置野怪类型与体力瓶使用上限
"""

import contextlib
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import topwar_checkin
from topwar_hunt import HuntEngine, load_config, save_config

MONSTERS = [
    ("clubstar", "俱星 5级 (每日限10次)"),
    ("generic",  "新野怪 (无限制 · 协议待抓包)"),
]


class LogWriter:
    """把 print 输出转发到 UI 日志队列"""
    def __init__(self, q, tag):
        self.q = q
        self.tag = tag

    def write(self, s):
        for line in s.rstrip("\n").split("\n"):
            if line.strip():
                self.q.put((self.tag, line))

    def flush(self):
        pass


class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("口袋奇兵助手")
        self.geometry("760x600")
        self.minsize(680, 480)
        self.q = queue.Queue()
        self.worker = None       # (name, thread)
        self.engine = None       # 打野引擎
        self._build()

    # ---------------- UI 构建 ----------------
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass

        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(12, 6))

        self.btn_checkin = ttk.Button(
            top, text="① 开始签到", width=20, command=self.on_checkin)
        self.btn_checkin.pack(side="left")

        self.btn_hunt = ttk.Button(
            top, text="② 自动打野…", width=20, command=self.on_hunt_dialog)
        self.btn_hunt.pack(side="left", padx=10)

        self.lbl_busy = ttk.Label(top, text="")
        self.lbl_busy.pack(side="left", padx=6)

        # 状态栏
        st = ttk.LabelFrame(self, text="打野状态")
        st.pack(fill="x", padx=12)
        self.lbl_state = self._stat(st, 0, "空闲")
        self.lbl_stamina = self._stat(st, 1, "-")
        self.lbl_bottles = self._stat(st, 2, "-")
        self.lbl_kills = self._stat(st, 3, "-")
        self.btn_stop = ttk.Button(st, text="停止打野",
                                   command=self.on_stop_hunt,
                                   state="disabled")
        self.btn_stop.grid(row=0, column=4, rowspan=2, padx=16)

        # 日志
        lg = ttk.LabelFrame(self, text="运行日志")
        lg.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        self.txt = scrolledtext.ScrolledText(
            lg, height=16, state="disabled", font=("Microsoft YaHei UI", 9))
        self.txt.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt.tag_config("checkin", foreground="#0a6b0a")
        self.txt.tag_config("hunt", foreground="#0a4a8b")
        self.txt.tag_config("sys", foreground="#a04000")

        self.after(200, self._poll_log)
        self.after(1000, self._poll_status)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _stat(self, parent, col, val):
        lbls = ("运行", "体力", "已用体力瓶", "本次召唤")
        f = ttk.Frame(parent)
        f.grid(row=0, column=col, padx=14, pady=6)
        ttk.Label(f, text=lbls[col] + ":", font=("", 9, "bold")).pack(
            anchor="w")
        v = ttk.Label(f, text=val, font=("", 10))
        v.pack(anchor="w")
        return v

    # ---------------- 日志 ----------------
    def log(self, tag, line):
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _poll_log(self):
        try:
            while True:
                tag, line = self.q.get_nowait()
                if line == "__HUNT_DONE__":
                    self._task_done("打野")
                    continue
                if line == "__CHECKIN_DONE__":
                    self._task_done("签到")
                    continue
                self.log(tag, line)
        except queue.Empty:
            pass
        self.after(200, self._poll_log)

    def _poll_status(self):
        e = self.engine
        if e is not None:
            self.lbl_state.configure(
                text="运行中" if e.running else "已停止")
            self.lbl_stamina.configure(
                text=f"{e.stamina}/{e.stamina_max}"
                if e.stamina is not None else "-")
            self.lbl_bottles.configure(
                text=f"{e.bottles_used}"
                + (f" (背包剩 {e.bottles_left})" if e.bottles_left is not None
                   else ""))
            self.lbl_kills.configure(text=str(e.hunts))
        self.after(1000, self._poll_status)

    # ---------------- 任务管理 ----------------
    def _busy(self):
        return self.worker is not None and self.worker[1].is_alive()

    def _set_busy(self, name):
        self.lbl_busy.configure(text=f"正在{name}…")
        self.btn_checkin.configure(state="disabled")
        self.btn_hunt.configure(state="disabled")
        if name == "打野":
            self.btn_stop.configure(state="normal")

    def _task_done(self, name):
        if self.worker and self.worker[0] == name:
            self.worker = None
        if name == "打野":
            self.engine = None
        self.lbl_busy.configure(text="")
        self.btn_checkin.configure(state="normal")
        self.btn_hunt.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.log("sys", f"—— {name}任务结束 ——")

    def _check_busy(self, name):
        if self._busy():
            messagebox.showwarning(
                "忙碌", f"当前正在执行「{self.worker[0]}」任务,"
                        f"请等待完成或先停止。")
            return True
        return False

    # ---------------- ① 签到 ----------------
    def on_checkin(self):
        if self._check_busy("签到"):
            return
        self._set_busy("签到")
        self.log("sys", "—— 签到任务开始 ——")
        q = self.q

        def work():
            buf = LogWriter(q, "checkin")
            try:
                with contextlib.redirect_stdout(buf):
                    topwar_checkin.main()
            except Exception as e:
                q.put(("checkin", f"[错误] {e}"))
            finally:
                q.put(("sys", "__CHECKIN_DONE__"))

        t = threading.Thread(target=work, daemon=True)
        self.worker = ("签到", t)
        t.start()

    # ---------------- ② 自动打野 ----------------
    def on_hunt_dialog(self):
        if self._check_busy("打野"):
            return
        cfg = load_config()
        win = tk.Toplevel(self)
        win.title("自动打野 · 详情设置")
        win.geometry("420x280")
        win.transient(self)
        win.grab_set()

        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="野怪类型:").grid(
            row=0, column=0, sticky="w", pady=4)
        mon_var = tk.StringVar(
            value=dict(MONSTERS).get(cfg.get("monster", "clubstar"),
                                     MONSTERS[0][1]))
        cb = ttk.Combobox(frm, textvariable=mon_var, state="readonly",
                          width=32, values=[m[1] for m in MONSTERS])
        cb.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="体力瓶使用上限:").grid(
            row=1, column=0, sticky="w", pady=4)
        bottle_var = tk.IntVar(value=int(cfg.get("max_bottles", 5)))
        ttk.Spinbox(frm, from_=0, to=999, textvariable=bottle_var,
                    width=8).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="俱星每日召唤上限:").grid(
            row=2, column=0, sticky="w", pady=4)
        limit_var = tk.IntVar(value=int(cfg.get("daily_limit", 10)))
        ttk.Spinbox(frm, from_=1, to=10, textvariable=limit_var,
                    width=8).grid(row=2, column=1, sticky="w", pady=4)

        tip = ("说明:\n"
               "· 体力不足时自动开启10点体力包, 使用个数达到上限后自动停止\n"
               "· 每次集结攻打消耗10点体力; 体力上限95, 自然恢复1点/6分钟\n"
               "· 打完一轮后自动等待队列回家再召唤下一只")
        ttk.Label(frm, text=tip, foreground="#555", justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 4))

        def start():
            try:
                nb = int(bottle_var.get())
                nl = int(limit_var.get())
            except Exception:
                messagebox.showerror("错误", "请输入有效数字", parent=win)
                return
            if nb < 0:
                messagebox.showerror("错误", "体力瓶上限不能为负", parent=win)
                return
            key = next(k for k, v in MONSTERS if v == mon_var.get())
            cfg.update({"monster": key, "max_bottles": nb, "daily_limit": nl})
            save_config(cfg)
            win.destroy()
            self._start_hunt(cfg)

        ttk.Button(frm, text="启动打野", command=start).grid(
            row=4, column=0, columnspan=2, pady=(12, 0), ipadx=20)

    def _start_hunt(self, cfg):
        self._set_busy("打野")
        self.log("sys", f"—— 打野任务开始 (类型: {cfg['monster']}, "
                        f"体力瓶上限: {cfg['max_bottles']}) ——")
        engine = HuntEngine(log=lambda m: self.q.put(("hunt", m)))
        self.engine = engine
        q = self.q

        def work():
            try:
                engine.connect()
                if cfg["monster"] == "clubstar":
                    engine.hunt_clubstar(cfg["max_bottles"],
                                         cfg.get("daily_limit", 10))
                else:
                    engine.log("[!] 该野怪类型的协议尚未实现:\n"
                               "    请先在游戏中演示一次完整攻打流程,"
                               "我再抓包分析并实现。")
            except Exception as e:
                engine.log(f"[错误] {e}")
            finally:
                engine.close()
                q.put(("sys", "__HUNT_DONE__"))

        t = threading.Thread(target=work, daemon=True)
        self.worker = ("打野", t)
        t.start()

    def on_stop_hunt(self):
        if self.engine is not None:
            self.engine.stop_evt.set()
            self.log("sys", "[..] 已请求停止, 等待当前步骤结束…")

    def on_close(self):
        if self.engine is not None:
            self.engine.stop_evt.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
