import AppKit
import AVFoundation
import Foundation
import Carbon.HIToolbox
import ScreenCaptureKit

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

final class EventLogger {
    static let shared = EventLogger()
    private let queue = DispatchQueue(label: "recorder.event-log")
    private let logURL: URL

    private init() {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/录屏助手", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        logURL = dir.appendingPathComponent("events.jsonl")
    }

    func write(_ event: String, details: [String: String] = [:]) {
        queue.async {
            var payload = details
            payload["event"] = event
            payload["time"] = ISO8601DateFormatter().string(from: Date())
            guard JSONSerialization.isValidJSONObject(payload),
                  let data = try? JSONSerialization.data(withJSONObject: payload),
                  var line = String(data: data, encoding: .utf8) else { return }
            line.append("\n")
            guard let bytes = line.data(using: .utf8) else { return }
            if !FileManager.default.fileExists(atPath: self.logURL.path) {
                try? bytes.write(to: self.logURL, options: .atomic)
                return
            }
            guard let handle = try? FileHandle(forWritingTo: self.logURL) else { return }
            defer { try? handle.close() }
            do {
                try handle.seekToEnd()
                try handle.write(contentsOf: bytes)
            } catch {
                return
            }
        }
    }
}

final class RegionSelectionView: NSView {
    var completion: ((CGRect?) -> Void)?
    private var startPoint: CGPoint?
    private var currentPoint: CGPoint?
    private let minimumSize: CGFloat = 20

    override var acceptsFirstResponder: Bool { true }

    override func mouseDown(with event: NSEvent) {
        startPoint = convert(event.locationInWindow, from: nil)
        currentPoint = startPoint
        needsDisplay = true
    }

    override func mouseDragged(with event: NSEvent) {
        currentPoint = convert(event.locationInWindow, from: nil)
        needsDisplay = true
    }

    override func mouseUp(with event: NSEvent) {
        currentPoint = convert(event.locationInWindow, from: nil)
        let rect = selectionRect()
        guard rect.width >= minimumSize, rect.height >= minimumSize else {
            startPoint = nil
            currentPoint = nil
            needsDisplay = true
            NSSound.beep()
            return
        }
        completion?(rect)
    }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 {
            completion?(nil)
        } else {
            super.keyDown(with: event)
        }
    }

    private func selectionRect() -> CGRect {
        guard let a = startPoint, let b = currentPoint else { return .zero }
        return CGRect(x: min(a.x, b.x), y: min(a.y, b.y),
                      width: abs(a.x - b.x), height: abs(a.y - b.y))
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor(calibratedWhite: 0, alpha: 0.42).setFill()
        bounds.fill()

        let title = "拖拽选择录屏区域 · Esc 取消"
        let attrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: NSColor.white,
            .font: NSFont.systemFont(ofSize: 18, weight: .semibold)
        ]
        let size = title.size(withAttributes: attrs)
        title.draw(at: CGPoint(x: bounds.midX - size.width / 2,
                               y: bounds.maxY - 54), withAttributes: attrs)

        let rect = selectionRect()
        guard !rect.isEmpty else { return }
        NSGraphicsContext.current?.saveGraphicsState()
        NSColor.clear.setFill()
        rect.fill(using: .copy)
        NSColor.systemCyan.setStroke()
        let border = NSBezierPath(rect: rect)
        border.lineWidth = 3
        border.stroke()
        NSGraphicsContext.current?.restoreGraphicsState()

        let label = "\(Int(rect.width)) × \(Int(rect.height))"
        let labelAttrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: NSColor.white,
            .backgroundColor: NSColor(calibratedWhite: 0.08, alpha: 0.82),
            .font: NSFont.monospacedDigitSystemFont(ofSize: 14, weight: .semibold)
        ]
        label.draw(at: CGPoint(x: rect.minX + 8,
                               y: max(rect.minY - 24, 8)), withAttributes: labelAttrs)
    }
}

final class RegionSelectionPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

final class RegionSelector {
    func select(on screen: NSScreen) -> CGRect? {
        let panel = RegionSelectionPanel(
            contentRect: screen.frame, styleMask: [.borderless],
            backing: .buffered, defer: false)
        panel.level = .screenSaver
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let view = RegionSelectionView(frame: CGRect(origin: .zero, size: screen.frame.size))
        panel.contentView = view

        var result: CGRect?
        view.completion = { localRect in
            if let localRect {
                result = localRect.offsetBy(dx: screen.frame.minX, dy: screen.frame.minY)
                NSApp.stopModal(withCode: .OK)
            } else {
                NSApp.stopModal(withCode: .cancel)
            }
        }

        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(view)
        _ = NSApp.runModal(for: panel)
        panel.orderOut(nil)
        return result
    }
}

@available(macOS 15.0, *)
final class NativeCaptureSegment: NSObject, SCRecordingOutputDelegate, SCStreamDelegate {
    let path: String
    private var stream: SCStream?
    private var recordingOutput: SCRecordingOutput?
    private var finishHandler: ((Result<String, Error>) -> Void)?
    private let finishLock = NSLock()
    private var finished = false
    private var pendingResult: Result<String, Error>?

    init(path: String) {
        self.path = path
    }

    func start(
        displayID: CGDirectDisplayID,
        captureRect: CGRect?,
        pixelSize: RecordingPixelSize,
        systemAudio: Bool,
        microphone: Bool,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        SCShareableContent.getExcludingDesktopWindows(
            false, onScreenWindowsOnly: true
        ) { [weak self] content, error in
            guard let self else { return }
            if let error {
                DispatchQueue.main.async { completion(.failure(error)) }
                return
            }
            guard let display = content?.displays.first(where: {
                $0.displayID == displayID
            }) else {
                let e = NSError(domain: "录屏助手", code: 1,
                                userInfo: [NSLocalizedDescriptionKey: "未找到要录制的显示器"])
                DispatchQueue.main.async { completion(.failure(e)) }
                return
            }

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.width = pixelSize.width
            config.height = pixelSize.height
            if let captureRect { config.sourceRect = captureRect }
            config.minimumFrameInterval = CMTime(value: 1, timescale: 30)
            config.queueDepth = 6
            config.pixelFormat = kCVPixelFormatType_32BGRA
            config.showsCursor = true
            config.showMouseClicks = true
            config.capturesAudio = systemAudio
            config.excludesCurrentProcessAudio = true
            config.sampleRate = 48_000
            config.channelCount = 2
            config.captureMicrophone = microphone

            let outputConfig = SCRecordingOutputConfiguration()
            outputConfig.outputURL = URL(fileURLWithPath: self.path)
            outputConfig.outputFileType = .mp4
            outputConfig.videoCodecType = .h264
            let output = SCRecordingOutput(
                configuration: outputConfig, delegate: self)
            let stream = SCStream(filter: filter, configuration: config, delegate: self)
            do {
                try stream.addRecordingOutput(output)
            } catch {
                DispatchQueue.main.async { completion(.failure(error)) }
                return
            }
            self.recordingOutput = output
            self.stream = stream
            stream.startCapture { error in
                DispatchQueue.main.async {
                    if let error {
                        completion(.failure(error))
                    } else {
                        completion(.success(()))
                    }
                }
            }
        }
    }

    func stop(completion: @escaping (Result<String, Error>) -> Void) {
        finishLock.lock()
        finishHandler = completion
        let pending = pendingResult
        pendingResult = nil
        finishLock.unlock()
        if let pending {
            finish(pending)
            return
        }
        guard let stream else {
            finish(.success(path))
            return
        }
        stream.stopCapture { [weak self] error in
            guard let self else { return }
            if let error { self.finish(.failure(error)) }
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) { [weak self] in
                guard let self, !self.finished else { return }
                if FileManager.default.fileExists(atPath: self.path) {
                    self.finish(.success(self.path))
                } else {
                    let e = NSError(
                        domain: "录屏助手", code: 2,
                        userInfo: [NSLocalizedDescriptionKey: "录屏文件未生成"])
                    self.finish(.failure(e))
                }
            }
        }
    }

    func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        finish(.success(path))
    }

    func recordingOutput(
        _ recordingOutput: SCRecordingOutput,
        didFailWithError error: Error
    ) {
        finish(.failure(error))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        finish(.failure(error))
    }

    private func finish(_ result: Result<String, Error>) {
        finishLock.lock()
        guard !finished else {
            finishLock.unlock()
            return
        }
        guard let handler = finishHandler else {
            pendingResult = result
            finishLock.unlock()
            return
        }
        finished = true
        finishHandler = nil
        finishLock.unlock()
        handler(result)
    }
}

final class Recorder: NSObject, NSApplicationDelegate {
    static var shared: Recorder?

    enum RecState { case idle, recording, paused }
    var state: RecState = .idle
    var isRecording: Bool { state != .idle }   // 兼容旧判断
    var isStarting = false

    var statusItem: NSStatusItem!
    var process: Process?            // macOS 13/14 兼容路径的 screencapture 进程
    var nativeSegment: AnyObject?    // macOS 15+ 的 NativeCaptureSegment
    var currentSegPath: String?      // 当前片段临时文件
    var currentSegmentIndex = 0
    var nextSegmentIndex = 0
    var segments: [(index: Int, path: String)] = [] // 仅在 recQ 上读写
    let pendingSegments = DispatchGroup()
    var finalPath: String?           // 最终输出文件
    var recordedBefore: TimeInterval = 0  // 已完成片段累计时长（不含暂停）
    var segStart: Date?              // 当前片段开始时刻
    var segUseMic = false            // 本次录制是否含麦克风（整段固定，保证 concat 一致）
    var segUseSystemAudio = false
    var selectedScreenID: CGDirectDisplayID?
    var selectedScreenFrame: CGRect?
    var selectedScreenScale: CGFloat = 1
    var selectedCaptureRect: CGRect?
    let recQ = DispatchQueue(label: "rec.segments")  // 串行处理片段收尾/合并
    var timer: Timer?
    var lastFile: String?
    // 是否录制麦克风（开关状态记住，重启后保留）。默认开。
    var recordMic: Bool {
        get { UserDefaults.standard.object(forKey: "recordMic") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "recordMic") }
    }
    // macOS 15+ 由 ScreenCaptureKit 原生录制电脑内部声音。
    var recordSystemAudio: Bool {
        get { UserDefaults.standard.object(forKey: "recordSystemAudio") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "recordSystemAudio") }
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
    var btnFullscreen: NSButton?
    var btnPause: NSButton?
    var btnStop: NSButton?
    var btnCollapse: NSButton?
    var btnClose: NSButton?
    // 控制条是否收起（只剩红点+计时）。记住状态。
    var barCollapsed: Bool {
        get { UserDefaults.standard.bool(forKey: "barCollapsed") }
        set { UserDefaults.standard.set(newValue, forKey: "barCollapsed") }
    }
    let barWidthFull: CGFloat = 410
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
        menu.addItem(withTitle: isRecording ? "结束录制  ⌃R" : "选区录屏  ⌃R",
                     action: #selector(toggle), keyEquivalent: "")
        if !isRecording {
            menu.addItem(withTitle: "全屏录制",
                         action: #selector(startFullscreen), keyEquivalent: "")
        }
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
        let audioItem = NSMenuItem(title: "录制电脑内部声音（macOS 15+）",
                                   action: #selector(toggleSystemAudio), keyEquivalent: "")
        audioItem.state = recordSystemAudio ? .on : .off
        menu.addItem(audioItem)
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
    @objc func toggle() {
        if isRecording {
            stop()
        } else if !isStarting {
            start()
        }
    }

    @objc func toggleMic() { recordMic.toggle() }
    @objc func toggleSystemAudio() {
        if #available(macOS 15.0, *) {
            recordSystemAudio.toggle()
        } else {
            recordSystemAudio = false
            alert("当前系统不支持原生内录",
                  "电脑内部声音需要 macOS 15 或更高版本。当前系统仍可录制画面和麦克风。")
        }
    }

    // 选区开始（idle → recording）
    @objc func start() {
        guard state == .idle, !isStarting else { return }
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { $0.frame.contains(mouse) })
            ?? NSScreen.main
        guard let screen,
              let selection = RegionSelector().select(on: screen),
              let rect = RecordingGeometry.captureRect(
                selectionInGlobalPoints: selection,
                screenFrame: screen.frame,
                minimumSize: 20) else { return }
        beginRecording(on: screen, captureRect: rect)
    }

    @objc func startFullscreen() {
        guard state == .idle, !isStarting, let screen = NSScreen.main else { return }
        beginRecording(on: screen, captureRect: nil)
    }

    func beginRecording(on screen: NSScreen, captureRect: CGRect?) {
        guard let displayID = displayID(for: screen) else {
            alert("无法开始录制", "未能识别当前显示器，请重新连接显示器后再试。")
            return
        }
        selectedScreenID = displayID
        selectedScreenFrame = screen.frame
        selectedScreenScale = screen.backingScaleFactor
        selectedCaptureRect = captureRect
        segUseMic = recordMic
        if #available(macOS 15.0, *) {
            segUseSystemAudio = recordSystemAudio
            finalPath = "\(recordDir)/录屏_\(timestamp()).mp4"
        } else {
            segUseSystemAudio = false
            finalPath = "\(recordDir)/录屏_\(timestamp()).mov"
        }
        recordedBefore = 0
        nextSegmentIndex = 0
        recQ.sync { segments = [] }
        isStarting = true
        updateUI()
        launchSegment { [weak self] result in
            guard let self else { return }
            self.isStarting = false
            switch result {
            case .success:
                self.state = .recording
                self.startTimer()
                EventLogger.shared.write("recording_started", details: [
                    "mode": captureRect == nil ? "fullscreen" : "region",
                    "system_audio": String(self.segUseSystemAudio),
                    "microphone": String(self.segUseMic)
                ])
            case .failure(let error):
                self.clearRecordingSelection()
                self.finalPath = nil
                EventLogger.shared.write("recording_start_failed",
                                         details: ["error": error.localizedDescription])
                self.alert("无法开始录制",
                           "\(error.localizedDescription)\n\n请检查“系统设置 → 隐私与安全性 → 屏幕与系统音频录制”。")
            }
            self.updateUI()
        }
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
            guard !isStarting else { return }
            isStarting = true
            updateUI()
            launchSegment { [weak self] result in
                guard let self else { return }
                self.isStarting = false
                switch result {
                case .success:
                    self.state = .recording
                    EventLogger.shared.write("recording_resumed")
                case .failure(let error):
                    EventLogger.shared.write("recording_resume_failed",
                                             details: ["error": error.localizedDescription])
                    self.alert("无法继续录制", error.localizedDescription)
                }
                self.updateUI()
            }
        case .idle:
            break
        }
    }

    // 结束（recording/paused → idle）：收尾 + 合并所有片段
    @objc func stop() {
        guard state != .idle, !isStarting else { return }
        if state == .recording {
            recordedBefore += Date().timeIntervalSince(segStart ?? Date())
        }
        segStart = nil
        state = .idle
        isStarting = true // 合并完成前不允许开始下一次，避免片段串台
        stopTimer()
        updateUI()
        let out = finalPath
        finalPath = nil
        stopCurrentSegment()
        pendingSegments.notify(queue: recQ) { [weak self] in
            guard let self else { return }
            let all = self.segments.sorted { $0.index < $1.index }.map(\.path)
            self.segments = []
            let completedOutput = self.finalize(segments: all, to: out)
            DispatchQueue.main.async {
                self.isStarting = false
                self.clearRecordingSelection()
                self.updateUI()
                if let completedOutput {
                    self.lastFile = completedOutput
                    EventLogger.shared.write("recording_finished", details: [
                        "output": completedOutput,
                        "segments": String(all.count)
                    ])
                }
            }
        }
    }

    func displayID(for screen: NSScreen) -> CGDirectDisplayID? {
        let key = NSDeviceDescriptionKey("NSScreenNumber")
        return (screen.deviceDescription[key] as? NSNumber)?.uint32Value
    }

    func clearRecordingSelection() {
        selectedScreenID = nil
        selectedScreenFrame = nil
        selectedCaptureRect = nil
        nativeSegment = nil
        process = nil
        currentSegPath = nil
    }

    // 启动一个录制片段。macOS 15+ 用 ScreenCaptureKit 原生内录；
    // macOS 13/14 保留 screencapture 兼容路径（画面 + 麦克风）。
    func launchSegment(completion: @escaping (Result<Void, Error>) -> Void) {
        guard let displayID = selectedScreenID, let screenFrame = selectedScreenFrame else {
            let error = NSError(domain: "录屏助手", code: 3,
                                userInfo: [NSLocalizedDescriptionKey: "录制区域已经失效"])
            completion(.failure(error))
            return
        }
        let index = nextSegmentIndex
        nextSegmentIndex += 1
        currentSegmentIndex = index

        if #available(macOS 15.0, *) {
            let seg = "\(recordDir)/.seg_\(UUID().uuidString).mp4"
            let rect = selectedCaptureRect ?? CGRect(
                origin: .zero, size: screenFrame.size)
            let pixels = RecordingGeometry.pixelSize(
                captureRect: rect, scaleFactor: selectedScreenScale)
            let capture = NativeCaptureSegment(path: seg)
            nativeSegment = capture
            currentSegPath = seg
            capture.start(
                displayID: displayID,
                captureRect: selectedCaptureRect,
                pixelSize: pixels,
                systemAudio: segUseSystemAudio,
                microphone: segUseMic,
                completion: { [weak self] result in
                    guard let self else { return }
                    if case .success = result {
                        self.segStart = Date()
                    } else {
                        self.nativeSegment = nil
                        self.currentSegPath = nil
                        try? FileManager.default.removeItem(atPath: seg)
                    }
                    completion(result)
                })
            return
        }

        let seg = "\(recordDir)/.seg_\(UUID().uuidString).mov"
        var args = ["-v", "-x"]            // -v 录视频  -x 不播放提示音
        if segUseMic { args.append("-g") } // -g 同时录麦克风
        if let rect = selectedCaptureRect {
            args.append("-R\(Int(rect.minX)),\(Int(rect.minY)),\(Int(rect.width)),\(Int(rect.height))")
        }
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
                let index = self.currentSegmentIndex
                self.recordedBefore += Date().timeIntervalSince(self.segStart ?? Date())
                self.recQ.async {
                    if let sp = sp, FileManager.default.fileExists(atPath: sp) {
                        self.segments.append((index, sp))
                    }
                }
                self.process = nil
                self.currentSegPath = nil
                self.launchSegment { result in
                    if case .failure(let error) = result {
                        self.stop()
                        self.alert("录制意外中断", error.localizedDescription)
                    }
                }
            }
        }
        do {
            try p.run()
        } catch {
            completion(.failure(error))
            return
        }
        process = p
        currentSegPath = seg
        segStart = Date()
        completion(.success(()))
    }

    // 结束当前片段并按创建顺序入列。
    func stopCurrentSegment() {
        let index = currentSegmentIndex
        let sp = currentSegPath
        currentSegPath = nil

        if #available(macOS 15.0, *),
           let capture = nativeSegment as? NativeCaptureSegment {
            nativeSegment = nil
            pendingSegments.enter()
            capture.stop { [weak self] result in
                guard let self else { return }
                self.recQ.async {
                    switch result {
                    case .success(let path):
                        if FileManager.default.fileExists(atPath: path) {
                            self.segments.append((index, path))
                        }
                    case .failure(let error):
                        EventLogger.shared.write("segment_finish_failed",
                                                 details: ["error": error.localizedDescription])
                    }
                    self.pendingSegments.leave()
                }
            }
            return
        }

        let p = process
        process = nil
        guard p != nil || sp != nil else { return }
        pendingSegments.enter()
        recQ.async { [weak self] in
            if let p = p { kill(p.processIdentifier, SIGINT); p.waitUntilExit() }
            if let sp = sp, FileManager.default.fileExists(atPath: sp) {
                self?.segments.append((index, sp))
            }
            self?.pendingSegments.leave()
        }
    }

    // 合并片段为最终文件（单段直接改名，多段用 ffmpeg concat 无损拼接）
    @discardableResult
    func finalize(segments segs: [String], to out: String?) -> String? {
        guard let out = out, !segs.isEmpty else { return nil }
        try? FileManager.default.removeItem(atPath: out)
        if segs.count == 1 {
            do {
                try FileManager.default.moveItem(atPath: segs[0], toPath: out)
            } catch {
                EventLogger.shared.write("recording_move_failed",
                                         details: ["error": error.localizedDescription])
            }
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
            if p.terminationStatus == 0,
               FileManager.default.fileExists(atPath: out) {
                for s in segs { try? FileManager.default.removeItem(atPath: s) }
            }
        }

        if FileManager.default.fileExists(atPath: out) {
            return out
        }

        let base = (out as NSString).deletingPathExtension
        let ext = (segs.first! as NSString).pathExtension
        var preserved: [String] = []
        for (offset, source) in segs.enumerated()
            where FileManager.default.fileExists(atPath: source) {
            let visible = "\(base)_第\(offset + 1)段.\(ext)"
            try? FileManager.default.removeItem(atPath: visible)
            do {
                try FileManager.default.moveItem(atPath: source, toPath: visible)
                preserved.append(visible)
            } catch {
                preserved.append(source)
            }
        }
        EventLogger.shared.write("recording_merge_failed", details: [
            "preserved_segments": preserved.joined(separator: "|")
        ])
        DispatchQueue.main.async { [weak self] in
            self?.alert("录屏片段未能合并",
                        "请确认已安装 ffmpeg（brew install ffmpeg）。片段已经保留，没有删除：\n\(preserved.joined(separator: "\n"))")
        }
        return preserved.first
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

        btnStart = makeBtn("选区", #selector(start))
        btnFullscreen = makeBtn("全屏", #selector(startFullscreen))
        btnPause = makeBtn("暂停", #selector(pauseResume))
        btnStop  = makeBtn("结束", #selector(stop))
        btnCollapse = makeBtn("▾", #selector(toggleCollapse))   // 缩小/展开
        btnClose = makeBtn("✕", #selector(hideBar))             // 隐藏
        for b in [btnStart!, btnFullscreen!, btnPause!, btnStop!,
                  btnCollapse!, btnClose!] { v.addSubview(b) }

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
        btnStart?.frame = NSRect(x: 92, y: 10, width: 56, height: 30)
        btnFullscreen?.frame = NSRect(x: 152, y: 10, width: 56, height: 30)
        btnPause?.frame = NSRect(x: 212, y: 10, width: 52, height: 30)
        btnStop?.frame  = NSRect(x: 268, y: 10, width: 52, height: 30)
        btnClose?.frame = NSRect(x: barWidthFull - 38, y: 13, width: 28, height: 24)
        btnStart?.isHidden = c
        btnFullscreen?.isHidden = c
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
        btnStart?.isEnabled = (state == .idle && !isStarting)
        btnFullscreen?.isEnabled = (state == .idle && !isStarting)
        btnPause?.isEnabled = (state != .idle && !isStarting)
        btnPause?.title = (state == .paused) ? "继续" : "暂停"
        btnStop?.isEnabled = (state != .idle && !isStarting)
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
            stop()
            terminateWhenFinalized(deadline: Date().addingTimeInterval(20))
            return
        }
        if isStarting {
            alert("正在处理录屏", "请等待当前录屏开始或文件合并完成后再退出。")
            return
        }
        NSApp.terminate(nil)
    }

    func terminateWhenFinalized(deadline: Date) {
        if !isStarting || Date() >= deadline {
            NSApp.terminate(nil)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
            self?.terminateWhenFinalized(deadline: deadline)
        }
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

@main
enum RecorderApp {
    static func main() {
        let app = NSApplication.shared
        let delegate = Recorder()
        app.delegate = delegate
        app.run()
    }
}

// ===== 常用虚拟键码对照（改快捷键时用）=====
// A=0x00 S=0x01 D=0x02 F=0x03 H=0x04 G=0x05 Z=0x06 X=0x07 C=0x08 V=0x09
// B=0x0B Q=0x0C W=0x0D E=0x0E R=0x0F Y=0x10 T=0x11
// 1=0x12 2=0x13 3=0x14 4=0x15 6=0x16 5=0x17 9=0x19 7=0x1A 8=0x1C 0=0x1D
// O=0x1F U=0x20 I=0x22 P=0x23 L=0x25 J=0x26 K=0x28 N=0x2D M=0x2E 空格=0x31
// 修饰键：cmdKey / shiftKey / optionKey / controlKey，可用 | 组合
