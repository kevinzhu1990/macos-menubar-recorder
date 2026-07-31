import CoreGraphics
import Foundation

struct RecordingPixelSize: Equatable {
    let width: Int
    let height: Int
}

enum RecordingGeometry {
    static func captureRect(
        selectionInGlobalPoints selection: CGRect,
        screenFrame: CGRect,
        minimumSize: CGFloat
    ) -> CGRect? {
        let clipped = selection.standardized.intersection(screenFrame)
        guard !clipped.isNull,
              clipped.width >= minimumSize,
              clipped.height >= minimumSize else {
            return nil
        }

        return CGRect(
            x: clipped.minX - screenFrame.minX,
            y: screenFrame.maxY - clipped.maxY,
            width: clipped.width,
            height: clipped.height
        )
    }

    static func pixelSize(
        captureRect: CGRect,
        scaleFactor: CGFloat
    ) -> RecordingPixelSize {
        let safeScale = max(scaleFactor, 1)
        return RecordingPixelSize(
            width: evenPixelDimension(captureRect.width * safeScale),
            height: evenPixelDimension(captureRect.height * safeScale)
        )
    }

    private static func evenPixelDimension(_ value: CGFloat) -> Int {
        let rounded = max(Int(value.rounded(.down)), 2)
        return rounded.isMultiple(of: 2) ? rounded : rounded - 1
    }
}
