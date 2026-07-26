# 录屏助手 · macOS Menu-Bar Screen Recorder

> **A native macOS menu-bar screen recorder + screenshot tool.**
> Full-screen or selected-region recording with pause/resume, native system-audio
> capture on macOS 15+, region screenshots that are **auto-copied to the clipboard**,
> optional microphone audio, and
> three global hotkeys — `⌃R` record · `⌃S` screenshot · `⌃B` toggle the floating
> control bar. No Dock icon, never steals focus. Two Swift source files, built with one
> script, no app dependencies except `ffmpeg` (only used to merge segments).
> *(Documentation below is in Chinese.)*

---

一个常驻 macOS 菜单栏的轻量录屏 + 截图工具，专为「边用 AI 边录屏」设计：
不抢焦点、无 Dock 图标、按快捷键就开始。

> 🪟 **Windows 用户**：有一个功能对齐的 Python 移植版，见 [`windows/`](windows/) 目录。

- **选区 / 全屏录屏**：控制条可直接选择录屏区域，也可一键录制主屏全屏。
- **电脑内部声音**：macOS 15 及以上使用原生 `ScreenCaptureKit`，无需 BlackHole
  或其他虚拟声卡；可与麦克风同时录制。
- **兼容旧系统**：macOS 13/14 使用系统 `screencapture` 录制画面和麦克风，
  不支持电脑内部声音。
- **暂停/继续**：分段实现，暂停处无卡顿，结束时一并合并。
- **截图**：用 `screencapture` 框选截图。
- 录屏可选**电脑内部声音**和**麦克风**（菜单里分别开关，默认都开）。
- **桌面控制条**：屏幕右上角浮动小条（红点+计时+选区/全屏/暂停/结束按钮），可拖动、
  可缩小（▾）、可隐藏（✕），始终置顶、所有桌面可见。
- 仅「合并分段」用到 `ffmpeg`（`brew install ffmpeg`）；录制本身不依赖它。

## 文件说明
- `recorder.swift` —— 程序源码（菜单栏逻辑）
- `RecordingCore.swift` —— 录屏区域坐标与 Retina 像素换算
- `tests/recording_core_tests.swift` —— 核心逻辑回归测试
- `setup-cert.sh` —— 生成固定的自签名「代码签名」证书（只需跑一次，build.sh 会自动调用）
- `build.sh` —— 编译脚本，用固定证书签名后生成 `录屏助手.app`
- `录屏助手.app` —— 编译产物，双击即用

## 安装 / 启动
1. 编译（只需一次，改了源码再跑）：
   ```bash
   cd ~/ScreenRecorder && bash build.sh
   ```
2. 双击 `录屏助手.app` 启动，菜单栏右上角出现一个圆点图标 ●。

## 使用
- **桌面控制条**：右上角的 `●  00:00  [选区][全屏][暂停][结束] ▾ ✕`，直接点按钮控制。
  `▾` 缩小成只剩红点+计时，`✕` 隐藏。
- **菜单栏图标**（红点 ●「录屏」）：左键 → 控制条隐藏时呼出、显示时开始/停止录屏；
  右键（或按住 Control 左键）→ 完整菜单。
- 录制中图标与控制条显示已录时长（如 `00:12`），暂停变橙色。

### 全局快捷键（全系统生效，任意 App 里都能按）
| 快捷键 | 功能 |
|---|---|
| **⌃R**（Control+R） | 选区后开始 / 结束录屏 |
| **⌃S**（Control+S） | 框选截图 |
| **⌃B**（Control+B） | 呼出 / 隐藏桌面控制条（呼出时弹回右上角） |

截图按下后拖框选区域即可（按空格可切到窗口截图模式，Esc 取消）。
截图会**同时保存文件并复制到剪贴板**，所以截完可直接到微信等处按 `⌘V` 粘贴。

文件默认保存：录屏 → **`~/Movies/录屏/`**，截图 → **`~/Pictures/截图/`**，均带时间戳。
想改目录或快捷键，编辑 `recorder.swift` 顶部的配置区后重新 `bash build.sh`
（文件底部附了键码对照表）。

## ⚠️ 首次使用要授权「屏幕录制」
第一次点开始录制时，macOS 会弹窗要求授权屏幕与系统音频录制权限：
- 在弹窗里点「打开系统设置」，或手动进入
  **系统设置 → 隐私与安全性 → 屏幕录制与系统录音**，
- 把「录屏助手」打开（打勾），按提示重新打开 App 即可。

没授权时录制会静默失败、不生成文件——这是正常的权限拦截，授权一次后永久生效。

> 本工具用一张**固定的自签名证书**签名（`setup-cert.sh` 生成，存在登录钥匙串里），
> 所以以后重新 `bash build.sh` 编译也**不会丢失屏幕录制权限**——只需首次授权这一次。
> 若哪天权限异常，可执行 `tccutil reset ScreenCapture com.local.screenrecorder` 后重新授权。

## 开机自启（可选）
想登录后自动常驻菜单栏：
**系统设置 → 通用 → 登录项 → 「开机时打开」** 里点 `+`，选 `录屏助手.app`。

## 备注
- 声音：macOS 15+ 可原生录电脑内部声音和麦克风；macOS 13/14 仅支持麦克风。
- 录制范围：支持拖框选区；控制条和右键菜单可直接开始主屏全屏录制。
- 暂停会生成新片段，结束时自动合并为单个文件；合并多段需要
  `brew install ffmpeg`。合并失败时片段会改为可见文件保留，不会静默删除。
- 运行日志：`~/Library/Logs/录屏助手/events.jsonl`。找不到控制条时按 `⌃B` 呼出。

## 开发验证
```bash
swiftc RecordingCore.swift tests/recording_core_tests.swift \
  -o /tmp/recording-core-tests && /tmp/recording-core-tests
swiftc -typecheck RecordingCore.swift recorder.swift \
  -framework AppKit -framework AVFoundation -framework ScreenCaptureKit
```
- 卸载：退出 App，删除整个 `~/ScreenRecorder` 文件夹即可。
