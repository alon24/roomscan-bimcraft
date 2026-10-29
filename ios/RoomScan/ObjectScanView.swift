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
    private var photoSession: PhotogrammetrySession?
    private var cancelled = false

    private var imagesDir: URL?
    private var savedCopy: SavedScan?      // after Save the screen works from the phone's copy, and the temp folder is gone
    private var uploads = 0
    private var toDelete: [URL] = []
    private var watcher: Task<Void, Never>?
    private var feedbackWatcher: Task<Void, Never>?

    var supported: Bool { ObjectCaptureSession.isSupported }

    /// Stop listening to the old session so it can be released; a second session cannot start while the first lives.
    private func releaseSession() {
        Screen.awake(false)
        watcher?.cancel(); watcher = nil
        feedbackWatcher?.cancel(); feedbackWatcher = nil
        session = nil
    }

    func begin() {
        releaseSession()
        state = .initializing
        usdzURL = nil; status = ""; progress = 0; feedback = ""
        dispose(imagesDir?.deletingLastPathComponent())      // the last scan's folder; a saved copy is in the phone's storage
        savedCopy = nil; photoURLs = []; photosMetaURL = nil
        let base = FileManager.default.temporaryDirectory.appendingPathComponent("object-\(UUID().uuidString)")
        TempFiles.hold(base)
        let images = base.appendingPathComponent("Images")
        try? FileManager.default.createDirectory(at: images, withIntermediateDirectories: true)
        imagesDir = images
        CameraOwner.claim(self) { [weak self] in MainActor.assumeIsolated { self?.leftScreen() } }   // the screen that had the camera stops first
        let s = ObjectCaptureSession()
        var config = ObjectCaptureSession.Configuration()
        config.checkpointDirectory = base.appendingPathComponent("Snapshots")
        s.start(imagesDirectory: images, configuration: config)
        Screen.awake(true)   // keep the screen on while capturing and building
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

    /// The tab was switched (or Settings opened): the capture must not keep the camera behind another tab. A capture in progress is
    /// finished, so the photos so far are built into a model as with Finish; before capturing there is nothing to keep.
    func leftScreen() {
        guard let s = session, !reconstructing else { return }
        switch s.state {
        case .capturing:
            s.finish()
            status = "Finished because you left the scan screen: building the model from the photos taken so far."
        case .initializing, .ready, .detecting:
            cancel()
            status = "Cancelled because you left the scan screen. Tap New object to start again."
        default:
            break
        }
        Reporter.log("object: scan screen left in state \(s.state)")
    }

    /// Up to 120 evenly spaced shots from Object Capture's image folder, downsized to JPEG for the web gallery.
    private func samplePhotos(from dir: URL) -> [URL] {
        let files = ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
            .filter { ["heic", "jpg", "jpeg", "png"].contains($0.pathExtension.lowercased()) }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        sampledIndexes = []
        guard !files.isEmpty else { return [] }
        let step = max(1, files.count / 120)
        let ctx = CIContext()
        var out: [URL] = []
        for (i, f) in files.enumerated() where i % step == 0 && out.count < 120 {
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

    /// Abandon whatever is going on (capture, build, result) and return to the start.
    func startOver() {
        cancelled = true
        photoSession?.cancel()
        session?.cancel()
        releaseSession()
        // A cancelled build may still be closing its files: leave that folder to the clean-up at launch.
        if reconstructing { TempFiles.release(imagesDir?.deletingLastPathComponent()) } else { dispose(imagesDir?.deletingLastPathComponent()) }
        imagesDir = nil; savedCopy = nil
        reconstructing = false
        feedback = ""
        resetResult()
    }

    /// Clear the finished or failed result so a new scan can start.
    func resetResult() {
        usdzURL = nil; status = ""; progress = 0; photoURLs = []; photosMetaURL = nil
    }

    func cancelBuild() {
        cancelled = true
        photoSession?.cancel()
        status = "Build cancelled"
    }

    private func reconstruct() async {
        guard let imagesDir else { return }
        let count = ((try? FileManager.default.contentsOfDirectory(at: imagesDir, includingPropertiesForKeys: nil)) ?? []).count
        Reporter.log("reconstruct: \(count) files")
        guard count >= 8 else {
            status = "Only \(count) photos were captured. Scan longer and from more sides."
            Reporter.report(tab: "object", message: "Too few photos to build", detail: "\(count) files in \(imagesDir.lastPathComponent)")
            releaseSession()
            return
        }
        photoURLs = samplePhotos(from: imagesDir)
        reconstructing = true
        cancelled = false
        status = "Building model…"
        let out = imagesDir.deletingLastPathComponent().appendingPathComponent("scan.usdz")
        // First with the camera poses too; if Apple's engine refuses that combination, again for the model alone.
        for withPoses in [true, false] {
            do {
                let ps = try PhotogrammetrySession(input: imagesDir, configuration: PhotogrammetrySession.Configuration())
                photoSession = ps
                try ps.process(requests: withPoses ? [.modelFile(url: out, detail: .reduced), .poses] : [.modelFile(url: out, detail: .reduced)])
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
                if usdzURL != nil || cancelled { break }
            } catch {
                if cancelled { break }
                Reporter.report(tab: "object", message: withPoses ? "Reconstruction failed (with poses)" : "Reconstruction failed",
                                detail: "\(error)\nimages: \(count)")
                if !withPoses || usdzURL != nil {
                    status = "Reconstruction failed: \(error.localizedDescription)"
                } else {
                    status = "Retrying without camera poses…"
                }
            }
        }
        photoSession = nil
        reconstructing = false
        releaseSession()
    }

    private var package: ScanPackage? {
        guard let usdzURL else { return nil }
        return ScanPackage(name: "Part \(Date().formatted())", kind: "object", usdz: usdzURL, photosMeta: photosMetaURL, photos: photoURLs,
                           info: ["scan type": "Object capture (walk around)", "app": AppVersion.text, "device": Reporter.deviceModel(),
                                  "ios": UIDevice.current.systemVersion, "photos sent": photoURLs.count, "detail": "reduced"])
    }

    func saveOnPhone() {
        if savedCopy != nil { status = "Already saved on the phone (Models tab, On this phone)"; return }
        guard let package else { return }
        status = keepOnPhone(package, move: uploads == 0) ? "Saved on the phone (Models tab, On this phone)" : "Could not save on the phone"
    }

    /// Copy into the phone's storage, then work from that copy and delete the temp folder.
    private func keepOnPhone(_ p: ScanPackage, move: Bool) -> Bool {
        guard let s = try? LocalStore.save(p, move: move) else { return false }
        savedCopy = s
        let q = LocalStore.package(s)
        usdzURL = q.usdz; photoURLs = q.photos; photosMetaURL = q.photosMeta
        dispose(imagesDir?.deletingLastPathComponent())
        imagesDir = nil
        return true
    }

    /// Delete a temp folder, but only once no upload is reading from it.
    private func dispose(_ url: URL?) {
        if let url { toDelete.append(url) }
        guard uploads == 0 else { return }
        toDelete.forEach { TempFiles.remove($0) }
        toDelete = []
    }

    func upload(server: String) async {
        uploads += 1
        defer { uploads -= 1; dispose(nil) }
        if let s = savedCopy {
            status = "Uploading…"
            do {
                _ = try await LocalStore.upload(s, server: server)
                status = "Uploaded"
            } catch {
                status = "Upload failed: \(error.localizedDescription). It is saved on the phone: upload it later from Models, On this phone."
                Reporter.report(tab: "object", message: "Upload failed", detail: "\(error)")
            }
            return
        }
        guard let package else { return }
        status = "Uploading…"
        do {
            _ = try await Uploader.upload(package, server: server)
            status = "Uploaded"
        } catch {
            let saved = keepOnPhone(package, move: uploads == 1)   // only this failed upload read the files
            status = "Upload failed: \(error.localizedDescription)." + (saved ? " Saved on the phone: upload it later from Models, On this phone." : "")
            Reporter.report(tab: "object", message: "Upload failed", detail: "\(error)")
        }
    }
}

/// Apple's walk-around Object Capture (the Object tab's "Without the ring").
struct ObjectScanView: View {
    @StateObject private var model = ObjectScanModel()
    @AppStorage("serverURL") private var serverURL = ServerAddress.defaultURL
    @State private var zoomObj: CGFloat = 1

    private var idle: Bool { model.session == nil && !model.reconstructing && model.usdzURL == nil }

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
                ObjectCaptureView(session: session).ignoresSafeArea().scaleEffect(session.state == .capturing ? zoomObj : 1)
            } else if let url = model.usdzURL {
                ModelPreview(url: url).ignoresSafeArea()
            } else {
                Color(.systemBackground).ignoresSafeArea()
            }
            VStack(spacing: 8) {
                if !idle || !model.status.isEmpty {
                    HStack {
                        Spacer()
                        Button("Start over") { model.startOver() }
                            .buttonStyle(.bordered).tint(.red).font(.caption)
                    }
                }
                if !guide.isEmpty { Text(guide).font(.caption).padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10)) }
                Spacer()
                if let s = model.session, s.state == .capturing {
                    ZoomControl(zoom: $zoomObj)
                    Text("\(s.numberOfShotsTaken) photos").font(.caption).padding(6).background(.thinMaterial, in: Capsule())
                }
                if !model.feedback.isEmpty { Text(model.feedback.capitalized).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                if model.reconstructing { ProgressView(value: model.progress).padding(.horizontal) }
                HStack {
                    if !model.supported {
                        EmptyView()
                    } else if model.session == nil {
                        if model.reconstructing {
                            Button("Cancel build") { model.cancelBuild() }
                        } else {
                            Button(model.usdzURL != nil ? "New scan" : "New object") { model.begin() }
                        }
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
                    if model.usdzURL != nil {
                        Button("Upload") { Task { await model.upload(server: serverURL) } }
                        Button("Save") { model.saveOnPhone() }
                    }
                }
                .buttonStyle(CaptureButtonStyle())
            }
            .padding()
        }
        .onDisappear { CameraOwner.release(model) }      // runs leftScreen only if it still has the camera
    }
}
