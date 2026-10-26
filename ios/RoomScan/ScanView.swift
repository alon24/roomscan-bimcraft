import SwiftUI
import RoomPlan

final class ScanModel: NSObject, ObservableObject, RoomCaptureViewDelegate, RoomCaptureSessionDelegate {
    let captureView = RoomCaptureView(frame: .zero)
    @Published var scanning = false
    @Published var status = ""
    @Published var jsonURL: URL?
    @Published var usdzURL: URL?
    private var result: CapturedRoom?
    private var lastLive = Date.distantPast
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @AppStorage("liveStream") private var liveStream = true

    override init() {
        super.init()
        captureView.delegate = self
        captureView.captureSession.delegate = self
    }

    // RoomCaptureViewDelegate inherits NSCoding; this model is never archived.
    required init?(coder: NSCoder) { nil }
    func encode(with coder: NSCoder) {}

    func start() {
        jsonURL = nil; usdzURL = nil; result = nil; status = ""
        captureView.captureSession.run(configuration: RoomCaptureSession.Configuration())
        scanning = true
    }

    func stop() {
        captureView.captureSession.stop()
        scanning = false
    }

    // MARK: RoomCaptureSessionDelegate: stream the partial room to the web UI (about 2 per second)
    func captureSession(_ session: RoomCaptureSession, didUpdate room: CapturedRoom) {
        guard liveStream, Date().timeIntervalSince(lastLive) > 0.5,
              let data = try? JSONEncoder().encode(room) else { return }
        lastLive = Date()
        let server = serverURL
        Task.detached { await Uploader.live(json: data, server: server) }
    }

    // MARK: RoomCaptureViewDelegate
    func captureView(shouldPresent roomDataForProcessing: CapturedRoomData, error: Error?) -> Bool { true }

    func captureView(didPresent processedResult: CapturedRoom, error: Error?) {
        result = processedResult
        let dir = FileManager.default.temporaryDirectory
        let json = dir.appendingPathComponent("scan.json")
        let usdz = dir.appendingPathComponent("scan.usdz")
        do {
            try JSONEncoder().encode(processedResult).write(to: json)
            jsonURL = json
            try? processedResult.export(to: usdz, exportOptions: .parametric)
            usdzURL = FileManager.default.fileExists(atPath: usdz.path) ? usdz : nil
            status = "Scan ready"
        } catch {
            status = "Export failed: \(error.localizedDescription)"
        }
    }

    @MainActor func upload(server: String) async {
        guard let jsonURL else { return }
        status = "Uploading…"
        do {
            try await Uploader.upload(json: jsonURL, usdz: usdzURL, name: "Scan \(Date().formatted())", server: server)
            status = "Uploaded"
        } catch {
            status = "Upload failed: \(error.localizedDescription) — use Share"
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
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"

    var body: some View {
        ZStack(alignment: .bottom) {
            CaptureViewRepresentable(view: model.captureView).ignoresSafeArea()
            VStack(spacing: 8) {
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                HStack {
                    if model.scanning {
                        Button("Stop") { model.stop() }
                    } else {
                        Button("Start scan") { model.start() }
                    }
                    if model.jsonURL != nil, !model.scanning {
                        Button("Upload") { Task { await model.upload(server: serverURL) } }
                        if let url = model.jsonURL { ShareLink(item: url) { Text("Share") } }
                    }
                }
                .buttonStyle(.borderedProminent)
            }
            .padding()
        }
        .toolbar {
            NavigationLink { SettingsView() } label: { Image(systemName: "gear") }
        }
    }
}
