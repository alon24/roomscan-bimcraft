import SwiftUI
import ARKit
import SceneKit
import CoreMotion

/// Guided object scan: remembers where the part is in the room (ARKit fuses camera, gyro, accelerometer and LiDAR
/// into one world frame) and tracks from which directions it has been seen. A dotted dome around the part in AR and a
/// radar on screen show what is captured (green) and what is missing (orange), so nothing is left out.
final class CoverageTracker: ObservableObject {
    enum Phase { case off, placing, tracing, ready, capturing }

    // Viewing directions around the part: three bands of elevation, each split into sectors of azimuth.
    static let sectors = [24, 16, 8]          // side, diagonal, top
    static let offsets = [0, 24, 40]
    static let bandElev: [Double] = [8, 38, 72]   // degrees, band centres
    static let cellCount = 48

    @Published var mode = 0                    // chosen with the picker: 0 free scan, 1 guided object, 2 turntable (phone fixed, plate turns)
    var enabled: Bool { mode != 0 }
    var turntable: Bool { mode == 2 }
    var ring: Bool { mode == 3 }               // printed marker ring: every frame is placed by the markers, the phone may move freely
    @Published var framesTaken = 0
    @Published var speed: RingSpeedMonitor.Reading?       // ring scans: how fast the tray is turning, read from the markers
    private let speedMonitor = RingSpeedMonitor()
    // Ring scans: which angles round the ring have been seen at which height (3 heights x 24 sectors of 15 degrees), same bands as the server's report
    @Published var ringCells = [Bool](repeating: false, count: 72)
    @Published var ringHint = ""
    static let ringSectors = 24
    // Camera angle above the ring plane that counts as low / middle / high. Real scans only ever reached 33 to 44 degrees (holding the
    // phone by hand with the markers in view), so a 25 degree edge for "low" was never reached: the outer ring would not fill.
    static let lowMax = 32.0, midMax = 50.0
    static func ringBand(_ el: Double) -> Int { el < lowMax ? 0 : (el < midMax ? 1 : 2) }
    static func bandName(_ b: Int) -> String { ["low", "middle", "high"][b] }
    private var ringReadings = 0
    private func noteRing(_ r: RingSpeedMonitor.Reading) {
        guard let az = r.az, let el = r.elev else { return }
        ringReadings += 1
        if ringReadings % 8 == 0 { Reporter.log("ring angle \(Int(el))° az \(Int(az))° band \(Self.bandName(Self.ringBand(el)))") }   // so a report shows which angles were reached
        let band = Self.ringBand(el)
        ringCells[band * Self.ringSectors + min(Self.ringSectors - 1, Int(az / (360 / Double(Self.ringSectors))))] = true
        ringHint = Self.hint(for: ringCells)
    }
    static func hint(for cells: [Bool]) -> String {
        func share(_ b: Int) -> Double { Double(cells[b * ringSectors..<(b + 1) * ringSectors].filter { $0 }.count) / Double(ringSectors) }
        let low = share(0), mid = share(1), high = share(2)
        if mid < 0.85 { return "Turn the tray slowly: the first turn is \(Int(mid * 100))% done." }
        if low < 0.7 { return "Now lower the phone until the angle reads under \(Int(lowMax))°, and turn the tray once more." }
        if high < 0.5 { return "Last: raise the phone until the angle reads over \(Int(midMax))°, looking down from above, for a quarter turn or more." }
        return "The whole ring is covered at every height. Tap Stop."
    }
    private var turntableTicks = 0
    @Published var phase: Phase = .off
    @Published var radius: Float = 0.15 { didSet { if phase == .ready { refresh() } } }
    @Published var cells = [Bool](repeating: false, count: CoverageTracker.cellCount)
    @Published var camAz = 0.0
    @Published var camElev = 0.0
    @Published var hint = ""
    @Published var tracePoints: [SIMD3<Float>] = []   // outline of the part's base, tapped on the screen
    @Published var height: Float = 0.10 { didSet { if phase == .ready { refresh() } } }
    private(set) var polygon: [SIMD2<Float>] = []      // closed outline in the world's X/Z plane
    private(set) var baseY: Float = 0
    private(set) var fitRadius: Float = 0.15
    private let traceNode = SCNNode()
    var hasOutline: Bool { polygon.count >= 3 }

    private(set) var centre = SIMD3<Float>(0, 0, 0)
    private var hitPoint = SIMD3<Float>(0, 0, 0)
    private var hitDir = SIMD3<Float>(0, 0, -1)
    private let objectNode = SCNNode()
    private let domeNode = SCNNode()
    private var dots: [SCNNode] = []
    private let motion = CMMotionManager()
    private var announced = false

    var captured: Int { cells.filter { $0 }.count }
    var coverage: Double { Double(captured) / Double(Self.cellCount) }
    var complete: Bool { coverage >= 0.85 }
    var isActive: Bool { phase != .off }
    var acceptsPoints: Bool { phase == .off || phase == .capturing }

    func attach(to scene: SCNScene) {
        scene.rootNode.addChildNode(objectNode)
        scene.rootNode.addChildNode(domeNode)
        scene.rootNode.addChildNode(traceNode)
    }

    func begin() {
        speedMonitor.onReading = { [weak self] r in self?.speed = r; self?.noteRing(r) }
        speed = nil
        ringCells = [Bool](repeating: false, count: 72); ringHint = ""
        phase = enabled ? (ring ? .capturing : .placing) : .off
        cells = [Bool](repeating: false, count: Self.cellCount)
        hint = ""
        announced = false
        tracePoints = []; polygon = []
        framesTaken = 0; turntableTicks = 0
        if ring { speedMonitor.reset() }
        clearNodes()
        if enabled, motion.isDeviceMotionAvailable {
            motion.deviceMotionUpdateInterval = 1.0 / 30
            motion.startDeviceMotionUpdates()
        }
    }

    func end() {
        motion.stopDeviceMotionUpdates()
        speed = nil
        phase = .off
        clearNodes()
    }

    /// Is this world point on the part (inside the capture ball)?
    func contains(_ p: SIMD3<Float>) -> Bool {
        guard phase == .capturing, !ring else { return true }
        guard hasOutline else { return simd_distance(p, centre) <= radius * 1.5 }
        guard p.y >= baseY - 0.02, p.y <= baseY + height + 0.02 else { return false }
        return Self.inside(SIMD2(p.x, p.z), polygon, grow: 1.1)
    }

    /// Point in polygon (ray casting), with the polygon scaled about its centre by `grow` to leave a margin.
    static func inside(_ p: SIMD2<Float>, _ poly: [SIMD2<Float>], grow: Float) -> Bool {
        let c = poly.reduce(SIMD2<Float>(0, 0), +) / Float(poly.count)
        let q = poly.map { c + ($0 - c) * grow }
        var inside = false
        var j = q.count - 1
        for i in 0..<q.count {
            if (q[i].y > p.y) != (q[j].y > p.y), p.x < (q[j].x - q[i].x) * (p.y - q[i].y) / (q[j].y - q[i].y) + q[i].x { inside.toggle() }
            j = i
        }
        return inside
    }

    // MARK: tracing the outline of the part's base
    func startTracing() {
        tracePoints = []; polygon = []
        phase = .tracing
        drawTrace()
    }

    func addTracePoint(_ view: ARSCNView, at location: CGPoint) {
        guard phase == .tracing,
              let q = view.raycastQuery(from: location, allowing: .estimatedPlane, alignment: .any),
              let r = view.session.raycast(q).first else { return }
        let c = r.worldTransform.columns.3
        tracePoints.append(SIMD3(c.x, c.y, c.z))
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        drawTrace()
    }

    func undoTrace() {
        guard !tracePoints.isEmpty else { return }
        tracePoints.removeLast()
        drawTrace()
    }

    /// Close the outline: it becomes the footprint of a prism the part stands in.
    func closeTrace() {
        guard tracePoints.count >= 3 else { return }
        let ys = tracePoints.map { $0.y }.sorted()
        baseY = ys[ys.count / 2]                                      // the table height
        polygon = tracePoints.map { SIMD2($0.x, $0.z) }
        let c = polygon.reduce(SIMD2<Float>(0, 0), +) / Float(polygon.count)
        fitRadius = max(0.03, polygon.map { simd_distance($0, c) }.max() ?? 0.1)
        phase = .ready
        refresh()
        Reporter.log("guided: outline closed, \(polygon.count) points, radius \(fitRadius)")
    }

    private func drawTrace() {
        traceNode.childNodes.forEach { $0.removeFromParentNode() }
        for (i, p) in tracePoints.enumerated() {
            traceNode.addChildNode(Self.dot(p, .systemYellow))
            if i > 0 { traceNode.addChildNode(Self.segment(tracePoints[i - 1], p, .systemYellow)) }
        }
    }

    private static func dot(_ p: SIMD3<Float>, _ color: UIColor) -> SCNNode {
        let n = SCNNode(geometry: SCNSphere(radius: 0.006))
        let m = SCNMaterial(); m.diffuse.contents = color; m.lightingModel = .constant
        n.geometry?.firstMaterial = m
        n.position = SCNVector3(p.x, p.y, p.z)
        return n
    }

    private static func segment(_ a: SIMD3<Float>, _ b: SIMD3<Float>, _ color: UIColor) -> SCNNode {
        let len = simd_distance(a, b)
        let cyl = SCNCylinder(radius: 0.0025, height: CGFloat(len))
        let m = SCNMaterial(); m.diffuse.contents = color; m.lightingModel = .constant
        cyl.firstMaterial = m
        let n = SCNNode(geometry: cyl)
        n.position = SCNVector3((a.x + b.x) / 2, (a.y + b.y) / 2, (a.z + b.z) / 2)
        n.look(at: SCNVector3(b.x, b.y, b.z), up: SCNVector3(0, 1, 0), localFront: SCNVector3(0, 1, 0))
        return n
    }

    /// The outline and height for the server, which uses them to crop and to close the underside.
    func footprintJSON() -> Data? {
        guard hasOutline else { return nil }
        let obj: [String: Any] = ["polygon": polygon.map { [$0.x, $0.y] }, "base_y": baseY, "height": height, "frame": "ARKit world, metres, polygon is [x, z]"]
        return try? JSONSerialization.data(withJSONObject: obj)
    }

    /// Put the part where the middle of the screen is pointing.
    func setObject(_ view: ARSCNView) {
        guard let frame = view.session.currentFrame else { return }
        let t = frame.camera.transform
        let cam = SIMD3<Float>(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let fwd = -SIMD3<Float>(t.columns.2.x, t.columns.2.y, t.columns.2.z)
        var hit = cam + fwd * 0.4
        let mid = CGPoint(x: view.bounds.midX, y: view.bounds.midY)
        if let q = view.raycastQuery(from: mid, allowing: .estimatedPlane, alignment: .any),
           let r = view.session.raycast(q).first {
            let c = r.worldTransform.columns.3
            hit = SIMD3(c.x, c.y, c.z)
        }
        hitPoint = hit
        hitDir = simd_normalize(hit - cam)
        phase = .ready
        refresh()
        Reporter.log("guided: object set at \(centre) radius \(radius)")
    }

    func reaim() {
        phase = .placing
        tracePoints = []; polygon = []
        clearNodes()
    }

    func beginCapture() {
        if ring { speedMonitor.reset() }
        cells = [Bool](repeating: false, count: Self.cellCount)
        announced = false
        phase = .capturing
        buildDome()
        Reporter.log("guided: capture started")
    }

    var message: String {
        switch phase {
        case .off: return ""
        case .placing: return "Mark the part. Best: tap Trace outline, then tap around its base on the table. Quick: aim the middle of the screen at it and tap Set object here."
        case .tracing: return tracePoints.count < 3 ? "Tap the screen at the corners of the part's base on the table, going around it. \(tracePoints.count) points so far." : "\(tracePoints.count) points. Keep tapping, or tap Close outline."
        case .ready: return hasOutline ? "The white frame is the space the part is in. Slide to match its height, then tap Start capturing. Only what is inside is kept, and the server fills in the underside." : "The white ball should just wrap the part. Slide to resize it, then tap Start capturing."
        case .capturing:
            if ring { return "Keep the part big in the picture with at least 4 markers in view (the rest of the ring may be cut off). Turn the spool slowly for one turn looking down at it, then one more turn with the phone low, nearly level with the top of the part. Then tap Stop." }
            if turntable { return "Keep the phone still, aimed at the part. Let the plate turn one or two full turns, slowly and steadily, then tap Stop. The server joins all the views into one model." }
            if complete { return "Coverage complete. Tap Stop to finish, or keep going to fill the last gaps." }
            return hint.isEmpty ? "Walk slowly around the part and keep it in the middle of the screen. Orange dots are views you still need, green are captured." : hint
        }
    }

    // MARK: per update (about 5 times a second)
    func update(frame: ARFrame?, recorder: PhotoRecorder?) {
        guard phase == .capturing, let frame, case .normal = frame.camera.trackingState else { return }
        if ring {
            // Nothing to track here: the markers in each photo place the frame. Just keep every frame, about 2.5 a second.
            turntableTicks += 1
            speedMonitor.feed(frame)
            if turntableTicks % 2 == 0 { recorder?.consider(frame, force: true); framesTaken = recorder?.count ?? 0 }
            return
        }
        let t = frame.camera.transform
        let cam = SIMD3<Float>(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let fwd = simd_normalize(-SIMD3<Float>(t.columns.2.x, t.columns.2.y, t.columns.2.z))
        let v = cam - centre
        let dist = simd_length(v)
        guard dist > 0.01 else { return }
        if turntable {
            // The part turns, the phone stays: just keep every frame (photo + LiDAR depth) about 2.5 times a second.
            turntableTicks += 1
            if turntableTicks % 2 == 0 { recorder?.consider(frame, force: true); framesTaken = recorder?.count ?? 0 }
            hint = ""
            return
        }
        let az = Double(atan2(v.x, v.z)), elev = Double(asin(max(-1, min(1, v.y / dist))))
        camAz = az; camElev = elev * 180 / .pi

        // gyro: a fast turn blurs the photos
        var spin = 0.0
        if let r = motion.deviceMotion?.rotationRate { spin = (r.x * r.x + r.y * r.y + r.z * r.z).squareRoot() }
        let looking = simd_dot(fwd, -v / dist) > 0.86      // within about 30 degrees of the part
        let rangeOK = dist > 0.12 && dist < 1.6
        if spin > 1.2 { hint = "Slow down: the camera is turning too fast."; return }
        if !looking { hint = "Point the camera at the part."; return }
        if !rangeOK { hint = dist <= 0.12 ? "Too close. Move back a little." : "Too far. Move closer."; return }

        let i = Self.cellIndex(az: az, elevDeg: camElev)
        if !cells[i] {
            cells[i] = true
            recorder?.consider(frame, force: true)
            paint(i)
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            if complete && !announced {
                announced = true
                UINotificationFeedbackGenerator().notificationOccurred(.success)
            }
        }
        hint = nextGapHint(az: az, elevDeg: camElev)
    }

    static func band(elevDeg: Double) -> Int { elevDeg < 22 ? 0 : (elevDeg < 55 ? 1 : 2) }

    static func cellIndex(az: Double, elevDeg: Double) -> Int {
        let b = band(elevDeg: elevDeg), n = sectors[b]
        let k = Int(((az + .pi) / (2 * .pi)) * Double(n)) % n
        return offsets[b] + max(0, k)
    }

    /// Azimuth and elevation (degrees) of the centre of a cell.
    static func cellCentre(_ i: Int) -> (az: Double, elev: Double) {
        let b = i >= offsets[2] ? 2 : (i >= offsets[1] ? 1 : 0)
        let n = sectors[b], k = i - offsets[b]
        return ((Double(k) + 0.5) * 2 * .pi / Double(n) - .pi, bandElev[b])
    }

    /// Where the nearest missing view is, in words.
    private func nextGapHint(az: Double, elevDeg: Double) -> String {
        var best: (d: Double, dAz: Double, dEl: Double)?
        for i in 0..<Self.cellCount where !cells[i] {
            let c = Self.cellCentre(i)
            var dAz = c.az - az
            while dAz > .pi { dAz -= 2 * .pi }
            while dAz < -.pi { dAz += 2 * .pi }
            let dEl = (c.elev - elevDeg) * .pi / 180
            let d = (dAz * dAz + dEl * dEl).squareRoot()
            if best == nil || d < best!.d { best = (d, dAz, dEl) }
        }
        guard let b = best else { return "" }
        var parts: [String] = []
        if abs(b.dAz) > 0.2 { parts.append(b.dAz > 0 ? "move right" : "move left") }
        if abs(b.dEl) > 0.25 { parts.append(b.dEl > 0 ? "raise the camera" : "lower the camera") }
        return parts.isEmpty ? "" : "Next gap: " + parts.joined(separator: " and ") + "."
    }

    // MARK: AR drawing
    private func clearNodes() {
        objectNode.childNodes.forEach { $0.removeFromParentNode() }
        domeNode.childNodes.forEach { $0.removeFromParentNode() }
        traceNode.childNodes.forEach { $0.removeFromParentNode() }
        dots = []
    }

    private func refresh() {
        if hasOutline { refreshPrism(); return }
        centre = hitPoint + hitDir * radius * 0.6
        objectNode.childNodes.forEach { $0.removeFromParentNode() }
        let sphere = SCNSphere(radius: CGFloat(radius))
        sphere.segmentCount = 16
        let m = SCNMaterial()
        m.diffuse.contents = UIColor.white.withAlphaComponent(0.5)
        m.fillMode = .lines
        m.lightingModel = .constant
        sphere.firstMaterial = m
        objectNode.addChildNode(SCNNode(geometry: sphere))
        objectNode.position = SCNVector3(centre.x, centre.y, centre.z)
        domeNode.position = objectNode.position
    }

    /// Wire frame of the traced outline extruded up by the part's height.
    private func refreshPrism() {
        let c2 = polygon.reduce(SIMD2<Float>(0, 0), +) / Float(polygon.count)
        centre = SIMD3(c2.x, baseY + height / 2, c2.y)
        objectNode.childNodes.forEach { $0.removeFromParentNode() }
        objectNode.position = SCNVector3(0, 0, 0)
        domeNode.position = SCNVector3(centre.x, centre.y, centre.z)
        let n = polygon.count
        for i in 0..<n {
            let a = polygon[i], b = polygon[(i + 1) % n]
            let a0 = SIMD3(a.x, baseY, a.y), b0 = SIMD3(b.x, baseY, b.y)
            let a1 = SIMD3(a.x, baseY + height, a.y), b1 = SIMD3(b.x, baseY + height, b.y)
            for (p, q) in [(a0, b0), (a1, b1), (a0, a1)] { objectNode.addChildNode(Self.segment(p, q, .white)) }
        }
        traceNode.childNodes.forEach { $0.removeFromParentNode() }
    }

    private func buildDome() {
        domeNode.childNodes.forEach { $0.removeFromParentNode() }
        dots = []
        let rv = max(0.28, (hasOutline ? fitRadius : radius) * 2.2)
        for i in 0..<Self.cellCount {
            let c = Self.cellCentre(i)
            let el = c.elev * .pi / 180
            let dir = SIMD3<Float>(Float(sin(c.az) * cos(el)), Float(sin(el)), Float(cos(c.az) * cos(el)))
            let node = SCNNode(geometry: SCNSphere(radius: 0.011))
            let m = SCNMaterial()
            m.diffuse.contents = UIColor.systemOrange
            m.lightingModel = .constant
            node.geometry?.firstMaterial = m
            node.position = SCNVector3(dir.x * rv, dir.y * rv, dir.z * rv)
            let pulse = SCNAction.repeatForever(.sequence([.scale(to: 1.5, duration: 0.5), .scale(to: 1.0, duration: 0.5)]))
            node.runAction(pulse, forKey: "pulse")
            domeNode.addChildNode(node)
            dots.append(node)
        }
    }

    private func paint(_ i: Int) {
        guard i < dots.count else { return }
        let n = dots[i]
        n.removeAction(forKey: "pulse")
        n.geometry?.firstMaterial?.diffuse.contents = UIColor.systemGreen
        n.runAction(.scale(to: 1.8, duration: 0.15))
    }
}

/// Top-down map of the viewing directions around the part, turned so you are always at the bottom:
/// a gap on the left of the map is to your left. Outer ring = sides, middle = from above at an angle, centre = straight above.
struct CoverageRadar: View {
    @ObservedObject var tracker: CoverageTracker

    var body: some View {
        TimelineView(.animation) { timeline in
            Canvas { ctx, size in
                let t = timeline.date.timeIntervalSinceReferenceDate
                let c = CGPoint(x: size.width / 2, y: size.height / 2), R = min(size.width, size.height) / 2 - 2
                // ring radii as fractions of R: side outside, top inside
                let rings: [(Double, Double)] = [(0.66, 1.0), (0.33, 0.66), (0.0, 0.33)]
                func pt(_ r: Double, _ phi: Double) -> CGPoint { CGPoint(x: c.x + R * r * sin(phi), y: c.y + R * r * cos(phi)) }
                ctx.fill(Path(ellipseIn: CGRect(x: c.x - R, y: c.y - R, width: R * 2, height: R * 2)), with: .color(.black.opacity(0.45)))
                for i in 0..<CoverageTracker.cellCount {
                    let b = i >= CoverageTracker.offsets[2] ? 2 : (i >= CoverageTracker.offsets[1] ? 1 : 0)
                    let n = Double(CoverageTracker.sectors[b]), k = Double(i - CoverageTracker.offsets[b])
                    let a0 = k * 2 * .pi / n - .pi - tracker.camAz, a1 = (k + 1) * 2 * .pi / n - .pi - tracker.camAz
                    let (rin, rout) = rings[b]
                    var p = Path()
                    p.move(to: pt(rout, a0))
                    for s in 1...6 { p.addLine(to: pt(rout, a0 + (a1 - a0) * Double(s) / 6)) }
                    for s in 0...6 { p.addLine(to: pt(max(rin, 0.001), a1 - (a1 - a0) * Double(s) / 6)) }
                    p.closeSubpath()
                    if tracker.cells[i] {
                        ctx.fill(p, with: .color(.green.opacity(0.85)))
                    } else {
                        ctx.fill(p, with: .color(.orange.opacity(0.15 + 0.15 * (sin(t * 4) + 1) / 2)))
                    }
                    ctx.stroke(p, with: .color(.black.opacity(0.6)), lineWidth: 1)
                }
                // you: always at the bottom, at the ring for the current height
                let b = CoverageTracker.band(elevDeg: tracker.camElev)
                let r = (rings[b].0 + rings[b].1) / 2
                let me = pt(r, 0)
                ctx.fill(Path(ellipseIn: CGRect(x: me.x - 6, y: me.y - 6, width: 12, height: 12)), with: .color(.white))
                ctx.stroke(Path(ellipseIn: CGRect(x: me.x - 6, y: me.y - 6, width: 12, height: 12)), with: .color(.black), lineWidth: 2)
            }
        }
    }
}

/// Everything the guided scan shows: the instruction, the radar and the placing controls.
struct GuidedPanel: View {
    @ObservedObject var tracker: CoverageTracker
    let setObject: () -> Void

    var body: some View {
        VStack(spacing: 8) {
            Text(tracker.message).font(.caption).padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
            if tracker.phase == .capturing && tracker.ring {
                HStack(spacing: 12) {
                    RingCoverageDial(cells: tracker.ringCells).frame(width: 96, height: 96)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(tracker.ringHint.isEmpty ? "Waiting for the ring…" : tracker.ringHint).font(.caption.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                        if let e = tracker.speed?.elev {   // the number to steer by: low is under 32, middle 32 to 50, high over 50
                            let b = CoverageTracker.ringBand(e)
                            Text("Camera angle \(Int(e))° (\(CoverageTracker.bandName(b)))").font(.caption2.monospacedDigit())
                                .padding(.horizontal, 8).padding(.vertical, 2)
                                .background(Theme.accent.opacity(b == 0 ? 0.9 : 0.5), in: Capsule()).foregroundStyle(.white)
                        }
                    }
                }
                .padding(10).card()
            }
            if tracker.phase == .capturing && tracker.ring, let s = tracker.speed {
                Text(s.text).font(.callout.weight(.semibold)).foregroundStyle(.white).padding(.horizontal, 12).padding(.vertical, 8)
                    .background(s.level >= 3 ? Color.red : (s.level == 2 ? Color.orange : (s.level == 1 ? Color.green : Color.gray)), in: Capsule())
            }
            if tracker.phase == .capturing && (tracker.turntable || tracker.ring) {
                Text("\(tracker.framesTaken) frames").font(.system(size: 34, weight: .bold, design: .rounded))
                    .padding(10).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
            }
            if tracker.phase == .capturing && !tracker.turntable && !tracker.ring {
                HStack(alignment: .center, spacing: 12) {
                    CoverageRadar(tracker: tracker).frame(width: 130, height: 130)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(Int(tracker.coverage * 100))%").font(.system(size: 40, weight: .bold, design: .rounded))
                        Text("\(tracker.captured) of \(CoverageTracker.cellCount) views").font(.caption)
                    }
                    .padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
                }
            }
            if tracker.phase == .ready {
                VStack(spacing: 4) {
                    if tracker.hasOutline {
                        Text("Part height: \(Int(tracker.height * 100)) cm").font(.caption)
                        Slider(value: $tracker.height, in: 0.01...0.5)
                    } else {
                        Text("Part size: about \(Int(tracker.radius * 200)) cm across").font(.caption)
                        Slider(value: $tracker.radius, in: 0.03...0.6)
                    }
                }
                .padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
            }
            HStack {
                if tracker.phase == .placing {
                    Button("Trace outline") { tracker.startTracing() }
                    Button("Set object here", action: setObject)
                }
                if tracker.phase == .tracing {
                    Button("Undo") { tracker.undoTrace() }
                    Button("Close outline") { tracker.closeTrace() }.disabled(tracker.tracePoints.count < 3)
                    Button("Cancel") { tracker.reaim() }
                }
                if tracker.phase == .ready {
                    Button("Start capturing") { tracker.beginCapture() }
                    Button("Re-aim") { tracker.reaim() }
                }
            }
            .buttonStyle(CaptureButtonStyle())
        }
    }
}

/// Catches taps while tracing the outline; passes nothing through otherwise.
struct TraceTapLayer: View {
    @ObservedObject var tracker: CoverageTracker
    let view: ARSCNView
    var zoom: CGFloat = 1

    var body: some View {
        if tracker.phase == .tracing {
            Color.clear.contentShape(Rectangle()).ignoresSafeArea()
                .gesture(SpatialTapGesture().onEnded { tap in
                    // the picture is magnified about the screen centre: map the tap back to the camera view's own coordinates
                    let c = CGPoint(x: view.bounds.midX, y: view.bounds.midY)
                    tracker.addTracePoint(view, at: CGPoint(x: c.x + (tap.location.x - c.x) / zoom, y: c.y + (tap.location.y - c.y) / zoom))
                })
        }
    }
}
