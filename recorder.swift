import AppKit
import Foundation
import Carbon.HIToolbox

// ============== 可改配置 ==============
// 录屏保存目录（自动创建）
let recordDir = ("~/Movies/录屏" as NSString).expandingTildeInPath
// 截图保存目录（自动创建）
let shotDir = ("~/Pictures/截图" as NSString).expandingTildeInPath

// 全局快捷键（虚拟键码 + 修饰键）。键码见文件底部对照表。
// 默认：录屏 = Control+R，截图 = Control+S（两键）
let recordKeyCode: UInt32 = 0x0F                       // R
let recordKeyMods: UInt32 = UInt32(controlKey)
let shotKeyCode:   UInt32 = 0x01                       // S
let shotKeyMods:   UInt32 = UInt32(controlKey)
let barKeyCode:    UInt32 = 0x0B                       // B = 呼出/隐藏控制条
let barKeyMods:    UInt32 = UInt32(controlKey)
// ====================================

final class Recorder: NSObject, NSApplicationDelegate {
    static var shared: Recorder?

    enum RecState { case idle, recording, paused }
    var state: RecState = .idle
    var isRecording: Bool { state != .idle }   // 兼容旧判断

    var statusItem: NSStatusItem!
    var process: Process?            // 当前片段的 ffmpeg 进程
    var currentSegPath: String?      // 当前片段临时文件
    var segments: [String] = []      // 已录完的片段（仅在 recQ 上读写）
    var finalPath: String?           // 最终输出文件
    var recordedBefore: TimeInterval = 0  // 已完成片段累计时长（不含暂停）
    var segStart: Date?              // 当前片段开始时刻
    var devInput: String?            // ffmpeg 输入串 "screen[:mic]"
    var segUseMic = false            // 本次录制是否含麦克风（整段固定，保证 concat 一致）
    let recQ = DispatchQueue(label: "rec.segments")  // 串行处理片段收尾/合并
    var timer: Timer?
    var lastFile: String?
    // 是否录制麦克风（开关状态记住，重启后保留）。默认开。
    var recordMic: Bool {
        get { UserDefaults.standard.object(forKey: "recordMic") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "recordMic") }
    }
    // 是否显示桌面控制条（红点+计时+按钮）。默认开。
    var showBar: Bool {
        get { UserDefaults.standard.object(forKey: "showBar") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "showBar") }
    }
    // 桌面控制条
    var bar: NSPanel?
    var barView: TimerView?
    var btnStart: NSButton?
    var btnPause: NSButton?
    var btnStop: NSButton?
    var btnCollapse: NSButton?
    var btnClose: NSButton?
    // 控制条是否收起（只剩红点+计时）。记住状态。
    var barCollapsed: Bool {
        get { UserDefaults.standard.bool(forKey: "barCollapsed") }
        set { UserDefaults.standard.set(newValue, forKey: "barCollapsed") }
    }
    let barWidthFull: CGFloat = 340
    let barWidthMin: CGFloat = 122

    func applicationDidFinishLaunching(_ notification: Notification) {
        Recorder.shared = self
        // 只在菜单栏出现，不显示 Dock 图标、不抢占前台焦点
        NSApp.setActivationPolicy(.accessory)

        for dir in [recordDir, shotDir] {
            try? FileManager.default.createDirectory(
                atPath: dir, withIntermediateDirectories: true)
        }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.action = #selector(clicked(_:))
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        updateIcon()
        registerHotKeys()
        setupBar()
        applyBarVisibility()
    }

    // 右键/Control=菜单；左键：控制条隐藏时→恢复显示，否则→开始/停止录屏
    @objc func clicked(_ sender: NSStatusBarButton) {
        let e = NSApp.currentEvent
        if e?.type == .rightMouseUp || (e?.modifierFlags.contains(.control) ?? false) {
            showMenu()
        } else if !showBar {
            showBarNow()
        } else {
            toggle()
        }
    }

    // 显示控制条并强制弹回右上角、展开，确保一定可见
    func showBarNow() {
        showBar = true
        if barCollapsed { barCollapsed = false }
        applyCollapse(false)
        positionBar()
        bar?.orderFrontRegardless()
    }

    func showMenu() {
        let menu = NSMenu()
        menu.addItem(withTitle: isRecording ? "结束录制  ⌃R" : "开始录制  ⌃R",
                     action: #selector(toggle), keyEquivalent: "")
        if isRecording {
            menu.addItem(withTitle: state == .paused ? "继续录制" : "暂停录制",
                         action: #selector(pauseResume), keyEquivalent: "")
        }
        menu.addItem(withTitle: "截图（框选）  ⌃S",
                     action: #selector(takeScreenshot), keyEquivalent: "")
        menu.addItem(.separator())
        let micItem = NSMenuItem(title: "录制麦克风声音",
                                 action: #selector(toggleMic), keyEquivalent: "")
        micItem.state = recordMic ? .on : .off
        menu.addItem(micItem)
        let barItem = NSMenuItem(title: "显示/隐藏控制条  ⌃B",
                                 action: #selector(toggleBar), keyEquivalent: "")
        barItem.state = showBar ? .on : .off
        menu.addItem(barItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "打开录屏文件夹", action: #selector(openRecordFolder), keyEquivalent: "")
        menu.addItem(withTitle: "打开截图文件夹", action: #selector(openShotFolder), keyEquivalent: "")
        if lastFile != nil {
            menu.addItem(withTitle: "在访达中显示上一个文件",
                         action: #selector(revealLast), keyEquivalent: "")
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出", action: #selector(quit), keyEquivalent: "q")
        for item in menu.items { item.target = self }
        statusItem.menu = menu
        statusItem.button?.performClick(nil) // 展开菜单
        statusItem.menu = nil                // 立刻清空，保证下次左键仍是开始/停止
    }

    // ---- 录屏 ----
    @objc func toggle() { isRecording ? stop() : start() }

    @objc func toggleMic() { recordMic.toggle() }

    // 开始（idle → recording）
    @objc func start() {
        guard state == .idle else { return }
        segUseMic = recordMic
        finalPath = "\(recordDir)/录屏_\(timestamp()).mov"
        recordedBefore = 0
        recQ.async { [weak self] in self?.segments = [] }
        guard launchSegment() else { return }
        state = .recording
        startTimer()
        updateUI()
    }

    // 暂停 / 继续
    @objc func pauseResume() {
        switch state {
        case .recording:
            recordedBefore += Date().timeIntervalSince(segStart ?? Date())
            segStart = nil
            state = .paused
            stopCurrentSegment()      // 结束当前片段（后台收尾）
            updateUI()
        case .paused:
            _ = launchSegment()
            state = .recording
            updateUI()
        case .idle:
            break
        }
    }

    // 结束（recording/paused → idle）：收尾 + 合并所有片段
    @objc func stop() {
        guard state != .idle else { return }
        if state == .recording {
            recordedBefore += Date().timeIntervalSince(segStart ?? Date())
        }
        segStart = nil
        state = .idle
        stopTimer()
        updateUI()
        let p = process; let sp = currentSegPath; let out = finalPath
        process = nil; currentSegPath = nil; finalPath = nil
        // 在串行队列收尾（FIFO 保证之前暂停的片段都已入列），再合并
        recQ.async { [weak self] in
            guard let self = self else { return }
            if let p = p { kill(p.processIdentifier, SIGINT); p.waitUntilExit() }
            if let sp = sp, FileManager.default.fileExists(atPath: sp) { self.segments.append(sp) }
            let all = self.segments; self.segments = []
            self.finalize(segments: all, to: out)
        }
    }

    // 启动一个录制片段：用系统 screencapture 全屏录制（直接用本 App 的录屏权限）
    @discardableResult
    func launchSegment() -> Bool {
        let seg = "\(recordDir)/.seg_\(UUID().uuidString).mov"
        var args = ["-v", "-x"]            // -v 录视频  -x 不播放提示音
        if segUseMic { args.append("-g") } // -g 同时录麦克风
        args.append(seg)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        p.arguments = args
        p.standardError = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        // 自动接力：screencapture 约 5 分钟会自行停止。若是它自己停的（仍在录制、
        // 且还是当前进程），就把这段入列并立刻开下一段，实现无限时长。
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self = self else { return }
                guard self.state == .recording, self.process === proc else { return }
                let sp = self.currentSegPath
                self.recordedBefore += Date().timeIntervalSince(self.segStart ?? Date())
                self.recQ.async {
                    if let sp = sp, FileManager.default.fileExists(atPath: sp) {
                        self.segments.append(sp)
                    }
                }
                self.process = nil
                self.currentSegPath = nil
                self.launchSegment()   // 接力下一段
            }
        }
        do { try p.run() } catch { return false }
        process = p
        currentSegPath = seg
        segStart = Date()
        return true
    }

    // 结束当前片段（后台等待 ffmpeg 写完文件尾后入列）
    func stopCurrentSegment() {
        let p = process; let sp = currentSegPath
        process = nil; currentSegPath = nil
        recQ.async { [weak self] in
            if let p = p { kill(p.processIdentifier, SIGINT); p.waitUntilExit() }
            if let sp = sp, FileManager.default.fileExists(atPath: sp) { self?.segments.append(sp) }
        }
    }

    // 合并片段为最终文件（单段直接改名，多段用 ffmpeg concat 无损拼接）
    func finalize(segments segs: [String], to out: String?) {
        guard let out = out, !segs.isEmpty else { return }
        if segs.count == 1 {
            try? FileManager.default.moveItem(atPath: segs[0], toPath: out)
        } else if let ff = ffmpegPath() {
            let list = NSTemporaryDirectory() + "concat_\(UUID().uuidString).txt"
            let body = segs.map { "file '\($0)'" }.joined(separator: "\n")
            try? body.write(toFile: list, atomically: true, encoding: .utf8)
            let p = Process()
            p.executableURL = URL(fileURLWithPath: ff)
            p.arguments = ["-y", "-nostdin", "-f", "concat", "-safe", "0",
                           "-i", list, "-c", "copy", out]
            p.standardError = FileHandle.nullDevice
            p.standardOutput = FileHandle.nullDevice
            try? p.run(); p.waitUntilExit()
            try? FileManager.default.removeItem(atPath: list)
            for s in segs { try? FileManager.default.removeItem(atPath: s) }
        }
        DispatchQueue.main.async { [weak self] in
            if FileManager.default.fileExists(atPath: out) { self?.lastFile = out }
        }
    }

    // 找 ffmpeg（GUI 启动的 App 没有 shell 的 PATH，必须用绝对路径）
    func ffmpegPath() -> String? {
        for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"] {
            if FileManager.default.isExecutableFile(atPath: p) { return p }
        }
        return nil
    }

    // 运行时探测：屏幕采集设备 + 第一个麦克风的 avfoundation 编号
    func avfDevices(ff: String) -> (screen: String?, mic: String?) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: ff)
        p.arguments = ["-f", "avfoundation", "-list_devices", "true", "-i", ""]
        let pipe = Pipe()
        p.standardError = pipe
        p.standardOutput = Pipe()
        guard (try? p.run()) != nil else { return (nil, nil) }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let out = String(data: data, encoding: .utf8) ?? ""
        var screen: String?, mic: String?, inAudio = false
        for line in out.components(separatedBy: "\n") {
            if line.contains("video devices") { inAudio = false; continue }
            if line.contains("audio devices") { inAudio = true; continue }
            guard let r = line.range(of: "\\[[0-9]+\\]", options: .regularExpression)
            else { continue }
            let idx = String(line[r].dropFirst().dropLast())
            if inAudio { if mic == nil { mic = idx } }
            else if line.contains("Capture screen") { screen = idx }
        }
        return (screen, mic)
    }

    func alert(_ title: String, _ msg: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = msg
        NSApp.activate(ignoringOtherApps: true)
        a.runModal()
    }

    // ---- 截图 ----
    @objc func takeScreenshot() {
        let path = "\(shotDir)/截图_\(timestamp()).png"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        // -i 交互式框选（也可空格切换窗口模式）  -x 静音
        p.arguments = ["-i", "-x", path]
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard FileManager.default.fileExists(atPath: path) else { return }
                self?.lastFile = path
                // 同时拷到剪贴板，截完可直接 ⌘V 粘到微信等
                let pb = NSPasteboard.general
                pb.clearContents()
                if let png = try? Data(contentsOf: URL(fileURLWithPath: path)) {
                    pb.setData(png, forType: .png)
                    if let img = NSImage(data: png), let tiff = img.tiffRepresentation {
                        pb.setData(tiff, forType: .tiff)   // 兼容更多 App
                    }
                }
            }
        }
        try? p.run()
    }

    // ---- 全局快捷键（Carbon，全系统生效，无需辅助功能权限）----
    func registerHotKeys() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: OSType(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hkID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hkID)
            DispatchQueue.main.async {
                switch hkID.id {
                case 1: Recorder.shared?.toggle()
                case 2: Recorder.shared?.takeScreenshot()
                case 3: Recorder.shared?.toggleBar()
                default: break
                }
            }
            return noErr
        }, 1, &spec, nil, nil)

        register(id: 1, keyCode: recordKeyCode, mods: recordKeyMods)
        register(id: 2, keyCode: shotKeyCode,   mods: shotKeyMods)
        register(id: 3, keyCode: barKeyCode,    mods: barKeyMods)
    }

    func register(id: UInt32, keyCode: UInt32, mods: UInt32) {
        var ref: EventHotKeyRef?
        let hkID = EventHotKeyID(signature: OSType(0x52454344) /*'RECD'*/, id: id)
        RegisterEventHotKey(keyCode, mods, hkID, GetApplicationEventTarget(), 0, &ref)
    }

    // ---- 界面 ----
    func updateUI() { updateIcon(); updateBar() }

    func updateIcon() {
        guard let button = statusItem.button else { return }
        button.image = makeMenuIcon(state: state)
        button.imagePosition = .imageLeading
        button.contentTintColor = nil
        // 始终带文字，便于在菜单栏里找到
        switch state {
        case .idle:      button.title = " 录屏"
        case .recording: button.title = " " + elapsed()
        case .paused:    button.title = " 暂停 " + elapsed()
        }
    }

    // 菜单栏图标：实心圆点（待机/录制=红，暂停=橙），比细线圈醒目得多
    func makeMenuIcon(state: RecState) -> NSImage {
        let side: CGFloat = 16
        let img = NSImage(size: NSSize(width: side, height: side))
        img.lockFocus()
        let color: NSColor = state == .paused ? .systemOrange : .systemRed
        color.setFill()
        NSBezierPath(ovalIn: NSRect(x: 2, y: 2, width: side - 4, height: side - 4)).fill()
        img.unlockFocus()
        img.isTemplate = false
        return img
    }

    // 已录时长（不含暂停）
    func currentSeconds() -> Int {
        var t = recordedBefore
        if state == .recording, let s = segStart { t += Date().timeIntervalSince(s) }
        return Int(t)
    }
    func elapsed() -> String {
        let s = currentSeconds()
        return String(format: "%02d:%02d", s / 60, s % 60)
    }
    func startTimer() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.updateUI()
        }
    }
    func stopTimer() { timer?.invalidate(); timer = nil }

    // ---- 桌面控制条 ----
    func setupBar() {
        let w = NSPanel(contentRect: NSRect(x: 0, y: 0, width: barWidthFull, height: 50),
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        w.isFloatingPanel = true
        w.becomesKeyOnlyIfNeeded = true
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = true
        w.level = .statusBar
        w.isMovableByWindowBackground = true   // 拖背景即可移动
        w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        let v = TimerView(frame: NSRect(x: 0, y: 0, width: barWidthFull, height: 50))
        v.autoresizingMask = [.width, .height]
        w.contentView = v
        barView = v

        btnStart = makeBtn("开始", #selector(start))
        btnPause = makeBtn("暂停", #selector(pauseResume))
        btnStop  = makeBtn("结束", #selector(stop))
        btnCollapse = makeBtn("▾", #selector(toggleCollapse))   // 缩小/展开
        btnClose = makeBtn("✕", #selector(hideBar))             // 隐藏
        for b in [btnStart!, btnPause!, btnStop!, btnCollapse!, btnClose!] { v.addSubview(b) }

        bar = w
        applyCollapse(barCollapsed)   // 按记住的状态排布
        positionBar()
        updateBar()
    }

    func makeBtn(_ title: String, _ sel: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: sel)
        b.bezelStyle = .rounded
        b.font = .systemFont(ofSize: 12)
        return b
    }

    // 收起 = 只剩红点+计时；展开 = 显示三个控制按钮
    func applyCollapse(_ c: Bool) {
        guard let w = bar else { return }
        let newW = c ? barWidthMin : barWidthFull
        let f = w.frame
        // 保持右上角不动地改变宽度
        w.setFrame(NSRect(x: f.maxX - newW, y: f.origin.y, width: newW, height: f.height),
                   display: true)
        btnStart?.frame = NSRect(x: 92, y: 10, width: 52, height: 30)
        btnPause?.frame = NSRect(x: 148, y: 10, width: 52, height: 30)
        btnStop?.frame  = NSRect(x: 204, y: 10, width: 52, height: 30)
        btnClose?.frame = NSRect(x: barWidthFull - 38, y: 13, width: 28, height: 24)
        btnStart?.isHidden = c
        btnPause?.isHidden = c
        btnStop?.isHidden = c
        btnClose?.isHidden = c
        // 折叠按钮：折叠时贴在计时后面，展开时在右侧
        btnCollapse?.frame = c ? NSRect(x: 84, y: 13, width: 30, height: 24)
                               : NSRect(x: barWidthFull - 72, y: 13, width: 28, height: 24)
        btnCollapse?.title = c ? "▸" : "▾"
        barView?.needsDisplay = true
    }

    @objc func toggleCollapse() {
        barCollapsed.toggle()
        applyCollapse(barCollapsed)
    }

    @objc func hideBar() {
        showBar = false
        applyBarVisibility()
    }

    func positionBar() {
        guard let w = bar, let scr = NSScreen.main else { return }
        let f = scr.visibleFrame
        w.setFrameOrigin(NSPoint(x: f.maxX - w.frame.width - 16,
                                 y: f.maxY - w.frame.height - 12))
    }

    func applyBarVisibility() {
        if showBar { bar?.orderFrontRegardless() } else { bar?.orderOut(nil) }
    }

    @objc func toggleBar() {
        if showBar {
            showBar = false
            applyBarVisibility()
        } else {
            showBarNow()
        }
    }

    func updateBar() {
        guard let v = barView else { return }
        v.state = state
        v.text = state == .idle ? "00:00" : elapsed()
        v.needsDisplay = true
        btnStart?.isEnabled = (state == .idle)
        btnPause?.isEnabled = (state != .idle)
        btnPause?.title = (state == .paused) ? "继续" : "暂停"
        btnStop?.isEnabled = (state != .idle)
    }

    func timestamp() -> String {
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        return fmt.string(from: Date())
    }

    @objc func openRecordFolder() { NSWorkspace.shared.open(URL(fileURLWithPath: recordDir)) }
    @objc func openShotFolder()   { NSWorkspace.shared.open(URL(fileURLWithPath: shotDir)) }
    @objc func revealLast() {
        if let f = lastFile {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: f)])
        }
    }
    @objc func quit() {
        if state != .idle {
            if state == .recording {
                recordedBefore += Date().timeIntervalSince(segStart ?? Date())
            }
            state = .idle
            let p = process; let sp = currentSegPath; let out = finalPath
            process = nil; currentSegPath = nil; finalPath = nil
            // 同步收尾并合并（recQ 串行，先处理完之前暂停的片段）
            recQ.sync {
                if let p = p { kill(p.processIdentifier, SIGINT); p.waitUntilExit() }
                if let sp = sp, FileManager.default.fileExists(atPath: sp) { segments.append(sp) }
                let all = segments; segments = []
                finalize(segments: all, to: out)
            }
        }
        NSApp.terminate(nil)
    }
}

// 桌面控制条视图：圆角半透明底 + 状态点 + 计时文字（按钮另加）
final class TimerView: NSView {
    var text = "00:00"
    var state: Recorder.RecState = .idle

    override func draw(_ dirtyRect: NSRect) {
        let bg = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1),
                              xRadius: 12, yRadius: 12)
        NSColor(white: 0.11, alpha: 0.92).setFill()
        bg.fill()
        // 状态点：录制=红 / 暂停=橙 / 待机=灰
        let color: NSColor = state == .recording ? .systemRed
                           : (state == .paused ? .systemOrange : .systemGray)
        let r: CGFloat = 9
        color.setFill()
        NSBezierPath(ovalIn: NSRect(x: 16, y: bounds.midY - r / 2, width: r, height: r)).fill()
        // 计时文字
        let attrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: NSColor.white,
            .font: NSFont.monospacedDigitSystemFont(ofSize: 18, weight: .semibold)]
        let s = NSAttributedString(string: text, attributes: attrs)
        s.draw(at: NSPoint(x: 34, y: bounds.midY - s.size().height / 2))
    }
}

let app = NSApplication.shared
let delegate = Recorder()
app.delegate = delegate
app.run()

// ===== 常用虚拟键码对照（改快捷键时用）=====
// A=0x00 S=0x01 D=0x02 F=0x03 H=0x04 G=0x05 Z=0x06 X=0x07 C=0x08 V=0x09
// B=0x0B Q=0x0C W=0x0D E=0x0E R=0x0F Y=0x10 T=0x11
// 1=0x12 2=0x13 3=0x14 4=0x15 6=0x16 5=0x17 9=0x19 7=0x1A 8=0x1C 0=0x1D
// O=0x1F U=0x20 I=0x22 P=0x23 L=0x25 J=0x26 K=0x28 N=0x2D M=0x2E 空格=0x31
// 修饰键：cmdKey / shiftKey / optionKey / controlKey，可用 | 组合
