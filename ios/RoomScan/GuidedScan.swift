import SwiftUI
import ARKit
import SceneKit
import CoreMotion

/// Guided object scan: remembers where the part is in the room (ARKit fuses camera, gyro, accelerometer and LiDAR
/// into one world frame) and tracks from which directions it has been seen. A dotted dome around the part in AR and a
/// radar on screen show what is captured (green) and what is missing (orange), so nothing is left out.
final class CoverageTracker: ObservableObject {
    enum Phase { case off, placing, ready, capturing }

    // Viewing directions around the part: three bands of elevation, each split into sectors of azimuth.
    static let sectors = [24, 16, 8]          // side, diagonal, top
    static let offsets = [0, 24, 40]
    static let bandElev: [Double] = [8, 38, 72]   // degrees, band centres
    static let cellCount = 48

    @Published var enabled = false             // chosen with the picker
    @Published var phase: Phase = .off
    @Published var radius: Float = 0.15 { didSet { if phase == .ready { refresh() } } }
    @Published var cells = [Bool](repeating: false, count: CoverageTracker.cellCount)
    @Published var camAz = 0.0
    @Published var camElev = 0.0
    @Published var hint = ""

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
    }

    func begin() {
        phase = enabled ? .placing : .off
        cells = [Bool](repeating: false, count: Self.cellCount)
        hint = ""
        announced = false
        clearNodes()
        if enabled, motion.isDeviceMotionAvailable {
            motion.deviceMotionUpdateInterval = 1.0 / 30
            motion.startDeviceMotionUpdates()
        }
    }

    func end() {
        motion.stopDeviceMotionUpdates()
        phase = .off
        clearNodes()
    }

    /// Is this world point on the part (inside the capture ball)?
    func contains(_ p: SIMD3<Float>) -> Bool {
        phase != .capturing || simd_distance(p, centre) <= radius * 1.5
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
        clearNodes()
    }

    func beginCapture() {
        cells = [Bool](repeating: false, count: Self.cellCount)
        announced = false
        phase = .capturing
        buildDome()
        Reporter.log("guided: capture started")
    }

    var message: String {
        switch phase {
        case .off: return ""
        case .placing: return "Aim the middle of the screen at the part, about 30 cm away, then tap Set object here."
        case .ready: return "The white ball should just wrap the part. Slide to resize it, then tap Start capturing."
        case .capturing:
            if complete { return "Coverage complete. Tap Stop to finish, or keep going to fill the last gaps." }
            return hint.isEmpty ? "Walk slowly around the part and keep it in the middle of the screen. Orange dots are views you still need, green are captured." : hint
        }
    }

    // MARK: per update (about 5 times a second)
    func update(frame: ARFrame?, recorder: PhotoRecorder?) {
        guard phase == .capturing, let frame, case .normal = frame.camera.trackingState else { return }
        let t = frame.camera.transform
        let cam = SIMD3<Float>(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let fwd = simd_normalize(-SIMD3<Float>(t.columns.2.x, t.columns.2.y, t.columns.2.z))
        let v = cam - centre
        let dist = simd_length(v)
        guard dist > 0.01 else { return }
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
        dots = []
    }

    private func refresh() {
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

    private func buildDome() {
        domeNode.childNodes.forEach { $0.removeFromParentNode() }
        dots = []
        let rv = max(0.28, radius * 2.2)
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
            if tracker.phase == .capturing {
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
                    Text("Part size: about \(Int(tracker.radius * 200)) cm across").font(.caption)
                    Slider(value: $tracker.radius, in: 0.03...0.6)
                }
                .padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
            }
            HStack {
                if tracker.phase == .placing { Button("Set object here", action: setObject) }
                if tracker.phase == .ready {
                    Button("Start capturing") { tracker.beginCapture() }
                    Button("Re-aim") { tracker.reaim() }
                }
            }
            .buttonStyle(.borderedProminent)
        }
    }
}
