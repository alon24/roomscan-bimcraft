import SwiftUI
import RoomPlan
import ARKit

final class ScanModel: NSObject, ObservableObject, RoomCaptureViewDelegate, RoomCaptureSessionDelegate {
    let captureView = RoomCaptureView(frame: .zero)
    @Published var scanning = false
    @Published var status = ""
    @Published var jsonURL: URL?
    @Published var usdzURL: URL?
    @Published var photoURLs: [URL] = []
    @Published var hud = ""
    @Published var wallCount = 0
    /// Quick test: the scan stops itself after `quickSeconds` and uploads, to see if anything comes out.
    @Published var quickLeft: Int?
    private var quick = false
    private var quickEnd = Date.distantFuture
    static let quickSeconds = 30
    private var scanStarted = Date()
    private var discarding = false
    let live3D = LiveRoomScene()
    private var lastScene = Date.distantPast
    private var liveRoom: CapturedRoom?
    private var tooDark = false
    @Published var photosMetaURL: URL?
    private var recorder: PhotoRecorder?
    private var photoTimer: Timer?
    private var result: CapturedRoom?
    private var lastLive = Date.distantPast
    @AppStorage("serverURL") private var serverURL = ServerAddress.defaultURL
    @AppStorage("liveStream") private var liveStream = true

    override init() {
        super.init()
        captureView.delegate = self
        captureView.captureSession.delegate = self
    }

    // RoomCaptureViewDelegate inherits NSCoding; this model is never archived.
    required init?(coder: NSCoder) { nil }
    func encode(with coder: NSCoder) {}

    func start(quick: Bool = false) {
        self.quick = quick
        quickEnd = quick ? Date().addingTimeInterval(TimeInterval(Self.quickSeconds)) : .distantFuture
        quickLeft = quick ? Self.quickSeconds : nil
        jsonURL = nil; usdzURL = nil; result = nil; status = ""
        photoURLs = []; photosMetaURL = nil
        live3D.reset()
        wallCount = 0; scanStarted = Date(); discarding = false
        recorder = PhotoRecorder()
        recorder?.maxPhotos = 80
        photoTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.updateHUD(); self?.capturePhoto() }
        captureView.captureSession.run(configuration: RoomCaptureSession.Configuration())
        scanning = true
        Screen.awake(true)   // keep the screen on while scanning
    }

    /// Light, tracking and live measurements, so a bad scan is noticed while there is still time to fix it.
    /// Ambient intensity is about 1000 in a well-lit room; LiDAR works in the dark, but the photos do not.
    private func updateHUD() {
        if quick, scanning {
            let left = Int(quickEnd.timeIntervalSinceNow.rounded(.up))
            quickLeft = max(0, left)
            if left <= 0 { stop(); return }
        }
        guard let frame = captureView.captureSession.arSession.currentFrame else { return }
        live3D.setPhone(frame.camera.transform)
        var parts: [String] = []
        if let light = frame.lightEstimate?.ambientIntensity {
            tooDark = light < 150
            parts.append(light < 150 ? "Too dark for good photos (\(Int(light)))" : light < 400 ? "Dim light (\(Int(light)))" : "Light OK")
        }
        switch frame.camera.trackingState {
        case .normal: break
        case .notAvailable: parts.append("Tracking lost")
        case .limited(.excessiveMotion): parts.append("Moving too fast")
        case .limited(.insufficientFeatures): parts.append("Not enough detail or light")
        case .limited: parts.append("Tracking limited")
        }
        if wallCount == 0 && Date().timeIntervalSince(scanStarted) > 10 {
            parts.append("No walls found yet: step back until you see the floor and the ceiling line, and point at the corners")
        }
        if let room = liveRoom, !room.walls.isEmpty {
            let longest = room.walls.map { $0.dimensions.x }.max() ?? 0
            let height = room.walls.map { $0.dimensions.y }.max() ?? 0
            parts.append(String(format: "%d walls · longest %.2f m · height %.2f m", room.walls.count, longest, height))
        }
        hud = parts.joined(separator: " · ")
    }

    /// A real photo whenever the camera has moved ~25 cm or turned ~15 degrees, so every view of the room is covered.
    private func capturePhoto() {
        guard !tooDark, let frame = captureView.captureSession.arSession.currentFrame else { return }
        recorder?.consider(frame)
    }

    private var stopAsked = Date.distantPast

    /// Stopping before any wall is found gives an empty plan: say so, and stop only on a second tap.
    func requestStop() {
        if wallCount == 0 && Date().timeIntervalSince(stopAsked) > 6 {
            stopAsked = Date()
            status = "No walls found yet, so the plan would be empty. Keep scanning: step back so the floor and ceiling lines show, turn slowly towards each wall and corner. Tap Stop again to stop anyway."
            return
        }
        stop()
    }

    func stop() {
        photoTimer?.invalidate(); photoTimer = nil
        quickLeft = nil
        photoURLs = recorder?.urls ?? []
        photosMetaURL = recorder?.writeManifest()
        captureView.captureSession.stop()
        scanning = false
        Screen.awake(false)
    }

    // MARK: RoomCaptureSessionDelegate: stream the partial room to the web UI (about 2 per second)
    func captureSession(_ session: RoomCaptureSession, didUpdate room: CapturedRoom) {
        liveRoom = room
        DispatchQueue.main.async { [weak self] in self?.wallCount = room.walls.count }
        if Date().timeIntervalSince(lastScene) > 0.5 {
            lastScene = Date()
            DispatchQueue.main.async { [live3D] in live3D.update(room) }
        }
        guard liveStream, Date().timeIntervalSince(lastLive) > 0.5,
              let data = try? JSONEncoder().encode(room) else { return }
        lastLive = Date()
        let server = serverURL
        Task.detached { await Uploader.live(json: data, server: server) }
    }

    // MARK: RoomCaptureViewDelegate
    func captureView(shouldPresent roomDataForProcessing: CapturedRoomData, error: Error?) -> Bool { true }

    /// Throw the scan away and return to the start, whatever state it is in.
    func reset() {
        photoTimer?.invalidate(); photoTimer = nil
        if scanning { discarding = true; captureView.captureSession.stop() }   // its final result must not come back
        scanning = false; quick = false; quickLeft = nil
        jsonURL = nil; usdzURL = nil; result = nil; liveRoom = nil
        status = ""; hud = ""; wallCount = 0
        photoURLs = []; photosMetaURL = nil; recorder = nil
        live3D.reset()
        Reporter.log("room: reset by user")
    }

    func captureView(didPresent processedResult: CapturedRoom, error: Error?) {
        if discarding { discarding = false; return }
        result = processedResult
        wallCount = processedResult.walls.count
        // A quick test still uploads furniture without walls, so it shows what the phone saw.
        if processedResult.walls.isEmpty && (!quick || processedResult.objects.isEmpty) {
            status = "No walls were found, so there is nothing to upload. Tap Reset and scan again: stand back so the floor and ceiling lines show, move slowly, and point at the corners."
            Reporter.report(tab: "room", message: "Scan finished with no walls", detail: "doors \(processedResult.doors.count), objects \(processedResult.objects.count)")
            DispatchQueue.main.async { [live3D] in live3D.update(processedResult) }
            return
        }
        DispatchQueue.main.async { [live3D] in live3D.update(processedResult) }
        let dir = FileManager.default.temporaryDirectory
        let json = dir.appendingPathComponent("scan.json")
        let usdz = dir.appendingPathComponent("scan.usdz")
        do {
            try JSONEncoder().encode(processedResult).write(to: json)
            jsonURL = json
            try? processedResult.export(to: usdz, exportOptions: .parametric)
            usdzURL = FileManager.default.fileExists(atPath: usdz.path) ? usdz : nil
            status = "Scan ready"
            if quick {
                quick = false
                let server = serverURL
                Task { await upload(server: server) }
            }
        } catch {
            status = "Export failed: \(error.localizedDescription)"
            Reporter.report(tab: "room", message: "Export failed", detail: "\(error)")
        }
    }

    private var package: ScanPackage? {
        guard let jsonURL else { return nil }
        return ScanPackage(name: "Floor plan \(Date().formatted())", kind: "room", json: jsonURL, usdz: usdzURL,
                           photosMeta: photosMetaURL, photos: photoURLs,
                           info: ["scan type": "Floor plan (RoomPlan)", "app": AppVersion.text, "device": Reporter.deviceModel()])
    }

    func saveOnPhone() {
        guard let package else { return }
        status = (try? LocalStore.save(package)) != nil ? "Saved on the phone (Models tab, On this phone)" : "Could not save on the phone"
    }

    @MainActor func upload(server: String) async {
        guard let package else { return }
        status = "Uploading…"
        do {
            _ = try await Uploader.upload(package, server: server)
            status = "Uploaded"
        } catch {
            let saved = (try? LocalStore.save(package)) != nil
            status = "Upload failed: \(error.localizedDescription)." + (saved ? " Saved on the phone: upload it later from Models, On this phone." : "")
            Reporter.report(tab: "room", message: "Upload failed", detail: "\(error)")
        }
    }
}

struct CaptureViewRepresentable: UIViewRepresentable {
    let view: RoomCaptureView
    func makeUIView(context: Context) -> RoomCaptureView { view }
    func updateUIView(_ uiView: RoomCaptureView, context: Context) {}
}

struct ScanView: View {
    @StateObject private var model = ScanModel()
    @AppStorage("serverURL") private var serverURL = ServerAddress.defaultURL
    @State private var mode = 0   // 0 camera, 1 3D model
    @State private var zoom: CGFloat = 1

    private var guide: String {
        if model.scanning {
            return "The phone is measuring the room. Walk slowly and point it at every wall, corner, door and window, from floor to ceiling. Apple's outline appears over the camera as it recognises walls; the 3D model tab shows them too: solid lines are finished, dashed lines are still missing. Tilting or moving the phone is fine: it knows where it is from its motion sensors. Tap Stop when the outline is closed."
        }
        if model.jsonURL != nil {
            return "Scan done. Turn the 3D model to check it. Tap Upload to send it to the server, or Start scan to redo it."
        }
        return "What this does: it measures a whole room and builds a floor plan with walls, doors and windows. 1. Tap Start scan.  2. Stand in the room, step back so you see the floor and the ceiling line, and slowly turn and walk, pointing at every wall and corner (about a minute).  3. Switch to 3D model to see what was captured.  4. Tap Stop, then Upload. Quick test scans for \(ScanModel.quickSeconds) seconds, stops by itself and uploads, to check that something comes out. Zoom with two fingers or the magnifier buttons: it magnifies what you see; the scan still records everything."
    }

    var body: some View {
        ZStack {
            CaptureViewRepresentable(view: model.captureView).ignoresSafeArea()
                .scaleEffect(zoom)
                .opacity(mode == 0 ? 1 : 0)
            if mode == 1 { LiveRoom3DView(model: model.live3D).ignoresSafeArea() }
            VStack(spacing: 8) {
                Picker("View", selection: $mode) {
                    Text("Camera").tag(0)
                    Text("3D model").tag(1)
                }
                .pickerStyle(.segmented)
                HelpCard(text: guide)
                if mode == 0 { ZoomControl(zoom: $zoom) }
                Spacer()
                if mode == 1 {
                    SceneSummary(scene: model.live3D)
                    Text("One finger turns, pinch zooms, two fingers move, double-tap resets").font(.caption2).foregroundStyle(.secondary)
                }
                if model.scanning {
                    Text((model.wallCount == 0 ? "Looking for walls…" : "\(model.wallCount) walls found")
                         + (model.quickLeft.map { " · \($0) s" } ?? ""))
                        .font(.title3.weight(.bold)).padding(8).background(.regularMaterial, in: Capsule())
                }
                if model.scanning, !model.hud.isEmpty { Text(model.hud).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                HStack {
                    if model.scanning {
                        Button("Stop") { model.requestStop() }
                    } else {
                        Button(model.jsonURL != nil || !model.status.isEmpty ? "Scan again" : "Start scan") { model.reset(); model.start() }
                        Button("Quick test (\(ScanModel.quickSeconds) s)") { model.reset(); model.start(quick: true) }
                    }
                    if model.scanning || model.jsonURL != nil || !model.status.isEmpty {
                        Button("Reset", role: .destructive) { model.reset() }
                    }
                    if mode == 1 { Button("Recenter") { model.live3D.recenter() } }
                    if model.jsonURL != nil, !model.scanning {
                        Button("Upload") { Task { await model.upload(server: serverURL) } }
                        Button("Save") { model.saveOnPhone() }
                    }
                }
                .buttonStyle(CaptureButtonStyle())
            }
            .padding()
        }
        .pinchZoom($zoom, enabled: mode == 0)
    }
}

/// Observes the 3D scene directly, because ScanView does not see changes inside a nested object.
struct SceneSummary: View {
    @ObservedObject var scene: LiveRoomScene
    var body: some View {
        Text(scene.summary).font(.caption).padding(6).background(.thinMaterial, in: Capsule())
    }
}
