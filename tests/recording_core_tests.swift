import AppKit
import Foundation

private var failures = 0

private func expectEqual<T: Equatable>(
    _ actual: T,
    _ expected: T,
    _ message: String
) {
    if actual != expected {
        failures += 1
        fputs("FAIL: \(message) — expected \(expected), got \(actual)\n", stderr)
    }
}

private func expectNil<T>(_ actual: T?, _ message: String) {
    if actual != nil {
        failures += 1
        fputs("FAIL: \(message) — expected nil, got \(String(describing: actual))\n", stderr)
    }
}

private func testSelectionConvertsFromCocoaToDisplayCoordinates() {
    let screen = CGRect(x: 0, y: 0, width: 1440, height: 900)
    let selection = CGRect(x: 100, y: 200, width: 600, height: 300)

    let result = RecordingGeometry.captureRect(
        selectionInGlobalPoints: selection,
        screenFrame: screen,
        minimumSize: 20
    )

    expectEqual(result, CGRect(x: 100, y: 400, width: 600, height: 300),
                "Cocoa bottom-left coordinates must become ScreenCaptureKit top-left coordinates")
}

private func testSelectionIsClippedToTheChosenDisplay() {
    let screen = CGRect(x: 1440, y: -100, width: 1920, height: 1080)
    let selection = CGRect(x: 1300, y: -200, width: 300, height: 400)

    let result = RecordingGeometry.captureRect(
        selectionInGlobalPoints: selection,
        screenFrame: screen,
        minimumSize: 20
    )

    expectEqual(result, CGRect(x: 0, y: 780, width: 160, height: 300),
                "Selections extending outside a display must be clipped")
}

private func testTinySelectionIsRejected() {
    let result = RecordingGeometry.captureRect(
        selectionInGlobalPoints: CGRect(x: 10, y: 10, width: 19, height: 200),
        screenFrame: CGRect(x: 0, y: 0, width: 1440, height: 900),
        minimumSize: 20
    )

    expectNil(result, "A selection smaller than the minimum width must be rejected")
}

private func testPixelSizeUsesScaleAndEvenDimensions() {
    let result = RecordingGeometry.pixelSize(
        captureRect: CGRect(x: 0, y: 0, width: 301, height: 199),
        scaleFactor: 2
    )

    expectEqual(result, RecordingPixelSize(width: 602, height: 398),
                "Retina point sizes must be converted to even pixel dimensions")
}

private func testPixelSizeNeverDropsBelowTwoPixels() {
    let result = RecordingGeometry.pixelSize(
        captureRect: CGRect(x: 0, y: 0, width: 0.2, height: 0.2),
        scaleFactor: 1
    )

    expectEqual(result, RecordingPixelSize(width: 2, height: 2),
                "Video dimensions must remain valid for H.264")
}

@main
private enum TestRunner {
    static func main() {
        testSelectionConvertsFromCocoaToDisplayCoordinates()
        testSelectionIsClippedToTheChosenDisplay()
        testTinySelectionIsRejected()
        testPixelSizeUsesScaleAndEvenDimensions()
        testPixelSizeNeverDropsBelowTwoPixels()

        if failures > 0 {
            exit(1)
        }

        print("PASS: 5 recording core tests")
    }
}
