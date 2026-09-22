// ocr_vision.swift — macOS 原生 Vision OCR（本地、免费、离线、支持中英）
// 用法: ocr_vision <img1> [img2 ...]    输出: <path>\t<text 用 " | " 连接>\n
// 编译: swiftc -O -o ocr_vision ocr_vision.swift
import Foundation
import Vision
import AppKit

let paths = Array(CommandLine.arguments.dropFirst())
if paths.isEmpty { FileHandle.standardError.write("usage: ocr_vision <img...>\n".data(using:.utf8)!); exit(2) }

for path in paths {
    guard let img = NSImage(contentsOfFile: path),
          let tiff = img.tiffRepresentation,
          let bmp = NSBitmapImageRep(data: tiff),
          let cg = bmp.cgImage else {
        print("\(path)\t")
        continue
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([req]) } catch { print("\(path)\t"); continue }
    var lines: [String] = []
    for obs in (req.results ?? []) {
        if let c = obs.topCandidates(1).first { lines.append(c.string) }
    }
    let text = lines.joined(separator: " | ").replacingOccurrences(of: "\t", with: " ")
    print("\(path)\t\(text)")
}
