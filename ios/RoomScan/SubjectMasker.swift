import Vision
import CoreImage
import UIKit
import ImageIO

/// The part's outline in a ring-scan photo (Vision's foreground instances), for the server's and the phone's carving.
enum SubjectMasker {
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
}
