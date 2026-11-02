import SwiftUI
import ARKit
import SceneKit

/// Walk the room and draw its plan live: aim at a corner, at the floor or the ceiling, close up, and tap. The phone keeps its place as you walk, so the corners
/// land in one drawing with their lengths. Stretches with no wall, as into a kitchen, can be marked open. All the working out stays on the server: this
/// sends the corners and what the LiDAR saw, and the server fits any 360 photos taken afterwards onto the plan.
final class SketchPlanModel: NSObject, ObservableObject {
    struct Corner: Identifiable { let id = UUID(); let x: Float; let y: Float; let z: Float; var openToNext = false }

    let sceneView = ARSCNView(frame: .zero)
    @Published var corners: [Corner] = []
    @Published var closed = false
    @Published var nextOpen = false
    @Published var reading: Double?
    @Published var aim: SIMD3<Float>?
    @Published var you: (x: Float, z: Float, heading: Float)?
    @Published var walls = Set<Int64>()
    @Published var level: PhoneLevel?
    @Published var floorY: Float?
    @Published var ceilingY: Float?
    @Published var note = ""
    @Published var saving = false
    @Published var planID: String?

    static let cell: Float = 0.1             // of the plan on the screen
    static let voxel: Float = 0.05           // of what is sent
    static let maxVoxels = 80_000
    static let nearEnough = 2.5              // metres: further than this a corner is less exact
    private var voxels: [Int64: SIMD3<Float>] = [:]
    private var bins: [Int: Int] = [:]       // readings per 5 cm of height
    private let root = SCNNode()
    private var timer: Timer?
    private var ticks = 0

    var height: Double? {
        guard let f = floorY, let c = ceilingY else { return nil }
        return Double(c - f)
    }
    var canSave: Bool { corners.count >= 3 && height != nil && !saving }

    /// What to do next, shown and spoken.
    var guide: String {
        if planID != nil { return "Saved. Add 360 photos from one spot, or open the plan on the server." }
        if closed { return "The room is closed. Tap Save plan." }
        if let r = reading, r > Self.nearEnough { return "Move closer: the corner is more exact from under \(String(format: "%.1f", Self.nearEnough)) metres." }
        if corners.isEmpty { return "Walk to a corner of the room. Aim the cross at it, at the floor or the ceiling, and tap Corner." }
        if floorY == nil { return "Tip the phone down so it sees the floor." }
        if ceilingY == nil { return "Tip the phone up so it sees the ceiling." }
        if nextOpen { return "Next stretch has no wall. Walk to where the wall starts again and tap Corner." }
        return "Follow the wall to the next corner and tap Corner."
    }

    static func key2(_ x: Float, _ z: Float) -> Int64 {
        (Int64((x / cell).rounded(.down)) + 100000) * 200003 + (Int64((z / cell).rounded(.down)) + 100000)
    }
    static func unkey2(_ k: Int64) -> (Int, Int) { (Int(k / 200003) - 100000, Int(k % 200003) - 100000) }
    private static func key3(_ p: SIMD3<Float>) -> Int64 {
        let x = Int64((p.x / voxel).rounded(.down)) + 20000, y = Int64((p.y / voxel).rounded(.down)) + 20000, z = Int64((p.z / voxel).rounded(.down)) + 20000
        return x * 1_600_000_000 + y * 40000 + z
    }

    func start() {
        let cfg = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        CameraOwner.claim(self) { [weak self] in self?.sceneView.session.pause(); Screen.awake(false) }
        Screen.awake(true)
        sceneView.session.run(cfg)
        if root.parent == nil { sceneView.scene.rootNode.addChildNode(root) }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.tick() }
    }

    func stop() { timer?.invalidate(); timer = nil; CameraOwner.release(self) }

    private func tick() {
        guard let frame = sceneView.session.currentFrame else { return }
        let t = frame.camera.transform
        level = PhoneLevel(t)
        reading = centre(frame)
        let pos = SIMD3<Float>(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let fwd = simd_normalize(-SIMD3<Float>(t.columns.2.x, t.columns.2.y, t.columns.2.z))
        if let r = reading { aim = pos + fwd * Float(CalibrationStore.shared.wall(r)) } else { aim = nil }
        you = (pos.x, pos.z, atan2(fwd.x, -fwd.z))
        ticks += 1
        if ticks % 2 == 0, case .normal = frame.camera.trackingState { addSeen(frame) }
        if ticks % 10 == 0 { findHeights(phoneY: pos.y); Voice.say(guide, every: 12) }
    }

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

    /// What the LiDAR sees now. Depth pixels are stored turned from the photo (server/app/meshscan.py, align_depth).
    private func addSeen(_ frame: ARFrame) {
        guard let map = frame.sceneDepth?.depthMap else { return }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), rb = CVPixelBufferGetBytesPerRow(map), m = (w - h) / 2
        let k = frame.camera.intrinsics, res = frame.camera.imageResolution, s = Float(w) / Float(res.width)
        let fx = k[0][0] * s, fy = k[1][1] * s, cx = k[2][0] * s, cy = k[2][1] * Float(h) / Float(res.height)
        let t = frame.camera.transform
        var fresh = Set<Int64>()
        for r in stride(from: 0, to: h, by: 8) {
            let row = base.advanced(by: r * rb).assumingMemoryBound(to: Float32.self)
            for col in stride(from: m, to: w - m, by: 8) {
                let d = row[col]
                guard d.isFinite, d > 0.2, d < 5 else { continue }
                let x = Float(r + m) + 0.5, y = Float(w - m - 1 - col) + 0.5
                let p4 = t * SIMD4<Float>((x - cx) * d / fx, -(y - cy) * d / fy, -d, 1)
                let p = SIMD3<Float>(p4.x, p4.y, p4.z)
                bins[Int((p.y / 0.05).rounded(.down)), default: 0] += 1
                if voxels.count < Self.maxVoxels { voxels[Self.key3(p)] = p }
                if let f = floorY, p.y < f + 0.3 { continue }            // the plan on the screen shows walls, not the floor or the ceiling
                if let c = ceilingY, p.y > c - 0.1 { continue }
                fresh.insert(Self.key2(p.x, p.z))
            }
        }
        if !fresh.isSubset(of: walls) { walls.formUnion(fresh) }
    }

    /// Floor and ceiling: flat surfaces pile up many readings at one height, walls spread theirs out.
    private func findHeights(phoneY: Float) {
        func peak(_ keep: (Float) -> Bool) -> Float? {
            let side = bins.filter { keep((Float($0.key) + 0.5) * 0.05) }
            guard let top = side.max(by: { $0.value < $1.value }), top.value >= 60 else { return nil }
            let others = side.values.sorted()
            guard top.value >= 3 * others[others.count / 2] else { return nil }
            return (Float(top.key) + 0.5) * 0.05
        }
        let f = peak { $0 < phoneY - 0.4 }, c = peak { $0 > phoneY + 0.15 }
        var changed = false
        if let f, floorY.map({ abs($0 - f) > 0.02 }) ?? true { floorY = f; changed = true }
        if let c, ceilingY.map({ abs($0 - c) > 0.02 }) ?? true { ceilingY = c; changed = true }
        if changed { redraw() }
    }

    func addCorner() {
        guard let frame = sceneView.session.currentFrame, case .normal = frame.camera.trackingState else { note = "Move slowly, the phone is finding its place."; return }
        guard !closed else { return }
        guard let p = aim else { note = "No reading there. Move closer, or aim at a matte part."; return }
        note = ""
        let last = corners.indices.last
        if let last { corners[last].openToNext = nextOpen }
        nextOpen = false
        corners.append(Corner(x: p.x, y: p.y, z: p.z))
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        if let last {
            let a = corners[last]
            Voice.say(String(format: "%.2f metres", hypot(p.x - a.x, p.z - a.z)), every: 0)
        }
        redraw()
    }

    func close() {
        guard corners.count >= 3 else { return }
        corners[corners.count - 1].openToNext = nextOpen
        nextOpen = false
        closed = true
        redraw()
    }

    func undo() {
        if closed { closed = false; corners[corners.count - 1].openToNext = false; redraw(); return }
        guard !corners.isEmpty else { return }
        corners.removeLast()
        if !corners.isEmpty { corners[corners.count - 1].openToNext = false }
        redraw()
    }

    func startOver() {
        corners = []; closed = false; nextOpen = false; planID = nil; note = ""
        redraw()
    }

    /// The posts and lines floating in the room, so the plan can be seen against the real walls.
    private func redraw() {
        root.childNodes.forEach { $0.removeFromParentNode() }
        for (i, c) in corners.enumerated() {
            let lo = floorY ?? (c.y - 0.05), hi = ceilingY ?? (c.y + 0.05)
            root.addChildNode(Self.bar(SIMD3(c.x, lo, c.z), SIMD3(c.x, hi, c.z), .white, 0.012))
            let hasNext = i + 1 < corners.count || closed
            guard hasNext else { continue }
            let n = corners[(i + 1) % corners.count], colour: UIColor = c.openToNext ? .systemOrange : .systemGreen
            root.addChildNode(Self.bar(SIMD3(c.x, lo, c.z), SIMD3(n.x, lo, n.z), colour, 0.012))
            root.addChildNode(Self.bar(SIMD3(c.x, hi, c.z), SIMD3(n.x, hi, n.z), colour, 0.012))
        }
    }

    private static func bar(_ a: SIMD3<Float>, _ b: SIMD3<Float>, _ colour: UIColor, _ radius: CGFloat) -> SCNNode {
        let d = b - a, len = simd_length(d)
        let g = SCNCylinder(radius: radius, height: CGFloat(max(len, 0.001)))
        g.firstMaterial?.diffuse.contents = colour
        g.firstMaterial?.lightingModel = .constant
        g.firstMaterial?.readsFromDepthBuffer = false
        let n = SCNNode(geometry: g)
        n.simdPosition = (a + b) / 2
        if len > 0.001 { n.simdOrientation = simd_quatf(from: SIMD3<Float>(0, 1, 0), to: d / len) }
        n.renderingOrder = 10
        return n
    }

    @MainActor func save(server: String) async {
        guard canSave, let f = floorY, let c = ceilingY, let url = URL(string: server)?.appendingPathComponent("api/plans") else { return }
        var gaps = corners.map(\.openToNext)
        if !closed { gaps[gaps.count - 1] = true }           // never closed: the way back to the first corner isn't a wall anyone marked
        var cloud: [Float] = []
        cloud.reserveCapacity(voxels.count * 3)
        for p in voxels.values { cloud.append(p.x); cloud.append(p.y); cloud.append(p.z) }
        let body: [String: Any] = ["corners": corners.map { [$0.x, $0.z] }, "open": gaps, "floor_y": f, "ceiling_y": c, "cloud": cloud,
                                   "name": "Plan " + Date().formatted(), "project": UserDefaults.standard.string(forKey: "project") ?? "", "info": ["app": AppVersion.text]]
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 60
        saving = true
        note = "Saving…"
        defer { saving = false }
        do {
            let (data, r) = try await URLSession.shared.data(for: req)
            guard (r as? HTTPURLResponse)?.statusCode == 200 else {
                let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                note = (j?["detail"] as? String) ?? "The server refused it."
                return
            }
            let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            planID = j?["id"] as? String
            note = ""
            Voice.say("Plan saved.", every: 0)
        } catch {
            note = "Could not reach the server: \(error.localizedDescription). The plan is still here, so try again."
        }
    }
}

struct SketchPlanView: View {
    @StateObject private var model = SketchPlanModel()
    @AppStorage("serverURL") private var serverURL = ServerAddress.defaultURL

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                ARSceneRepresentable(view: model.sceneView)
                Image(systemName: "plus").font(.system(size: 34, weight: .light)).foregroundStyle(.white).shadow(radius: 2)
                LevelLine(level: model.level)
            }
            SketchMap(model: model).frame(height: 190)
            VStack(spacing: 6) {
                Text(model.guide).font(.footnote).multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                Text(line).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                if let id = model.planID {
                    NavigationLink("Add 360 photos from one spot") { MeshScanView(sweepMode: true, liveGuide: true, joinPlan: id) }
                        .buttonStyle(.borderedProminent)
                    HStack {
                        NavigationLink("Open the plan") { WebPage(scanID: id) }
                        Spacer()
                        Button("New plan") { model.startOver() }
                    }
                } else {
                    Button { model.addCorner() } label: { Text("Corner").font(.headline).frame(maxWidth: .infinity) }
                        .buttonStyle(.borderedProminent).controlSize(.large).disabled(model.closed)
                    HStack(spacing: 10) {
                        Button(model.nextOpen ? "Next: no wall" : "Next: wall") { model.nextOpen.toggle() }
                            .tint(model.nextOpen ? .orange : .accentColor).disabled(model.closed || model.corners.isEmpty)
                        Button("Close room") { model.close() }.disabled(model.closed || model.corners.count < 3)
                        Button("Undo") { model.undo() }.disabled(model.corners.isEmpty)
                        VoiceToggle()
                        Spacer()
                        Button("Save plan") { Task { await model.save(server: serverURL) } }.disabled(!model.canSave)
                    }
                    .font(.footnote).buttonStyle(.bordered)
                }
                if !model.note.isEmpty { Text(model.note).font(.footnote).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true) }
            }
            .padding(10)
        }
        .onAppear { model.start() }
        .onDisappear { model.stop() }
    }

    private var line: String {
        var bits = ["\(model.corners.count) corners"]
        if let h = model.height { bits.append(String(format: "height %.2f m", h)) }
        if let r = model.reading { bits.append(String(format: "cross %.2f m away", r)) } else { bits.append("no reading at the cross") }
        return bits.joined(separator: " · ")
    }
}

/// The plan as it grows: grey = walls the LiDAR has seen, black = what you marked, dashed orange = no wall, dashed green = from the last corner to the cross.
private struct SketchMap: View {
    @ObservedObject var model: SketchPlanModel

    var body: some View {
        Canvas { ctx, size in
            ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Color(.secondarySystemBackground)))
            let cell = SketchPlanModel.cell
            var xs: [Float] = [], zs: [Float] = []
            let cells = model.walls.map { SketchPlanModel.unkey2($0) }
            for c in cells { xs.append((Float(c.0) + 0.5) * cell); zs.append((Float(c.1) + 0.5) * cell) }
            for c in model.corners { xs.append(c.x); zs.append(c.z) }
            if let y = model.you { xs.append(y.x); zs.append(y.z) }
            guard let x0 = xs.min(), let x1 = xs.max(), let z0 = zs.min(), let z1 = zs.max() else { return }
            let pad: CGFloat = 24
            let scale = min((size.width - 2 * pad) / CGFloat(max(x1 - x0, 1)), (size.height - 2 * pad) / CGFloat(max(z1 - z0, 1)))
            let midX = CGFloat(x0 + x1) / 2, midZ = CGFloat(z0 + z1) / 2
            func at(_ x: Float, _ z: Float) -> CGPoint {
                CGPoint(x: size.width / 2 + (CGFloat(x) - midX) * scale, y: size.height / 2 + (CGFloat(z) - midZ) * scale)
            }
            let side = max(1.5, CGFloat(cell) * scale)
            var seen = Path()
            for c in cells {
                let p = at((Float(c.0) + 0.5) * cell, (Float(c.1) + 0.5) * cell)
                seen.addRect(CGRect(x: p.x - side / 2, y: p.y - side / 2, width: side, height: side))
            }
            ctx.fill(seen, with: .color(.gray.opacity(0.45)))
            func stretch(_ a: CGPoint, _ b: CGPoint, _ colour: Color, dashed: Bool, label: String?) {
                var l = Path(); l.move(to: a); l.addLine(to: b)
                ctx.stroke(l, with: .color(colour), style: StrokeStyle(lineWidth: dashed ? 1.5 : 2.5, lineCap: .round, dash: dashed ? [3, 5] : []))
                if let label {
                    ctx.draw(Text(label).font(.system(size: 10, weight: .semibold).monospacedDigit()).foregroundColor(colour), at: CGPoint(x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 8))
                }
            }
            let cs = model.corners
            for (i, c) in cs.enumerated() where i + 1 < cs.count || model.closed {
                let n = cs[(i + 1) % cs.count]
                stretch(at(c.x, c.z), at(n.x, n.z), c.openToNext ? .orange : .primary, dashed: c.openToNext, label: String(format: "%.2f", hypot(n.x - c.x, n.z - c.z)))
            }
            if let last = cs.last, !model.closed, let aim = model.aim {
                stretch(at(last.x, last.z), at(aim.x, aim.z), model.nextOpen ? .orange : .green, dashed: true, label: String(format: "%.2f", hypot(aim.x - last.x, aim.z - last.z)))
            }
            for c in cs {
                let p = at(c.x, c.z)
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8)), with: .color(.primary))
            }
            if let y = model.you {
                let p = at(y.x, y.z), a = CGFloat(y.heading)
                var arrow = Path()
                arrow.move(to: CGPoint(x: p.x + sin(a) * 9, y: p.y - cos(a) * 9))
                arrow.addLine(to: CGPoint(x: p.x + sin(a + 2.5) * 6, y: p.y - cos(a + 2.5) * 6))
                arrow.addLine(to: CGPoint(x: p.x + sin(a - 2.5) * 6, y: p.y - cos(a - 2.5) * 6))
                arrow.closeSubpath()
                ctx.fill(arrow, with: .color(.blue))
            }
        }
    }
}
