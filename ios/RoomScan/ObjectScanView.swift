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
    @Published var feedback = ""
    @Published var reconstructing = false
    @Published var usdzURL: URL?
    @Published var photoURLs: [URL] = []
    @Published var photosMetaURL: URL?
    private var sampledIndexes: [Int] = []

    private var imagesDir: URL?
    private var watcher: Task<Void, Never>?
    private var feedbackWatcher: Task<Void, Never>?

    var supported: Bool { ObjectCaptureSession.isSupported }

    /// Stop listening to the old session so it can be released; a second session cannot start while the first lives.
    private func releaseSession() {
        watcher?.cancel(); watcher = nil
        feedbackWatcher?.cancel(); feedbackWatcher = nil
        session = nil
    }

    func begin() {
        releaseSession()
        state = .initializing
        usdzURL = nil; status = ""; progress = 0; feedback = ""
        let base = FileManager.default.temporaryDirectory.appendingPathComponent("object-\(UUID().uuidString)")
        let images = base.appendingPathComponent("Images")
        try? FileManager.default.createDirectory(at: images, withIntermediateDirectories: true)
        imagesDir = images
        let s = ObjectCaptureSession()
        var config = ObjectCaptureSession.Configuration()
        config.checkpointDirectory = base.appendingPathComponent("Snapshots")
        s.start(imagesDirectory: images, configuration: config)
        session = s
        feedbackWatcher = Task { [weak self] in
            for await set in s.feedbackUpdates {
                self?.feedback = set.map { Self.words("\($0)") }.sorted().joined(separator: ", ")
            }
        }
        watcher = Task { [weak self] in
            for await st in s.stateUpdates {
                guard let self else { return }
                self.state = st
                Reporter.log("object state: \(st)")
                if case .failed(let e) = st {
                    self.status = "Capture failed: \(e.localizedDescription)"
                    Reporter.report(tab: "object", message: "Capture failed", detail: "\(e)")
                }
                if st == .completed { await self.reconstruct() }
            }
        }
    }

    /// "objectTooClose" -> "object too close"
    private static func words(_ s: String) -> String {
        var out = ""
        for ch in s { if ch.isUppercase { out += " " + ch.lowercased() } else { out.append(ch) } }
        return out
    }

    func cancel() {
        session?.cancel()
        releaseSession()
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
            Reporter.report(tab: "object", message: "Reconstruction failed", detail: "\(error)")
        }
        reconstructing = false
        releaseSession()
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
            Reporter.report(tab: "object", message: "Upload failed", detail: "\(error)")
        }
    }
}

struct ObjectScanView: View {
    @StateObject private var model = ObjectScanModel()
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"

    private var guide: String {
        if !model.supported { return "Object Capture needs an iPhone with LiDAR." }
        if model.reconstructing { return "Building the 3D model on the phone. Keep the app open; this takes a few minutes." }
        if model.usdzURL != nil { return "Done. Turn the model to check it, then tap Upload." }
        guard let s = model.session else {
            return "Put the part on a table with a plain background and good light. Tap New object."
        }
        switch s.state {
        case .initializing: return "Getting ready…"
        case .ready: return "Point the camera at the part so it is in the middle of the screen, about 30 cm away. Then tap Detect object."
        case .detecting: return "The box shows what will be scanned. Drag its sides to fit the part, then tap Start capture."
        case .capturing:
            if s.userCompletedScanPass {
                return "That side is done. Turn the part onto another side so the underside shows, then tap Flip and continue. Tap Finish when every side is done."
            }
            return "Walk slowly around the part, keeping it in view. The ring fills as photos are taken, and dots show what has been captured. Tap Finish when done."
        case .finishing: return "Finishing…"
        default: return ""
        }
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            if let session = model.session, !model.reconstructing {
                ObjectCaptureView(session: session).ignoresSafeArea()
            } else if let url = model.usdzURL {
                ModelPreview(url: url).ignoresSafeArea()
            } else {
                Color(.systemBackground).ignoresSafeArea()
            }
            VStack(spacing: 8) {
                if !guide.isEmpty { Text(guide).font(.caption).padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10)) }
                Spacer()
                if let s = model.session, s.state == .capturing {
                    Text("\(s.numberOfShotsTaken) photos").font(.caption).padding(6).background(.thinMaterial, in: Capsule())
                }
                if !model.feedback.isEmpty { Text(model.feedback.capitalized).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                if model.reconstructing { ProgressView(value: model.progress).padding(.horizontal) }
                HStack {
                    if !model.supported {
                        EmptyView()
                    } else if model.session == nil {
                        Button("New object") { model.begin() }
                    } else if !model.reconstructing {
                        switch model.session?.state ?? .initializing {
                        case .ready: Button("Detect object") { _ = model.session?.startDetecting() }
                        case .detecting: Button("Start capture") { model.session?.startCapturing() }
                        case .capturing:
                            if model.session?.userCompletedScanPass == true {
                                Button("Flip and continue") { model.session?.beginNewScanPassAfterFlip() }
                            }
                            Button("Finish") { model.session?.finish() }
                        case .failed: Text("Capture failed. Tap Cancel and try again.").font(.caption)
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
