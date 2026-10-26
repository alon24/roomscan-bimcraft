import SwiftUI
import RealityKit

/// Object Capture for small parts: guided capture, on-device reconstruction to USDZ, upload.
/// All mesh cleanup and STL/3MF export happens on the server.
@MainActor
final class ObjectScanModel: ObservableObject {
    @Published var session: ObjectCaptureSession?
    @Published var state: ObjectCaptureSession.CaptureState = .initializing
    @Published var status = ""
    @Published var progress = 0.0
    @Published var reconstructing = false
    @Published var usdzURL: URL?

    private var imagesDir: URL?
    private var watcher: Task<Void, Never>?

    var supported: Bool { ObjectCaptureSession.isSupported }

    func begin() {
        usdzURL = nil; status = ""; progress = 0
        let base = FileManager.default.temporaryDirectory.appendingPathComponent("object-\(UUID().uuidString)")
        let images = base.appendingPathComponent("Images")
        try? FileManager.default.createDirectory(at: images, withIntermediateDirectories: true)
        imagesDir = images
        let s = ObjectCaptureSession()
        var config = ObjectCaptureSession.Configuration()
        config.checkpointDirectory = base.appendingPathComponent("Snapshots")
        s.start(imagesDirectory: images, configuration: config)
        session = s
        watcher = Task { [weak self] in
            for await st in s.stateUpdates {
                guard let self else { return }
                self.state = st
                if case .failed(let e) = st { self.status = "Capture failed: \(e.localizedDescription)" }
                if st == .completed { await self.reconstruct() }
            }
        }
    }

    func cancel() {
        session?.cancel()
        watcher?.cancel()
        session = nil
    }

    private func reconstruct() async {
        guard let imagesDir else { return }
        reconstructing = true
        status = "Building model…"
        let out = imagesDir.deletingLastPathComponent().appendingPathComponent("scan.usdz")
        do {
            let ps = try PhotogrammetrySession(input: imagesDir, configuration: PhotogrammetrySession.Configuration())
            try ps.process(requests: [.modelFile(url: out, detail: .reduced)])
            for try await o in ps.outputs {
                switch o {
                case .requestProgress(_, let f): progress = f
                case .requestError(_, let e): throw e
                case .requestComplete: usdzURL = out; status = "Model ready"
                default: break
                }
            }
        } catch {
            status = "Reconstruction failed: \(error.localizedDescription)"
        }
        reconstructing = false
        session = nil
    }

    func upload(server: String, scale: Double) async {
        guard let usdzURL else { return }
        status = "Uploading…"
        do {
            try await Uploader.upload(json: nil, usdz: usdzURL, name: "Part \(Date().formatted())", server: server, kind: "object")
            status = "Uploaded"
        } catch {
            status = "Upload failed: \(error.localizedDescription) — use Share"
        }
    }
}

struct ObjectScanView: View {
    @StateObject private var model = ObjectScanModel()
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"

    var body: some View {
        ZStack(alignment: .bottom) {
            if let session = model.session, !model.reconstructing {
                ObjectCaptureView(session: session).ignoresSafeArea()
            } else {
                Color(.systemBackground).ignoresSafeArea()
            }
            VStack(spacing: 8) {
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                if model.reconstructing { ProgressView(value: model.progress).padding(.horizontal) }
                HStack {
                    if !model.supported {
                        Text("Object Capture needs a LiDAR iPhone").foregroundStyle(.secondary)
                    } else if model.session == nil {
                        Button("New object") { model.begin() }
                    } else {
                        switch model.state {
                        case .ready: Button("Detect object") { _ = model.session?.startDetecting() }
                        case .detecting: Button("Start capture") { model.session?.startCapturing() }
                        case .capturing: Button("Finish") { model.session?.finish() }
                        default: EmptyView()
                        }
                        Button("Cancel") { model.cancel() }
                    }
                    if let url = model.usdzURL {
                        Button("Upload") { Task { await model.upload(server: serverURL, scale: 1) } }
                        ShareLink(item: url) { Text("Share") }
                    }
                }
                .buttonStyle(.borderedProminent)
            }
            .padding()
        }
    }
}
