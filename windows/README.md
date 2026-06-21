# 录屏助手 · Windows 版（Python）

macOS 版的 Windows 移植，功能对齐：托盘常驻 + 桌面悬浮控制条 + 全局快捷键。

> ⚠️ 此 Windows 版尚未在 Windows 上实测，是从 macOS 版移植过来的。
> 如运行有问题，欢迎提 issue，会据反馈修。

## 功能
- **录屏**：ffmpeg `gdigrab` 全屏录制（Windows 上无时长限制，可长录），输出 `.mp4`，
  存 `~/Videos/录屏/`
- **暂停 / 继续**：分段录制，结束时用 ffmpeg 无损合并成一个文件
- **截图**：调用 Windows 自带「截图」(`ms-screenclip`) 框选，**自动进剪贴板**，
  同时存盘到 `~/Pictures/截图/`（可直接 Ctrl+V 粘到微信等）
- **可选录麦克风**（托盘菜单开关）
- **桌面悬浮控制条**：红点 + 计时 + 开始/暂停/结束，可拖动、缩小(▾)、隐藏(✕)
- **托盘图标**：右键菜单含全部功能

## 全局快捷键
| 快捷键 | 功能 |
|---|---|
| **Ctrl+R** | 开始 / 结束录屏 |
| **Ctrl+S** | 框选截图 |
| **Ctrl+B** | 呼出 / 隐藏控制条 |

## 安装
1. 装 **Python 3.9+**：https://www.python.org/downloads/ （安装时勾选 *Add Python to PATH*）
2. 装 **ffmpeg** 并加入 PATH：
   - 用包管理器：`winget install Gyan.FFmpeg`，或
   - 手动从 https://www.gyan.dev/ffmpeg/builds/ 下载，把 `bin` 目录加进系统 PATH
   - 验证：命令行运行 `ffmpeg -version` 能输出版本即可
3. 装依赖库：
   ```cmd
   pip install pillow pystray keyboard
   ```

## 运行
```cmd
python recorder.py
```
启动后托盘出现红点图标，桌面右上角出现控制条。

> **快捷键需要管理员权限**：`keyboard` 库注册全局热键通常要管理员身份。
> 若 Ctrl+R/S/B 没反应，请用「以管理员身份运行」命令行再启动，或直接用控制条按钮/托盘菜单。

## 打包成 exe（可选，免装 Python 直接双击用）
```cmd
pip install pyinstaller
pyinstaller --noconsole --onefile --name 录屏助手 recorder.py
```
生成的 `dist\录屏助手.exe` 可直接分发（目标机器仍需自备 ffmpeg）。

## 说明 / 限制
- 截图依赖 Windows 10/11 自带的「截图」(Snip)，靠剪贴板回读存盘；
  若取消框选(Esc)，则不存盘、也不覆盖你原有剪贴板内容。
- `Ctrl+S` 注册为全局热键并抑制原按键，运行期间其他程序里的 Ctrl+S（保存）会被接管；
  不想要可在 `recorder.py` 顶部改 `HOTKEY_SHOT`。
- 录麦克风用 dshow，自动取第一个麦克风设备；多设备需要指定可改 `detect_mic`。
- 默认 `libx264 -preset ultrafast`，CPU 占用低；想要更小文件可调 `VIDEO_BITRATE_CRF`。
