import ARKit
import CoreImage

struct PhotoMeta: Codable {
    let file: String
    let timestamp: Double            // wall clock, seconds since 1970
    let transform: [[Float]]         // camera-to-world, 4 columns, same world frame as the CapturedRoom
    let intrinsics: [[Float]]        // 3 columns, for an image of `width` x `height`
    let width: Int
    let height: Int
    let exposureDuration: Double
    let exposureOffset: Float
    let ambientIntensity: Float?
    let ambientColorTemperature: Float?
    let depthFile: String?           // raw little-endian Float32, row-major, metres; aligned with the image
    let depthWidth: Int?
    let depthHeight: Int?
}

/// Everything needed to put the photos back in the room: poses, intrinsics, depth, and the conventions.
struct PhotoManifest: Codable {
    var version = 2
    var units = "metres"
    var worldFrame = "ARKit session world: right-handed, +Y up. Same frame as the CapturedRoom surface transforms."
    var cameraFrame = "ARKit camera: +X right, +Y up, looks down -Z. transform maps camera space to world."
    var imageFrame = "Unrotated sensor image (landscape). Origin top-left, u right, v down. intrinsics columns: [fx,0,0],[0,fy,0],[cx,cy,1] for width x height; scale them for the depth map size."
    var photos: [PhotoMeta]
}

/// Takes a photo whenever the camera has moved ~25 cm or turned ~15 degrees, with its pose, intrinsics and depth,
/// in the same ARKit world frame as the mesh. The server uses them to colour the mesh.
final class PhotoRecorder {
    let dir: URL
    private(set) var metas: [PhotoMeta] = []
    private(set) var urls: [URL] = []
    private var lastPose: simd_float4x4?
    private var lastTimestamp: TimeInterval = -1
    private let ciContext = CIContext()
    var maxPhotos = 120
    var maxSide: CGFloat = 1280

    var count: Int { metas.count }

    init() {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("mesh-photos-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    /// `force` skips the movement test: ring scans and "Keep every frame" take photos on a clock instead.
    func consider(_ frame: ARFrame, force: Bool = false) {
        // `currentFrame` stays on the last frame when the session is interrupted (another tab's camera), and forced
        // captures would otherwise fill the scan with copies of it.
        guard metas.count < maxPhotos, frame.timestamp != lastTimestamp, case .normal = frame.camera.trackingState else { return }
        let t = frame.camera.transform
        if !force, let last = lastPose {
            let moved = simd_length(t.columns.3 - last.columns.3) > 0.25
            let turned = simd_dot(simd_normalize(t.columns.2), simd_normalize(last.columns.2)) < 0.966
            if !moved && !turned { return }
        }
        let buf = frame.capturedImage
        let w = CVPixelBufferGetWidth(buf), h = CVPixelBufferGetHeight(buf)
        let k = min(1, maxSide / CGFloat(max(w, h)))
        let img = CIImage(cvPixelBuffer: buf).transformed(by: CGAffineTransform(scaleX: k, y: k))
        guard let jpeg = ciContext.jpegRepresentation(
            of: img, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
            options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.8]
        ) else { return }
        let n = metas.count + 1
        let name = String(format: "p%03d.jpg", n)
        guard (try? jpeg.write(to: dir.appendingPathComponent(name))) != nil else { return }
        lastPose = t
        lastTimestamp = frame.timestamp
        let m = frame.camera.intrinsics
        let kf = Float(k)
        let scaled = simd_float3x3(columns: (m.columns.0 * kf, m.columns.1 * kf, SIMD3(m.columns.2.x * kf, m.columns.2.y * kf, 1)))
        func cols4(_ a: simd_float4x4) -> [[Float]] { (0..<4).map { c in (0..<4).map { a[c][$0] } } }
        func cols3(_ a: simd_float3x3) -> [[Float]] { (0..<3).map { c in (0..<3).map { a[c][$0] } } }
        var depthName: String?, dw: Int?, dh: Int?
        if let depth = frame.sceneDepth?.depthMap ?? frame.smoothedSceneDepth?.depthMap {
            let dname = String(format: "p%03d.depth", n)
            if let (w2, h2) = Self.writeDepth(depth, to: dir.appendingPathComponent(dname)) {
                depthName = dname; dw = w2; dh = h2
                urls.append(dir.appendingPathComponent(dname))
            }
        }
        metas.append(PhotoMeta(
            file: name, timestamp: Date().timeIntervalSince1970, transform: cols4(t), intrinsics: cols3(scaled),
            width: Int(CGFloat(w) * k), height: Int(CGFloat(h) * k),
            exposureDuration: frame.camera.exposureDuration, exposureOffset: frame.camera.exposureOffset,
            ambientIntensity: frame.lightEstimate.map { Float($0.ambientIntensity) },
            ambientColorTemperature: frame.lightEstimate.map { Float($0.ambientColorTemperature) },
            depthFile: depthName, depthWidth: dw, depthHeight: dh))
        urls.append(dir.appendingPathComponent(name))
    }

    /// photos.json with the conventions, or nil if no photo was taken.
    func writeManifest() -> URL? {
        guard !metas.isEmpty else { return nil }
        let url = dir.appendingPathComponent("photos.json")
        let enc = JSONEncoder()
        enc.keyEncodingStrategy = .convertToSnakeCase
        return (try? enc.encode(PhotoManifest(photos: metas)).write(to: url)) != nil ? url : nil
    }

    private static func writeDepth(_ buf: CVPixelBuffer, to url: URL) -> (Int, Int)? {
        CVPixelBufferLockBaseAddress(buf, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buf, .readOnly) }
        guard CVPixelBufferGetPixelFormatType(buf) == kCVPixelFormatType_DepthFloat32,
              let base = CVPixelBufferGetBaseAddress(buf) else { return nil }
        let w = CVPixelBufferGetWidth(buf), h = CVPixelBufferGetHeight(buf), bpr = CVPixelBufferGetBytesPerRow(buf)
        var data = Data(capacity: w * h * 4)
        for y in 0..<h { data.append(Data(bytes: base + y * bpr, count: w * 4)) }
        return (try? data.write(to: url)) != nil ? (w, h) : nil
    }
}
