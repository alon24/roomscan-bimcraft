import ARKit
import SceneKit
import SwiftUI

/// A saved distance, kept on the phone. `span` is between two points; `point` is camera to the wall.
struct Measurement: Codable, Identifiable {
    var id = UUID()
    var date = Date()
    var kind: String
    var metres: Double        // as shown (corrected if the calibration was on)
    var raw: Double?          // what the sensor said, so a later calibration can recompute it
    var sd: Double?           // spread over the averaging second, metres
    var calibrated: Bool?
}

/// A frozen reading: an average over about a second, with its spread.
struct Held {
    var value: Double
    var raw: Double
    var sd: Double
    var n: Int
    var kind: CalKind
}

/// Point the camera, read the LiDAR distance at the crosshair. Tap to pin spots: pins are ARKit anchors, so they stay on
/// their spot in the room while you move, and are written to a saved room map so they come back after you leave.
final class MeasureModel: NSObject, ObservableObject, ARSessionDelegate {
    let sceneView = ARSCNView()
    static let minRange = 0.25, maxRange = 5.0
    static let mapURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("measure.worldmap")

    @Published var distance: Double?     // smoothed camera-to-target, metres
    @Published var steady = false
    @Published var span: Double?         // last pin to the crosshair, metres
    @Published var frozen: Held?
    @Published var holding = false
    @Published var poor = false          // depth is there but the sensor does not trust it (glass, mirror, shiny black)
    @Published var saved: [Measurement] = []
    @Published var hasLiDAR = true
    @Published var overlay: UIImage?     // live depth, colour coded, drawn over the camera picture
    @Published var showDepth = true
    @Published var pins: [SIMD3<Float>] = []
    @Published var relocalizing = false

    var camPos = SIMD3<Float>(0, 0, 0)
    private var pinAnchors: [String: ARAnchor] = [:]
    private var recent: [Double] = []
    private var lastPoint: SIMD3<Float>?
    private var live = true
    private var frameCount = 0
    private var holdBuf: [Double] = []
    private var holdUntil = Date()
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

    override init() {
        super.init()
        sceneView.session.delegate = self
        sceneView.automaticallyUpdatesLighting = true
        // Taps are read on the camera view itself, so the point is in exactly the same coordinates the pins are drawn in.
        sceneView.addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tapped(_:))))
        if let d = UserDefaults.standard.data(forKey: "measurements"), let m = try? JSONDecoder().decode([Measurement].self, from: d) { saved = m }
        NotificationCenter.default.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
            self?.saveMap(thenPause: false)
        }
    }

    func start() {
        let cfg = ARWorldTrackingConfiguration()
        hasLiDAR = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
        if hasLiDAR { cfg.frameSemantics.insert(.sceneDepth) }
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) { cfg.sceneReconstruction = .mesh }
        cfg.planeDetection = [.horizontal, .vertical]
        if let data = try? Data(contentsOf: Self.mapURL),
           let map = try? NSKeyedUnarchiver.unarchivedObject(ofClass: ARWorldMap.self, from: data) {
            cfg.initialWorldMap = map   // brings the saved pins back once the phone recognises the place
        }
        live = true
        pinAnchors = [:]
        pins = []
        sceneView.session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
        Screen.awake(true)
    }

    func stop() {
        live = false
        saveMap(thenPause: true)
        Screen.awake(false)
    }

    /// Writes the room map (with the pins in it). Skipped when there are no pins or the phone has not seen enough yet,
    /// so an older good map is never replaced by a bad one.
    private func saveMap(thenPause: Bool) {
        let session = sceneView.session
        guard !pinAnchors.isEmpty else { if thenPause { session.pause() }; return }
        session.getCurrentWorldMap { [weak self] map, _ in
            if let map, let data = try? NSKeyedArchiver.archivedData(withRootObject: map, requiringSecureCoding: true) {
                try? data.write(to: Self.mapURL, options: .atomic)
            }
            if thenPause, self?.live == false { session.pause() }
        }
    }

    // MARK: depth

    /// Median of the 3x3 depth pixels around a point (normalised camera-image coordinates), ignoring zeros.
    private func depth(_ frame: ARFrame, nx: Double, ny: Double) -> Double? {
        guard let data = frame.smoothedSceneDepth ?? frame.sceneDepth else { return nil }
        let map = data.depthMap
        // The confidence map says how far LiDAR trusts each pixel (0 low, 1 medium, 2 high). Low pixels are ignored.
        let conf = frame.sceneDepth?.confidenceMap ?? data.confidenceMap
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        if let conf { CVPixelBufferLockBaseAddress(conf, .readOnly) }
        defer { if let conf { CVPixelBufferUnlockBaseAddress(conf, .readOnly) } }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), rb = CVPixelBufferGetBytesPerRow(map)
        let cbase = conf.flatMap { CVPixelBufferGetBaseAddress($0) }
        let crb = conf.map { CVPixelBufferGetBytesPerRow($0) } ?? 0
        let cx = min(max(Int(nx * Double(w)), 1), w - 2), cy = min(max(Int(ny * Double(h)), 1), h - 2)
        var v = [Float]()
        for dy in -1...1 { for dx in -1...1 {
            let row = base.advanced(by: (cy + dy) * rb).assumingMemoryBound(to: Float32.self)
            let d = row[cx + dx]
            if let cbase, cbase != base {
                let level = cbase.advanced(by: (cy + dy) * crb).assumingMemoryBound(to: UInt8.self)[cx + dx]
                if level < 1 { continue }
            }
            if d.isFinite, d > 0 { v.append(d) }
        } }
        guard v.count >= 4 else { return nil }
        return Double(v.sorted()[v.count / 2])
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard live else { return }
        let lost: Bool
        if case .limited(.relocalizing) = frame.camera.trackingState { lost = true } else { lost = false }
        if lost != relocalizing { relocalizing = lost }

        var d = depth(frame, nx: 0.5, ny: 0.5)
        if d == nil {
            // No LiDAR: fall back to an ARKit raycast from the screen centre (less exact).
            let c = CGPoint(x: sceneView.bounds.midX, y: sceneView.bounds.midY)
            if let q = sceneView.raycastQuery(from: c, allowing: .estimatedPlane, alignment: .any), let hit = session.raycast(q).first {
                let p = hit.worldTransform.columns.3
                let cam = frame.camera.transform.columns.3
                d = Double(simd_length(SIMD3(p.x - cam.x, p.y - cam.y, p.z - cam.z)))
            }
        }
        let t = frame.camera.transform
        camPos = SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        poor = d == nil && frame.sceneDepth != nil
        guard let depth = d else { distance = nil; steady = false; return }
        recent.append(depth)
        if recent.count > 12 { recent.removeFirst() }
        let med = recent.sorted()[recent.count / 2]
        let spread = (recent.max() ?? 0) - (recent.min() ?? 0)
        distance = med
        steady = recent.count >= 8 && spread < 0.006 && med >= Self.minRange && med <= Self.maxRange

        frameCount += 1
        if showDepth, frameCount % 3 == 0 { overlay = depthImage(frame) }
        let fwd = -SIMD3(t.columns.2.x, t.columns.2.y, t.columns.2.z)
        let point = camPos + fwd * Float(med)
        lastPoint = point
        if let last = pins.last { span = Double(simd_length(point - last)) } else { span = nil }

        if holding {
            // Average the raw reading over the hold second: one frame is noisy, thirty are not.
            let rawNow = pins.last.map { Double(simd_length(camPos + fwd * Float(depth) - $0)) } ?? depth
            holdBuf.append(rawNow)
            if Date() >= holdUntil { finishHold() }
        }
    }

    private func finishHold() {
        holding = false
        guard !holdBuf.isEmpty else { return }
        let n = Double(holdBuf.count)
        let mean = holdBuf.reduce(0, +) / n
        let sd = (holdBuf.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / n).squareRoot()
        let kind: CalKind = pins.isEmpty ? .wall : .span
        let value = kind == .wall ? CalibrationStore.shared.wall(mean) : CalibrationStore.shared.leg(mean)
        frozen = Held(value: value, raw: mean, sd: sd, n: holdBuf.count, kind: kind)
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    // MARK: pins

    private func refreshPins() {
        let ordered = pinAnchors.sorted { (Int($0.key.dropFirst(4)) ?? 0) < (Int($1.key.dropFirst(4)) ?? 0) }
        pins = ordered.map { SIMD3($0.value.transform.columns.3.x, $0.value.transform.columns.3.y, $0.value.transform.columns.3.z) }
    }

    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) { track(anchors) }
    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) { track(anchors) }
    func session(_ session: ARSession, didRemove anchors: [ARAnchor]) {
        for a in anchors { if let n = a.name, n.hasPrefix("pin-") { pinAnchors[n] = nil } }
        refreshPins()
    }
    private func track(_ anchors: [ARAnchor]) {
        var changed = false
        for a in anchors { if let n = a.name, n.hasPrefix("pin-") { pinAnchors[n] = a; changed = true } }
        if changed { refreshPins() }
    }

    @objc private func tapped(_ g: UITapGestureRecognizer) {
        let p = g.location(in: sceneView)
        // The readout card and buttons sit at the bottom; a tap there is not a point on the wall.
        guard p.y < sceneView.bounds.height - 300 else { return }
        addPin(at: p)
    }

    /// Pin the spot under a tap. ARKit gives the ray through that screen point (so the pin is drawn exactly where it was
    /// touched); the LiDAR depth there says how far along the ray the surface is.
    private func addPin(at p: CGPoint) {
        guard let frame = sceneView.session.currentFrame,
              let q = sceneView.raycastQuery(from: p, allowing: .estimatedPlane, alignment: .any) else { fail(); return }
        let dir = simd_normalize(q.direction)
        let fwd = -SIMD3(frame.camera.transform.columns.2.x, frame.camera.transform.columns.2.y, frame.camera.transform.columns.2.z)
        var world: SIMD3<Float>?

        let size = sceneView.bounds.size
        let orientation = sceneView.window?.windowScene?.interfaceOrientation ?? .portrait
        let img = CGPoint(x: p.x / size.width, y: p.y / size.height)
            .applying(frame.displayTransform(for: orientation, viewportSize: size).inverted())
        if let d = depth(frame, nx: Double(img.x), ny: Double(img.y)), d >= Self.minRange, d <= Self.maxRange {
            let along = Float(d) / max(simd_dot(dir, fwd), 0.2)
            world = q.origin + dir * along
        } else if let hit = sceneView.session.raycast(q).first {
            let c = hit.worldTransform.columns.3
            world = SIMD3(c.x, c.y, c.z)
        }
        guard let w = world else { fail(); return }

        var m = matrix_identity_float4x4
        m.columns.3 = SIMD4(w.x, w.y, w.z, 1)
        let next = (pinAnchors.keys.compactMap { Int($0.dropFirst(4)) }.max() ?? 0) + 1
        sceneView.session.add(anchor: ARAnchor(name: "pin-\(next)", transform: m))
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    private func fail() { UINotificationFeedbackGenerator().notificationOccurred(.error) }

    func undoPin() {
        guard let last = pinAnchors.sorted(by: { (Int($0.key.dropFirst(4)) ?? 0) < (Int($1.key.dropFirst(4)) ?? 0) }).last else { return }
        sceneView.session.remove(anchor: last.value)
    }

    func clearPins() {
        for a in pinAnchors.values { sceneView.session.remove(anchor: a) }
        pinAnchors = [:]
        pins = []
        span = nil
        try? FileManager.default.removeItem(at: Self.mapURL)
    }

    /// Distance between the last two pins.
    var pinSpan: Double? { pins.count >= 2 ? Double(simd_length(pins[pins.count - 1] - pins[pins.count - 2])) : nil }

    // MARK: depth picture

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

    // MARK: readings

    func freeze() {
        if let s = pinSpan {   // two pins are fixed points: nothing to average
            frozen = Held(value: CalibrationStore.shared.leg(s), raw: s, sd: 0, n: 1, kind: .span)
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            return
        }
        guard distance != nil else { return }
        holdBuf = []
        holdUntil = Date().addingTimeInterval(1)
        holding = true
    }

    func save() {
        guard let f = frozen else { return }
        saved.insert(Measurement(kind: pins.isEmpty ? "point" : "span", metres: f.value, raw: f.raw, sd: f.sd,
                                 calibrated: abs(f.value - f.raw) > 1e-9), at: 0)
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
    @State private var showCal = false
    @ObservedObject private var cal = CalibrationStore.shared

    /// `kind` says which correction applies to a raw sensor value; `.none` shows the number as is.
    static func text(_ raw: Double, _ kind: CalKind = .none) -> (big: String, small: String) {
        let m = kind == .wall ? CalibrationStore.shared.wall(raw) : kind == .span ? CalibrationStore.shared.leg(raw) : raw
        return (String(format: "%.3f m", m), String(format: "%.1f cm  ·  %.0f mm", m * 100, m * 1000))
    }

    private var status: (String, Color) {
        if model.poor { return ("Can't read this surface (glass, mirror, shiny black?)", .red) }
        if model.relocalizing { return ("Looking for your saved place… move slowly", .orange) }
        guard let d = model.distance else { return ("Point at a surface", .gray) }
        if d < MeasureModel.minRange { return ("Too close, move back", .red) }
        if d > MeasureModel.maxRange { return ("Too far for LiDAR (max 5 m)", .red) }
        return model.steady ? ("Steady, reading is good", .green) : ("Hold still…", .orange)
    }

    var body: some View {
        ZStack {
            // Everything drawn on the picture is an overlay of the camera view itself, so it shares its exact frame.
            ARSceneRepresentable(view: model.sceneView).ignoresSafeArea()
                .overlay {
                    GeometryReader { geo in
                        ZStack {
                            if model.showDepth, let img = model.overlay {
                                Image(uiImage: img).resizable().scaledToFill().frame(width: geo.size.width, height: geo.size.height).clipped()
                            }
                            PinLayer(model: model, size: geo.size)
                            Crosshair(colour: status.1).position(x: geo.size.width / 2, y: geo.size.height / 2)
                        }
                    }
                    .allowsHitTesting(false)
                }
                .ignoresSafeArea()

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
        .onAppear {
            model.start()
            if UserDefaults.standard.string(forKey: "measureSheet") == "cal" { showCal = true }   // CI screenshots
        }
        .onDisappear { model.stop() }
        .sheet(isPresented: $showList) { MeasurementList(model: model) }
        .sheet(isPresented: $showCal) { CalibrationView(model: model) }
    }

    private var readout: some View {
        VStack(spacing: 4) {
            if let s = model.pinSpan {
                let t = Self.text(s, .span)
                Text("pin \(model.pins.count - 1) → pin \(model.pins.count)").font(.caption).foregroundStyle(.secondary)
                Text(t.big).font(.system(size: 40, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
                if let c = model.span { Text("crosshair from last pin " + Self.text(c, .span).big).font(.caption2).foregroundStyle(.secondary) }
            } else if let s = model.span {
                let t = Self.text(s, .span)
                Text("pin 1 → here").font(.caption).foregroundStyle(.secondary)
                Text(t.big).font(.system(size: 40, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
            } else if let d = model.distance {
                let t = Self.text(d, .wall)
                Text(t.big).font(.system(size: 44, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
                Text("Tap the picture to pin a spot").font(.caption2).foregroundStyle(.secondary)
            } else {
                Text("—").font(.system(size: 44, weight: .bold, design: .rounded))
            }
            Label(status.0, systemImage: "circle.fill").font(.caption).foregroundStyle(status.1)
            Button { showCal = true } label: {
                Text(cal.summary).font(.caption2).foregroundStyle(.secondary).underline()
            }
            if model.holding { Text("Averaging for 1 s, keep still…").font(.caption).foregroundStyle(.orange) }
            if let f = model.frozen {
                HStack {
                    Text("Held: " + Self.text(f.value).big + (f.sd > 0 ? String(format: " ± %.1f cm", f.sd * 100) : "")).font(.headline)
                    Button("Save") { model.save() }.buttonStyle(.borderedProminent)
                    Button("Drop") { model.frozen = nil }.buttonStyle(.bordered)
                }.padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity).padding(12).card()
    }

    private var controls: some View {
        HStack(spacing: 10) {
            Button { model.showDepth.toggle(); if !model.showDepth { model.overlay = nil } } label: { Image(systemName: model.showDepth ? "eye.fill" : "eye.slash") }
                .buttonStyle(CaptureButtonStyle())
            Button { model.freeze() } label: { Label("Hold", systemImage: "hand.raised").lineLimit(1) }
                .buttonStyle(CaptureButtonStyle()).disabled(model.distance == nil || model.holding)
            Button { model.undoPin() } label: { Image(systemName: "arrow.uturn.backward") }
                .buttonStyle(CaptureButtonStyle()).disabled(model.pins.isEmpty)
            Button { model.clearPins() } label: { Image(systemName: "trash") }
                .buttonStyle(CaptureButtonStyle()).disabled(model.pins.isEmpty)
            Button { showList = true } label: { Label("\(model.saved.count)", systemImage: "list.bullet").lineLimit(1) }
                .buttonStyle(CaptureButtonStyle())
        }
        .padding(.bottom, 52)
    }
}

/// Pins stay on their spot in the room. In view: a numbered dot with its distance from the camera and dashed lines with the
/// length of each leg. Out of view: an arrow at the screen edge pointing the way to turn.
private struct PinLayer: View {
    @ObservedObject var model: MeasureModel
    let size: CGSize

    private func project(_ w: SIMD3<Float>) -> (pt: CGPoint, front: Bool) {
        let p = model.sceneView.projectPoint(SCNVector3(w.x, w.y, w.z))
        return (CGPoint(x: CGFloat(p.x), y: CGFloat(p.y)), p.z > 0 && p.z < 1)
    }

    private func onScreen(_ q: (pt: CGPoint, front: Bool)) -> Bool {
        q.front && q.pt.x > 10 && q.pt.x < size.width - 10 && q.pt.y > 60 && q.pt.y < size.height - 280
    }

    /// Where the arrow sits: on the edge of the usable area, in the direction of the pin.
    private func edge(_ q: (pt: CGPoint, front: Bool)) -> (CGPoint, Double) {
        let c = CGPoint(x: size.width / 2, y: (size.height - 220) / 2)
        var dx = q.pt.x - c.x, dy = q.pt.y - c.y
        if !q.front { dx = -dx; dy = -dy }
        if abs(dx) < 1 && abs(dy) < 1 { dy = -1 }
        let hx = size.width / 2 - 36, hy = (size.height - 220) / 2 - 70
        let s = min(hx / max(abs(dx), 0.001), hy / max(abs(dy), 0.001))
        return (CGPoint(x: c.x + dx * s, y: c.y + dy * s), Double(atan2(dy, dx)) + .pi / 2)
    }

    var body: some View {
        let _ = model.distance   // redraw every frame so pins follow the camera
        let pts = model.pins.map(project)
        ZStack {
            ForEach(0..<max(pts.count - 1, 0), id: \.self) { i in
                let a = model.pins[i], b = model.pins[i + 1]
                if pts[i].front && pts[i + 1].front {
                    Path { p in p.move(to: pts[i].pt); p.addLine(to: pts[i + 1].pt) }
                        .stroke(.white, style: StrokeStyle(lineWidth: 2, dash: [6, 4]))
                }
                let m = project((a + b) / 2)
                if onScreen(m) {
                    Text(MeasureView.text(Double(simd_length(b - a)), .span).big).font(.caption.bold())
                        .padding(.horizontal, 6).padding(.vertical, 2).background(.black.opacity(0.7), in: Capsule()).foregroundStyle(.white)
                        .position(x: m.pt.x, y: m.pt.y - 16)
                }
            }
            ForEach(0..<pts.count, id: \.self) { i in
                if onScreen(pts[i]) {
                    VStack(spacing: 2) {
                        Text("\(i + 1)").font(.caption2.bold()).foregroundStyle(.white).frame(width: 20, height: 20)
                            .background(Theme.accent, in: Circle()).overlay(Circle().stroke(.white, lineWidth: 2))
                        Text(MeasureView.text(Double(simd_length(model.pins[i] - model.camPos)), .wall).big).font(.caption2.bold())
                            .padding(3).background(.black.opacity(0.7), in: Capsule()).foregroundStyle(.white)
                    }.position(x: pts[i].pt.x, y: pts[i].pt.y + 14)
                } else {
                    let e = edge(pts[i])
                    VStack(spacing: 0) {
                        Image(systemName: "location.north.fill").rotationEffect(.radians(e.1)).font(.title3).foregroundStyle(Theme.accent)
                        Text("\(i + 1)").font(.caption2.bold()).foregroundStyle(.white)
                            .padding(.horizontal, 5).background(.black.opacity(0.7), in: Capsule())
                    }.position(e.0)
                }
            }
        }
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
