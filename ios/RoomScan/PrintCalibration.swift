import SwiftUI
import ARKit
import SceneKit

/// LiDAR calibration with the printed target (docs/lidar-target.md). Fully guided: tap the middle of the print once, and from
/// then on ARKit knows where the print is, so the app always knows how far away you are and at what tilt, which station that is,
/// and what to do next. A photo with its depth is taken by itself whenever you are at a station that still needs one and the
/// phone is steady. The server reads the markers in the photos and works out the correction.
struct CalStation: Identifiable, Equatable {
    let id: String
    let distance: Double       // metres from the middle of the print
    let tilt: Double           // degrees from straight down
    let frames: Int
    let say: String
}

final class PrintCalibrationModel: NSObject, ObservableObject {
    enum Phase { case aim, capture, sending, done }

    let sceneView = ARSCNView(frame: .zero)
    @Published var phase = Phase.aim
    @Published var stations: [CalStation] = PrintCalibrationModel.builtIn
    @Published var taken: [String: Int] = [:]
    @Published var distance: Double?         // now, metres from the middle of the print
    @Published var tilt: Double?             // now, degrees from straight down
    @Published var aimError: Double?         // degrees between where the phone points and the print
    @Published var current: CalStation?      // the station the phone is at now
    @Published var next: CalStation?         // the nearest one still to do
    @Published var hint = "Lay the print flat on the floor in good light (a hard floor, not a rug), with room to walk round it. Point the crosshair at its middle and tap \"This is the middle\"."
    @Published var steady = false
    @Published var result: [String: Any]?
    @Published var note = ""
    var distanceTol = 0.15, tiltTol = 7.0

    private var recorder = PhotoRecorder()
    private var centre: simd_float3?
    private var marker: SCNNode?
    private var timer: Timer?
    private var lastPose: simd_float4x4?
    private var lastShot: TimeInterval = 0

    /// The same list the server gives (lidartarget.stations), for when it cannot be reached.
    static let builtIn: [CalStation] = {
        var out: [CalStation] = []
        for d in [0.3, 0.5, 0.8, 1.2] {
            for t in [0.0, 30, 55] where !(d >= 1.2 && t == 55) {
                out.append(CalStation(id: "d\(Int(d * 100))t\(Int(t))", distance: d, tilt: t, frames: 3,
                                      say: "\(Int(d * 100)) cm away, " + (t == 0 ? "straight down" : "tilted \(Int(t))° from straight down")))
            }
        }
        return out
    }()

    var doneCount: Int { stations.filter { (taken[$0.id] ?? 0) >= $0.frames }.count }
    var photoCount: Int { recorder.count }

    func start() {
        let cfg = ARWorldTrackingConfiguration()
        cfg.planeDetection = [.horizontal]
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        CameraOwner.claim(self) { [weak self] in self?.sceneView.session.pause(); Screen.awake(false) }
        Screen.awake(true)      // the screen stays on until this screen is left
        sceneView.session.run(cfg)
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.update() }
        Task { await loadStations() }
    }

    func stop() { timer?.invalidate(); timer = nil; CameraOwner.release(self) }

    func reset() {
        TempFiles.remove(recorder.dir)
        recorder = PhotoRecorder()
        recorder.maxSide = 1920              // the markers must be sharp enough to read
        recorder.maxPhotos = 60
        taken = [:]; result = nil; note = ""; centre = nil; phase = .aim
        marker?.removeFromParentNode(); marker = nil
    }

    @MainActor private func loadStations() async {
        guard let url = URL(string: ServerAddress.url)?.appendingPathComponent("api/lidar-target"),
              let reply = try? await URLSession.shared.data(from: url),
              let j = try? JSONSerialization.jsonObject(with: reply.0) as? [String: Any],
              let list = j["stations"] as? [[String: Any]] else { return }
        let parsed = list.compactMap { s -> CalStation? in
            guard let id = s["id"] as? String, let d = (s["distance_m"] as? NSNumber)?.doubleValue,
                  let t = (s["tilt_deg"] as? NSNumber)?.doubleValue else { return nil }
            return CalStation(id: id, distance: d, tilt: t, frames: s["frames"] as? Int ?? 3, say: s["say"] as? String ?? id)
        }
        if !parsed.isEmpty { stations = parsed }
        distanceTol = (j["distance_tolerance"] as? NSNumber)?.doubleValue ?? distanceTol
        tiltTol = (j["tilt_tolerance_deg"] as? NSNumber)?.doubleValue ?? tiltTol
    }

    /// Where the crosshair meets the floor or table: a horizontal plane ARKit found, else the LiDAR depth straight ahead.
    func markMiddle() {
        guard let frame = sceneView.session.currentFrame else { note = "The camera is not ready yet."; return }
        let mid = CGPoint(x: sceneView.bounds.midX, y: sceneView.bounds.midY)
        var p: simd_float3?
        if let q = sceneView.raycastQuery(from: mid, allowing: .estimatedPlane, alignment: .horizontal),
           let hit = sceneView.session.raycast(q).first {
            let c = hit.worldTransform.columns.3
            p = simd_float3(c.x, c.y, c.z)
        } else if let d = Self.centreDepth(frame) {
            let t = frame.camera.transform
            let fwd = -simd_float3(t.columns.2.x, t.columns.2.y, t.columns.2.z)
            p = simd_float3(t.columns.3.x, t.columns.3.y, t.columns.3.z) + fwd * d
        }
        guard let p else { note = "Could not find the surface there. Point at the print from about 40 cm and try again."; return }
        centre = p
        note = ""
        let ring = SCNNode(geometry: SCNTube(innerRadius: 0.095, outerRadius: 0.105, height: 0.002))   // about the print's size
        ring.geometry?.firstMaterial?.diffuse.contents = UIColor.systemTeal.withAlphaComponent(0.8)
        ring.geometry?.firstMaterial?.readsFromDepthBuffer = false
        ring.simdPosition = p
        marker?.removeFromParentNode()
        sceneView.scene.rootNode.addChildNode(ring)
        marker = ring
        phase = .capture
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    static func centreDepth(_ frame: ARFrame) -> Float? {
        guard let map = frame.sceneDepth?.depthMap, CVPixelBufferGetPixelFormatType(map) == kCVPixelFormatType_DepthFloat32 else { return nil }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), row = CVPixelBufferGetBytesPerRow(map)
        let v = (base + (h / 2) * row).assumingMemoryBound(to: Float32.self)[w / 2]
        return v > 0.1 ? v : nil
    }

    private func update() {
        guard phase == .capture, let p = centre, let frame = sceneView.session.currentFrame, case .normal = frame.camera.trackingState else {
            if phase == .capture { hint = "Tracking is limited: move the phone slowly over the print." }
            return
        }
        let t = frame.camera.transform
        let cam = simd_float3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let fwd = simd_normalize(-simd_float3(t.columns.2.x, t.columns.2.y, t.columns.2.z))
        let v = cam - p
        let dist = Double(simd_length(v))
        let til = Double(acos(max(-1, min(1, simd_dot(simd_normalize(v), simd_float3(0, 1, 0)))))) * 180 / .pi
        let aim = Double(acos(max(-1, min(1, simd_dot(fwd, simd_normalize(-v)))))) * 180 / .pi
        // steady: moved under 1 cm and turned under 1.5 degrees in the last 0.2 s
        if let l = lastPose {
            let moved = simd_length(cam - simd_float3(l.columns.3.x, l.columns.3.y, l.columns.3.z))
            let lf = simd_normalize(-simd_float3(l.columns.2.x, l.columns.2.y, l.columns.2.z))
            let turned = Double(acos(max(-1, min(1, simd_dot(fwd, lf))))) * 180 / .pi
            steady = moved < 0.01 && turned < 1.5
        }
        lastPose = t
        distance = dist; tilt = til; aimError = aim

        func at(_ s: CalStation) -> Bool { abs(dist - s.distance) <= distanceTol * s.distance && abs(til - s.tilt) <= tiltTol }
        let todo = stations.filter { (taken[$0.id] ?? 0) < $0.frames }
        current = stations.first(where: at)
        // the nearest station still to do, distance and tilt weighed by their tolerances
        next = todo.min { a, b in
            func cost(_ s: CalStation) -> Double { pow((dist - s.distance) / (distanceTol * s.distance), 2) + pow((til - s.tilt) / tiltTol, 2) }
            return cost(a) < cost(b)
        }
        guard let n = next else {
            hint = "All stations done. Tap \"Send and calculate\"."
            return
        }
        if aim > 12 { hint = "Point the phone at the print: the teal ring should be in the middle of the screen."; return }
        if let c = current, (taken[c.id] ?? 0) < c.frames {
            if !steady { hint = "\(c.say): hold still…"; return }
            if frame.timestamp - lastShot > 0.6 {
                let before = recorder.count
                recorder.consider(frame, force: true)
                recorder.waitForWrites()
                if recorder.count > before {
                    lastShot = frame.timestamp
                    taken[c.id, default: 0] += 1
                    let left = c.frames - (taken[c.id] ?? 0)
                    UIImpactFeedbackGenerator(style: left == 0 ? .heavy : .light).impactOccurred()
                    hint = left > 0 ? "\(c.say): \(left) more, keep still." : "Done: \(c.say)."
                }
            }
            return
        }
        hint = Self.directions(dist: dist, tilt: til, to: n)
    }

    /// What to do to reach station `s`: one thing at a time, tilt first (it means walking round), then distance.
    static func directions(dist: Double, tilt: Double, to s: CalStation) -> String {
        let dt = s.tilt - tilt, dd = s.distance - dist
        if abs(dt) > 7 {
            if s.tilt == 0 { return "Next: \(s.say). Hold the phone right above the print, looking straight down." }
            return dt > 0 ? "Next: \(s.say). Move out to the side and lower the phone, keep it pointed at the print (\(Int(tilt))° now)."
                          : "Next: \(s.say). Raise the phone higher above the print (\(Int(tilt))° now)."
        }
        if abs(dd) > 0.15 * s.distance {
            return dd > 0 ? "Next: \(s.say). Move back \(Int(abs(dd) * 100)) cm." : "Next: \(s.say). Move closer \(Int(abs(dd) * 100)) cm."
        }
        return "Next: \(s.say). Nearly there, hold still."
    }

    @MainActor func send(printScale: Double) async {
        guard let meta = recorder.writeManifest() else { note = "No photos yet."; return }
        phase = .sending; note = "Sending \(recorder.count) photos and calculating…"
        do {
            result = try await Uploader.lidarCal(server: ServerAddress.url, printScale: printScale, photos: recorder.urls, meta: meta)
            note = ""; phase = .done
        } catch {
            note = "Could not send: \(error.localizedDescription)"; phase = .capture
            Reporter.report(tab: "print-calibration", message: "Send failed", detail: "\(error)")
        }
    }
}

struct PrintCalibrationView: View {
    @StateObject private var model = PrintCalibrationModel()
    @ObservedObject private var cal = CalibrationStore.shared
    @State private var rulerText = ""
    @State private var zoom: CGFloat = 1
    @FocusState private var rulerFocused: Bool

    private static let rulerMM = 101.0      // outer edge to outer edge of the printed ruler

    private var rulerReading: Double? { Double(rulerText.replacingOccurrences(of: ",", with: ".")) }
    private var rulerOK: Bool { rulerReading.map { $0 > 91 && $0 < 111 } ?? false }
    private var printScale: Double { rulerOK ? rulerReading! / Self.rulerMM : 1 }

    var body: some View {
        Form {
            Section("1. The print") {
                Text("""
                Print lidar-target-v2 (4 colours, parts already on filaments 1-4: \(ServerAddress.url)/prints/lidar-target-v2-4colour.3mf), matte, ironing off. \
                Check it: the six black squares print crisp, the plate lies flat (a sheet of paper must not slide under it), and nothing is glossy. \
                Measure the ruler with calipers, from the outer edge of the first long tick to the outer edge of the last, and type what they read. It should be close to 101.0 mm.
                """).font(.callout)
                HStack {
                    Text("Ruler reading")
                    TextField("101.0", text: $rulerText)
                        .keyboardType(.decimalPad).multilineTextAlignment(.trailing).focused($rulerFocused)
                    Text("mm").foregroundStyle(.secondary)
                    if rulerFocused {
                        Button("OK") { rulerFocused = false }.buttonStyle(.borderedProminent)
                    }
                }
                if !rulerText.isEmpty {
                    if rulerOK {
                        Text(String(format: "The print is %.2f%% of its size. Used in the calculation.", printScale * 100)).font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text("That doesn't look like the ruler (about 101 mm), so it won't be used.").font(.caption).foregroundStyle(.orange)
                    }
                }
            }
            Section("2. Scan it") {
                ZStack {
                    ARSceneRepresentable(view: model.sceneView)
                    Image(systemName: "plus").font(.system(size: 28, weight: .light)).foregroundStyle(.white).shadow(radius: 2)
                }
                .scaleEffect(zoom).frame(height: 360).clipped().pinchZoom($zoom).listRowInsets(EdgeInsets())
                if model.phase == .aim {
                    Text(model.hint).font(.callout)
                    Button("This is the middle") { model.markMiddle() }.buttonStyle(.borderedProminent)
                } else {
                    Readout(model: model)
                    StationGrid(model: model)
                    HStack {
                        Text("\(model.doneCount) of \(model.stations.count) stations · \(model.photoCount) photos").font(.caption.monospacedDigit())
                        Spacer()
                        Button("Start over", role: .destructive) { model.reset() }.font(.caption)
                    }
                }
                if !model.note.isEmpty { Text(model.note).font(.callout).foregroundStyle(.orange) }
            }
            Section("3. Calculate") {
                Button(model.phase == .sending ? "Calculating…" : "Send and calculate") { Task { await model.send(printScale: printScale) } }
                    .disabled(model.phase == .sending || model.doneCount < 3)
                if model.doneCount < 3 { Text("Do at least 3 stations first (all of them is best).").font(.caption).foregroundStyle(.secondary) }
                if let r = model.result { PrintResult(result: r, cal: cal) }
            }
        }
        .scrollDismissesKeyboard(.immediately)
        .navigationTitle("Calibrate with the print")
        .onAppear { model.reset(); model.start() }
        .onDisappear { model.stop() }
    }
}

/// Live: where you are, where the next station is, and what to do.
private struct Readout: View {
    @ObservedObject var model: PrintCalibrationModel

    private func row(_ name: String, _ now: String, _ want: String, _ ok: Bool) -> some View {
        HStack {
            Text(name).frame(width: 70, alignment: .leading)
            Text(now).bold().monospacedDigit()
            Image(systemName: "arrow.right").font(.caption2).foregroundStyle(.secondary)
            Text(want).monospacedDigit().foregroundStyle(.secondary)
            Spacer()
            Image(systemName: ok ? "checkmark.circle.fill" : "circle").foregroundStyle(ok ? .green : .secondary)
        }
    }

    var body: some View {
        let n = model.current ?? model.next
        VStack(alignment: .leading, spacing: 6) {
            Text(model.hint).font(.headline).fixedSize(horizontal: false, vertical: true)
            if let n, let d = model.distance, let t = model.tilt {
                row("Distance", "\(Int(d * 100)) cm", "\(Int(n.distance * 100)) cm", abs(d - n.distance) <= model.distanceTol * n.distance)
                row("Tilt", "\(Int(t))°", "\(Int(n.tilt))°", abs(t - n.tilt) <= model.tiltTol)
                row("Aim", "\(Int(model.aimError ?? 0))° off", "under 12°", (model.aimError ?? 99) <= 12)
                row("Steady", model.steady ? "yes" : "moving", "still", model.steady)
            }
        }
        .font(.callout)
    }
}

/// Distance across, tilt down: a tick for every station done, the next one highlighted.
private struct StationGrid: View {
    @ObservedObject var model: PrintCalibrationModel

    var body: some View {
        let tilts = Array(Set(model.stations.map(\.tilt))).sorted()
        let dists = Array(Set(model.stations.map(\.distance))).sorted()
        Grid(alignment: .center, horizontalSpacing: 6, verticalSpacing: 6) {
            GridRow {
                Text("")
                ForEach(dists, id: \.self) { Text("\(Int($0 * 100)) cm").font(.caption2).foregroundStyle(.secondary) }
            }
            ForEach(tilts, id: \.self) { t in
                GridRow {
                    Text(t == 0 ? "down" : "\(Int(t))°").font(.caption2).foregroundStyle(.secondary)
                    ForEach(dists, id: \.self) { d in
                        if let s = model.stations.first(where: { $0.distance == d && $0.tilt == t }) {
                            let n = model.taken[s.id] ?? 0
                            let done = n >= s.frames
                            let isNext = model.next?.id == s.id, isHere = model.current?.id == s.id
                            ZStack {
                                RoundedRectangle(cornerRadius: 6).fill(done ? Color.green.opacity(0.35) : isHere ? Theme.accent.opacity(0.35) : Color.secondary.opacity(0.12))
                                RoundedRectangle(cornerRadius: 6).strokeBorder(isNext ? Theme.accent : .clear, lineWidth: 2)
                                if done { Image(systemName: "checkmark").font(.caption.bold()) } else { Text("\(n)/\(s.frames)").font(.caption2.monospacedDigit()) }
                            }
                            .frame(height: 30)
                        } else {
                            Text("–").foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }
}

private struct PrintResult: View {
    let result: [String: Any]
    @ObservedObject var cal: CalibrationStore

    private func num(_ k: String, in d: [String: Any]?) -> Double? { (d?[k] as? NSNumber)?.doubleValue }

    var body: some View {
        let fit = result["fit"] as? [String: Any]
        let good = result["good"] as? Bool ?? false
        VStack(alignment: .leading, spacing: 6) {
            Label(good ? "Calibrated" : "Not good enough yet", systemImage: good ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(good ? .green : .orange).font(.headline)
            Text("Photos used: \(result["frames_used"] as? Int ?? 0) of \(result["frames"] as? Int ?? 0)")
            if let a = num("a", in: fit), let b = num("b", in: fit) {
                Text(String(format: "Error before ±%.1f mm, after ±%.1f mm", num("rms_before_mm", in: fit) ?? 0, num("rms_after_mm", in: fit) ?? 0))
                Text(String(format: "Correction: scale %.4f, offset %+.1f mm", a, b * 1000)).foregroundStyle(.secondary)
                if let s = num("tracking_scale", in: result["camera"] as? [String: Any]) {
                    Text(String(format: "ARKit's tracking against the print: %.3f (1.000 = same metres)", s)).foregroundStyle(.secondary)
                }
                BinList(title: "Left over by distance", rows: (result["by_distance"] as? [String: Any])?["after"] as? [[String: Any]], key: "m", unit: "m")
                BinList(title: "Left over by angle to the surface", rows: (result["by_angle"] as? [String: Any])?["after"] as? [[String: Any]], key: "deg", unit: "°")
                Button(cal.printFit?.a == a && cal.printFit?.b == b ? "In use for measuring" : "Use this correction for measuring") {
                    cal.printFit = PrintFit(a: a, b: b, rmsBefore: (num("rms_before_mm", in: fit) ?? 0) / 1000, rmsAfter: (num("rms_after_mm", in: fit) ?? 0) / 1000, date: Date())
                }
                .buttonStyle(.borderedProminent).disabled(!good || (cal.printFit?.a == a && cal.printFit?.b == b))
            }
            ForEach(result["messages"] as? [String] ?? [], id: \.self) { Text($0).font(.callout) }
        }
        .font(.caption)
    }
}

private struct BinList: View {
    let title: String
    let rows: [[String: Any]]?
    let key: String
    let unit: String

    var body: some View {
        if let rows, !rows.isEmpty {
            Text(title).font(.caption.bold()).padding(.top, 4)
            ForEach(Array(rows.enumerated()), id: \.offset) { _, r in
                let range = r[key] as? [NSNumber] ?? []
                Text("\(range.map { $0.stringValue }.joined(separator: "–")) \(unit): bias \((r["bias_mm"] as? NSNumber)?.stringValue ?? "?") mm, ±\((r["rms_mm"] as? NSNumber)?.stringValue ?? "?") mm")
                    .monospacedDigit()
            }
        }
    }
}
