import SwiftUI
import RoomPlan
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

final class ScanModel: NSObject, ObservableObject, RoomCaptureViewDelegate, RoomCaptureSessionDelegate {
    let captureView = RoomCaptureView(frame: .zero)
    @Published var scanning = false
    @Published var status = ""
    @Published var jsonURL: URL?
    @Published var usdzURL: URL?
    @Published var photoURLs: [URL] = []
    @Published var hud = ""
    let live3D = LiveRoomScene()
    private var lastScene = Date.distantPast
    private var liveRoom: CapturedRoom?
    private var tooDark = false
    @Published var photosMetaURL: URL?
    private var photoDir: URL?
    private var photoMetas: [PhotoMeta] = []
    private var lastPose: simd_float4x4?
    private var photoTimer: Timer?
    private let ciContext = CIContext()
    private let maxPhotos = 80
    private var result: CapturedRoom?
    private var lastLive = Date.distantPast
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @AppStorage("liveStream") private var liveStream = true

    override init() {
        super.init()
        captureView.delegate = self
        captureView.captureSession.delegate = self
    }

    // RoomCaptureViewDelegate inherits NSCoding; this model is never archived.
    required init?(coder: NSCoder) { nil }
    func encode(with coder: NSCoder) {}

    func start() {
        jsonURL = nil; usdzURL = nil; result = nil; status = ""
        photoURLs = []; photosMetaURL = nil; photoMetas = []; lastPose = nil
        live3D.reset()
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("room-photos-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        photoDir = dir
        photoTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.updateHUD(); self?.capturePhoto() }
        captureView.captureSession.run(configuration: RoomCaptureSession.Configuration())
        scanning = true
    }

    /// Light, tracking and live measurements, so a bad scan is noticed while there is still time to fix it.
    /// Ambient intensity is about 1000 in a well-lit room; LiDAR works in the dark, but the photos do not.
    private func updateHUD() {
        guard let frame = captureView.captureSession.arSession.currentFrame else { return }
        live3D.setPhone(frame.camera.transform)
        var parts: [String] = []
        if let light = frame.lightEstimate?.ambientIntensity {
            tooDark = light < 150
            parts.append(light < 150 ? "Too dark for good photos (\(Int(light)))" : light < 400 ? "Dim light (\(Int(light)))" : "Light OK")
        }
        switch frame.camera.trackingState {
        case .normal: break
        case .notAvailable: parts.append("Tracking lost")
        case .limited(.excessiveMotion): parts.append("Moving too fast")
        case .limited(.insufficientFeatures): parts.append("Not enough detail or light")
        case .limited: parts.append("Tracking limited")
        }
        if let room = liveRoom, !room.walls.isEmpty {
            let longest = room.walls.map { $0.dimensions.x }.max() ?? 0
            let height = room.walls.map { $0.dimensions.y }.max() ?? 0
            parts.append(String(format: "%d walls · longest %.2f m · height %.2f m", room.walls.count, longest, height))
        }
        hud = parts.joined(separator: " · ")
    }

    /// A real photo whenever the camera has moved ~25 cm or turned ~15 degrees, so every view of the room is covered.
    private func capturePhoto() {
        guard let dir = photoDir, photoMetas.count < maxPhotos, !tooDark,
              let frame = captureView.captureSession.arSession.currentFrame,
              case .normal = frame.camera.trackingState else { return }
        let t = frame.camera.transform
        if let last = lastPose {
            let moved = simd_length(t.columns.3 - last.columns.3) > 0.25
            let turned = simd_dot(simd_normalize(t.columns.2), simd_normalize(last.columns.2)) < 0.966
            if !moved && !turned { return }
        }
        let buf = frame.capturedImage
        let w = CVPixelBufferGetWidth(buf), h = CVPixelBufferGetHeight(buf)
        let k = min(1, 1280 / CGFloat(max(w, h)))
        let img = CIImage(cvPixelBuffer: buf).transformed(by: CGAffineTransform(scaleX: k, y: k))
        guard let jpeg = ciContext.jpegRepresentation(
            of: img, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
            options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.8]
        ) else { return }
        let name = String(format: "p%03d.jpg", photoMetas.count + 1)
        let url = dir.appendingPathComponent(name)
        guard (try? jpeg.write(to: url)) != nil else { return }
        lastPose = t
        let m = frame.camera.intrinsics
        let sw = Int(CGFloat(w) * k), sh = Int(CGFloat(h) * k)
        let scaled = simd_float3x3(columns: (m.columns.0 * Float(k), m.columns.1 * Float(k), SIMD3(m.columns.2.x * Float(k), m.columns.2.y * Float(k), 1)))
        func cols4(_ a: simd_float4x4) -> [[Float]] { (0..<4).map { c in (0..<4).map { a[c][$0] } } }
        func cols3(_ a: simd_float3x3) -> [[Float]] { (0..<3).map { c in (0..<3).map { a[c][$0] } } }
        var depthName: String?, dw: Int?, dh: Int?
        if let depth = frame.sceneDepth?.depthMap ?? frame.smoothedSceneDepth?.depthMap {
            let dname = String(format: "p%03d.depth", photoMetas.count + 1)
            let durl = dir.appendingPathComponent(dname)
            if let (w2, h2) = Self.writeDepth(depth, to: durl) { depthName = dname; dw = w2; dh = h2; photoURLs.append(durl) }
        }
        photoMetas.append(PhotoMeta(
            file: name, timestamp: Date().timeIntervalSince1970, transform: cols4(t), intrinsics: cols3(scaled), width: sw, height: sh,
            exposureDuration: frame.camera.exposureDuration, exposureOffset: frame.camera.exposureOffset,
            ambientIntensity: frame.lightEstimate.map { Float($0.ambientIntensity) },
            ambientColorTemperature: frame.lightEstimate.map { Float($0.ambientColorTemperature) },
            depthFile: depthName, depthWidth: dw, depthHeight: dh))
        photoURLs.append(url)
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

    func stop() {
        photoTimer?.invalidate(); photoTimer = nil
        if let dir = photoDir, !photoMetas.isEmpty {
            let meta = dir.appendingPathComponent("photos.json")
            let enc = JSONEncoder()
            enc.keyEncodingStrategy = .convertToSnakeCase
            if (try? enc.encode(PhotoManifest(photos: photoMetas)).write(to: meta)) != nil { photosMetaURL = meta }
        }
        captureView.captureSession.stop()
        scanning = false
    }

    // MARK: RoomCaptureSessionDelegate: stream the partial room to the web UI (about 2 per second)
    func captureSession(_ session: RoomCaptureSession, didUpdate room: CapturedRoom) {
        liveRoom = room
        if Date().timeIntervalSince(lastScene) > 0.5 {
            lastScene = Date()
            DispatchQueue.main.async { [live3D] in live3D.update(room) }
        }
        guard liveStream, Date().timeIntervalSince(lastLive) > 0.5,
              let data = try? JSONEncoder().encode(room) else { return }
        lastLive = Date()
        let server = serverURL
        Task.detached { await Uploader.live(json: data, server: server) }
    }

    // MARK: RoomCaptureViewDelegate
    func captureView(shouldPresent roomDataForProcessing: CapturedRoomData, error: Error?) -> Bool { true }

    func captureView(didPresent processedResult: CapturedRoom, error: Error?) {
        result = processedResult
        DispatchQueue.main.async { [live3D] in live3D.update(processedResult) }
        let dir = FileManager.default.temporaryDirectory
        let json = dir.appendingPathComponent("scan.json")
        let usdz = dir.appendingPathComponent("scan.usdz")
        do {
            try JSONEncoder().encode(processedResult).write(to: json)
            jsonURL = json
            try? processedResult.export(to: usdz, exportOptions: .parametric)
            usdzURL = FileManager.default.fileExists(atPath: usdz.path) ? usdz : nil
            status = "Scan ready"
        } catch {
            status = "Export failed: \(error.localizedDescription)"
        }
    }

    @MainActor func upload(server: String) async {
        guard let jsonURL else { return }
        status = "Uploading…"
        do {
            let id = try await Uploader.upload(json: jsonURL, usdz: usdzURL, name: "Scan \(Date().formatted())", server: server,
                                      photos: photoURLs, photosMeta: photosMetaURL)
            if let id { UserDefaults.standard.set(id, forKey: "lastScanID") }
            status = "Uploaded"
        } catch {
            status = "Upload failed: \(error.localizedDescription) — use Share"
        }
    }
}

struct CaptureViewRepresentable: UIViewRepresentable {
    let view: RoomCaptureView
    func makeUIView(context: Context) -> RoomCaptureView { view }
    func updateUIView(_ uiView: RoomCaptureView, context: Context) {}
}

struct ScanView: View {
    @StateObject private var model = ScanModel()
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @State private var mode = 0   // 0 camera, 1 3D model

    private var guide: String {
        if model.scanning {
            return "Walk slowly around the room and point at every wall, corner, door, window, the floor and the ceiling. In the 3D model, solid lines are finished edges and dashed lines are still missing. Tap Stop when the outline is closed."
        }
        if model.jsonURL != nil {
            return "Scan done. Turn the 3D model to check it. Tap Upload to send it to the server, or Start scan to redo it."
        }
        return "1. Tap Start scan.  2. Walk slowly and point the phone at all walls.  3. Switch to 3D model to see what was captured, and zoom with two fingers.  4. Tap Stop, then Upload."
    }

    var body: some View {
        ZStack {
            CaptureViewRepresentable(view: model.captureView).ignoresSafeArea()
                .opacity(mode == 0 ? 1 : 0)
            if mode == 1 { LiveRoom3DView(model: model.live3D).ignoresSafeArea() }
            VStack(spacing: 8) {
                Picker("View", selection: $mode) {
                    Text("Camera").tag(0)
                    Text("3D model").tag(1)
                }
                .pickerStyle(.segmented)
                Text(guide).font(.caption).padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
                Spacer()
                if mode == 1 {
                    SceneSummary(scene: model.live3D)
                    Text("One finger turns, pinch zooms, two fingers move, double-tap resets").font(.caption2).foregroundStyle(.secondary)
                }
                if model.scanning, !model.hud.isEmpty { Text(model.hud).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                HStack {
                    if model.scanning {
                        Button("Stop") { model.stop() }
                    } else {
                        Button("Start scan") { model.start() }
                    }
                    if mode == 1 { Button("Recenter") { model.live3D.recenter() } }
                    if model.jsonURL != nil, !model.scanning {
                        Button("Upload") { Task { await model.upload(server: serverURL) } }
                        if let url = model.jsonURL { ShareLink(item: url) { Text("Share") } }
                    }
                }
                .buttonStyle(.borderedProminent)
            }
            .padding()
        }
        .toolbar {
            NavigationLink { SettingsView() } label: { Image(systemName: "gear") }
        }
    }
}

/// Observes the 3D scene directly, because ScanView does not see changes inside a nested object.
struct SceneSummary: View {
    @ObservedObject var scene: LiveRoomScene
    var body: some View {
        Text(scene.summary).font(.caption).padding(6).background(.thinMaterial, in: Capsule())
    }
}
