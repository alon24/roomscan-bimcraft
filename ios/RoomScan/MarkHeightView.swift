import SwiftUI
import ARKit
import SceneKit

/// Point the crosshair at the floor or the ceiling, from close by, and press the button: a point floats there, and the height comes from
/// the points you marked. The little plan in the corner fills in as the LiDAR sees the room, so what is missing shows as a gap.
final class MarkHeightModel: NSObject, ObservableObject {
    struct Mark: Identifiable { let id = UUID(); let kind: String; let p: simd_float3; let node: SCNNode }

    let sceneView = ARSCNView(frame: .zero)
    @Published var marks: [Mark] = []
    @Published var reading: Double?          // metres to what the crosshair is on
    @Published var seen = Set<Int64>()       // plan cells the LiDAR has seen
    @Published var floorSeen = Set<Int64>()
    @Published var ceilingSeen = Set<Int64>()
    @Published var you: (x: Float, z: Float, heading: Float)?
    @Published var note = ""

    static let cell: Float = 0.2
    static let nearEnough = 2.0              // metres: further than this the point is less exact
    private let session = UUID().uuidString
    private var timer: Timer?
    private var ticks = 0

    private func ys(_ kind: String) -> [Float] { marks.filter { $0.kind == kind }.map(\.p.y).sorted() }
    private func median(_ v: [Float]) -> Float? { v.isEmpty ? nil : v[v.count / 2] }
    var height: Double? {
        guard let f = median(ys("floor")), let c = median(ys("ceiling")) else { return nil }
        return Double(c - f)
    }
    func count(_ kind: String) -> Int { marks.filter { $0.kind == kind }.count }

    static func key(_ x: Float, _ z: Float) -> Int64 {
        (Int64((x / cell).rounded(.down)) + 100000) * 200003 + (Int64((z / cell).rounded(.down)) + 100000)
    }
    static func unkey(_ k: Int64) -> (Int, Int) { (Int(k / 200003) - 100000, Int(k % 200003) - 100000) }

    func start() {
        let cfg = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        CameraOwner.claim(self) { [weak self] in self?.sceneView.session.pause(); Screen.awake(false) }
        Screen.awake(true)
        sceneView.session.run(cfg)
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.tick() }
    }

    func stop() { timer?.invalidate(); timer = nil; CameraOwner.release(self) }

    /// The depth at the crosshair: the middle of five readings. The middle of the depth map is the middle of the photo whichever way it is stored.
    private func centre(_ frame: ARFrame) -> Double? {
        guard let map = (frame.smoothedSceneDepth ?? frame.sceneDepth)?.depthMap else { return nil }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), rb = CVPixelBufferGetBytesPerRow(map)
        func at(_ x: Int, _ y: Int) -> Float { base.advanced(by: y * rb).assumingMemoryBound(to: Float32.self)[x] }
        let v = [at(w / 2, h / 2), at(w / 2 - 2, h / 2), at(w / 2 + 2, h / 2), at(w / 2, h / 2 - 2), at(w / 2, h / 2 + 2)].filter { $0.isFinite && $0 > 0.1 }.sorted()
        return v.count >= 3 ? Double(v[v.count / 2]) : nil
    }

    private func tick() {
        guard let frame = sceneView.session.currentFrame else { return }
        reading = centre(frame)
        let t = frame.camera.transform
        let fwd = -simd_float3(t.columns.2.x, t.columns.2.y, t.columns.2.z)
        you = (t.columns.3.x, t.columns.3.z, atan2(fwd.x, -fwd.z))
        ticks += 1
        if ticks % 2 == 0 { addToPlan(frame) }
    }

    /// Adds what the LiDAR sees now to the plan. Depth pixels are stored turned from the photo (server/app/meshscan.py, align_depth).
    private func addToPlan(_ frame: ARFrame) {
        guard let map = frame.sceneDepth?.depthMap else { return }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), rb = CVPixelBufferGetBytesPerRow(map), m = (w - h) / 2
        let k = frame.camera.intrinsics, res = frame.camera.imageResolution, s = Float(w) / Float(res.width)
        let fx = k[0][0] * s, fy = k[1][1] * s, cx = k[2][0] * s, cy = k[2][1] * Float(h) / Float(res.height)
        let t = frame.camera.transform
        let floorY = median(ys("floor")), ceilY = median(ys("ceiling"))
        var a = Set<Int64>(), f = Set<Int64>(), c = Set<Int64>()
        for r in stride(from: 0, to: h, by: 12) {
            let row = base.advanced(by: r * rb).assumingMemoryBound(to: Float32.self)
            for col in stride(from: m, to: w - m, by: 12) {
                let d = row[col]
                guard d.isFinite, d > 0.2, d < 5 else { continue }
                let x = Float(r + m) + 0.5, y = Float(w - m - 1 - col) + 0.5
                let p = t * SIMD4<Float>((x - cx) * d / fx, -(y - cy) * d / fy, -d, 1)
                let key = Self.key(p.x, p.z)
                a.insert(key)
                if let floorY, abs(p.y - floorY) < 0.08 { f.insert(key) }
                if let ceilY, abs(p.y - ceilY) < 0.08 { c.insert(key) }
            }
        }
        if !a.isSubset(of: seen) { seen.formUnion(a) }
        if !f.isSubset(of: floorSeen) { floorSeen.formUnion(f) }
        if !c.isSubset(of: ceilingSeen) { ceilingSeen.formUnion(c) }
    }

    func mark(_ kind: String) {
        guard let frame = sceneView.session.currentFrame, case .normal = frame.camera.trackingState else { note = "Move slowly, the phone is finding its place."; return }
        guard let raw = centre(frame) else { note = "No reading there. Move closer, or aim at a matte part."; return }
        let d = Float(CalibrationStore.shared.wall(raw))
        let t = frame.camera.transform
        let fwd = simd_normalize(-simd_float3(t.columns.2.x, t.columns.2.y, t.columns.2.z))
        let p = simd_float3(t.columns.3.x, t.columns.3.y, t.columns.3.z) + fwd * d
        let node = Self.pointNode(kind: kind, number: count(kind) + 1)
        node.simdPosition = p
        sceneView.scene.rootNode.addChildNode(node)
        marks.append(Mark(kind: kind, p: p, node: node))
        note = raw > Self.nearEnough ? "That was over 2 m away: closer is more exact." : ""
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        Voice.say(kind == "floor" ? "Floor point" : "Ceiling point", every: 0)
    }

    func undo() {
        guard let m = marks.popLast() else { return }
        m.node.removeFromParentNode()
    }

    /// A dot on the surface, and a label floating beside it, always facing you.
    private static func pointNode(kind: String, number: Int) -> SCNNode {
        let colour: UIColor = kind == "floor" ? .systemBlue : .systemOrange
        let root = SCNNode()
        let dot = SCNNode(geometry: SCNSphere(radius: 0.02))
        dot.geometry?.firstMaterial?.diffuse.contents = colour
        dot.geometry?.firstMaterial?.readsFromDepthBuffer = false
        dot.renderingOrder = 10
        root.addChildNode(dot)
        let text = SCNText(string: "\(kind == "floor" ? "Floor" : "Ceiling") \(number)", extrusionDepth: 0)
        text.font = .systemFont(ofSize: 10, weight: .bold)
        text.firstMaterial?.diffuse.contents = colour
        text.firstMaterial?.readsFromDepthBuffer = false
        let label = SCNNode(geometry: text)
        label.scale = SCNVector3(0.005, 0.005, 0.005)
        label.position = SCNVector3(0, kind == "floor" ? 0.08 : -0.12, 0)     // floats off the surface, towards the room
        label.constraints = [SCNBillboardConstraint()]
        label.renderingOrder = 10
        root.addChildNode(label)
        return root
    }

    @MainActor func send(server: String) async {
        guard let url = URL(string: server)?.appendingPathComponent("api/scans/\(scanIDForSend)/marks") else { return }
        let body: [String: Any] = ["session": session, "marks": marks.map { ["kind": $0.kind, "x": $0.p.x, "y": $0.p.y, "z": $0.p.z] as [String: Any] }]
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        note = "Saving…"
        do {
            let (data, r) = try await URLSession.shared.data(for: req)
            guard (r as? HTTPURLResponse)?.statusCode == 200 else { note = "The server refused it."; return }
            let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            if let h = (j?["height_m"] as? NSNumber)?.doubleValue {
                note = String(format: "Saved: the room is now %.2f m high.", h)
            } else {
                note = "Saved."
            }
        } catch {
            note = "Could not reach the server. Your points are still here: try again."
        }
    }
    var scanIDForSend = ""
}

struct MarkHeightView: View {
    let scanID: String
    @StateObject private var model = MarkHeightModel()
    @AppStorage("serverURL") private var serverURL = ServerAddress.defaultURL

    var body: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .topTrailing) {
                ARSceneRepresentable(view: model.sceneView)
                Image(systemName: "plus").font(.system(size: 34, weight: .light)).foregroundStyle(.white).shadow(radius: 2)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                MarkPlan(model: model).frame(width: 150, height: 150).padding(10)
            }
            VStack(spacing: 8) {
                Text(heightLine).font(.headline.monospacedDigit())
                if let r = model.reading {
                    Text(String(format: "Crosshair is %.2f m away", r) + (r > MarkHeightModel.nearEnough ? ": move closer" : ""))
                        .font(.caption).foregroundStyle(r > MarkHeightModel.nearEnough ? Color.orange : Color.secondary)
                } else {
                    Text("No reading at the crosshair").font(.caption).foregroundStyle(.orange)
                }
                HStack(spacing: 10) {
                    Button { model.mark("floor") } label: { Text("Floor (\(model.count("floor")))").font(.title3.bold()).frame(maxWidth: .infinity) }
                        .buttonStyle(.borderedProminent).tint(.blue).controlSize(.large)
                    Button { model.mark("ceiling") } label: { Text("Ceiling (\(model.count("ceiling")))").font(.title3.bold()).frame(maxWidth: .infinity) }
                        .buttonStyle(.borderedProminent).tint(.orange).controlSize(.large)
                }
                HStack {
                    Button("Undo") { model.undo() }.disabled(model.marks.isEmpty)
                    Spacer()
                    Button("Save to the scan") { model.scanIDForSend = scanID; Task { await model.send(server: serverURL) } }
                        .buttonStyle(.bordered).disabled(model.marks.isEmpty)
                }
                if !model.note.isEmpty { Text(model.note).font(.callout).fixedSize(horizontal: false, vertical: true) }
            }
            .padding(12)
        }
        .navigationTitle("Floor and ceiling points")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { model.start() }
        .onDisappear { model.stop() }
    }

    private var heightLine: String {
        if let h = model.height { return String(format: "Height %.2f m", h) }
        return "Move close and mark a floor and a ceiling point"
    }
}

/// A plan seen from above: grey = seen by the LiDAR, blue = floor, orange = ceiling, big dots = your points, arrow = you. Gaps are what is missing.
private struct MarkPlan: View {
    @ObservedObject var model: MarkHeightModel

    var body: some View {
        Canvas { ctx, size in
            let cells = model.seen.map(MarkHeightModel.unkey)
            guard !cells.isEmpty else { return }
            let c = CGFloat(MarkHeightModel.cell)
            var xs = cells.map { CGFloat($0.0) * c }, zs = cells.map { CGFloat($0.1) * c }
            if let y = model.you { xs.append(CGFloat(y.x)); zs.append(CGFloat(y.z)) }
            let lox = xs.min()!, hix = xs.max()! + c, loz = zs.min()!, hiz = zs.max()! + c
            let scale = min((size.width - 12) / max(hix - lox, 1), (size.height - 12) / max(hiz - loz, 1))
            func pos(_ x: CGFloat, _ z: CGFloat) -> CGPoint { CGPoint(x: 6 + (x - lox) * scale, y: 6 + (z - loz) * scale) }
            func paint(_ set: Set<Int64>, _ colour: Color) {
                for k in set {
                    let (i, j) = MarkHeightModel.unkey(k)
                    ctx.fill(Path(CGRect(origin: pos(CGFloat(i) * c, CGFloat(j) * c), size: CGSize(width: c * scale + 0.5, height: c * scale + 0.5))), with: .color(colour))
                }
            }
            paint(model.seen, .white.opacity(0.35))
            paint(model.floorSeen, .blue.opacity(0.45))
            paint(model.ceilingSeen, .orange.opacity(0.45))
            for m in model.marks {
                let p = pos(CGFloat(m.p.x), CGFloat(m.p.z))
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8)), with: .color(m.kind == "floor" ? .blue : .orange))
                ctx.stroke(Path(ellipseIn: CGRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8)), with: .color(.white), lineWidth: 1)
            }
            if let y = model.you {
                let p = pos(CGFloat(y.x), CGFloat(y.z)), a = CGFloat(y.heading)
                var tip = Path()
                tip.move(to: CGPoint(x: p.x + sin(a) * 8, y: p.y - cos(a) * 8))
                tip.addLine(to: CGPoint(x: p.x + sin(a + 2.5) * 6, y: p.y - cos(a + 2.5) * 6))
                tip.addLine(to: CGPoint(x: p.x + sin(a - 2.5) * 6, y: p.y - cos(a - 2.5) * 6))
                tip.closeSubpath()
                ctx.fill(tip, with: .color(.white))
            }
        }
        .background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 10))
        .allowsHitTesting(false)
    }
}
