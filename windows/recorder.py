# -*- coding: utf-8 -*-
"""
录屏助手 (Windows 版) — 菜单栏/托盘极简录屏 + 截图工具
功能对齐 macOS 版：
  - 全屏录制（ffmpeg gdigrab，可无限时长），暂停/继续（分段+无损合并）
  - 框选截图（调用 Windows 自带 ms-screenclip，自动进剪贴板）并存盘
  - 全局快捷键：Ctrl+R 录屏 / Ctrl+S 截图 / Ctrl+B 呼出控制条
  - 托盘图标 + 桌面悬浮控制条（红点+计时+开始/暂停/结束）
  - 可选录麦克风

依赖：Python 3.9+，ffmpeg 在 PATH 中；pip install pillow pystray keyboard
运行：python recorder.py
"""

import os
import sys
import time
import uuid
import shutil
import threading
import subprocess
from datetime import datetime

import tkinter as tk

from PIL import Image, ImageDraw, ImageGrab
import pystray
import keyboard

# ============== 可改配置 ==============
RECORD_DIR = os.path.join(os.path.expanduser("~"), "Videos", "录屏")
SHOT_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "截图")
FRAMERATE = "30"
VIDEO_BITRATE_CRF = "23"   # libx264 质量，数字越小越清晰、文件越大
HOTKEY_RECORD = "ctrl+r"
HOTKEY_SHOT = "ctrl+s"
HOTKEY_BAR = "ctrl+b"
# ====================================

CREATE_NO_WINDOW = 0x08000000  # 不弹 ffmpeg 控制台黑框


def ffmpeg_path():
    # 1) PyInstaller 打包进来的 ffmpeg.exe（_MEIPASS 或 exe 同目录）
    cands = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "ffmpeg.exe"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "ffmpeg.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    # 2) 系统 PATH
    p = shutil.which("ffmpeg")
    if p:
        return p
    # 3) 常见安装位置
    for c in [r"C:\ffmpeg\bin\ffmpeg.exe",
              r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.isfile(c):
            return c
    return None


def detect_mic(ff):
    """用 dshow 列出音频设备，返回第一个麦克风名字（找不到返回 None）。"""
    try:
        r = subprocess.run([ff, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW)
        out = (r.stderr or "")
        names = []
        in_audio = False
        for line in out.splitlines():
            low = line.lower()
            if "(audio)" in low:
                # 形如:  [dshow @ ...] "麦克风 (Realtek...)" (audio)
                if '"' in line:
                    names.append(line.split('"')[1])
        return names[0] if names else None
    except Exception:
        return None


class Recorder:
    def __init__(self):
        os.makedirs(RECORD_DIR, exist_ok=True)
        os.makedirs(SHOT_DIR, exist_ok=True)
        self.ff = ffmpeg_path()

        self.state = "idle"            # idle / recording / paused
        self.proc = None               # 当前片段 ffmpeg 进程
        self.cur_seg = None
        self.segments = []
        self.final_path = None
        self.recorded_before = 0.0
        self.seg_start = None
        self.lock = threading.Lock()

        self.record_mic = True
        self.bar_visible = True
        self.bar_collapsed = False
        self.last_file = None

        self._build_bar()
        self._build_tray()
        self._register_hotkeys()
        self._tick()

    # ---------- 录制 ----------
    def _seg_cmd(self, seg, use_mic, mic_name):
        cmd = [self.ff, "-y", "-f", "gdigrab", "-framerate", FRAMERATE,
               "-i", "desktop"]
        if use_mic and mic_name:
            cmd += ["-f", "dshow", "-i", "audio=" + mic_name]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-crf", VIDEO_BITRATE_CRF]
        if use_mic and mic_name:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        cmd.append(seg)
        return cmd

    def _launch_segment(self):
        seg = os.path.join(RECORD_DIR, f".seg_{uuid.uuid4().hex}.mp4")
        mic = detect_mic(self.ff) if self.record_mic else None
        cmd = self._seg_cmd(seg, self.record_mic, mic)
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self._alert(f"启动录制失败：{e}")
            return False
        self.cur_seg = seg
        self.seg_start = time.time()
        return True

    def _stop_proc(self, proc):
        """优雅停止 ffmpeg：往 stdin 写 q，让它写完文件尾。"""
        if not proc:
            return
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    def start(self):
        if self.state != "idle":
            return
        if not self.ff:
            self._alert("未找到 ffmpeg，请先安装并加入 PATH。")
            return
        self.final_path = os.path.join(
            RECORD_DIR, "录屏_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".mp4")
        self.segments = []
        self.recorded_before = 0.0
        if not self._launch_segment():
            return
        self.state = "recording"
        self._update_ui()

    def pause_resume(self):
        if self.state == "recording":
            self.recorded_before += time.time() - (self.seg_start or time.time())
            self.seg_start = None
            self.state = "paused"
            proc, seg = self.proc, self.cur_seg
            self.proc, self.cur_seg = None, None
            threading.Thread(target=self._finalize_seg, args=(proc, seg),
                             daemon=True).start()
            self._update_ui()
        elif self.state == "paused":
            if self._launch_segment():
                self.state = "recording"
                self._update_ui()

    def _finalize_seg(self, proc, seg):
        self._stop_proc(proc)
        with self.lock:
            if seg and os.path.isfile(seg):
                self.segments.append(seg)

    def stop(self):
        if self.state == "idle":
            return
        if self.state == "recording":
            self.recorded_before += time.time() - (self.seg_start or time.time())
        self.seg_start = None
        self.state = "idle"
        proc, seg, out = self.proc, self.cur_seg, self.final_path
        self.proc, self.cur_seg, self.final_path = None, None, None
        self._update_ui()
        threading.Thread(target=self._finish, args=(proc, seg, out),
                         daemon=True).start()

    def _finish(self, proc, seg, out):
        self._stop_proc(proc)
        with self.lock:
            if seg and os.path.isfile(seg):
                self.segments.append(seg)
            segs = self.segments
            self.segments = []
        if not out or not segs:
            return
        if len(segs) == 1:
            try:
                shutil.move(segs[0], out)
            except Exception:
                pass
        else:
            self._concat(segs, out)
            for s in segs:
                try:
                    os.remove(s)
                except Exception:
                    pass
        if os.path.isfile(out):
            self.last_file = out

    def _concat(self, segs, out):
        lst = os.path.join(RECORD_DIR, f".concat_{uuid.uuid4().hex}.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for s in segs:
                f.write("file '%s'\n" % s.replace("\\", "/"))
        try:
            subprocess.run([self.ff, "-y", "-f", "concat", "-safe", "0",
                            "-i", lst, "-c", "copy", out],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
        finally:
            try:
                os.remove(lst)
            except Exception:
                pass

    # ---------- 截图 ----------
    def take_screenshot(self):
        threading.Thread(target=self._shot_worker, daemon=True).start()

    def _shot_worker(self):
        # 记录截图前剪贴板基线
        try:
            base = ImageGrab.grabclipboard()
            base_bytes = base.tobytes() if isinstance(base, Image.Image) else None
        except Exception:
            base_bytes = None
        # 调用系统框选截图（完成后图片自动进剪贴板）
        try:
            subprocess.Popen(["explorer", "ms-screenclip:"])
        except Exception as e:
            self._alert(f"无法启动截图：{e}")
            return
        # 轮询剪贴板拿到新图，存盘（用户取消则超时退出，不覆盖原剪贴板）
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                img = ImageGrab.grabclipboard()
            except Exception:
                img = None
            if isinstance(img, Image.Image):
                if img.tobytes() != base_bytes:
                    path = os.path.join(
                        SHOT_DIR, "截图_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".png")
                    try:
                        img.save(path, "PNG")
                        self.last_file = path
                    except Exception:
                        pass
                    return

    # ---------- 控制条 (tkinter) ----------
    def _build_bar(self):
        self.root = tk.Tk()
        self.root.title("录屏助手")
        self.root.overrideredirect(True)          # 无边框
        self.root.attributes("-topmost", True)     # 始终置顶
        self.root.configure(bg="#1c1c1c")
        try:
            self.root.attributes("-alpha", 0.96)
        except Exception:
            pass

        self.dot = tk.Canvas(self.root, width=16, height=16, bg="#1c1c1c",
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(12, 4), pady=12)
        self.dot_id = self.dot.create_oval(3, 3, 13, 13, fill="#ff3b30", outline="")

        self.time_lbl = tk.Label(self.root, text="00:00", fg="white", bg="#1c1c1c",
                                 font=("Consolas", 14, "bold"))
        self.time_lbl.pack(side="left", padx=(0, 8))

        self.btn_start = tk.Button(self.root, text="开始", width=5, command=self.start)
        self.btn_pause = tk.Button(self.root, text="暂停", width=5, command=self.pause_resume)
        self.btn_stop = tk.Button(self.root, text="结束", width=5, command=self.stop)
        self.btn_collapse = tk.Button(self.root, text="▾", width=2, command=self.toggle_collapse)
        self.btn_close = tk.Button(self.root, text="✕", width=2, command=self.hide_bar)
        for b in (self.btn_start, self.btn_pause, self.btn_stop,
                  self.btn_collapse, self.btn_close):
            b.pack(side="left", padx=2, pady=8)

        # 拖动移动窗口
        for w in (self.root, self.dot, self.time_lbl):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self.root.update_idletasks()
        self._position_bar()
        self._apply_collapse()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_bar)

    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        x = self.root.winfo_x() + e.x - self._dx
        y = self.root.winfo_y() + e.y - self._dy
        self.root.geometry(f"+{x}+{y}")

    def _position_bar(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        w = self.root.winfo_width()
        self.root.geometry(f"+{sw - w - 24}+{16}")

    def _apply_collapse(self):
        show = not self.bar_collapsed
        for b in (self.btn_start, self.btn_pause, self.btn_stop, self.btn_close):
            if show:
                b.pack(side="left", padx=2, pady=8)
            else:
                b.pack_forget()
        self.btn_collapse.configure(text="▸" if self.bar_collapsed else "▾")
        self._position_bar()

    def toggle_collapse(self):
        self.bar_collapsed = not self.bar_collapsed
        self._apply_collapse()

    def hide_bar(self):
        self.bar_visible = False
        self.root.withdraw()

    def show_bar(self):
        self.bar_visible = True
        self.bar_collapsed = False
        self._apply_collapse()
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._position_bar()

    def toggle_bar(self):
        if self.bar_visible:
            self.hide_bar()
        else:
            self.show_bar()

    # ---------- 计时/界面刷新 ----------
    def _elapsed_secs(self):
        t = self.recorded_before
        if self.state == "recording" and self.seg_start:
            t += time.time() - self.seg_start
        return int(t)

    def _fmt(self, s):
        return f"{s // 60:02d}:{s % 60:02d}"

    def _tick(self):
        self._update_ui()
        self.root.after(500, self._tick)

    def _update_ui(self):
        color = {"recording": "#ff3b30", "paused": "#ff9500", "idle": "#8e8e93"}[self.state]
        self.dot.itemconfigure(self.dot_id, fill=color)
        self.time_lbl.configure(text=self._fmt(self._elapsed_secs()))
        self.btn_start.configure(state=("normal" if self.state == "idle" else "disabled"))
        self.btn_pause.configure(state=("disabled" if self.state == "idle" else "normal"),
                                 text=("继续" if self.state == "paused" else "暂停"))
        self.btn_stop.configure(state=("disabled" if self.state == "idle" else "normal"))

    def _alert(self, msg):
        try:
            from tkinter import messagebox
            messagebox.showwarning("录屏助手", msg)
        except Exception:
            print(msg)

    # ---------- 托盘 ----------
    def _tray_icon_img(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((12, 12, 52, 52), fill=(255, 59, 48, 255))
        return img

    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("显示/隐藏控制条 (Ctrl+B)", lambda: self._ui(self.toggle_bar)),
            pystray.MenuItem("开始/结束录屏 (Ctrl+R)",
                             lambda: self._ui(lambda: self.stop() if self.state != "idle" else self.start())),
            pystray.MenuItem("截图 (Ctrl+S)", lambda: self.take_screenshot()),
            pystray.MenuItem("录制麦克风", self._toggle_mic,
                             checked=lambda i: self.record_mic),
            pystray.MenuItem("打开录屏文件夹", lambda: os.startfile(RECORD_DIR)),
            pystray.MenuItem("打开截图文件夹", lambda: os.startfile(SHOT_DIR)),
            pystray.MenuItem("退出", self._quit),
        )
        self.tray = pystray.Icon("录屏助手", self._tray_icon_img(), "录屏助手", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _toggle_mic(self, icon, item):
        self.record_mic = not self.record_mic

    def _quit(self, icon=None, item=None):
        if self.state != "idle":
            # 同步收尾
            if self.state == "recording":
                self.recorded_before += time.time() - (self.seg_start or time.time())
            self._finish(self.proc, self.cur_seg, self.final_path)
        try:
            self.tray.stop()
        except Exception:
            pass
        self._ui(self.root.destroy)

    # ---------- 快捷键 ----------
    def _register_hotkeys(self):
        try:
            keyboard.add_hotkey(HOTKEY_RECORD,
                                lambda: self._ui(lambda: self.stop() if self.state != "idle" else self.start()),
                                suppress=True)
            keyboard.add_hotkey(HOTKEY_SHOT, lambda: self.take_screenshot(), suppress=True)
            keyboard.add_hotkey(HOTKEY_BAR, lambda: self._ui(self.toggle_bar), suppress=True)
        except Exception as e:
            print("注册快捷键失败（可能需要管理员权限）：", e)

    def _ui(self, fn):
        """把回调切回 tkinter 主线程执行。"""
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Recorder().run()
