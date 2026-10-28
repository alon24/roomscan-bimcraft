import ARKit
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
    private func centreDepth(_ frame: ARFrame) -> Double? {
        guard let map = (frame.smoothedSceneDepth ?? frame.sceneDepth)?.depthMap else { return nil }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), rb = CVPixelBufferGetBytesPerRow(map)
        var v = [Float]()
        for dy in -1...1 { for dx in -1...1 {
            let row = base.advanced(by: (h / 2 + dy) * rb).assumingMemoryBound(to: Float32.self)
            let d = row[w / 2 + dx]
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

        let t = frame.camera.transform
        let cam = SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let fwd = -SIMD3(t.columns.2.x, t.columns.2.y, t.columns.2.z)
        let point = cam + fwd * Float(med)
        lastPoint = point
        if let a = markA { span = Double(simd_length(point - a)) } else { span = nil }
    }

    func mark() {
        if markA == nil { markA = lastPoint } else { markA = nil; span = nil }
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }

    func freeze() {
        guard let v = span ?? distance else { return }
        frozen = v
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    func save() {
        guard let v = frozen else { return }
        saved.insert(Measurement(kind: span != nil ? "span" : "point", metres: v), at: 0)
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

    private var readout: some View {
        VStack(spacing: 4) {
            if let s = model.span {
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
                Label(model.markA == nil ? "Mark A" : "Clear A", systemImage: model.markA == nil ? "mappin" : "xmark")
            }.buttonStyle(CaptureButtonStyle())
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
            .overlay { if model.saved.isEmpty { Text("Nothing saved yet.\nTap Hold, then Save.").multilineTextAlignment(.center).foregroundStyle(.secondary) } }
            .navigationTitle("Measurements")
            .toolbar { Button("Done") { dismiss() } }
        }
    }
}
