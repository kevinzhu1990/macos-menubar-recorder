import AppKit

// 用法: swift make-icon.swift <输出的 .iconset 目录>
// 自绘 App 图标：深色圆角底 + 红色录制环 + 中心红点。

func render(pixels: Int, to path: String) {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
        isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

    let s = CGFloat(pixels)
    // 圆角底（深色渐变）
    let pad = s * 0.06
    let bgRect = NSRect(x: pad, y: pad, width: s - 2 * pad, height: s - 2 * pad)
    let bg = NSBezierPath(roundedRect: bgRect, xRadius: s * 0.22, yRadius: s * 0.22)
    let grad = NSGradient(colors: [
        NSColor(red: 0.20, green: 0.21, blue: 0.26, alpha: 1),
        NSColor(red: 0.06, green: 0.06, blue: 0.09, alpha: 1)])!
    grad.draw(in: bg, angle: -90)

    // 红色录制环
    let cx = s / 2, cy = s / 2
    let r = s * 0.28
    let ring = NSBezierPath(ovalIn: NSRect(x: cx - r, y: cy - r, width: 2 * r, height: 2 * r))
    ring.lineWidth = s * 0.055
    NSColor.systemRed.setStroke()
    ring.stroke()

    // 中心红点
    let rr = s * 0.15
    NSColor.systemRed.setFill()
    NSBezierPath(ovalIn: NSRect(x: cx - rr, y: cy - rr, width: 2 * rr, height: 2 * rr)).fill()

    NSGraphicsContext.restoreGraphicsState()
    let png = rep.representation(using: .png, properties: [:])!
    try? png.write(to: URL(fileURLWithPath: path))
}

let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.iconset"
try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

// iconset 要求的标准文件名 -> 像素尺寸
let specs: [(String, Int)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]
for (name, px) in specs { render(pixels: px, to: "\(outDir)/\(name)") }
print("图标 PNG 已生成到 \(outDir)")
