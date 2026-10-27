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
    private var galleryDir: URL?          // the original photos, for the web gallery, when the build uses masked copies
    var realHeightMM: Double?             // turntable scans have no scale of their own; the server rescales to this height
    private var cancelled = false

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
        galleryDir = nil
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

    /// Build the model from the turntable photos. The photos are kept unless the build succeeds, so a failure can be retried.
    func buildFromTurntable(_ turntable: TurntableCapture) {
        guard let dir = turntable.imagesDir else { return }
        turntable.shutdown()
        usdzURL = nil; status = ""; progress = 0
        reconstructing = true
        Task {
            // Remove the static background first: otherwise photogrammetry matches the background, not the part.
            let files = ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
                .filter { $0.pathExtension.lowercased() == "jpg" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
            status = "Removing the background from \(files.count) photos…"
            let result = await SubjectMasker.mask(images: files) { [weak self] n, total in
                Task { @MainActor in self?.progress = Double(n) / Double(max(total, 1)); self?.status = "Removing the background \(n)/\(total)…" }
            }
            Reporter.log("turntable: masked \(result?.masked ?? 0) of \(files.count) photos")
            if let result { imagesDir = result.dir; galleryDir = dir } else { imagesDir = dir; galleryDir = nil }
            await reconstruct(sequential: true)
            if usdzURL != nil { turntable.reset() }
        }
    }

    /// Abandon whatever is going on (capture, build, result, turntable photos) and return to the start.
    func startOver(_ turntable: TurntableCapture) {
        cancelled = true
        photoSession?.cancel()
        session?.cancel()
        releaseSession()
        reconstructing = false
        feedback = ""
        turntable.reset()
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

    private func reconstruct(sequential: Bool = false) async {
        guard let imagesDir else { return }
        let count = ((try? FileManager.default.contentsOfDirectory(at: imagesDir, includingPropertiesForKeys: nil)) ?? []).count
        Reporter.log("reconstruct: \(count) files, sequential=\(sequential)")
        guard count >= 8 else {
            status = "Only \(count) photos were captured. Scan longer and from more sides."
            Reporter.report(tab: "object", message: "Too few photos to build", detail: "\(count) files in \(imagesDir.lastPathComponent)")
            releaseSession()
            return
        }
        photoURLs = samplePhotos(from: galleryDir ?? imagesDir)
        reconstructing = true
        cancelled = false
        status = "Building model…"
        let out = imagesDir.deletingLastPathComponent().appendingPathComponent("scan.usdz")
        // First with the camera poses too; if Apple's engine refuses that combination, again for the model alone.
        for withPoses in [true, false] {
            do {
                var config = PhotogrammetrySession.Configuration()
                if sequential { config.sampleOrdering = .sequential }   // turntable photos follow each other around the part
                let ps = try PhotogrammetrySession(input: imagesDir, configuration: config)
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

    func upload(server: String, scale: Double) async {
        guard let usdzURL else { return }
        status = "Uploading…"
        do {
            let id = try await Uploader.upload(json: nil, usdz: usdzURL, name: "Part \(Date().formatted())", server: server, kind: "object", photos: photoURLs, photosMeta: photosMetaURL, realHeightMM: realHeightMM,
                                               info: ["scan type": "Object capture (walk around)", "app": AppVersion.text, "device": Reporter.deviceModel(), "ios": UIDevice.current.systemVersion, "photos sent": photoURLs.count, "detail": "reduced"])
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
    @StateObject private var turntable = TurntableCapture()
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    var walkOnly = false          // the Object tab's "Without the ring": Apple's walk-around capture only
    @State private var mode = 0   // 0 walk around the part, 1 turntable (phone fixed, part turns)
    @State private var heightText = ""
    @State private var zoomObj: CGFloat = 1

    private var idle: Bool { model.session == nil && !model.reconstructing && model.usdzURL == nil }

    private var guide: String {
        if idle && mode == 1 {
            if turntable.running { return "Capturing every \(String(format: "%.1f", turntable.interval)) s. Let the part turn once or twice, slowly, then tap Stop." }
            if turntable.count > 0 { return "\(turntable.count) photos taken. Tap Build model, or lay the part on another side and tap Add pass to cover the underside." }
            return "PHOTOS ONLY: this mode does not use the LiDAR, so the size is not real and plain white parts reconstruct badly. For real size, LiDAR and the printed ring, use the Object tab, With the ring. Phone on a stand, 20 to 40 cm from the plate. Hang a plain sheet behind the part so nothing else is in the picture, keep hands out of view, and use steady light. A plain white part has nothing to match: stick on some tape or paper marks. Start the plate turning slowly, then tap Start."
        }
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

    /// "LiDAR turntable" is the Mesh tab's turntable and ring scanner (LiDAR depth, real size), shown here under the same mode switch.
    var body: some View {
        if mode == 1 && idle && !walkOnly {
            VStack(spacing: 0) {
                Picker("Mode", selection: $mode) {
                    Text("Walk around").tag(0)
                    Text("LiDAR turntable").tag(1)
                }
                .pickerStyle(.segmented).padding(.horizontal).padding(.top, 8)
                MeshScanView(initialMode: 3)
            }
        } else {
            walkBody
        }
    }

    private var walkBody: some View {
        ZStack(alignment: .bottom) {
            if let session = model.session, !model.reconstructing {
                ObjectCaptureView(session: session).ignoresSafeArea().scaleEffect(session.state == .capturing ? zoomObj : 1)
            } else if let url = model.usdzURL {
                ModelPreview(url: url).ignoresSafeArea()
            } else if idle && mode == 1 {
                CameraPreview(session: turntable.session, capture: turntable).ignoresSafeArea()
            } else {
                Color(.systemBackground).ignoresSafeArea()
            }
            VStack(spacing: 8) {
                if !idle || turntable.count > 0 || !model.status.isEmpty {
                    HStack {
                        Spacer()
                        Button("Start over") { model.startOver(turntable) }
                            .buttonStyle(.bordered).tint(.red).font(.caption)
                    }
                }
                if idle && !walkOnly {
                    Picker("Mode", selection: $mode) {
                        Text("Walk around").tag(0)
                        Text("LiDAR turntable").tag(1)
                    }
                    .pickerStyle(.segmented)
                    .disabled(turntable.running)
                }
                if !guide.isEmpty { Text(guide).font(.caption).padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10)) }
                Spacer()
                if idle && mode == 1 {
                    TextField("Real height of the part in mm (sets the size)", text: $heightText)
                        .keyboardType(.decimalPad).textFieldStyle(.roundedBorder).font(.caption)
                        .onChange(of: heightText) { _, v in model.realHeightMM = Double(v.replacingOccurrences(of: ",", with: ".")) }
                    if !turntable.running {
                        HStack {
                            Image(systemName: "minus.magnifyingglass")
                            Slider(value: Binding(get: { Double(turntable.zoom) }, set: { turntable.setZoom(CGFloat($0)) }), in: 1...6)
                            Image(systemName: "plus.magnifyingglass")
                            Text(String(format: "%.1f×", Double(turntable.zoom))).font(.caption.monospacedDigit())
                        }
                        .padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
                    }
                    HStack {
                        Text("\(turntable.count) photos").font(.caption).padding(6).background(.thinMaterial, in: Capsule())
                        if !turntable.running {
                            Stepper("Every \(String(format: "%.1f", turntable.interval)) s", value: $turntable.interval, in: 0.5...5, step: 0.5)
                                .font(.caption).padding(6).background(.thinMaterial, in: Capsule())
                        }
                    }
                }
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
                    } else if idle && mode == 1 {
                        if turntable.running {
                            Button("Stop") { turntable.stop() }
                        } else {
                            Button(turntable.count > 0 ? "Add pass" : "Start") { turntable.start() }
                            if turntable.count > 0 {
                                Button("Build model") { model.buildFromTurntable(turntable) }
                                Button("Discard") { turntable.reset() }
                            }
                        }
                    } else if model.session == nil {
                        if model.reconstructing {
                            Button("Cancel build") { model.cancelBuild() }
                        } else if mode == 1 {
                            Button("New scan") { model.startOver(turntable) }
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
                    if let url = model.usdzURL {
                        Button("Upload") { Task { await model.upload(server: serverURL, scale: 1) } }
                        ShareLink(item: url) { Text("Share") }
                    }
                }
                .buttonStyle(.borderedProminent)
            }
            .padding()
        }
        .onAppear { if mode == 1 { turntable.prepare() } }
        .onChange(of: mode) { _, m in if m == 1 { turntable.prepare() } else { turntable.shutdown() } }
        .onChange(of: idle) { _, nowIdle in if !nowIdle { turntable.shutdown() } else if mode == 1 { turntable.prepare() } }
        .onDisappear { turntable.shutdown() }
    }
}
