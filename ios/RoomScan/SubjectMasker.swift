import Vision
import CoreImage
import UIKit
import ImageIO

/// A turntable scan has a fixed camera, so the photos all show the same static background. Photogrammetry then thinks
/// the background is the subject and the part spins "wrongly". This copies every photo with everything except the
/// part (found with Vision's subject lifting) replaced by flat grey, so only the part has features to match.
enum SubjectMasker {
    /// Returns the folder with the masked copies, or nil if no photo could be masked.
    static func mask(images: [URL], progress: @escaping (Int, Int) -> Void) async -> (dir: URL, masked: Int)? {
        let base = FileManager.default.temporaryDirectory.appendingPathComponent("masked-\(UUID().uuidString)")
        let out = base.appendingPathComponent("Images")
        try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        return await Task.detached(priority: .userInitiated) { () -> (URL, Int)? in
            let ctx = CIContext()
            let srgb = CGColorSpace(name: CGColorSpace.sRGB)!
            var masked = 0
            for (n, url) in images.enumerated() {
                autoreleasepool {
                    progress(n, images.count)
                    guard var img = CIImage(contentsOf: url, options: [.applyOrientationProperty: true]) else { return }
                    let k = min(1, 1600 / max(img.extent.width, img.extent.height))
                    img = img.transformed(by: CGAffineTransform(scaleX: k, y: k))
                    var result = img
                    if let subject = subject(of: img) {
                        let grey = CIImage(color: CIColor(red: 0.5, green: 0.5, blue: 0.5)).cropped(to: img.extent)
                        result = subject.composited(over: grey)
                        masked += 1
                    }
                    let dest = out.appendingPathComponent(url.lastPathComponent)
                    if let jpeg = ctx.jpegRepresentation(of: result, colorSpace: srgb, options: [:]) { try? jpeg.write(to: dest) }
                }
            }
            progress(images.count, images.count)
            return masked > 0 ? (out, masked) : nil
        }.value
    }

    /// Everything Vision lifts out of the background, plus which separate thing each pixel belongs to (0 = background, 1, 2...
    /// = the part, the plate and ring, a hand). The labels let the server and the phone build leave the ring and plate out.
    static func outlineAndLabelsPNG(_ url: URL, ctx: CIContext) -> (outline: Data, labels: Data?)? {
        guard let img = CIImage(contentsOf: url) else { return nil }
        let handler = VNImageRequestHandler(ciImage: img, options: [:])
        let request = VNGenerateForegroundInstanceMaskRequest()
        guard (try? handler.perform([request])) != nil, let obs = request.results?.first, !obs.allInstances.isEmpty,
              let buf = try? obs.generateScaledMaskForImage(forInstances: obs.allInstances, from: handler),
              let png = ctx.pngRepresentation(of: CIImage(cvPixelBuffer: buf), format: .L8, colorSpace: CGColorSpaceCreateDeviceGray(), options: [:])
        else { return nil }
        return (png, rawGrayPNG(obs.instanceMask))
    }

    /// An 8-bit one-channel pixel buffer written as a PNG with the values exactly as they are (no colour conversion).
    static func rawGrayPNG(_ buf: CVPixelBuffer) -> Data? {
        CVPixelBufferLockBaseAddress(buf, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buf, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buf) else { return nil }
        let w = CVPixelBufferGetWidth(buf), h = CVPixelBufferGetHeight(buf), bpr = CVPixelBufferGetBytesPerRow(buf)
        var bytes = Data(count: w * h)
        bytes.withUnsafeMutableBytes { out in
            for y in 0..<h { memcpy(out.baseAddress! + y * w, base + y * bpr, w) }
        }
        guard let provider = CGDataProvider(data: bytes as CFData),
              let cg = CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: w, space: CGColorSpaceCreateDeviceGray(),
                               bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue), provider: provider, decode: nil,
                               shouldInterpolate: false, intent: .defaultIntent) else { return nil }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, "public.png" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, cg, nil)
        return CGImageDestinationFinalize(dest) ? out as Data : nil
    }

    /// Everything Vision lifts out of the background in one photo, as a black and white PNG at the photo's own size
    /// (white = foreground). The server takes the flat stage and ring off with the LiDAR, so the plate being included is fine.
    static func outlinePNG(_ url: URL, ctx: CIContext) -> Data? {
        guard let img = CIImage(contentsOf: url) else { return nil }
        let handler = VNImageRequestHandler(ciImage: img, options: [:])
        let request = VNGenerateForegroundInstanceMaskRequest()
        guard (try? handler.perform([request])) != nil, let obs = request.results?.first, !obs.allInstances.isEmpty,
              let buf = try? obs.generateScaledMaskForImage(forInstances: obs.allInstances, from: handler) else { return nil }
        return ctx.pngRepresentation(of: CIImage(cvPixelBuffer: buf), format: .L8, colorSpace: CGColorSpaceCreateDeviceGray(), options: [:])
    }

    /// The part as an image with a transparent background: the foreground instance nearest the middle of the picture.
    private static func subject(of image: CIImage) -> CIImage? {
        let handler = VNImageRequestHandler(ciImage: image, options: [:])
        let request = VNGenerateForegroundInstanceMaskRequest()
        guard (try? handler.perform([request])) != nil, let obs = request.results?.first, !obs.allInstances.isEmpty else { return nil }
        var chosen = obs.allInstances
        if let label = centralLabel(obs.instanceMask) { chosen = IndexSet(integer: label) }
        guard let buf = try? obs.generateMaskedImage(ofInstances: chosen, from: handler, croppedToInstancesExtent: false) else { return nil }
        return CIImage(cvPixelBuffer: buf)
    }

    /// The most common instance label in the middle of the label map (0 is background).
    private static func centralLabel(_ map: CVPixelBuffer) -> Int? {
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), bpr = CVPixelBufferGetBytesPerRow(map)
        var counts = [UInt8: Int]()
        for y in Int(Double(h) * 0.35)..<Int(Double(h) * 0.65) {
            let row = (base + y * bpr).assumingMemoryBound(to: UInt8.self)
            for x in Int(Double(w) * 0.35)..<Int(Double(w) * 0.65) where row[x] != 0 { counts[row[x], default: 0] += 1 }
        }
        return counts.max { $0.value < $1.value }.map { Int($0.key) }
    }
}
