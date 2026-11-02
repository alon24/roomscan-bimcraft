import SwiftUI
import ARKit
import SceneKit
import AudioToolbox

/// LiDAR calibration with the printed target (docs/lidar-target.md). One motion, no angles to hit: tap Start with the phone over the
/// print, stand up slowly, then step back twice keeping the print on screen, then the same from another side. ARKit tracks the print
/// from the tap, so the app knows the distance and tilt, takes the photos by itself and speaks the next step. The server reads the
/// markers and the floor in the photos and works out the correction.
final class PrintCalibrationModel: NSObject, ObservableObject {
    enum Phase: Equatable { case aim, rise, back, side, sending, done }

    /// One band of distance (rise) or tilt (back, side) and how many photos it has.
    struct Band: Identifiable { let id: Int; let lo: Double; let hi: Double; var taken = 0 }

    static let photosPerBand = 2
    static let distanceEdges = [0.30, 0.45, 0.60, 0.80, 1.00, 1.25, 1.60]   // metres from the print's middle
    static let tiltEdges = [10.0, 20, 30, 40, 50, 60]                       // degrees from straight down
    static let minRise = 4, minTilt = 2                                     // bands needed before "Next" is offered

    static func bands(_ e: [Double]) -> [Band] { zip(e, e.dropFirst()).enumerated().map { Band(id: $0.offset, lo: $0.element.0, hi: $0.element.1) } }

    let sceneView = ARSCNView(frame: .zero)
    @Published var phase = Phase.aim
    @Published var rise = PrintCalibrationModel.bands(distanceEdges)
    @Published var back = PrintCalibrationModel.bands(tiltEdges)
    @Published var side = PrintCalibrationModel.bands(tiltEdges)
    @Published var hint = "Lay the print on a bare, hard floor. Hold the phone flat over its middle, about 40 cm up, and tap Start."
    @Published var result: [String: Any]?
    @Published var note = ""
    @Published var printScale = 1.0

    private var recorder = PhotoRecorder()
    private var centre: simd_float3?
    private var marker: SCNNode?
    private var timer: Timer?
    private var lastPose: simd_float4x4?
    private var lastShot: TimeInterval = 0
    private var backAzimuth: Double?          // direction of the first step back, degrees round the print

    var photoCount: Int { recorder.count }
    private static func filled(_ b: [Band]) -> Int { b.filter { $0.taken >= photosPerBand }.count }
    var canGoOn: Bool {
        switch phase {
        case .rise: return Self.filled(rise) >= Self.minRise
        case .back: return Self.filled(back) >= Self.minTilt
        case .side: return true
        default: return false
        }
    }

    func start() {
        let cfg = ARWorldTrackingConfiguration()
        cfg.planeDetection = [.horizontal]
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        CameraOwner.claim(self) { [weak self] in self?.sceneView.session.pause(); Screen.awake(false) }
        Screen.awake(true)      // the screen stays on until this screen is left
        sceneView.session.run(cfg)
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.update() }
    }

    func stop() { timer?.invalidate(); timer = nil; Voice.stop(); CameraOwner.release(self) }

    func reset() {
        TempFiles.remove(recorder.dir)
        recorder = PhotoRecorder()
        recorder.maxSide = 1920              // the markers must be sharp enough to read
        recorder.maxPhotos = 60
        rise = Self.bands(Self.distanceEdges); back = Self.bands(Self.tiltEdges); side = Self.bands(Self.tiltEdges)
        result = nil; note = ""; centre = nil; phase = .aim; backAzimuth = nil; lastPose = nil
        hint = "Lay the print on a bare, hard floor. Hold the phone flat over its middle, about 40 cm up, and tap Start."
        marker?.removeFromParentNode(); marker = nil
    }

    private func say(_ text: String, every seconds: TimeInterval = 4) {
        hint = text
        Voice.say(text, every: seconds)
    }

    /// Where the crosshair meets the floor: a horizontal plane ARKit found, else the LiDAR depth straight ahead.
    func begin() {
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
        guard let p else { note = "Could not find the floor there. Point at the print from about 40 cm and try again."; return }
        centre = p
        note = ""
        let ring = SCNNode(geometry: SCNTube(innerRadius: 0.095, outerRadius: 0.105, height: 0.002))   // about the print's size
        ring.geometry?.firstMaterial?.diffuse.contents = UIColor.systemTeal.withAlphaComponent(0.8)
        ring.geometry?.firstMaterial?.readsFromDepthBuffer = false
        ring.simdPosition = p
        marker?.removeFromParentNode()
        sceneView.scene.rootNode.addChildNode(ring)
        marker = ring
        phase = .rise
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

    /// The next step, by button or because this one is full. After the last one the photos go to the server.
    func goOn() {
        switch phase {
        case .rise: phase = .back
        case .back: phase = .side
        case .side: Task { @MainActor in await self.send() }
        default: break
        }
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        AudioServicesPlaySystemSound(1057)
    }

    private func update() {
        guard [Phase.rise, .back, .side].contains(phase), let p = centre, let frame = sceneView.session.currentFrame else { return }
        guard case .normal = frame.camera.trackingState else { say("Move slowly, the phone is finding its place."); return }
        let t = frame.camera.transform
        let cam = simd_float3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let fwd = simd_normalize(-simd_float3(t.columns.2.x, t.columns.2.y, t.columns.2.z))
        let v = cam - p
        let dist = Double(simd_length(v))
        let tilt = Double(acos(max(-1, min(1, simd_dot(simd_normalize(v), simd_float3(0, 1, 0)))))) * 180 / .pi
        let aim = Double(acos(max(-1, min(1, simd_dot(fwd, simd_normalize(-v)))))) * 180 / .pi
        let azimuth = Double(atan2(v.z, v.x)) * 180 / .pi
        var fast = false
        if let l = lastPose {       // slow enough for sharp markers: under 20 cm/s and 25 degrees/s
            let moved = simd_length(cam - simd_float3(l.columns.3.x, l.columns.3.y, l.columns.3.z))
            let lf = simd_normalize(-simd_float3(l.columns.2.x, l.columns.2.y, l.columns.2.z))
            fast = moved > 0.04 || Double(acos(max(-1, min(1, simd_dot(fwd, lf))))) * 180 / .pi > 5
        }
        lastPose = t
        if aim > 15 { say("Keep the print in the middle of the screen."); return }
        if fast { say("A little slower."); return }

        switch phase {
        case .rise:
            guard tilt < 12 else { say("Hold the phone over the print, looking straight down."); return }
            if let i = rise.firstIndex(where: { dist >= $0.lo && dist < $0.hi && $0.taken < Self.photosPerBand }), shoot(frame) { rise[i].taken += 1 }
            if Self.filled(rise) == rise.count { goOn(); return }
            let top = rise.last(where: { $0.taken < Self.photosPerBand })?.hi ?? 1.6
            say(dist < 0.3 ? "Raise the phone a little." : dist >= top ? "That is high enough." : "Stand up slowly, phone flat over the print.")
        case .back:
            if tilt > 20, backAzimuth == nil { backAzimuth = azimuth }
            if dist >= 0.35, let i = back.firstIndex(where: { tilt >= $0.lo && tilt < $0.hi && $0.taken < Self.photosPerBand }), shoot(frame) { back[i].taken += 1 }
            if Self.filled(back) == back.count { goOn(); return }
            say(tilt < 10 ? "Stand over the print, then walk slowly backwards." : "Keep walking back, print in the middle of the screen.")
        case .side:
            let apart = backAzimuth.map { abs(remainder(azimuth - $0, 360)) } ?? 90
            if tilt > 15 && apart < 50 { say("Walk round to another side of the print."); return }
            if dist >= 0.35, let i = side.firstIndex(where: { tilt >= $0.lo && tilt < $0.hi && $0.taken < Self.photosPerBand }), shoot(frame) { side[i].taken += 1 }
            if Self.filled(side) == side.count { goOn(); return }
            say(tilt < 10 ? "Same again from this side: walk slowly backwards." : "Keep walking back, print in the middle of the screen.")
        default: break
        }
    }

    /// Takes a photo if the last one was at least half a second ago. True when one was stored.
    private func shoot(_ frame: ARFrame) -> Bool {
        guard frame.timestamp - lastShot > 0.5 else { return false }
        let before = recorder.count
        recorder.consider(frame, force: true)
        recorder.waitForWrites()
        guard recorder.count > before else { return false }
        lastShot = frame.timestamp
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        AudioServicesPlaySystemSound(1104)
        return true
    }

    @MainActor func send() async {
        guard let meta = recorder.writeManifest() else { note = "No photos yet."; return }
        phase = .sending
        say("Done. Calculating.")
        do {
            let r = try await Uploader.lidarCal(server: ServerAddress.url, printScale: printScale, photos: recorder.urls, meta: meta)
            result = r; note = ""; phase = .done
            AudioServicesPlaySystemSound(1057)
            if r["good"] as? Bool == true { apply(r) }
        } catch {
            note = "Could not reach the server: \(error.localizedDescription)"; phase = .side
            Reporter.report(tab: "print-calibration", message: "Send failed", detail: "\(error)")
        }
    }

    private func apply(_ r: [String: Any]) {
        guard let fit = r["fit"] as? [String: Any], let a = (fit["a"] as? NSNumber)?.doubleValue, let b = (fit["b"] as? NSNumber)?.doubleValue else { return }
        let angles = (r["angle_correction"] as? [[String: Any]])?.compactMap { d -> PrintFit.AnglePoint? in
            guard let g = (d["deg"] as? NSNumber)?.doubleValue, let m = (d["m"] as? NSNumber)?.doubleValue else { return nil }
            return PrintFit.AnglePoint(deg: g, m: m)
        }
        CalibrationStore.shared.printFit = PrintFit(a: a, b: b, rmsBefore: ((fit["rms_before_mm"] as? NSNumber)?.doubleValue ?? 0) / 1000,
                                                    rmsAfter: ((fit["rms_after_mm"] as? NSNumber)?.doubleValue ?? 0) / 1000, date: Date(), angles: angles)
    }
}

struct PrintCalibrationView: View {
    @StateObject private var model = PrintCalibrationModel()
    @State private var rulerText = ""
    @State private var zoom: CGFloat = 1
    @FocusState private var rulerFocused: Bool

    private static let rulerMM = 101.0      // outer edge to outer edge of the printed ruler

    private var rulerReading: Double? { Double(rulerText.replacingOccurrences(of: ",", with: ".")) }
    private var rulerOK: Bool { rulerReading.map { $0 > 91 && $0 < 111 } ?? false }

    private var step: (Int, String) {
        switch model.phase {
        case .rise: return (1, "Stand up")
        case .back: return (2, "Step back")
        case .side: return (3, "Another side")
        default: return (0, "")
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .top) {
                ARSceneRepresentable(view: model.sceneView).scaleEffect(zoom).clipped().pinchZoom($zoom)
                Image(systemName: "plus").font(.system(size: 30, weight: .light)).foregroundStyle(.white).shadow(radius: 2)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                if model.phase != .done {
                    VStack(spacing: 4) {
                        if step.0 > 0 { Text("Step \(step.0) of 3 · \(step.1)").font(.caption.bold()) }
                        Text(model.hint).font(.title3.bold()).multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(12).frame(maxWidth: .infinity).background(.black.opacity(0.65)).foregroundStyle(.white)
                }
            }
            panel.padding(14)
        }
        .scrollDismissesKeyboard(.immediately)
        .navigationTitle("Calibrate with the print")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { model.reset(); model.start() }
        .onDisappear { model.stop() }
    }

    @ViewBuilder private var panel: some View {
        switch model.phase {
        case .aim:
            VStack(spacing: 10) {
                HStack {
                    Text("Ruler reading")
                    TextField("101.0", text: $rulerText)
                        .keyboardType(.decimalPad).multilineTextAlignment(.trailing).focused($rulerFocused)
                    Text("mm").foregroundStyle(.secondary)
                    if rulerFocused { Button("OK") { rulerFocused = false }.buttonStyle(.borderedProminent) }
                }
                if !rulerText.isEmpty {
                    Text(rulerOK ? String(format: "The print is %.2f%% of its size.", (rulerReading ?? 101) / Self.rulerMM * 100)
                                 : "That doesn't look like the ruler (about 101 mm), so it won't be used.")
                        .font(.caption).foregroundStyle(rulerOK ? Color.secondary : Color.orange)
                }
                Text("Optional: calipers over the printed ruler, outer edge to outer edge.").font(.caption).foregroundStyle(.secondary)
                Button {
                    rulerFocused = false
                    model.printScale = rulerOK ? (rulerReading ?? Self.rulerMM) / Self.rulerMM : 1
                    model.begin()
                } label: { Text("Start").font(.title3.bold()).frame(maxWidth: .infinity) }
                .buttonStyle(.borderedProminent).controlSize(.large)
                if !model.note.isEmpty { Text(model.note).font(.callout).foregroundStyle(.orange) }
            }
        case .rise: capture(model.rise) { "\(Int($0.hi * 100))" }
        case .back: capture(model.back) { "\(Int($0.hi))°" }
        case .side: capture(model.side) { "\(Int($0.hi))°" }
        case .sending:
            VStack(spacing: 8) { ProgressView(); Text("Sending \(model.photoCount) photos and calculating…").font(.callout) }.frame(maxWidth: .infinity)
        case .done: PrintResult(result: model.result ?? [:]) { model.reset() }
        }
    }

    private func capture(_ bands: [PrintCalibrationModel.Band], label: @escaping (PrintCalibrationModel.Band) -> String) -> some View {
        VStack(spacing: 10) {
            HStack(spacing: 4) {
                ForEach(bands) { b in
                    VStack(spacing: 2) {
                        RoundedRectangle(cornerRadius: 4)
                            .fill(b.taken >= PrintCalibrationModel.photosPerBand ? Color.green : b.taken > 0 ? Theme.accent.opacity(0.5) : Color.secondary.opacity(0.2))
                            .frame(height: 22)
                        Text(label(b)).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
            }
            if !model.note.isEmpty { Text(model.note).font(.callout).foregroundStyle(.orange) }
            HStack {
                Button("Start over", role: .destructive) { model.reset() }
                VoiceToggle()
                Spacer()
                Button(model.phase == .side ? "Finish" : "Next step") { model.goOn() }
                    .buttonStyle(.borderedProminent).disabled(!model.canGoOn)
            }
        }
    }
}

/// One plain sentence, and if something is wrong one thing to do about it and a way to do it.
private struct PrintResult: View {
    let result: [String: Any]
    let again: () -> Void

    var body: some View {
        let good = result["good"] as? Bool ?? false
        let fit = result["fit"] as? [String: Any]
        VStack(alignment: .leading, spacing: 8) {
            Label(good ? "Calibrated, correction in use" : "Not calibrated yet", systemImage: good ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(good ? .green : .orange).font(.headline)
            if let s = result["summary"] as? String { Text(s).font(.body).fixedSize(horizontal: false, vertical: true) }
            if let f = result["fix"] as? String { Text(f).font(.callout.bold()).fixedSize(horizontal: false, vertical: true) }
            DisclosureGroup("Details") {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Photos used: \(result["frames_used"] as? Int ?? 0) of \(result["frames"] as? Int ?? 0)")
                    if let a = (fit?["a"] as? NSNumber)?.doubleValue, let b = (fit?["b"] as? NSNumber)?.doubleValue {
                        Text(String(format: "Scale %.4f, offset %+.1f mm", a, b * 1000))
                    }
                    ForEach(result["by_colour"] as? [[String: Any]] ?? [], id: \.description) { c in
                        Text("\(c["colour"] as? String ?? ""): reads \((c["bias_mm"] as? NSNumber)?.stringValue ?? "?") mm off")
                    }
                }
                .font(.caption).frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.caption)
            Button(good ? "Calibrate again" : "Try again", action: again).buttonStyle(.borderedProminent)
        }
    }
}
