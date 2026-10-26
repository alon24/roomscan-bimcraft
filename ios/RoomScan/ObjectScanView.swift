import SwiftUI
import RealityKit
import CoreImage

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
    @Published var photoURLs: [URL] = []
    @Published var photosMetaURL: URL?
    private var sampledIndexes: [Int] = []

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

    /// Up to 24 evenly spaced shots from Object Capture's image folder, downsized to JPEG for the web gallery.
    private func samplePhotos(from dir: URL) -> [URL] {
        let files = ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
            .filter { ["heic", "jpg", "jpeg", "png"].contains($0.pathExtension.lowercased()) }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        sampledIndexes = []
        guard !files.isEmpty else { return [] }
        let step = max(1, files.count / 24)
        let ctx = CIContext()
        var out: [URL] = []
        for (i, f) in files.enumerated() where i % step == 0 && out.count < 24 {
            guard var img = CIImage(contentsOf: f, options: [.applyOrientationProperty: true]) else { continue }
            let k = min(1, 1280 / max(img.extent.width, img.extent.height))
            img = img.transformed(by: CGAffineTransform(scaleX: k, y: k))
            guard let jpeg = ctx.jpegRepresentation(of: img, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, options: [:]) else { continue }
            let url = dir.deletingLastPathComponent().appendingPathComponent(String(format: "o%03d.jpg", out.count + 1))
            if (try? jpeg.write(to: url)) != nil { out.append(url); sampledIndexes.append(i) }
        }
        return out
    }

    /// Camera poses of the uploaded photos, in the photogrammetry model's frame (not the printer frame).
    /// Assumes sample ids follow the sorted file order of the images folder; the server records that assumption.
    private func writePoses(_ poses: PhotogrammetrySession.Poses, in dir: URL) {
        var photos: [[String: Any]] = []
        for (n, sample) in sampledIndexes.enumerated() {
            guard let pose = poses.posesBySample[sample] else { continue }
            let q = pose.rotation.vector, t = pose.translation
            photos.append(["file": String(format: "o%03d.jpg", n + 1), "sample_index": sample,
                           "rotation_xyzw": [q.x, q.y, q.z, q.w], "translation": [t.x, t.y, t.z]])
        }
        let manifest: [String: Any] = ["version": 2, "frame": "photogrammetry model (USDZ) frame, metres",
                                       "sample_index_assumption": "sorted order of Object Capture image files", "photos": photos]
        let url = dir.appendingPathComponent("photos.json")
        if let data = try? JSONSerialization.data(withJSONObject: manifest) , (try? data.write(to: url)) != nil { photosMetaURL = url }
    }

    private func reconstruct() async {
        guard let imagesDir else { return }
        photoURLs = samplePhotos(from: imagesDir)
        reconstructing = true
        status = "Building model…"
        let out = imagesDir.deletingLastPathComponent().appendingPathComponent("scan.usdz")
        do {
            let ps = try PhotogrammetrySession(input: imagesDir, configuration: PhotogrammetrySession.Configuration())
            try ps.process(requests: [.modelFile(url: out, detail: .reduced), .poses])
            for try await o in ps.outputs {
                switch o {
                case .requestProgress(_, let f): progress = f
                case .requestError(_, let e): throw e
                case .requestComplete(_, let result):
                    switch result {
                    case .poses(let p): writePoses(p, in: imagesDir.deletingLastPathComponent())
                    case .modelFile: usdzURL = out; status = "Model ready"
                    default: break
                    }
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
            let id = try await Uploader.upload(json: nil, usdz: usdzURL, name: "Part \(Date().formatted())", server: server, kind: "object", photos: photoURLs, photosMeta: photosMetaURL)
            if let id { UserDefaults.standard.set(id, forKey: "lastScanID") }
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
