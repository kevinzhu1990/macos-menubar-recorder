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
import ctypes
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
SYSTEM_AUDIO_KEYWORDS = (
    "stereo mix", "立体声混音", "what u hear", "wave out mix",
    "混音", "loopback", "virtual-audio-capturer", "cable output"
)
MIC_AUDIO_KEYWORDS = ("microphone", "mic", "麦克风", "麦克風")
# ====================================

CREATE_NO_WINDOW = 0x08000000  # 不弹 ffmpeg 控制台黑框
MIN_REGION_SIZE = 20


def make_dpi_aware():
    """Keep Tk coordinates aligned with ffmpeg gdigrab physical pixels."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen_bounds():
    if sys.platform == "win32":
        user32 = ctypes.windll.user32
        return (
            user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
            user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
            user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
            user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
        )
    return (0, 0, 0, 0)


make_dpi_aware()


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


def detect_audio_devices(ff):
    """用 dshow 列出音频设备，返回设备名列表。"""
    try:
        r = subprocess.run([ff, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW)
        out = (r.stderr or "")
        names = []
        for line in out.splitlines():
            low = line.lower()
            if "(audio)" in low:
                # 形如:  [dshow @ ...] "麦克风 (Realtek...)" (audio)
                if '"' in line:
                    names.append(line.split('"')[1])
        return names
    except Exception:
        return []


def choose_record_audio_device(ff):
    """优先选择系统混音/虚拟声卡，找不到再选择麦克风。"""
    devices = detect_audio_devices(ff)
    if not devices:
        return None, []

    for name in devices:
        low = name.lower()
        if any(k in low for k in SYSTEM_AUDIO_KEYWORDS):
            return name, devices

    for name in devices:
        low = name.lower()
        if any(k in low for k in MIC_AUDIO_KEYWORDS):
            return name, devices

    return devices[0], devices


class RegionSelector:
    def __init__(self, parent):
        self.parent = parent
        self.result = None
        self.start_x = None
        self.start_y = None
        self.end_x = None
        self.end_y = None
        self.rect_id = None
        self.size_id = None
        self.action_window_id = None
        self.action_frame = None
        self.left, self.top, self.screen_w, self.screen_h = virtual_screen_bounds()
        if self.screen_w <= 0 or self.screen_h <= 0:
            self.left = 0
            self.top = 0
            self.screen_w = parent.winfo_screenwidth()
            self.screen_h = parent.winfo_screenheight()

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.38)
        except Exception:
            pass
        self.win.configure(bg="black")
        self.win.geometry(self._geometry())

        self.canvas = tk.Canvas(
            self.win, bg="black", cursor="crosshair", highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.screen_w // 2, 34,
            text="拖拽选择录屏区域，松开后确认；Esc 取消",
            fill="white", font=("Microsoft YaHei UI", 16, "bold")
        )
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", lambda _event: self._cancel())
        self.win.bind("<Return>", lambda _event: self._confirm())

    def _signed(self, value):
        return f"+{value}" if value >= 0 else str(value)

    def _geometry(self):
        return (
            f"{self.screen_w}x{self.screen_h}"
            f"{self._signed(self.left)}{self._signed(self.top)}"
        )

    def select(self):
        self.win.lift()
        self.win.focus_force()
        try:
            self.win.grab_set()
        except Exception:
            pass
        self.parent.wait_window(self.win)
        return self.result

    def _on_press(self, event):
        self.start_x = self._clamp(event.x, 0, self.screen_w)
        self.start_y = self._clamp(event.y, 0, self.screen_h)
        self.end_x = self.start_x
        self.end_y = self.start_y
        self._clear_selection()
        self._draw_selection()

    def _on_drag(self, event):
        if self.start_x is None or self.start_y is None:
            return
        self.end_x = self._clamp(event.x, 0, self.screen_w)
        self.end_y = self._clamp(event.y, 0, self.screen_h)
        self._draw_selection()

    def _on_release(self, event):
        if self.start_x is None or self.start_y is None:
            return
        self.end_x = self._clamp(event.x, 0, self.screen_w)
        self.end_y = self._clamp(event.y, 0, self.screen_h)
        x, y, w, h = self._relative_region()
        if w < MIN_REGION_SIZE or h < MIN_REGION_SIZE:
            self._clear_selection()
            self.canvas.create_text(
                self.screen_w // 2, 72,
                text="区域太小，请重新拖拽选择",
                fill="#ffdd57", font=("Microsoft YaHei UI", 12, "bold"),
                tags=("hint",)
            )
            return
        self.canvas.delete("hint")
        self._show_actions(x, y, w, h)

    def _draw_selection(self):
        x, y, w, h = self._relative_region()
        x2 = x + w
        y2 = y + h
        if self.rect_id is None:
            self.rect_id = self.canvas.create_rectangle(
                x, y, x2, y2, outline="#00d1ff", width=3
            )
        else:
            self.canvas.coords(self.rect_id, x, y, x2, y2)
        label = f"{w} x {h}"
        label_x = max(12, min(x + 8, self.screen_w - 120))
        label_y = y - 28 if y > 44 else y + 8
        if self.size_id is None:
            self.size_id = self.canvas.create_text(
                label_x, label_y, text=label, anchor="nw",
                fill="white", font=("Consolas", 12, "bold")
            )
        else:
            self.canvas.coords(self.size_id, label_x, label_y)
            self.canvas.itemconfigure(self.size_id, text=label)

    def _show_actions(self, x, y, w, h):
        self._hide_actions()
        frame = tk.Frame(
            self.canvas, bg="#1c1c1c", bd=1,
            highlightthickness=1, highlightbackground="#00d1ff"
        )
        tk.Button(frame, text="开始录屏", width=9, command=self._confirm).pack(
            side="left", padx=(8, 4), pady=8
        )
        tk.Button(frame, text="重新选择", width=9, command=self._reset).pack(
            side="left", padx=4, pady=8
        )
        tk.Button(frame, text="取消", width=7, command=self._cancel).pack(
            side="left", padx=(4, 8), pady=8
        )
        frame.update_idletasks()
        px = x + w + 12
        py = y
        if px + frame.winfo_reqwidth() > self.screen_w - 12:
            px = x
            py = y + h + 12
        if py + frame.winfo_reqheight() > self.screen_h - 12:
            py = max(12, y - frame.winfo_reqheight() - 12)
        self.action_frame = frame
        self.action_window_id = self.canvas.create_window(px, py, anchor="nw", window=frame)

    def _relative_region(self):
        x1 = self._clamp(min(self.start_x, self.end_x), 0, self.screen_w)
        y1 = self._clamp(min(self.start_y, self.end_y), 0, self.screen_h)
        x2 = self._clamp(max(self.start_x, self.end_x), 0, self.screen_w)
        y2 = self._clamp(max(self.start_y, self.end_y), 0, self.screen_h)
        return x1, y1, x2 - x1, y2 - y1

    def _absolute_region(self):
        x, y, w, h = self._relative_region()
        # libx264 with yuv420p needs even dimensions.
        w -= w % 2
        h -= h % 2
        if w < MIN_REGION_SIZE or h < MIN_REGION_SIZE:
            return None
        return int(self.left + x), int(self.top + y), int(w), int(h)

    def _confirm(self):
        region = self._absolute_region()
        if not region:
            return
        self.result = region
        self._close()

    def _reset(self):
        self.start_x = None
        self.start_y = None
        self.end_x = None
        self.end_y = None
        self._clear_selection()

    def _cancel(self):
        self.result = None
        self._close()

    def _clear_selection(self):
        self.canvas.delete("hint")
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        if self.size_id is not None:
            self.canvas.delete(self.size_id)
            self.size_id = None
        self._hide_actions()

    def _hide_actions(self):
        if self.action_window_id is not None:
            self.canvas.delete(self.action_window_id)
            self.action_window_id = None
        if self.action_frame is not None:
            self.action_frame.destroy()
            self.action_frame = None

    def _close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()

    def _clamp(self, value, low, high):
        return max(low, min(int(value), high))


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
        self.record_region = None      # (x, y, width, height)
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
        cmd = [self.ff, "-y", "-f", "gdigrab", "-framerate", FRAMERATE]
        if self.record_region:
            x, y, w, h = self.record_region
            cmd += ["-offset_x", str(x), "-offset_y", str(y),
                    "-video_size", f"{w}x{h}"]
        cmd += ["-i", "desktop"]
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
        audio_name, _audio_devices = (choose_record_audio_device(self.ff)
                                      if self.record_mic else (None, []))
        if self.record_mic and not audio_name:
            self._alert("未检测到可录制的声音设备，本次将只录画面。\n\n如果要录电脑播放声音，请在 Windows 声音设置里启用“立体声混音”，或安装 VB-CABLE 这类虚拟声卡后再录。")
        cmd = self._seg_cmd(seg, self.record_mic, audio_name)
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
        region = RegionSelector(self.root).select()
        if not region:
            return
        self.record_region = region
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
        self.record_region = None
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

        self.btn_start = tk.Button(self.root, text="选区录屏", width=7, command=self.start)
        self.btn_pause = tk.Button(self.root, text="暂停", width=5, command=self.pause_resume)
        self.btn_stop = tk.Button(self.root, text="结束", width=5, command=self.stop)
        self.btn_shortcuts = tk.Button(
            self.root, text="快捷键", width=6, command=self.show_shortcuts
        )
        self.btn_collapse = tk.Button(self.root, text="▾", width=2, command=self.toggle_collapse)
        self.btn_close = tk.Button(self.root, text="✕", width=2, command=self.hide_bar)
        for b in (self.btn_start, self.btn_pause, self.btn_stop,
                  self.btn_shortcuts, self.btn_collapse, self.btn_close):
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
        for b in (self.btn_start, self.btn_pause, self.btn_stop,
                  self.btn_shortcuts, self.btn_close):
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

    def show_shortcuts(self):
        """显示快捷键和基础操作说明；重复点击时复用现有窗口。"""
        win = getattr(self, "shortcut_win", None)
        if win and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.shortcut_win = win
        win.title("快捷键 · 录屏助手")
        win.configure(bg="#1c1c1c")
        win.resizable(False, False)
        win.attributes("-topmost", True)

        def close():
            self.shortcut_win = None
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: close())

        tk.Label(
            win, text="录屏助手快捷键", fg="white", bg="#1c1c1c",
            font=("Microsoft YaHei UI", 16, "bold")
        ).pack(padx=28, pady=(22, 4))

        tk.Label(
            win, text="记住这 3 个组合键，就能完成日常操作",
            fg="#a8a8a8", bg="#1c1c1c",
            font=("Microsoft YaHei UI", 9)
        ).pack(padx=28, pady=(0, 16))

        shortcuts = [
            ("Ctrl + R", "选区后开始 / 结束录屏"),
            ("Ctrl + S", "框选截图并复制"),
            ("Ctrl + B", "显示 / 隐藏控制条"),
        ]
        for key, action in shortcuts:
            row = tk.Frame(win, bg="#292929")
            row.pack(fill="x", padx=22, pady=4)

            tk.Label(
                row, text=key, width=11, anchor="center",
                fg="white", bg="#3a3a3a",
                font=("Consolas", 11, "bold")
            ).pack(side="left", padx=8, pady=9)

            tk.Label(
                row, text=action, anchor="w",
                fg="white", bg="#292929",
                font=("Microsoft YaHei UI", 10)
            ).pack(side="left", padx=(8, 16), pady=9)

        tk.Label(
            win,
            text="也可以直接使用悬浮控制条或右下角托盘菜单。\n"
                 "快捷键无响应时，请尝试以管理员身份运行。",
            justify="left", fg="#b8b8b8", bg="#1c1c1c",
            font=("Microsoft YaHei UI", 9)
        ).pack(fill="x", padx=28, pady=(14, 12))

        tk.Button(
            win, text="知道了", width=12, command=close,
            font=("Microsoft YaHei UI", 9)
        ).pack(pady=(0, 20))

        win.update_idletasks()
        x = max(0, (win.winfo_screenwidth() - win.winfo_width()) // 2)
        y = max(0, (win.winfo_screenheight() - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.focus_force()

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
            pystray.MenuItem("快捷键 / 使用说明", lambda: self._ui(self.show_shortcuts)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("显示/隐藏控制条 (Ctrl+B)", lambda: self._ui(self.toggle_bar)),
            pystray.MenuItem("选区后开始/结束录屏 (Ctrl+R)",
                             lambda: self._ui(lambda: self.stop() if self.state != "idle" else self.start())),
            pystray.MenuItem("截图 (Ctrl+S)", lambda: self.take_screenshot()),
            pystray.MenuItem("录制声音（系统优先）", self._toggle_mic,
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
