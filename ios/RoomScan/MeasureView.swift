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

private struct PinMeta: Codable {
    var labels: [String: String] = [:]
    var links: [PinLink] = []
}

private func pinNumber(_ name: String) -> Int { Int(name.dropFirst(4)) ?? 0 }

/// Point the camera, read the LiDAR distance at the crosshair. Tap to pin spots: pins are ARKit anchors, so they stay on
/// their spot in the room while you move, and are written to a saved room map so they come back after you leave.
/// Pins are independent: tap a pin to select it, select the ones you want to measure, link pairs you want to keep.
final class MeasureModel: NSObject, ObservableObject, ARSessionDelegate {
    let sceneView = ARSCNView()
    static let minRange = 0.25, maxRange = 5.0
    private static let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    static let mapURL = support.appendingPathComponent("measure.worldmap")
    static let metaURL = support.appendingPathComponent("measure.pins.json")

    @Published var distance: Double?     // smoothed camera-to-target, metres
    @Published var steady = false
    @Published var span: Double?         // the one selected pin to the crosshair, metres
    @Published var frozen: Held?
    @Published var holding = false
    @Published var poor = false          // depth is there but the sensor does not trust it (glass, mirror, shiny black)
    @Published var saved: [Measurement] = []
    @Published var hasLiDAR = true
    @Published var overlay: UIImage?     // live depth, colour coded, drawn over the camera picture
    @Published var showDepth = true
    @Published var pins: [Pin] = []
    @Published var selected: [String] = []     // pin names, in the order they were picked
    @Published var labels: [String: String] = [:]
    @Published var links: [PinLink] = []
    @Published var relocalizing = false
    @Published var actionPin: String?          // the pin whose action menu is open

    var camPos = SIMD3<Float>(0, 0, 0)
    var zoom: CGFloat = 1                // display zoom, so a tap can be matched to a pin
    private var pinAnchors: [String: ARAnchor] = [:]
    private var gone = Set<UUID>()             // anchors removed by Move or Delete: a late update must not bring them back
    private var savedHighest = 0               // highest pin number in the saved room, so a new pin never reuses one not found yet
    private var mapSave: DispatchWorkItem?
    private var mapDirty = false               // pins changed since the map was last written
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
            if self?.live == true { self?.saveMap(thenPause: false) }      // not while another tab has the camera
        }
    }

    func start() {
        CameraOwner.claim(self) { [weak self] in self?.leaveNow() }      // the screen that had the camera stops first
        let cfg = ARWorldTrackingConfiguration()
        hasLiDAR = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
        if hasLiDAR { cfg.frameSemantics.insert(.sceneDepth) }
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) { cfg.sceneReconstruction = .mesh }
        cfg.planeDetection = [.horizontal, .vertical]
        if let data = try? Data(contentsOf: Self.mapURL),
           let map = try? NSKeyedUnarchiver.unarchivedObject(ofClass: ARWorldMap.self, from: data) {
            cfg.initialWorldMap = map   // brings the saved pins back once the phone recognises the place
            loadMeta()
            let names = map.anchors.compactMap(\.name).filter { $0.hasPrefix("pin-") } + Array(labels.keys) + links.flatMap { [$0.a, $0.b] }
            savedHighest = names.map(pinNumber).max() ?? 0
        } else {
            // No saved room: pin numbers start again at 1, so old names and links must not attach to new pins.
            try? FileManager.default.removeItem(at: Self.metaURL)
            labels = [:]; links = []
            savedHighest = 0
        }
        gone = []
        live = true
        pinAnchors = [:]
        pins = []
        selected = []
        sceneView.session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
        Screen.awake(true)
    }

    /// The tab went away. If another screen has already taken the camera, it stopped this one (`leaveNow`): nothing to do. Otherwise
    /// save the map, then pause, keeping the camera until then so the pause can never land on the next tab's session.
    func stop() {
        guard CameraOwner.owns(self) else { return }
        live = false
        mapSave?.cancel()
        Screen.awake(false)
        saveMap(thenPause: true)
    }

    /// Another screen takes the camera: pause now. The map was saved when the pins last changed.
    private func leaveNow() {
        live = false
        mapSave?.cancel()
        sceneView.session.pause()
        Screen.awake(false)
    }

    /// Writes the room map (with the pins in it). Skipped when there are no pins, while the phone is still looking for the saved place,
    /// or before it has mapped enough, so an older good map is never replaced by a bad one.
    private func saveMap(thenPause: Bool) {
        let session = sceneView.session
        let finish = { [weak self] in
            guard thenPause, let self, !self.live, CameraOwner.owns(self) else { return }
            session.pause()
            CameraOwner.drop(self)
        }
        guard !pinAnchors.isEmpty, !relocalizing, let status = session.currentFrame?.worldMappingStatus,
              status == .mapped || status == .extending else {
            // Not mapped enough yet (the first seconds of a session): try again shortly, so new pins are not lost.
            if !thenPause, live, mapDirty, !pinAnchors.isEmpty { scheduleMapSave() }
            finish(); return
        }
        session.getCurrentWorldMap { [weak self] map, _ in
            let ok = map.flatMap { try? NSKeyedArchiver.archivedData(withRootObject: $0, requiringSecureCoding: true) }
                .map { (try? $0.write(to: Self.mapURL, options: .atomic)) != nil } ?? false
            DispatchQueue.main.async {
                if ok { self?.mapDirty = false } else if !thenPause, self?.live == true, self?.mapDirty == true { self?.scheduleMapSave() }
                finish()
            }
        }
    }

    /// After a pin is placed, moved or deleted: save the map a moment later (the anchor is in the session by then), so the pins
    /// come back even when leaving the tab gives no time to save.
    private func scheduleMapSave() {
        mapDirty = true
        mapSave?.cancel()
        let w = DispatchWorkItem { [weak self] in if self?.live == true { self?.saveMap(thenPause: false) } }
        mapSave = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 1, execute: w)
    }

    func saveMeta() {
        if let d = try? JSONEncoder().encode(PinMeta(labels: labels, links: links)) { try? d.write(to: Self.metaURL, options: .atomic) }
    }

    private func loadMeta() {
        guard let d = try? Data(contentsOf: Self.metaURL), let m = try? JSONDecoder().decode(PinMeta.self, from: d) else { return }
        labels = m.labels
        links = m.links
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
        let one = selPins.count == 1 ? selPins[0].pos : nil
        if let one { span = Double(simd_length(point - one)) } else { span = nil }

        if holding {
            // Average the raw reading over the hold second: one frame is noisy, thirty are not.
            let rawNow = one.map { Double(simd_length(camPos + fwd * Float(depth) - $0)) } ?? depth
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
        let kind: CalKind = selPins.count == 1 ? .span : .wall
        let value = kind == .wall ? CalibrationStore.shared.wall(mean) : CalibrationStore.shared.leg(mean)
        frozen = Held(value: value, raw: mean, sd: sd, n: holdBuf.count, kind: kind)
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    // MARK: pins

    private func number(_ name: String) -> Int { pinNumber(name) }

    private func refreshPins() {
        pins = pinAnchors.sorted { number($0.key) < number($1.key) }.map {
            Pin(name: $0.key, pos: SIMD3($0.value.transform.columns.3.x, $0.value.transform.columns.3.y, $0.value.transform.columns.3.z))
        }
    }

    func pin(_ name: String) -> Pin? { pins.first { $0.name == name } }
    var selPins: [Pin] { selected.compactMap(pin) }

    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) { track(anchors) }
    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) { track(anchors) }
    func session(_ session: ARSession, didRemove anchors: [ARAnchor]) {
        // By identifier: Move removes the old anchor and adds a new one with the same name, in either order.
        for a in anchors { if let n = a.name, n.hasPrefix("pin-"), pinAnchors[n]?.identifier == a.identifier { pinAnchors[n] = nil } }
        refreshPins()
    }
    private func track(_ anchors: [ARAnchor]) {
        var changed = false
        for a in anchors where !gone.contains(a.identifier) {
            if let n = a.name, n.hasPrefix("pin-") { pinAnchors[n] = a; changed = true }
        }
        if changed { refreshPins() }
    }

    @objc private func tapped(_ g: UITapGestureRecognizer) {
        let p = g.location(in: sceneView)
        // The readout card and buttons sit at the bottom; a tap there is not on the picture. (Compared on screen: the view is
        // magnified about its centre, so its own y is `zoom` times closer to the middle.)
        let midY = sceneView.bounds.midY
        guard midY + (p.y - midY) * zoom < sceneView.bounds.height - 300 else { return }
        // Only the bullseye in the middle creates a pin: aim it at the spot, then tap it. (The screen is magnified about
        // its centre, so a distance on screen is `zoom` times the distance in the camera view's own coordinates.)
        let centre = CGPoint(x: sceneView.bounds.midX, y: sceneView.bounds.midY)
        if hypot(p.x - centre.x, p.y - centre.y) < 48 / zoom { pinAtCrosshair(); return }
        // A tap on an existing pin opens its menu. A tap anywhere else does nothing.
        let reach = 34 / zoom
        let hit = pins.compactMap { pin -> (Pin, CGFloat)? in
            let s = sceneView.projectPoint(SCNVector3(pin.pos.x, pin.pos.y, pin.pos.z))
            guard s.z > 0 && s.z < 1 else { return nil }
            let d = hypot(CGFloat(s.x) - p.x, CGFloat(s.y) - p.y)
            return d < reach ? (pin, d) : nil
        }.min { $0.1 < $1.1 }
        if let hit { actionPin = hit.0.name; UISelectionFeedbackGenerator().selectionChanged() }
    }

    func toggleSelect(_ name: String) {
        if let i = selected.firstIndex(of: name) { selected.remove(at: i) } else { selected.append(name) }
        UISelectionFeedbackGenerator().selectionChanged()
    }

    private func anchor(_ name: String, at w: SIMD3<Float>) -> ARAnchor {
        var m = matrix_identity_float4x4
        m.columns.3 = SIMD4(w.x, w.y, w.z, 1)
        return ARAnchor(name: name, transform: m)
    }

    private func place(_ w: SIMD3<Float>) {
        let next = max(pinAnchors.keys.map(number).max() ?? 0, savedHighest) + 1
        let name = "pin-\(next)"
        savedHighest = next
        sceneView.session.add(anchor: anchor(name, at: w))
        scheduleMapSave()
        // With one pin selected, the new one joins it so the distance shows at once; otherwise it starts a new pick.
        if selected.count == 1 { selected.append(name) } else { selected = [name] }
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    /// Aim the crosshair, then pin exactly there (no finger covering the spot).
    func pinAtCrosshair() {
        guard let p = lastPoint, let d = distance, d >= Self.minRange, d <= Self.maxRange else { fail(); return }
        place(p)
    }

    /// Re-place an existing pin where the crosshair points now. Its number, name and links stay.
    func moveToCrosshair(_ name: String) {
        guard let p = lastPoint, let d = distance, d >= Self.minRange, d <= Self.maxRange else { fail(); return }
        if let a = pinAnchors[name] { gone.insert(a.identifier); sceneView.session.remove(anchor: a) }
        let moved = anchor(name, at: p)
        pinAnchors[name] = moved
        refreshPins()
        sceneView.session.add(anchor: moved)
        scheduleMapSave()
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    func link(_ a: String, _ b: String) {
        guard a != b, !isLinked(a, b) else { return }
        links.append(PinLink(a: a, b: b))
        saveMeta()
    }

    private func fail() { UINotificationFeedbackGenerator().notificationOccurred(.error) }

    func deletePin(_ name: String) {
        if let a = pinAnchors[name] { gone.insert(a.identifier); sceneView.session.remove(anchor: a) }
        pinAnchors[name] = nil
        labels[name] = nil
        links.removeAll { $0.a == name || $0.b == name }
        selected.removeAll { $0 == name }
        refreshPins()
        saveMeta()
        // The last pin gone: forget the saved room too, or its old map would bring the deleted pins back next time.
        if pinAnchors.isEmpty { forgetSavedRoom() } else { scheduleMapSave() }
    }

    func rename(_ name: String, _ label: String) {
        let t = label.trimmingCharacters(in: .whitespacesAndNewlines)
        labels[name] = t.isEmpty ? nil : String(t.prefix(30))
        saveMeta()
    }

    func isLinked(_ a: String, _ b: String) -> Bool { links.contains { ($0.a == a && $0.b == b) || ($0.a == b && $0.b == a) } }

    func toggleLink() {
        guard selected.count == 2 else { return }
        let a = selected[0], b = selected[1]
        if isLinked(a, b) { links.removeAll { ($0.a == a && $0.b == b) || ($0.a == b && $0.b == a) } } else { links.append(PinLink(a: a, b: b)) }
        saveMeta()
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }

    func clearPins() {
        for a in pinAnchors.values { gone.insert(a.identifier); sceneView.session.remove(anchor: a) }
        pinAnchors = [:]
        pins = []
        selected = []
        labels = [:]
        links = []
        span = nil
        forgetSavedRoom()
    }

    private func forgetSavedRoom() {
        mapSave?.cancel()
        mapDirty = false
        savedHighest = 0
        try? FileManager.default.removeItem(at: Self.mapURL)
        try? FileManager.default.removeItem(at: Self.metaURL)
    }

    /// Raw distance between the two selected pins (nil unless exactly two are selected).
    var pinSpan: Double? {
        let s = selPins
        return s.count == 2 ? Double(simd_length(s[0].pos - s[1].pos)) : nil
    }

    // MARK: depth picture

    /// Colour-codes the depth map (0.3 m red ... 4 m blue) as a translucent picture; shown rotated for portrait.
    private func depthImage(_ frame: ARFrame) -> UIImage? {
        guard let map = (frame.smoothedSceneDepth ?? frame.sceneDepth)?.depthMap else { return nil }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), rb = CVPixelBufferGetBytesPerRow(map)
        // Filled in place and handed to the image as is (no bitmap context, no second copy). The image keeps these bytes while it is
        // on screen, so each picture needs its own buffer; about 200 KB, every third frame.
        var px = Data(count: w * h * 4)
        px.withUnsafeMutableBytes { (buf: UnsafeMutableRawBufferPointer) in
            let out = buf.bindMemory(to: UInt8.self)
            for y in 0..<h {
                let row = base.advanced(by: y * rb).assumingMemoryBound(to: Float32.self)
                for x in 0..<w {
                    let d = row[x]
                    guard d.isFinite, d > 0 else { continue }
                    let i = Int(min(max((d - 0.3) / 3.7, 0), 1) * 255)
                    let o = (y * w + x) * 4
                    out[o] = Self.ramp[i * 3] / 2; out[o + 1] = Self.ramp[i * 3 + 1] / 2; out[o + 2] = Self.ramp[i * 3 + 2] / 2; out[o + 3] = 120
                }
            }
        }
        guard let provider = CGDataProvider(data: px as CFData),
              let cg = CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4,
                               space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                               provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent) else { return nil }
        return UIImage(cgImage: cg, scale: 1, orientation: .right)
    }

    // MARK: readings

    func freeze() {
        let s = selPins
        if s.count >= 2 {   // fixed points: nothing to average. Two pins: the distance; more: the closed perimeter.
            let st = SelStats(s.map(\.pos))
            let raw = st.distance ?? st.perimeter ?? st.path
            frozen = Held(value: CalibrationStore.shared.leg(raw), raw: raw, sd: 0, n: 1, kind: .span)
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
        saved.insert(Measurement(kind: f.kind == .span ? "span" : "point", metres: f.value, raw: f.raw, sd: f.sd,
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
    @State private var showPins = false
    @State private var renaming: String?
    @State private var newName = ""
    @State private var zoom: CGFloat = 1
    @ObservedObject private var cal = CalibrationStore.shared

    /// `kind` says which correction applies to a raw sensor value; `.none` shows the number as is.
    static func text(_ raw: Double, _ kind: CalKind = .none) -> (big: String, small: String) {
        let m = kind == .wall ? CalibrationStore.shared.wall(raw) : kind == .span ? CalibrationStore.shared.leg(raw) : raw
        return (String(format: "%.3f m", m), String(format: "%.1f cm  ·  %.0f mm", m * 100, m * 1000))
    }

    private var pinTitle: String {
        guard let n = model.actionPin, let p = model.pin(n) else { return "Pin" }
        return "Pin \(p.number)" + (model.labels[n].map { $0.isEmpty ? "" : " · " + $0 } ?? "")
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
            // Zoom magnifies the picture (camera + depth colours) about the centre; the lens and the measurement are unchanged.
            // Pins are projected, then moved by the same zoom, so they stay on their spot. The crosshair stays in the centre.
            ARSceneRepresentable(view: model.sceneView).ignoresSafeArea()
                .overlay {
                    GeometryReader { geo in
                        if model.showDepth, let img = model.overlay {
                            Image(uiImage: img).resizable().scaledToFill().frame(width: geo.size.width, height: geo.size.height).clipped()
                        }
                    }
                    .allowsHitTesting(false)
                }
                .scaleEffect(zoom)
                .overlay {
                    GeometryReader { geo in
                        ZStack {
                            PinLayer(model: model, size: geo.size, zoom: zoom)
                            Crosshair(colour: status.1).position(x: geo.size.width / 2, y: geo.size.height / 2)
                            Text("tap to pin").font(.caption2.bold()).padding(.horizontal, 6).padding(.vertical, 1)
                                .background(.black.opacity(0.55), in: Capsule()).foregroundStyle(.white)
                                .position(x: geo.size.width / 2, y: geo.size.height / 2 + 50)
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
                ZoomControl(zoom: $zoom)
                Spacer()
                readout
                controls
            }
            .padding(.horizontal, 16).padding(.bottom, 8)
        }
        .pinchZoom($zoom, enabled: true)
        .onChange(of: zoom) { _, z in model.zoom = z }
        .onAppear {
            model.start()
            if UserDefaults.standard.string(forKey: "measureSheet") == "cal" { showCal = true }   // CI screenshots
        }
        .onDisappear { model.stop() }
        .sheet(isPresented: $showList) { MeasurementList(model: model) }
        .sheet(isPresented: $showCal) { CalibrationView(model: model) }
        .sheet(isPresented: $showPins) { PinsView(model: model) }
        .confirmationDialog(pinTitle, isPresented: Binding(get: { model.actionPin != nil }, set: { if !$0 { model.actionPin = nil } }), titleVisibility: .visible) {
            if let n = model.actionPin {
                Button(model.selected.contains(n) ? "Unselect" : "Select to measure") { model.toggleSelect(n) }
                Button("Measure from here to the crosshair") { model.selected = [n] }
                if model.selected.count == 1, model.selected[0] != n {
                    Button(model.isLinked(n, model.selected[0]) ? "Remove the line to the selected pin" : "Keep a line to the selected pin") {
                        let other = model.selected[0]
                        if model.isLinked(n, other) { model.links.removeAll { ($0.a == n && $0.b == other) || ($0.a == other && $0.b == n) }; model.saveMeta() }
                        else { model.link(n, other) }
                    }
                }
                Button("Name…") { renaming = n; newName = model.labels[n] ?? "" }
                Button("Move to the crosshair") { model.moveToCrosshair(n) }
                Button("Delete", role: .destructive) { model.deletePin(n) }
            }
        }
        .alert("Name this pin", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("e.g. left wall, window top", text: $newName)
            Button("Save") { if let r = renaming { model.rename(r, newName) }; renaming = nil }
            Button("Cancel", role: .cancel) { renaming = nil }
        }
    }

    private var readout: some View {
        let sel = model.selPins
        return VStack(spacing: 4) {
            if sel.count >= 3 {
                let st = SelStats(sel.map(\.pos))
                let f = CalibrationStore.shared.leg(1)
                Text("\(sel.count) pins, in the order picked").font(.caption).foregroundStyle(.secondary)
                Text(String(format: "%.3f m²", (st.area ?? 0) * f * f)).font(.system(size: 36, weight: .bold, design: .rounded)).monospacedDigit()
                Text("perimeter " + Self.text(st.perimeter ?? 0, .span).big + "  ·  path " + Self.text(st.path, .span).big).font(.footnote).foregroundStyle(.secondary)
                Text("angles " + st.angles.map { String(format: "%.1f°", $0) }.joined(separator: "  ")).font(.caption2).foregroundStyle(.secondary)
            } else if sel.count == 2, let s = model.pinSpan {
                let t = Self.text(s, .span)
                let st = SelStats(sel.map(\.pos))
                Text("pin \(sel[0].number) → pin \(sel[1].number)").font(.caption).foregroundStyle(.secondary)
                Text(t.big).font(.system(size: 40, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
                Text("along the floor " + Self.text(st.horizontal ?? 0, .span).big + "  ·  height " + Self.text(st.vertical ?? 0, .span).big)
                    .font(.caption2).foregroundStyle(.secondary)
                Button(model.isLinked(sel[0].name, sel[1].name) ? "Unlink these two" : "Keep a line between these two") { model.toggleLink() }
                    .font(.caption.bold())
            } else if sel.count == 1, let s = model.span {
                let t = Self.text(s, .span)
                Text("pin \(sel[0].number) → here").font(.caption).foregroundStyle(.secondary)
                Text(t.big).font(.system(size: 40, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
            } else if let d = model.distance {
                let t = Self.text(d, .wall)
                Text(t.big).font(.system(size: 44, weight: .bold, design: .rounded)).monospacedDigit()
                Text(t.small).font(.footnote).foregroundStyle(.secondary)
                Text("Aim the crosshair at a spot and press Pin here (or tap the bullseye). Tap two pins to measure between them.").font(.caption2).foregroundStyle(.secondary)
            } else {
                Text("—").font(.system(size: 44, weight: .bold, design: .rounded))
            }
            Label(status.0, systemImage: "circle.fill").font(.caption).foregroundStyle(status.1)
            Button { showCal = true } label: {
                Text(cal.summary).font(.caption2).foregroundStyle(.secondary).underline()
            }
            if !model.selected.isEmpty {
                Button("Clear selection (keeps the pins)") { model.selected = [] }.font(.caption2)
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
        VStack(spacing: 10) {
            // The main action: drops a pin where the crosshair points, so no finger has to hit the bullseye.
            Button { model.pinAtCrosshair() } label: {
                Label("Pin here", systemImage: "plus.viewfinder").font(.title3.bold()).frame(maxWidth: .infinity)
            }
            .buttonStyle(CaptureButtonStyle()).disabled(model.distance == nil)
            HStack(spacing: 10) {
                Button { model.showDepth.toggle(); if !model.showDepth { model.overlay = nil } } label: { Image(systemName: model.showDepth ? "eye.fill" : "eye.slash") }
                    .buttonStyle(CaptureButtonStyle())
                Button { model.freeze() } label: { Label("Hold", systemImage: "hand.raised").lineLimit(1) }
                    .buttonStyle(CaptureButtonStyle()).disabled(model.distance == nil || model.holding)
                Button { showPins = true } label: { Label("\(model.pins.count)", systemImage: "mappin.and.ellipse").lineLimit(1) }
                    .buttonStyle(CaptureButtonStyle())
                Button { showList = true } label: { Label("\(model.saved.count)", systemImage: "list.bullet").lineLimit(1) }
                    .buttonStyle(CaptureButtonStyle())
            }
        }
        .padding(.bottom, 52)
    }
}

/// Pins stay on their spot in the room. In view: a numbered dot (amber when selected) with its name and distance from the
/// camera. Kept links are solid lines with their length; the selected pair is dashed. Out of view: an arrow at the edge.
private struct PinLayer: View {
    @ObservedObject var model: MeasureModel
    let size: CGSize
    let zoom: CGFloat

    private var screen: ScreenProjector {
        ScreenProjector(view: model.sceneView, size: size, zoom: zoom, side: 10, top: 60, bottom: 280, arrowBand: 220, arrowInset: 70)
    }
    private func project(_ w: SIMD3<Float>) -> (pt: CGPoint, front: Bool) { screen.project(w) }
    private func onScreen(_ q: (pt: CGPoint, front: Bool)) -> Bool { screen.onScreen(q) }
    private func edge(_ q: (pt: CGPoint, front: Bool)) -> (CGPoint, Double) { screen.edge(q) }

    /// Lines to draw: every kept link, plus the selected pair when exactly two are selected.
    private func segments() -> [(a: Pin, b: Pin, dashed: Bool)] {
        var out: [(Pin, Pin, Bool)] = []
        let sel = model.selPins
        for l in model.links {
            guard let a = model.pin(l.a), let b = model.pin(l.b) else { continue }
            if sel.count == 2 && model.isLinked(sel[0].name, sel[1].name) && Set([a.name, b.name]) == Set(sel.map(\.name)) { continue }
            out.append((a, b, false))
        }
        if sel.count == 2 { out.append((sel[0], sel[1], true)) }
        return out.map { (a: $0.0, b: $0.1, dashed: $0.2) }
    }

    var body: some View {
        let _ = model.distance   // redraw every frame so pins follow the camera
        let segs = segments()
        ZStack {
            ForEach(0..<segs.count, id: \.self) { i in
                let a = project(segs[i].a.pos), b = project(segs[i].b.pos)
                if a.front && b.front {
                    Path { p in p.move(to: a.pt); p.addLine(to: b.pt) }
                        .stroke(segs[i].dashed ? Color.white : Theme.accent, style: StrokeStyle(lineWidth: 2.5, dash: segs[i].dashed ? [6, 4] : []))
                }
                let m = project((segs[i].a.pos + segs[i].b.pos) / 2)
                if onScreen(m) {
                    Text(MeasureView.text(Double(simd_length(segs[i].a.pos - segs[i].b.pos)), .span).big).font(.caption.bold())
                        .padding(.horizontal, 6).padding(.vertical, 2).background(.black.opacity(0.7), in: Capsule()).foregroundStyle(.white)
                        .position(x: m.pt.x, y: m.pt.y - 16)
                }
            }
            ForEach(model.pins) { pin in
                let q = project(pin.pos)
                let isSel = model.selected.contains(pin.name)
                if onScreen(q) {
                    // The bullseye's centre is the pinned point itself; the text hangs below it so it never shifts the marker.
                    let colour = isSel ? Color.orange : Theme.accent
                    ZStack {
                        Circle().stroke(.white, lineWidth: 3).frame(width: 32, height: 32)
                        Circle().stroke(colour, lineWidth: 3).frame(width: 20, height: 20)
                        Circle().fill(colour).frame(width: 7, height: 7)
                    }
                    .shadow(color: .black.opacity(0.6), radius: 2)
                    .position(x: q.pt.x, y: q.pt.y)
                    VStack(spacing: 2) {
                        Text("\(pin.number)" + (model.labels[pin.name].map { $0.isEmpty ? "" : " · " + $0 } ?? "")).font(.caption2.bold())
                            .padding(.horizontal, 6).padding(.vertical, 1).background(colour, in: Capsule()).foregroundStyle(.white)
                        Text(MeasureView.text(Double(simd_length(pin.pos - model.camPos)), .wall).big).font(.caption2)
                            .padding(.horizontal, 4).background(.black.opacity(0.6), in: Capsule()).foregroundStyle(.white)
                    }.position(x: q.pt.x, y: q.pt.y + 40)
                } else {
                    let e = edge(q)
                    VStack(spacing: 0) {
                        Image(systemName: "location.north.fill").rotationEffect(.radians(e.1)).font(.title3).foregroundStyle(isSel ? Color.orange : Theme.accent)
                        Text("\(pin.number)").font(.caption2.bold()).foregroundStyle(.white)
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
            .overlay { if model.saved.isEmpty { Text("Nothing saved yet.\nAim the crosshair and press Pin here, tap two pins, then Hold and Save.").multilineTextAlignment(.center).foregroundStyle(.secondary) } }
            .navigationTitle("Measurements")
            .toolbar { Button("Done") { dismiss() } }
        }
    }
}
