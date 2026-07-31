# -*- coding: utf-8 -*-
"""
录屏助手 (Windows 版) — 菜单栏/托盘极简录屏 + 截图工具
功能对齐 macOS 版：
  - 全屏录制（ffmpeg gdigrab，可无限时长），暂停/继续（分段+无损合并）
  - 框选截图（调用 Windows 自带 ms-screenclip，自动进剪贴板）并存盘
  - 全局快捷键：Ctrl+R 录屏 / Ctrl+S 截图 / Ctrl+B 呼出控制条
  - 托盘图标 + 桌面悬浮控制条（红点+计时+开始/暂停/结束）
  - 可选录电脑内部声音（需立体声混音或虚拟声卡）

依赖：Python 3.9+，ffmpeg 在 PATH 中；pip install pillow pystray keyboard
运行：python recorder.py
"""

import os
import sys
import time
import uuid
import json
import shutil
import threading
import subprocess
import ctypes
import wave
import urllib.request
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
HOTKEY_PAUSE = "ctrl+p"
HOTKEY_SHOT = "ctrl+s"
HOTKEY_BAR = "ctrl+b"
CONFIG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ScreenRecorder")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
SYSTEM_AUDIO_KEYWORDS = (
    "stereo mix", "立体声混音", "what u hear", "wave out mix",
    "混音", "loopback", "virtual-audio-capturer", "cable output"
)
# ====================================

CREATE_NO_WINDOW = 0x08000000  # 不弹 ffmpeg 控制台黑框
MIN_REGION_SIZE = 20
APP_VERSION = "1.2.2"
UPDATE_VERSION_URL = (
    "https://github.com/kevinzhu1990/macos-menubar-recorder/"
    "releases/download/win-latest/version.json"
)
UPDATE_DOWNLOAD_URL = (
    "https://github.com/kevinzhu1990/macos-menubar-recorder/"
    "releases/download/win-latest/ScreenRecorderSetup.exe"
)


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


def choose_system_audio_device(ff):
    """只选择能录电脑内部声音的混音/虚拟声卡设备。"""
    devices = detect_audio_devices(ff)
    if not devices:
        return None, []

    for name in devices:
        low = name.lower()
        if any(k in low for k in SYSTEM_AUDIO_KEYWORDS):
            return name, devices

    return None, devices


class LoopbackAudioRecorder:
    def __init__(self, path):
        self.path = path
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.started = threading.Event()
        self.ready = False

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.started.wait(timeout=3)
        return self.ready

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        return self.path if os.path.isfile(self.path) and os.path.getsize(self.path) > 44 else None

    def _run(self):
        pa = None
        stream = None
        wf = None
        try:
            import pyaudiowpatch as pyaudio

            pa = pyaudio.PyAudio()
            device = pa.get_default_wasapi_loopback()
            channels = int(device.get("maxInputChannels") or 2)
            rate = int(device.get("defaultSampleRate") or 48000)
            fmt = pyaudio.paInt16
            wf = wave.open(self.path, "wb")
            wf.setnchannels(channels)
            wf.setsampwidth(pa.get_sample_size(fmt))
            wf.setframerate(rate)
            stream = pa.open(
                format=fmt,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=1024,
            )
            self.ready = True
            self.started.set()
            while not self.stop_event.is_set():
                data = stream.read(1024, exception_on_overflow=False)
                wf.writeframes(data)
        except Exception as e:
            self.error = e
            self.started.set()
        finally:
            try:
                if stream:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if wf:
                    wf.close()
            except Exception:
                pass
            try:
                if pa:
                    pa.terminate()
            except Exception:
                pass


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
            self.win.attributes("-alpha", 0.52)
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
            text="拖拽选择录屏区域，松开后点击“开始录屏”；Esc 取消",
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
            self.canvas, bg="#f1f9ff", bd=0,
            highlightthickness=2, highlightbackground="#28c79a"
        )
        tk.Label(
            frame, text="区域已选好", fg="#142033", bg="#f1f9ff",
            font=("Microsoft YaHei UI", 9, "bold")
        ).pack(side="left", padx=(10, 6))
        tk.Button(
            frame, text="开始录屏", width=10, command=self._confirm,
            fg="white", bg="#28c79a", activeforeground="white",
            activebackground="#20b78d", relief="flat", bd=0,
            cursor="hand2", font=("Microsoft YaHei UI", 10, "bold")
        ).pack(
            side="left", padx=4, pady=9, ipady=3
        )
        tk.Button(
            frame, text="重新选择", width=8, command=self._reset,
            fg="#4353c7", bg="#e5edff", activebackground="#d8e2ff",
            relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold")
        ).pack(
            side="left", padx=4, pady=9, ipady=3
        )
        tk.Button(
            frame, text="取消", width=6, command=self._cancel,
            fg="#d83b4d", bg="#ffe5e8", activebackground="#ffd6db",
            relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold")
        ).pack(
            side="left", padx=(4, 10), pady=9, ipady=3
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


class RoundedButton(tk.Canvas):
    """Small flat button drawn on a canvas so the floating bar can use rounded controls."""

    def __init__(self, parent, text, command, width, bg, fg="#142033",
                 hover_bg=None, disabled_bg="#e7eef4", disabled_fg="#9aa8b5"):
        super().__init__(parent, width=width, height=32, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.label = text
        self.normal_bg = bg
        self.hover_bg = hover_bg or bg
        self.fg = fg
        self.disabled_bg = disabled_bg
        self.disabled_fg = disabled_fg
        self.button_state = "normal"
        self.bind("<Enter>", lambda _e: self._draw(True))
        self.bind("<Leave>", lambda _e: self._draw(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw(False)

    def _round_rect(self, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2,
            x1 + radius, y2, x1, y2, x1, y2 - radius,
            x1, y1 + radius, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def _draw(self, hovering=False):
        self.delete("all")
        disabled = self.button_state == "disabled"
        fill = self.disabled_bg if disabled else (self.hover_bg if hovering else self.normal_bg)
        text_color = self.disabled_fg if disabled else self.fg
        self._round_rect(1, 1, int(self.cget("width")) - 1, 31, 10,
                         fill=fill, outline="")
        self.create_text(int(self.cget("width")) // 2, 16, text=self.label,
                         fill=text_color, font=("Microsoft YaHei UI", 9, "bold"))
        super().configure(cursor="arrow" if disabled else "hand2")

    def _click(self, _event):
        if self.button_state == "normal" and self.command:
            self.command()

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if "text" in kwargs:
            self.label = kwargs.pop("text")
        if "state" in kwargs:
            self.button_state = kwargs.pop("state")
        if kwargs:
            super().configure(**kwargs)
        self._draw(False)

    config = configure


class Recorder:
    def __init__(self):
        os.makedirs(RECORD_DIR, exist_ok=True)
        os.makedirs(SHOT_DIR, exist_ok=True)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self._cleanup_stale_recording_files()
        self.ff = ffmpeg_path()

        self.state = "idle"            # idle / recording / paused
        self.proc = None               # 当前片段 ffmpeg 进程
        self.cur_seg = None
        self.cur_audio = None
        self.segments = []
        self.final_path = None
        self.record_region = None      # (x, y, width, height)
        self.recorded_before = 0.0
        self.seg_start = None
        self.lock = threading.Lock()

        self.framerate = "30"
        self.video_crf = "23"
        self.draw_mouse = True
        self.countdown_seconds = 3
        self.auto_stop_minutes = 0
        self.record_system_audio = True
        self.bar_width = 390
        self.bar_height = 96
        self._load_settings()
        self.bar_visible = True
        self.bar_collapsed = False
        self.last_file = None

        self._build_bar()
        self._build_tray()
        self._register_hotkeys()
        self._tick()

    def _load_settings(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        self.framerate = str(data.get("framerate", self.framerate))
        self.video_crf = str(data.get("video_crf", self.video_crf))
        self.draw_mouse = bool(data.get("draw_mouse", self.draw_mouse))
        self.countdown_seconds = int(data.get("countdown_seconds", self.countdown_seconds))
        self.auto_stop_minutes = int(data.get("auto_stop_minutes", self.auto_stop_minutes))
        self.record_system_audio = bool(
            data.get("record_system_audio", self.record_system_audio)
        )
        self.bar_width = max(350, int(data.get("bar_width", self.bar_width)))
        self.bar_height = max(90, int(data.get("bar_height", self.bar_height)))

    def _save_settings(self):
        data = {
            "framerate": self.framerate,
            "video_crf": self.video_crf,
            "draw_mouse": self.draw_mouse,
            "countdown_seconds": self.countdown_seconds,
            "auto_stop_minutes": self.auto_stop_minutes,
            "record_system_audio": self.record_system_audio,
            "bar_width": self.bar_width,
            "bar_height": self.bar_height,
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 录制 ----------
    def _seg_cmd(self, seg):
        cmd = [
            self.ff, "-y", "-f", "gdigrab",
            "-draw_mouse", "1" if self.draw_mouse else "0",
            "-framerate", self.framerate,
        ]
        if self.record_region:
            x, y, w, h = self.record_region
            cmd += ["-offset_x", str(x), "-offset_y", str(y),
                    "-video_size", f"{w}x{h}"]
        cmd += ["-i", "desktop"]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-crf", self.video_crf]
        cmd.append(seg)
        return cmd

    def _launch_segment(self):
        seg = os.path.join(RECORD_DIR, f".video_{uuid.uuid4().hex}.mp4")
        audio = None
        if self.record_system_audio:
            audio_path = os.path.join(RECORD_DIR, f".audio_{uuid.uuid4().hex}.wav")
            audio = LoopbackAudioRecorder(audio_path)
            if not audio.start():
                self._alert(
                    "无法启动电脑内部声音录制，已取消录制。\n\n"
                    "请确认 Windows 默认输出设备可正常播放声音，然后重新开始录屏。"
                )
                return False
        cmd = self._seg_cmd(seg)
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            if audio:
                audio.stop()
            self._alert(f"启动录制失败：{e}")
            return False
        self.cur_seg = seg
        self.cur_audio = audio
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

    def _start_recording(self, region):
        if self.state != "idle":
            return
        if not self.ff:
            self._alert("未找到 ffmpeg，请先安装并加入 PATH。")
            return
        self.record_region = region
        self.final_path = os.path.join(
            RECORD_DIR, "录屏_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".mp4")
        self.segments = []
        self.recorded_before = 0.0
        if not self._launch_segment():
            self.record_region = None
            return
        self.state = "recording"
        self._update_ui()

    def start(self):
        region = RegionSelector(self.root).select()
        if not region:
            return
        self._start_after_countdown(region)

    def start_fullscreen(self):
        self._start_after_countdown(None)

    def _start_after_countdown(self, region):
        seconds = max(0, int(self.countdown_seconds))
        if seconds == 0:
            self._start_recording(region)
            return

        self.state = "countdown"
        self._update_ui()
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#5965f3")
        label = tk.Label(
            win, text=str(seconds), fg="white", bg="#5965f3",
            font=("Microsoft YaHei UI", 34, "bold"), padx=26, pady=12
        )
        label.pack()
        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_width()) // 2
        y = (win.winfo_screenheight() - win.winfo_height()) // 3
        win.geometry(f"+{x}+{y}")

        def tick(value):
            if not win.winfo_exists():
                return
            if value <= 0:
                win.destroy()
                self.state = "idle"
                self._start_recording(region)
                return
            label.configure(text=str(value))
            win.after(1000, lambda: tick(value - 1))

        tick(seconds)

    def pause_resume(self):
        if self.state == "recording":
            self.recorded_before += time.time() - (self.seg_start or time.time())
            self.seg_start = None
            self.state = "paused"
            proc, seg, audio = self.proc, self.cur_seg, self.cur_audio
            self.proc, self.cur_seg, self.cur_audio = None, None, None
            threading.Thread(target=self._finalize_seg, args=(proc, seg, audio),
                             daemon=True).start()
            self._update_ui()
        elif self.state == "paused":
            if self._launch_segment():
                self.state = "recording"
                self._update_ui()

    def _finalize_seg(self, proc, seg, audio=None):
        audio_path = audio.stop() if audio else None
        self._stop_proc(proc)
        final_seg = self._mux_segment(seg, audio_path)
        with self.lock:
            if final_seg and os.path.isfile(final_seg):
                self.segments.append(final_seg)

    def stop(self):
        if self.state == "idle":
            return
        if self.state == "recording":
            self.recorded_before += time.time() - (self.seg_start or time.time())
        self.seg_start = None
        self.state = "idle"
        proc, seg, audio, out = self.proc, self.cur_seg, self.cur_audio, self.final_path
        self.proc, self.cur_seg, self.cur_audio, self.final_path = None, None, None, None
        self.record_region = None
        self._update_ui()
        threading.Thread(target=self._finish, args=(proc, seg, audio, out),
                         daemon=True).start()

    def _finish(self, proc, seg, audio, out):
        audio_path = audio.stop() if audio else None
        self._stop_proc(proc)
        final_seg = self._mux_segment(seg, audio_path)
        with self.lock:
            if final_seg and os.path.isfile(final_seg):
                self.segments.append(final_seg)
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
            self._ui(self._refresh_recordings)

    def _mux_segment(self, video_path, audio_path=None):
        if not video_path or not os.path.isfile(video_path):
            return None
        if not audio_path or not os.path.isfile(audio_path):
            return video_path

        out = os.path.join(RECORD_DIR, f".seg_{uuid.uuid4().hex}.mp4")
        mux_succeeded = False
        try:
            result = subprocess.run([
                self.ff, "-y",
                "-i", video_path,
                "-i", audio_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-af", "apad",
                "-shortest",
                out,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
               creationflags=CREATE_NO_WINDOW)
            mux_succeeded = (
                result.returncode == 0
                and os.path.isfile(out)
                and os.path.getsize(out) > 0
            )
            if mux_succeeded:
                return out
            try:
                if os.path.isfile(out):
                    os.remove(out)
            except Exception:
                pass
            return video_path
        finally:
            if mux_succeeded:
                for p in (video_path, audio_path):
                    try:
                        if p and os.path.isfile(p):
                            os.remove(p)
                    except Exception:
                        pass

    def _cleanup_stale_recording_files(self):
        """Remove abandoned recording intermediates from previous crashes."""
        prefixes = (".audio_", ".video_", ".seg_", ".concat_")
        cutoff = time.time() - 24 * 60 * 60
        try:
            names = os.listdir(RECORD_DIR)
        except Exception:
            return
        for name in names:
            if not name.startswith(prefixes):
                continue
            path = os.path.join(RECORD_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                pass

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
        self.root.configure(bg="#b9dff0")
        try:
            self.root.attributes("-alpha", 0.98)
        except Exception:
            pass

        self.root.minsize(350, 90)
        self.root.geometry(f"{self.bar_width}x{self.bar_height}")

        self.bar_frame = tk.Frame(self.root, bg="#f1f9ff", padx=8, pady=6)
        self.bar_frame.pack(fill="both", expand=True, padx=5, pady=5)
        self.top_row = tk.Frame(self.bar_frame, bg="#f1f9ff")
        self.top_row.pack(fill="x")
        self.bottom_row = tk.Frame(self.bar_frame, bg="#f1f9ff")
        self.bottom_row.pack(anchor="center", pady=(5, 0))

        self.title_lbl = tk.Label(
            self.top_row, text="录屏助手", fg="#142033", bg="#f1f9ff",
            font=("Microsoft YaHei UI", 10, "bold")
        )
        self.title_lbl.pack(side="left", padx=(2, 8))

        self.status_frame = tk.Frame(self.top_row, bg="#e3f5f7", padx=7, pady=3)
        self.status_frame.pack(side="left", padx=(0, 8))
        self.dot = tk.Canvas(self.status_frame, width=12, height=18, bg="#e3f5f7",
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 3))
        self.dot_id = self.dot.create_oval(3, 6, 9, 12, fill="#28c79a", outline="")

        self.time_lbl = tk.Label(self.status_frame, text="00:00", fg="#142033", bg="#e3f5f7",
                                 font=("Consolas", 11, "bold"))
        self.time_lbl.pack(side="left")

        self.btn_start = RoundedButton(
            self.bottom_row, "选区录屏", self.start, 70, "#28c79a", "white", "#20b78d")
        self.btn_fullscreen = RoundedButton(
            self.bottom_row, "全屏", self.start_fullscreen, 48, "#5965f3", "white", "#4853df")
        self.btn_pause = RoundedButton(
            self.bottom_row, "暂停", self.pause_resume, 48, "#dcecf7", "#334155", "#cfe4f2")
        self.btn_stop = RoundedButton(
            self.bottom_row, "结束", self.stop, 48, "#ffe5e8", "#d83b4d", "#ffd6db")
        self.btn_shot = RoundedButton(
            self.bottom_row, "截图", self.take_screenshot, 48, "#e3f5f7", "#147f86", "#d5eff1")
        self.btn_shortcuts = RoundedButton(
            self.bottom_row, "工具", self.show_tools, 48, "#e5edff", "#4353c7", "#d8e2ff")
        self.btn_collapse = RoundedButton(
            self.top_row, "▾", self.toggle_collapse, 32, "#e7f1f7", "#526171", "#dceaf3")
        self.btn_close = RoundedButton(
            self.top_row, "×", self.hide_bar, 32, "#e7f1f7", "#526171", "#ffdfe3")
        for b in (self.btn_start, self.btn_fullscreen, self.btn_pause, self.btn_stop,
                  self.btn_shot, self.btn_shortcuts):
            b.pack(side="left", padx=2)
        self.btn_close.pack(side="right", padx=(2, 0))
        self.btn_collapse.pack(side="right", padx=2)

        # 拖动移动窗口
        for w in (self.bar_frame, self.top_row, self.title_lbl, self.status_frame,
                  self.dot, self.time_lbl):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self._add_resize_handles()
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

    def _add_resize_handles(self):
        specs = [
            ("n", "size_ns", {"x": 7, "y": 0, "relwidth": 1, "width": -14, "height": 6}),
            ("s", "size_ns", {"x": 7, "rely": 1, "y": -6, "relwidth": 1, "width": -14, "height": 6}),
            ("w", "size_we", {"x": 0, "y": 7, "width": 6, "relheight": 1, "height": -14}),
            ("e", "size_we", {"relx": 1, "x": -6, "y": 7, "width": 6, "relheight": 1, "height": -14}),
            ("nw", "size_nw_se", {"x": 0, "y": 0, "width": 8, "height": 8}),
            ("ne", "size_ne_sw", {"relx": 1, "x": -8, "y": 0, "width": 8, "height": 8}),
            ("sw", "size_ne_sw", {"x": 0, "rely": 1, "y": -8, "width": 8, "height": 8}),
            ("se", "size_nw_se", {"relx": 1, "x": -8, "rely": 1, "y": -8, "width": 8, "height": 8}),
        ]
        self.resize_handles = []
        for edge, cursor, place_args in specs:
            handle = tk.Frame(self.root, bg="#b9dff0", cursor=cursor)
            handle.place(**place_args)
            handle.bind("<ButtonPress-1>", lambda e, side=edge: self._resize_start(e, side))
            handle.bind("<B1-Motion>", self._resize_move)
            handle.bind("<ButtonRelease-1>", self._resize_end)
            self.resize_handles.append(handle)

    def _resize_start(self, event, edge):
        self._resize_edge = edge
        self._resize_origin = (
            event.x_root, event.y_root,
            self.root.winfo_x(), self.root.winfo_y(),
            self.root.winfo_width(), self.root.winfo_height(),
        )

    def _resize_move(self, event):
        edge = getattr(self, "_resize_edge", "")
        sx, sy, x, y, width, height = self._resize_origin
        dx, dy = event.x_root - sx, event.y_root - sy
        min_width, min_height = 350, 90
        if "e" in edge:
            width = max(min_width, width + dx)
        if "s" in edge:
            height = max(min_height, height + dy)
        if "w" in edge:
            new_width = max(min_width, width - dx)
            x += width - new_width
            width = new_width
        if "n" in edge:
            new_height = max(min_height, height - dy)
            y += height - new_height
            height = new_height
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _resize_end(self, _event):
        if not self.bar_collapsed:
            self.bar_width = self.root.winfo_width()
            self.bar_height = self.root.winfo_height()
            self._save_settings()
        self._resize_edge = ""

    def _position_bar(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        w = self.root.winfo_width()
        self.root.geometry(f"+{sw - w - 24}+{16}")

    def _apply_collapse(self):
        show = not self.bar_collapsed
        if show:
            self.root.minsize(350, 90)
            self.bottom_row.pack(anchor="center", pady=(5, 0))
            self.root.geometry(f"{self.bar_width}x{self.bar_height}")
        else:
            self.root.minsize(220, 50)
            self.bottom_row.pack_forget()
            self.root.geometry("240x52")
        self.btn_collapse.configure(text="▸" if self.bar_collapsed else "▾")
        self._position_bar()

    def toggle_collapse(self):
        if not self.bar_collapsed:
            self.bar_width = self.root.winfo_width()
            self.bar_height = self.root.winfo_height()
            self._save_settings()
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

    def show_tools(self):
        win = getattr(self, "tools_win", None)
        if win and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            self._refresh_recordings()
            return

        win = tk.Toplevel(self.root)
        self.tools_win = win
        win.title("录屏助手")
        win.configure(bg="#eef8ff")
        win.resizable(False, False)
        win.attributes("-topmost", True)

        def close():
            self.tools_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)

        header = tk.Frame(win, bg="#eef8ff")
        header.pack(fill="x", padx=24, pady=(20, 12))
        tk.Label(
            header, text="录屏助手", fg="#142033", bg="#eef8ff",
            font=("Microsoft YaHei UI", 18, "bold")
        ).pack(side="left")
        tk.Label(
            header, text=f"版本 {APP_VERSION} · 录制、截图与文件管理",
            fg="#607387", bg="#eef8ff",
            font=("Microsoft YaHei UI", 9)
        ).pack(side="left", padx=(10, 0), pady=(7, 0))

        actions = tk.Frame(win, bg="#eef8ff")
        actions.pack(fill="x", padx=20, pady=(0, 14))

        def action_button(text, command, color, column):
            button = tk.Button(
                actions, text=text, command=command, fg="white", bg=color,
                activebackground=color, activeforeground="white",
                relief="flat", bd=0, cursor="hand2",
                font=("Microsoft YaHei UI", 10, "bold"), padx=18, pady=10
            )
            button.grid(row=0, column=column, padx=4, sticky="ew")
            actions.grid_columnconfigure(column, weight=1)

        action_button("选区录屏", lambda: (close(), self.start()), "#28c79a", 0)
        action_button("全屏录屏", lambda: (close(), self.start_fullscreen()), "#5965f3", 1)
        action_button("截图", self.take_screenshot, "#2aa9d6", 2)
        action_button("录制设置", self.show_settings, "#718096", 3)

        file_bar = tk.Frame(win, bg="#ffffff", highlightthickness=1,
                            highlightbackground="#d7ebf6")
        file_bar.pack(fill="x", padx=24, pady=(0, 10))
        tk.Label(
            file_bar, text="最近录屏", fg="#142033", bg="#ffffff",
            font=("Microsoft YaHei UI", 11, "bold")
        ).pack(side="left", padx=14, pady=10)
        tk.Button(
            file_bar, text="打开录屏文件夹", command=lambda: os.startfile(RECORD_DIR),
            fg="#4353c7", bg="#ffffff", activebackground="#eef4ff",
            relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold")
        ).pack(side="right", padx=8)
        tk.Button(
            file_bar, text="打开截图文件夹", command=lambda: os.startfile(SHOT_DIR),
            fg="#147f86", bg="#ffffff", activebackground="#e8f7f8",
            relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold")
        ).pack(side="right", padx=8)

        list_frame = tk.Frame(win, bg="#ffffff", highlightthickness=1,
                              highlightbackground="#d7ebf6")
        list_frame.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        self.recordings_list = tk.Listbox(
            list_frame, height=10, activestyle="none", selectmode="browse",
            bg="#ffffff", fg="#263548", selectbackground="#dce8ff",
            selectforeground="#263548", relief="flat", bd=0,
            font=("Microsoft YaHei UI", 9)
        )
        self.recordings_list.pack(fill="both", expand=True, padx=10, pady=10)
        self.recordings_list.bind("<Double-Button-1>", lambda _e: self._open_selected_recording())

        bottom = tk.Frame(win, bg="#eef8ff")
        bottom.pack(fill="x", padx=24, pady=(0, 18))
        tk.Button(
            bottom, text="播放选中", command=self._open_selected_recording,
            fg="white", bg="#5965f3", activebackground="#4853df",
            activeforeground="white", relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"), padx=16, pady=7
        ).pack(side="left")
        tk.Button(
            bottom, text="快捷键说明", command=self.show_shortcuts,
            fg="#4353c7", bg="#e5edff", activebackground="#d8e2ff",
            relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"), padx=16, pady=7
        ).pack(side="left", padx=8)
        tk.Button(
            bottom, text="检查更新", command=self.check_for_updates,
            fg="#147f86", bg="#e3f5f7", activebackground="#d5eff1",
            relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"), padx=16, pady=7
        ).pack(side="left")

        self._refresh_recordings()
        win.update_idletasks()
        x = max(0, (win.winfo_screenwidth() - win.winfo_width()) // 2)
        y = max(0, (win.winfo_screenheight() - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        win.focus_force()

    def _refresh_recordings(self):
        box = getattr(self, "recordings_list", None)
        if not box or not box.winfo_exists():
            return
        box.delete(0, "end")
        try:
            files = [
                os.path.join(RECORD_DIR, name)
                for name in os.listdir(RECORD_DIR)
                if name.lower().endswith(".mp4") and not name.startswith(".")
            ]
            files.sort(key=os.path.getmtime, reverse=True)
        except Exception:
            files = []
        self.recent_recordings = files[:30]
        if not self.recent_recordings:
            box.insert("end", "暂无录屏文件")
            return
        for path in self.recent_recordings:
            size_mb = os.path.getsize(path) / (1024 * 1024)
            stamp = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%m-%d %H:%M")
            box.insert("end", f"{stamp}    {size_mb:.1f} MB    {os.path.basename(path)}")

    def _open_selected_recording(self):
        box = getattr(self, "recordings_list", None)
        files = getattr(self, "recent_recordings", [])
        if not box or not files:
            return
        selected = box.curselection()
        if not selected or selected[0] >= len(files):
            return
        try:
            os.startfile(files[selected[0]])
        except Exception as e:
            self._alert(f"无法打开录屏：{e}")

    def show_settings(self):
        win = getattr(self, "settings_win", None)
        if win and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("录制设置")
        win.configure(bg="#eef8ff")
        win.resizable(False, False)
        win.attributes("-topmost", True)

        def close():
            self.settings_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)

        tk.Label(
            win, text="录制设置", fg="#142033", bg="#eef8ff",
            font=("Microsoft YaHei UI", 16, "bold")
        ).pack(anchor="w", padx=24, pady=(20, 14))

        panel = tk.Frame(win, bg="#ffffff", highlightthickness=1,
                         highlightbackground="#d7ebf6")
        panel.pack(fill="both", padx=24)

        fps_var = tk.StringVar(value=self.framerate)
        quality_by_crf = {"18": "超清", "23": "高清（推荐）", "28": "流畅"}
        crf_by_quality = {value: key for key, value in quality_by_crf.items()}
        quality_var = tk.StringVar(value=quality_by_crf.get(self.video_crf, "高清（推荐）"))
        countdown_var = tk.StringVar(value=str(self.countdown_seconds))
        auto_stop_var = tk.StringVar(value=str(self.auto_stop_minutes))
        audio_var = tk.BooleanVar(value=self.record_system_audio)
        mouse_var = tk.BooleanVar(value=self.draw_mouse)

        def option_row(row, title, variable, values, suffix=""):
            tk.Label(
                panel, text=title, fg="#263548", bg="#ffffff",
                font=("Microsoft YaHei UI", 10)
            ).grid(row=row, column=0, sticky="w", padx=16, pady=10)
            menu = tk.OptionMenu(panel, variable, *values)
            menu.configure(
                width=14, fg="#263548", bg="#edf5fb", activebackground="#dcecf7",
                relief="flat", bd=0, highlightthickness=0,
                font=("Microsoft YaHei UI", 9)
            )
            menu.grid(row=row, column=1, sticky="e", padx=(20, 4), pady=6)
            tk.Label(
                panel, text=suffix, fg="#607387", bg="#ffffff",
                font=("Microsoft YaHei UI", 9)
            ).grid(row=row, column=2, sticky="w", padx=(0, 16))

        option_row(0, "画质", quality_var, ["高清（推荐）", "超清", "流畅"])
        option_row(1, "帧率", fps_var, ["30", "60"], "帧/秒")
        option_row(2, "开始倒计时", countdown_var, ["0", "3", "5"], "秒")
        option_row(3, "自动结束", auto_stop_var, ["0", "10", "30", "60"], "分钟（0 为不限）")

        check_style = {
            "fg": "#263548", "bg": "#ffffff", "activebackground": "#ffffff",
            "selectcolor": "#ffffff", "font": ("Microsoft YaHei UI", 10),
            "bd": 0, "highlightthickness": 0,
        }
        tk.Checkbutton(
            panel, text="录制电脑内部声音", variable=audio_var, **check_style
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 4))
        tk.Checkbutton(
            panel, text="录制鼠标指针", variable=mouse_var, **check_style
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=12, pady=(4, 12))

        def save():
            self.framerate = fps_var.get()
            self.video_crf = crf_by_quality.get(quality_var.get(), "23")
            self.countdown_seconds = int(countdown_var.get())
            self.auto_stop_minutes = int(auto_stop_var.get())
            self.record_system_audio = bool(audio_var.get())
            self.draw_mouse = bool(mouse_var.get())
            self._save_settings()
            close()

        tk.Button(
            win, text="保存设置", command=save, fg="white", bg="#28c79a",
            activebackground="#20b78d", activeforeground="white",
            relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"), padx=24, pady=8
        ).pack(pady=18)

        win.update_idletasks()
        x = max(0, (win.winfo_screenwidth() - win.winfo_width()) // 2)
        y = max(0, (win.winfo_screenheight() - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        win.focus_force()

    def check_for_updates(self):
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            request = urllib.request.Request(
                UPDATE_VERSION_URL,
                headers={"User-Agent": f"ScreenRecorder/{APP_VERSION}"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                metadata = json.load(response)
            latest = str(metadata.get("version") or "")
            if not latest:
                raise ValueError("服务器未提供版本号")
            download_url = metadata.get("download_url") or UPDATE_DOWNLOAD_URL
            self._ui(
                lambda version=latest, url=download_url:
                self._show_update_result(version, url)
            )
        except Exception as exc:
            message = str(exc)
            self._ui(lambda text=message: self._alert(f"检查更新失败：{text}"))

    def _show_update_result(self, latest, download_url=UPDATE_DOWNLOAD_URL):
        from tkinter import messagebox

        def version_tuple(value):
            return tuple(int(part) for part in value.split("."))

        if version_tuple(latest) > version_tuple(APP_VERSION):
            download = messagebox.askyesno(
                "发现新版本",
                f"发现录屏助手 {latest}。\n"
                f"当前版本：{APP_VERSION}\n\n"
                "是否打开新版安装包下载？",
            )
            if download:
                os.startfile(download_url)
        else:
            messagebox.showinfo(
                "检查更新",
                f"当前已是最新版。\n\n版本：{APP_VERSION}",
            )

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
        win.configure(bg="#eef8ff")
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
            win, text="录屏助手快捷键", fg="#142033", bg="#eef8ff",
            font=("Microsoft YaHei UI", 16, "bold")
        ).pack(padx=28, pady=(22, 4))

        tk.Label(
            win, text="记住这 4 个组合键，就能完成日常操作",
            fg="#607387", bg="#eef8ff",
            font=("Microsoft YaHei UI", 9)
        ).pack(padx=28, pady=(0, 16))

        shortcuts = [
            ("Ctrl + R", "选区后开始 / 结束录屏"),
            ("Ctrl + P", "暂停 / 继续当前录屏"),
            ("控制条", "点击“全屏”直接录整块桌面"),
            ("Ctrl + S", "框选截图并复制"),
            ("Ctrl + B", "显示 / 隐藏控制条"),
            ("Enter / Esc", "确认选区 / 取消选区"),
        ]
        for key, action in shortcuts:
            row = tk.Frame(win, bg="#ffffff", highlightthickness=1,
                           highlightbackground="#d7ebf6")
            row.pack(fill="x", padx=22, pady=4)

            tk.Label(
                row, text=key, width=11, anchor="center",
                fg="#ffffff", bg="#5965f3",
                font=("Consolas", 11, "bold")
            ).pack(side="left", padx=8, pady=9)

            tk.Label(
                row, text=action, anchor="w",
                fg="#263548", bg="#ffffff",
                font=("Microsoft YaHei UI", 10)
            ).pack(side="left", padx=(8, 16), pady=9)

        tk.Label(
            win,
            text="也可以直接使用悬浮控制条或右下角托盘菜单。\n"
                 "快捷键无响应时，请尝试以管理员身份运行。",
            justify="left", fg="#607387", bg="#eef8ff",
            font=("Microsoft YaHei UI", 9)
        ).pack(fill="x", padx=28, pady=(14, 12))

        tk.Button(
            win, text="知道了", width=12, command=close,
            fg="white", bg="#28c79a", activebackground="#20b78d",
            activeforeground="white", relief="flat", bd=0,
            font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2",
            padx=10, pady=6
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
        if (
            self.state == "recording"
            and self.auto_stop_minutes > 0
            and self._elapsed_secs() >= self.auto_stop_minutes * 60
        ):
            self.stop()
        self.root.after(500, self._tick)

    def _update_ui(self):
        color = {
            "recording": "#ff3b30",
            "paused": "#ff9500",
            "countdown": "#5965f3",
            "idle": "#28c79a",
        }[self.state]
        self.dot.itemconfigure(self.dot_id, fill=color)
        self.time_lbl.configure(text=self._fmt(self._elapsed_secs()))
        self.btn_start.configure(state=("normal" if self.state == "idle" else "disabled"))
        self.btn_fullscreen.configure(state=("normal" if self.state == "idle" else "disabled"))
        self.btn_pause.configure(state=("normal" if self.state in ("recording", "paused") else "disabled"),
                                 text=("继续" if self.state == "paused" else "暂停"))
        self.btn_stop.configure(
            state=("normal" if self.state in ("recording", "paused") else "disabled")
        )

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
            pystray.MenuItem("打开工具中心", lambda: self._ui(self.show_tools)),
            pystray.MenuItem("录制设置", lambda: self._ui(self.show_settings)),
            pystray.MenuItem("检查更新", lambda: self._ui(self.check_for_updates)),
            pystray.MenuItem("快捷键 / 使用说明", lambda: self._ui(self.show_shortcuts)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("显示/隐藏控制条 (Ctrl+B)", lambda: self._ui(self.toggle_bar)),
            pystray.MenuItem("选区后开始/结束录屏 (Ctrl+R)",
                             lambda: self._ui(lambda: self.stop() if self.state != "idle" else self.start())),
            pystray.MenuItem("全屏开始/结束录屏",
                             lambda: self._ui(lambda: self.stop() if self.state != "idle" else self.start_fullscreen())),
            pystray.MenuItem("截图 (Ctrl+S)", lambda: self.take_screenshot()),
            pystray.MenuItem("录电脑内部声音", self._toggle_system_audio,
                             checked=lambda i: self.record_system_audio),
            pystray.MenuItem("打开录屏文件夹", lambda: os.startfile(RECORD_DIR)),
            pystray.MenuItem("打开截图文件夹", lambda: os.startfile(SHOT_DIR)),
            pystray.MenuItem("退出", self._quit),
        )
        self.tray = pystray.Icon("录屏助手", self._tray_icon_img(), "录屏助手", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _toggle_system_audio(self, icon, item):
        self.record_system_audio = not self.record_system_audio
        self._save_settings()

    def _quit(self, icon=None, item=None):
        if self.state != "idle":
            # 同步收尾
            if self.state == "recording":
                self.recorded_before += time.time() - (self.seg_start or time.time())
            self._finish(self.proc, self.cur_seg, self.cur_audio, self.final_path)
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
            keyboard.add_hotkey(
                HOTKEY_PAUSE,
                lambda: self._ui(
                    self.pause_resume
                    if self.state in ("recording", "paused")
                    else lambda: None
                ),
                suppress=True,
            )
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
