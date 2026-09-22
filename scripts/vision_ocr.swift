import Foundation
import ImageIO
import Vision

struct RecognizedLine: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
}

guard CommandLine.arguments.count == 2 else {
    fputs("usage: vision_ocr.swift IMAGE\n", stderr)
    exit(2)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("unable to decode image\n", stderr)
    exit(3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["en-US"]

do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let lines = (request.results ?? []).compactMap { observation -> RecognizedLine? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        return RecognizedLine(
            text: candidate.string,
            confidence: candidate.confidence,
            x: observation.boundingBox.minX,
            y: observation.boundingBox.maxY
        )
    }.sorted { left, right in
        abs(left.y - right.y) > 0.015 ? left.y > right.y : left.x < right.x
    }
    let data = try JSONEncoder().encode(lines)
    FileHandle.standardOutput.write(data)
} catch {
    fputs("Vision OCR failed: \(error.localizedDescription)\n", stderr)
    exit(4)
}
