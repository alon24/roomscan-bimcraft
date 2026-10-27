import SwiftUI
import ARKit

/// After printing the marker ring: what to measure with calipers, a few photos of the ring, and a check on the server
/// (do the markers fit the sizes you measured, does the LiDAR agree). If it checks out, the server uses your sizes for ring scans.
final class RingCalibrationModel: NSObject, ObservableObject {
    let sceneView = ARSCNView(frame: .zero)
    @Published var count = 0
    @Published var note = ""
    @Published var result: [String: Any]?
    @Published var busy = false
    private var recorder = PhotoRecorder()

    static let tips = [
        "Photo 1: straight above the ring, about 30 cm away.",
        "Photo 2: from the front, tilted about 45 degrees.",
        "Photo 3: from the right, tilted about 45 degrees.",
        "Photo 4: from behind, tilted about 45 degrees.",
        "Photo 5: from the left, tilted about 45 degrees.",
        "Photo 6: from a low angle, about 30 degrees above the table.",
    ]

    var tip: String { count < Self.tips.count ? Self.tips[count] : "Enough photos. You can take more, or send them now." }

    func start() {
        let cfg = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        sceneView.session.run(cfg)
    }

    func pause() { sceneView.session.pause() }

    func reset() {
        recorder = PhotoRecorder()
        recorder.maxSide = 1920
        recorder.maxPhotos = 12
        count = 0; result = nil; note = ""
    }

    func take() {
        recorder.maxSide = 1920
        guard let frame = sceneView.session.currentFrame else { note = "The camera is not ready yet."; return }
        let before = recorder.count
        recorder.consider(frame, force: true)
        if recorder.count == before { note = "Hold the phone still for a moment and try again." } else { note = ""; count = recorder.count }
    }

    @MainActor func send(server: String, markerMM: Double, outerMM: Double?) async {
        guard let meta = recorder.writeManifest() else { note = "Take at least one photo first."; return }
        busy = true; note = "Checking…"
        do {
            let centre = outerMM.map { $0 - markerMM }         // outer edge to outer edge of opposite markers, minus one marker = centre to centre
            result = try await Uploader.calibrateRing(server: server, markerMM: markerMM, centreDistanceMM: centre,
                                                      photos: recorder.urls, meta: meta)
            note = ""
        } catch {
            note = "Could not check: \(error.localizedDescription)"
            Reporter.report(tab: "ring-calibration", message: "Check failed", detail: "\(error)")
        }
        busy = false
    }
}

struct RingCalibrationView: View {
    @StateObject private var model = RingCalibrationModel()
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @State private var markerText = ""
    @State private var outerText = ""

    private var markerMM: Double? { Double(markerText.replacingOccurrences(of: ",", with: ".")) }
    private var outerMM: Double? { Double(outerText.replacingOccurrences(of: ",", with: ".")) }

    var body: some View {
        Form {
            Section("1. What to measure with the calipers") {
                Text("""
                A. Marker size. Measure the outer edge of the black border of three different markers, in both directions, and average them. It should be 22.0 mm.

                B. Across the ring. Measure from the outer edge of marker 0 (the one just outside the orange triangle) to the outer edge of the marker exactly opposite. It should be 184.0 mm.

                C. Flat. Put the ring on a flat table and press it down. A sheet of paper must not slide under any part of it. If it rocks or lifts, the pose will be a little off.

                D. Matte. Tilt the ring under a lamp. No shiny patches on the white or the black.

                Tip: a reading that is 0.3 mm off is fine; 1 mm off is not. The photo check below catches big mistakes.
                """).font(.callout)
            }
            Section("2. Your readings (millimetres)") {
                TextField("A. Marker size (22.0)", text: $markerText).keyboardType(.decimalPad)
                TextField("B. Across the ring (184.0), optional", text: $outerText).keyboardType(.decimalPad)
            }
            Section("3. Take 6 photos of the ring") {
                ARSceneRepresentable(view: model.sceneView)
                    .frame(height: 320).listRowInsets(EdgeInsets())
                Text(model.tip).font(.callout)
                Text("Each photo: at least 4 markers in view, sharp, no glare, steady phone.").font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Take photo") { model.take() }.buttonStyle(.borderedProminent)
                    Text("\(model.count) taken").font(.callout.monospacedDigit())
                    Spacer()
                    Button("Start over", role: .destructive) { model.reset() }
                }
                if !model.note.isEmpty { Text(model.note).font(.callout) }
            }
            Section("4. Check") {
                Button(model.busy ? "Checking…" : "Send photos and check") {
                    Task { await model.send(server: serverURL, markerMM: markerMM ?? 22.0, outerMM: outerMM) }
                }
                .disabled(model.busy || model.count < 3)
                if model.count < 3 { Text("Take at least 3 photos first.").font(.caption).foregroundStyle(.secondary) }
                if let r = model.result { ResultBlock(result: r) }
            }
        }
        .navigationTitle("Calibrate the ring")
        .onAppear { model.reset(); model.start() }
        .onDisappear { model.pause() }
    }
}

private struct ResultBlock: View {
    let result: [String: Any]

    var body: some View {
        let good = result["good"] as? Bool ?? false
        let saved = result["saved"] as? Bool ?? false
        VStack(alignment: .leading, spacing: 6) {
            Label(good ? (saved ? "Good. Saved for ring scans." : "Good.") : "Not good yet",
                  systemImage: good ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(good ? .green : .orange).font(.headline)
            Text("Photos with 4 or more markers: \(result["photos_good"] as? Int ?? 0) of \(result["photos"] as? Int ?? 0)")
            Text("Markers per photo: \(String(describing: result["markers_avg"] ?? "?")), width in pixels: \(String(describing: result["edge_px_avg"] ?? "?"))")
            Text("Fit error: \(String(describing: result["error_px"] ?? "?")) px (under 1.5 is good)")
            if let s = result["lidar_scale"] { Text("LiDAR check: \(String(describing: s)) (1.00 means it agrees with your readings, within about 3%)") }
            ForEach(result["messages"] as? [String] ?? [], id: \.self) { Text($0).font(.callout) }
        }
        .font(.caption)
    }
}
