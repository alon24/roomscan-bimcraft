import ARKit
import SceneKit
import SwiftUI

/// A saved distance, kept on the phone. `span` is point A to point B; `point` is camera to the wall.
struct Measurement: Codable, Identifiable {
    var id = UUID()
    var date = Date()
    var kind: String
    var metres: Double
}

/// Point the camera, read the LiDAR distance at the crosshair. Mark A, move, and the live A to B span shows too.
final class MeasureModel: NSObject, ObservableObject, ARSessionDelegate {
    let sceneView = ARSCNView()
    static let minRange = 0.25, maxRange = 5.0

    @Published var distance: Double?     // smoothed camera-to-target, metres
    @Published var steady = false
    @Published var span: Double?         // A to the crosshair, metres
    @Published var markA: SIMD3<Float>?
    @Published var frozen: Double?
    @Published var saved: [Measurement] = []
    @Published var hasLiDAR = true
    @Published var overlay: UIImage?     // live depth, colour coded, drawn over the camera picture
    @Published var showDepth = true
    @Published var pins: [SIMD3<Float>] = []
    var camPos = SIMD3<Float>(0, 0, 0)
    private var frameCount = 0
    private static let ramp: [UInt8] = {   // near = red, far = blue
        var r = [UInt8]()
        for i in 0..<256 {
            let c = UIColor(hue: CGFloat(i) / 255 * 0.66, saturation: 1, brightness: 1, alpha: 1)
            var R: CGFloat = 0, G: CGFloat = 0, B: CGFloat = 0
            c.getRed(&R, green: &G, blue: &B, alpha: nil)
            r += [UInt8(R * 255), UInt8(G * 255), UInt8(B * 255)]
        }
        return r
    }()

    private var recent: [Double] = []
    private var lastPoint: SIMD3<Float>?
    private var live = true

    override init() {
        super.init()
        sceneView.session.delegate = self
        sceneView.automaticallyUpdatesLighting = true
        if let d = UserDefaults.standard.data(forKey: "measurements"), let m = try? JSONDecoder().decode([Measurement].self, from: d) { saved = m }
    }

    func start() {
        let cfg = ARWorldTrackingConfiguration()
        hasLiDAR = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
        if hasLiDAR { cfg.frameSemantics.insert(.sceneDepth) }
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) { cfg.sceneReconstruction = .mesh }
        cfg.planeDetection = [.horizontal, .vertical]
        live = true
        sceneView.session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
        Screen.awake(true)
    }

    func stop() {
        live = false
        sceneView.session.pause()
        Screen.awake(false)
    }

    /// Median of the 3x3 depth pixels around the centre, ignoring zeros, so one noisy pixel cannot move the reading.
    private func centreDepth(_ frame: ARFrame) -> Double? { depth(frame, nx: 0.5, ny: 0.5) }

    /// Depth in metres at a point given in normalised camera-image coordinates (0...1, sensor orientation).
    private func depth(_ frame: ARFrame, nx: Double, ny: Double) -> Double? {
        guard let map = (frame.smoothedSceneDepth ?? frame.sceneDepth)?.depthMap else { return nil }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), rb = CVPixelBufferGetBytesPerRow(map)
        let cx = min(max(Int(nx * Double(w)), 1), w - 2), cy = min(max(Int(ny * Double(h)), 1), h - 2)
        var v = [Float]()
        for dy in -1...1 { for dx in -1...1 {
            let row = base.advanced(by: (cy + dy) * rb).assumingMemoryBound(to: Float32.self)
            let d = row[cx + dx]
            if d.isFinite, d > 0 { v.append(d) }
        } }
        guard v.count >= 4 else { return nil }
        return Double(v.sorted()[v.count / 2])
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard live else { return }
        var d = centreDepth(frame)
        if d == nil {
            // No LiDAR: fall back to an ARKit raycast from the screen centre (less exact).
            let c = CGPoint(x: sceneView.bounds.midX, y: sceneView.bounds.midY)
            if let q = sceneView.raycastQuery(from: c, allowing: .estimatedPlane, alignment: .any), let hit = session.raycast(q).first {
                let p = hit.worldTransform.columns.3
                let cam = frame.camera.transform.columns.3
                d = Double(simd_length(SIMD3(p.x - cam.x, p.y - cam.y, p.z - cam.z)))
            }
        }
        guard let depth = d else { distance = nil; steady = false; return }
        recent.append(depth)
        if recent.count > 12 { recent.removeFirst() }
        let med = recent.sorted()[recent.count / 2]
        let spread = (recent.max() ?? 0) - (recent.min() ?? 0)
        distance = med
        steady = recent.count >= 8 && spread < 0.006 && med >= Self.minRange && med <= Self.maxRange

        frameCount += 1
        if showDepth, frameCount % 3 == 0 { overlay = depthImage(frame) }
        let t = frame.camera.transform
        let cam = SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let fwd = -SIMD3(t.columns.2.x, t.columns.2.y, t.columns.2.z)
        let point = cam + fwd * Float(med)
        lastPoint = point
        camPos = cam
        if let a = markA { span = Double(simd_length(point - a)) } else { span = nil }
    }

    /// Colour-codes the depth map (0.3 m red ... 4 m blue) as a translucent picture; shown rotated for portrait.
    private func depthImage(_ frame: ARFrame) -> UIImage? {
        guard let map = (frame.smoothedSceneDepth ?? frame.sceneDepth)?.depthMap else { return nil }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), rb = CVPixelBufferGetBytesPerRow(map)
        var px = [UInt8](repeating: 0, count: w * h * 4)
        for y in 0..<h {
            let row = base.advanced(by: y * rb).assumingMemoryBound(to: Float32.self)
            for x in 0..<w {
                let d = row[x]
                guard d.isFinite, d > 0 else { continue }
                let i = Int(min(max((d - 0.3) / 3.7, 0), 1) * 255)
                let o = (y * w + x) * 4
                px[o] = Self.ramp[i * 3] / 2; px[o + 1] = Self.ramp[i * 3 + 1] / 2; px[o + 2] = Self.ramp[i * 3 + 2] / 2; px[o + 3] = 120
            }
        }
        guard let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue),
              let cg = ctx.makeImage() else { return nil }
        return UIImage(cgImage: cg, scale: 1, orientation: .right)
    }

    /// Pin the point under a tap: read the depth there and keep it as a fixed spot in the room.
    func tap(_ p: CGPoint) {
        guard let frame = sceneView.session.currentFrame else { return }
        let size = sceneView.bounds.size
        guard size.width > 0 else { return }
        let toView = frame.displayTransform(for: .portrait, viewportSize: size)
        let img = CGPoint(x: p.x / size.width, y: p.y / size.height).applying(toView.inverted())
        guard let d = depth(frame, nx: Double(img.x), ny: Double(img.y)), d >= Self.minRange, d <= Self.maxRange else {
            UINotificationFeedbackGenerator().notificationOccurred(.error); return
        }
        let k = frame.camera.intrinsics, res = frame.camera.imageResolution
        let u = Float(img.x) * Float(res.width), v = Float(img.y) * Float(res.height)
        let z = Float(d)
        let cam4 = SIMD4((u - k[2][0]) / k[0][0] * z, -(v - k[2][1]) / k[1][1] * z, -z, 1)
        let w = frame.camera.transform * cam4
        if pins.count >= 2 { pins = [] }
        pins.append(SIMD3(w.x, w.y, w.z))
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    var pinSpan: Double? { pins.count == 2 ? Double(simd_length(pins[1] - pins[0])) : nil }

    func mark() {
        if markA == nil { markA = lastPoint } else { markA = nil; span = nil }
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }

    func freeze() {
        guard let v = pinSpan ?? span ?? distance else { return }
        frozen = v
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    func save() {
        guard let v = frozen else { return }
        saved.insert(Measurement(kind: (span != nil || pinSpan != nil) ? "span" : "point", metres: v), at: 0)
        persist()
        frozen = nil
    }

    func remove(_ ids: [UUID]) { saved.removeAll { ids.contains($0.id) }; persist() }

    private func persist() {
        if let d = try? JSONEncoder().encode(saved) { UserDefaults.standard.set(d, forKey: "measurements") }
    }
}

struct MeasureView: View {
    @StateObject private var model = MeasureModel()
    @State private var showList = false

    static func text(_ m: Double) -> (big: String, small: String) {
        (String(format: "%.3f m", m), String(format: "%.1f cm  ·  %.0f mm", m * 100, m * 1000))
    }

    private var status: (String, Color) {
        guard let d = model.distance else { return ("Point at a surface", .gray) }
        if d < MeasureModel.minRange { return ("Too close, move back", .red) }
        if d > MeasureModel.maxRange { return ("Too far for LiDAR (max 5 m)", .red) }
        return model.steady ? ("Steady, reading is good", .green) : ("Hold still…", .orange)
    }

    var body: some View {
        ZStack {
            ARSceneRepresentable(view: model.sceneView).ignoresSafeArea()
            if model.showDepth, let img = model.overlay {
                Image(uiImage: img).resizable().scaledToFill().ignoresSafeArea().allowsHitTesting(false)
            }
            Color.clear.contentShape(Rectangle()).ignoresSafeArea().onTapGesture(coordinateSpace: .local) { model.tap($0) }
            pinLabels.allowsHitTesting(false)
            Crosshair(colour: status.1).allowsHitTesting(false)

            VStack(spacing: 8) {
                if !model.hasLiDAR {
                    Text("No LiDAR on this phone: readings use the camera only and are less exact.")
                        .font(.caption).padding(8).background(.red.opacity(0.8), in: RoundedRectangle(cornerRadius: 8)).foregroundStyle(.white)
                }
                Spacer()
                readout
                controls
            }
            .padding(.horizontal, 16).padding(.bottom, 8)
        }
        .onAppear { model.start() }
        .onDisappear { model.stop() }
        .sheet(isPresented: $showList) { MeasurementList(model: model) }
    }

    /// Pins stay on their spot in the room; the label shows how far each is from the camera right now, and A to B between two.
    private var pinLabels: some View {
        let _ = model.distance   // redraw every frame
        let pts = model.pins.map { model.sceneView.projectPoint(SCNVector3($0.x, $0.y, $0.z)) }
        return ZStack {
            if pts.count == 2 {
                Path { p in p.move(to: CGPoint(x: CGFloat(pts[0].x), y: CGFloat(pts[0].y))); p.addLine(to: CGPoint(x: CGFloat(pts[1].x), y: CGFloat(pts[1].y))) }
                    .stroke(.white, style: StrokeStyle(lineWidth: 2, dash: [6, 4]))
                if let s = model.pinSpan {
                    Text(Self.text(s).big).font(.headline).padding(4).background(.black.opacity(0.7), in: Capsule()).foregroundStyle(.white)
                        .position(x: CGFloat(pts[0].x + pts[1].x) / 2, y: CGFloat(pts[0].y + pts[1].y) / 2 - 18)
                }
            }
            ForEach(Array(pts.enumerated()), id: \.offset) { i, p in
                if p.z > 0 && p.z < 1 {
                    VStack(spacing: 2) {
                        Circle().fill(.white).frame(width: 12, height: 12).overlay(Circle().stroke(Theme.accent, lineWidth: 3))
                        Text(Self.text(Double(simd_length(model.pins[i] - model.camPos))).big).font(.caption2.bold())
                            .padding(3).background(.black.opacity(0.7), in: Capsule()).foregroundStyle(.white)
                    }.position(x: CGFloat(p.x), y: CGFloat(p.y))
                }
            }
        }.ignoresSafeArea()
    }

    private var readout: some View {
        VStack(spacing: 4) {
            if let s = model.pinSpan {
                let t = Self.text(s)
                Text("pin to pin").font(.caption).foregroundStyle(.secondary)
                Text(t.big).font(.system(size: 40, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
            } else if let s = model.span {
                let t = Self.text(s)
                Text("A → here").font(.caption).foregroundStyle(.secondary)
                Text(t.big).font(.system(size: 40, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
                if let d = model.distance { Text("camera to target " + Self.text(d).big).font(.caption2).foregroundStyle(.secondary) }
            } else if let d = model.distance {
                let t = Self.text(d)
                Text(t.big).font(.system(size: 44, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
            } else {
                Text("—").font(.system(size: 44, weight: .bold, design: .rounded))
            }
            Label(status.0, systemImage: "circle.fill").font(.caption).foregroundStyle(status.1)
            if let f = model.frozen {
                HStack {
                    Text("Held: " + Self.text(f).big).font(.headline)
                    Button("Save") { model.save() }.buttonStyle(.borderedProminent)
                    Button("Drop") { model.frozen = nil }.buttonStyle(.bordered)
                }.padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity).padding(12).card()
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Button { model.mark() } label: {
                Label(model.markA == nil ? "Mark A" : "Clear A", systemImage: model.markA == nil ? "mappin" : "xmark").lineLimit(1).minimumScaleFactor(0.7)
            }.buttonStyle(CaptureButtonStyle())
            Button { model.showDepth.toggle(); if !model.showDepth { model.overlay = nil } } label: { Image(systemName: model.showDepth ? "eye.fill" : "eye.slash") }
                .buttonStyle(CaptureButtonStyle())
            Button { model.freeze() } label: { Label("Hold", systemImage: "hand.raised") }
                .buttonStyle(CaptureButtonStyle()).disabled(model.distance == nil)
            Button { showList = true } label: { Label("\(model.saved.count)", systemImage: "list.bullet") }
                .buttonStyle(CaptureButtonStyle())
        }
        .padding(.bottom, 52)
    }
}

private struct Crosshair: View {
    let colour: Color
    var body: some View {
        ZStack {
            Circle().stroke(colour, lineWidth: 2).frame(width: 34, height: 34)
            Circle().fill(colour).frame(width: 5, height: 5)
            Rectangle().fill(colour).frame(width: 2, height: 14).offset(y: -26)
            Rectangle().fill(colour).frame(width: 2, height: 14).offset(y: 26)
            Rectangle().fill(colour).frame(width: 14, height: 2).offset(x: -26)
            Rectangle().fill(colour).frame(width: 14, height: 2).offset(x: 26)
        }
        .shadow(color: .black.opacity(0.6), radius: 2)
    }
}

struct MeasurementList: View {
    @ObservedObject var model: MeasureModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                ForEach(model.saved) { m in
                    HStack {
                        Image(systemName: m.kind == "span" ? "ruler" : "scope").foregroundStyle(Theme.accent)
                        VStack(alignment: .leading) {
                            Text(MeasureView.text(m.metres).big).font(.headline).monospacedDigit()
                            Text(MeasureView.text(m.metres).small + "  ·  " + m.date.formatted(date: .abbreviated, time: .shortened))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete { model.remove($0.map { model.saved[$0].id }) }
            }
            .overlay { if model.saved.isEmpty { Text("Nothing saved yet.\nTap a spot to pin it, then Hold and Save.").multilineTextAlignment(.center).foregroundStyle(.secondary) } }
            .navigationTitle("Measurements")
            .toolbar { Button("Done") { dismiss() } }
        }
    }
}
