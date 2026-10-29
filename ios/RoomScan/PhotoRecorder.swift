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
///
/// `consider` decides on the caller's thread and returns at once; the JPEG and depth files are written on a background queue.
/// A frame shows up in `metas`, `urls` and `count` only after its files exist, so a reader never sees a half-written frame.
final class PhotoRecorder {
    let dir: URL
    private let lock = NSLock()
    private let work = DispatchQueue(label: "photorecorder", qos: .userInitiated)
    // guarded by `lock`
    private var _metas: [PhotoMeta] = []
    private var _urls: [URL] = []
    private var lastPose: simd_float4x4?
    private var lastTimestamp: TimeInterval = -1
    private var pending = 0           // frames accepted whose files are still being written
    private var nextIndex = 0         // file number of the last accepted frame
    private let ciContext = CIContext()
    var maxPhotos = 120
    var maxSide: CGFloat = 1280

    var metas: [PhotoMeta] { lock.lock(); defer { lock.unlock() }; return _metas }
    var count: Int { lock.lock(); defer { lock.unlock() }; return _metas.count }
    /// Waits for frames still being written, so the list is complete.
    var urls: [URL] { waitForWrites(); lock.lock(); defer { lock.unlock() }; return _urls }

    init() {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("mesh-photos-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    /// Blocks until every accepted frame is on disk (or failed). Quick: at most two frames are ever queued.
    func waitForWrites() { work.sync {} }

    /// `force` skips the movement test: ring scans and "Keep every frame" take photos on a clock instead.
    /// Returns whether the frame was taken (its files are written shortly after).
    @discardableResult
    func consider(_ frame: ARFrame, force: Bool = false) -> Bool {
        // `currentFrame` stays on the last frame when the session is interrupted (another tab's camera), and forced
        // captures would otherwise fill the scan with copies of it.
        guard case .normal = frame.camera.trackingState else { return false }
        let t = frame.camera.transform
        lock.lock()
        // At most two frames waiting for the disk: each holds a camera buffer, and ARKit stalls when too many are held.
        guard _metas.count + pending < maxPhotos, pending < 2, frame.timestamp != lastTimestamp else { lock.unlock(); return false }
        if !force, let last = lastPose {
            let moved = simd_length(t.columns.3 - last.columns.3) > 0.25
            let turned = simd_dot(simd_normalize(t.columns.2), simd_normalize(last.columns.2)) < 0.966
            if !moved && !turned { lock.unlock(); return false }
        }
        let ts = frame.timestamp
        let prevPose = lastPose, prevTimestamp = lastTimestamp
        lastPose = t
        lastTimestamp = ts
        pending += 1
        nextIndex += 1
        let n = nextIndex
        lock.unlock()

        // Everything the background write needs, taken from the frame now (the buffers are retained, the rest are values).
        let buf = frame.capturedImage
        let w = CVPixelBufferGetWidth(buf), h = CVPixelBufferGetHeight(buf)
        let k = min(1, maxSide / CGFloat(max(w, h)))
        let m = frame.camera.intrinsics
        let depthBuf = frame.sceneDepth?.depthMap ?? frame.smoothedSceneDepth?.depthMap
        let stamp = Date().timeIntervalSince1970
        let exposure = frame.camera.exposureDuration, offset = frame.camera.exposureOffset
        let intensity = frame.lightEstimate.map { Float($0.ambientIntensity) }
        let temperature = frame.lightEstimate.map { Float($0.ambientColorTemperature) }
        let dir = self.dir, ciContext = self.ciContext

        work.async { [self] in
            var meta: PhotoMeta?
            var files: [URL] = []
            autoreleasepool {
                let img = CIImage(cvPixelBuffer: buf).transformed(by: CGAffineTransform(scaleX: k, y: k))
                guard let jpeg = ciContext.jpegRepresentation(
                    of: img, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                    options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.8]
                ) else { return }
                let name = String(format: "p%03d.jpg", n)
                guard (try? jpeg.write(to: dir.appendingPathComponent(name))) != nil else { return }
                let kf = Float(k)
                let scaled = simd_float3x3(columns: (m.columns.0 * kf, m.columns.1 * kf, SIMD3(m.columns.2.x * kf, m.columns.2.y * kf, 1)))
                func cols4(_ a: simd_float4x4) -> [[Float]] { (0..<4).map { c in (0..<4).map { a[c][$0] } } }
                func cols3(_ a: simd_float3x3) -> [[Float]] { (0..<3).map { c in (0..<3).map { a[c][$0] } } }
                var depthName: String?, dw: Int?, dh: Int?
                if let depthBuf {
                    let dname = String(format: "p%03d.depth", n)
                    if let (w2, h2) = PhotoRecorder.writeDepth(depthBuf, to: dir.appendingPathComponent(dname)) {
                        depthName = dname; dw = w2; dh = h2
                        files.append(dir.appendingPathComponent(dname))
                    }
                }
                meta = PhotoMeta(
                    file: name, timestamp: stamp, transform: cols4(t), intrinsics: cols3(scaled),
                    width: Int(CGFloat(w) * k), height: Int(CGFloat(h) * k),
                    exposureDuration: exposure, exposureOffset: offset,
                    ambientIntensity: intensity, ambientColorTemperature: temperature,
                    depthFile: depthName, depthWidth: dw, depthHeight: dh)
                files.append(dir.appendingPathComponent(name))
            }
            lock.lock()
            pending -= 1
            if let meta {
                _metas.append(meta)
                _urls.append(contentsOf: files)
            } else if lastTimestamp == ts {
                lastPose = prevPose; lastTimestamp = prevTimestamp    // the photo failed: as if this frame had never been taken
            }
            lock.unlock()
        }
        return true
    }

    /// photos.json with the conventions, or nil if no photo was taken. Waits for frames still being written.
    func writeManifest() -> URL? {
        waitForWrites()
        let metas = self.metas
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
