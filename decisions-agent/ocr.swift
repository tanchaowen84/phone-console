import Foundation
import Vision
import ImageIO

// Local screenshot text boxes; no Accessibility or device connection.
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = false
try VNImageRequestHandler(url: url, options: [:]).perform([request])
let rows: [[String: Any]] = (request.results ?? []).compactMap { item in
    guard let text = item.topCandidates(1).first else { return nil }
    let r = item.boundingBox
    return ["text": text.string, "confidence": text.confidence,
            "rect": [r.minX, 1-r.maxY, r.width, r.height]]
}
let data = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
