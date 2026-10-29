import CoreImage
import SwiftUI
import ARKit
import SceneKit
import Metal

/// Free-form scan: ARKit's LiDAR mesh is drawn over the camera while you scan, so you see exactly what has been
/// captured. Photos with poses and depth go along; the server colours the mesh from them.
final class MeshScanModel: NSObject, ObservableObject, ARSCNViewDelegate {
    let sceneView = ARSCNView(frame: .zero)
    @Published var scanning = false
    @Published var previewOn = UserDefaults.standard.object(forKey: "previewOn") as? Bool ?? true   // the live camera before Start; the icon switches it off
    @Published var status = ""
    @Published var hud = ""
    @Published var stopNote = ""          // why the scan stopped by itself (the screen was left), shown until the next scan
    @Published var plyURL: URL?
    @Published var photoURLs: [URL] = []
    @Published var photosMetaURL: URL?
    private var maskURLs: [URL] = []
    private var outlining = false
    @Published var serverOnly = false     // ring scans: only the server (or Build here) can put the frames together
    @Published var phoneResult: OnPhoneRing.Result?
    @Published var buildingOnPhone = false
    var canBuildOnPhone: Bool { ring.enabled && !outlining && !maskURLs.isEmpty && !buildingOnPhone && phoneResult == nil }
    @Published var modelScene: SCNScene?
    private var recorder: PhotoRecorder?
    private var savedCopy: SavedScan?      // after Save the screen works from the phone's copy, and the temp folder is gone
    private var uploads = 0
    private var toDelete: [URL] = []
    private var timer: Timer?
    private var ticks = 0
    struct Dot { var x: Float; var y: Float; var color: SIMD3<Float> }
    @Published var dots: [Dot] = []                // the captured points as the camera sees them now, drawn flat on top of the picture
    @Published var pointCount = 0
    @Published var framesSoFar = 0
    private var overlayTimer: Timer?
    private var lastCount = 0
    private var stalled = 0
    let ring = RingTracker()         // Object tab: the printed marker ring
    let sweep = SweepCoverage()      // "Sweep from one spot": where the camera has looked
    let live = LiveLink()            // optional two-way link to the server (docs/live-guide.md)
    var liveWanted = false           // room scans: the Room tab asks for it
    var sweepMode = false
    @Published var ringActive = false
    private var coverageTimer: Timer?
    // Depth point cloud, built from the LiDAR depth map of every camera frame. This does not depend on ARKit's mesh
    // anchors, which never arrived on the test phone.
    private var cloud: [SIMD3<Float>] = []
    private var cloudColours: [SIMD3<Float>] = []
    private var occupied = Set<Int64>()
    private var cloudTimer: Timer?
    private let ciContext = CIContext()
    private let cloudQueue = DispatchQueue(label: "mesh.cloud", qos: .userInitiated)
    private var cloudBusy = false      // a frame is being read on cloudQueue
    private var cloudGen = 0           // bumped on start, stop and reset: a late result from before is dropped
    private let maxPoints = 1_000_000
    private let voxel: Float = 0.015
    private let lock = NSLock()
    private var meshAnchors: [UUID: ARMeshAnchor] = [:]   // kept from the delegate, so Stop does not depend on currentFrame

    static var supported: Bool { ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) }

    override init() {
        super.init()
        sceneView.delegate = self
        sceneView.automaticallyUpdatesLighting = true
    }

    /// Show the camera as soon as the tab opens, so it is clear the app is alive before anything is scanned.
    func startPreview() {
        guard previewOn, !scanning, plyURL == nil else { return }
        let cfg = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        sceneView.session.run(cfg)
    }

    func setPreview(_ on: Bool) {
        previewOn = on
        UserDefaults.standard.set(on, forKey: "previewOn")
        if on { startPreview() } else if !scanning { sceneView.session.pause() }
    }

    func pausePreview() {
        if !scanning { sceneView.session.pause() }
    }

    /// Project the stored points into the current camera view. Plain 2D dots cannot fail to render the way 3D geometry can.
    private func refreshOverlay() {
        guard scanning, let frame = sceneView.session.currentFrame, !cloud.isEmpty else { dots = []; return }
        let size = sceneView.bounds.size
        guard size.width > 0 else { return }
        let inv = simd_inverse(frame.camera.transform)
        let step = max(1, cloud.count / 5000)
        var out = [Dot]()
        out.reserveCapacity(5200)
        for i in stride(from: 0, to: cloud.count, by: step) {
            let p = cloud[i]
            let c = inv * SIMD4<Float>(p.x, p.y, p.z, 1)
            guard c.z < -0.1 else { continue }                                   // in front of the camera
            let sp = frame.camera.projectPoint(p, orientation: .portrait, viewportSize: size)
            guard sp.x >= 0, sp.x <= size.width, sp.y >= 0, sp.y <= size.height else { continue }
            out.append(Dot(x: Float(sp.x), y: Float(sp.y), color: cloudColours[i]))
        }
        dots = out
        pointCount = cloud.count
        framesSoFar = recorder?.count ?? 0
    }

    func start() {
        plyURL = nil; photoURLs = []; photosMetaURL = nil; modelScene = nil; status = ""; maskURLs = []; serverOnly = false; phoneResult = nil
        dispose(recorder?.dir)      // an earlier scan that produced nothing
        savedCopy = nil; stopNote = ""
        recorder = PhotoRecorder()
        recorder?.maxPhotos = UserDefaults.standard.object(forKey: "allFrames") as? Bool ?? true ? 400 : 120
        ring.begin()
        sweep.reset()
        if liveWanted && (UserDefaults.standard.object(forKey: "liveGuide") as? Bool ?? true) {
            live.begin(server: ServerAddress.url, kind: sweepMode ? "sweep" : "room",
                       name: (sweepMode ? "Sweep " : "3D room ") + Date().formatted(), project: UserDefaults.standard.string(forKey: "project") ?? "", info: scanInfo)
        }
        if ring.enabled { recorder?.maxSide = 1920 }       // the markers must be sharp enough to read
        ringActive = ring.capturing
        overlayTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in self?.refreshOverlay() }
        lastCount = 0; stalled = 0; pointCount = 0; dots = []
        coverageTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.ring.update(frame: self.sceneView.session.currentFrame, recorder: self.recorder)
            if self.sweepMode, let f = self.sceneView.session.currentFrame { self.sweep.update(f) }
            self.live.pump(self.recorder, frame: self.sceneView.session.currentFrame)
        }
        ticks = 0
        cloud = []; cloudColours = []; occupied = []; cloudGen += 1
        cloudTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.addPoints() }
        lock.lock(); meshAnchors = [:]; lock.unlock()
        let cfg = ARWorldTrackingConfiguration()
        cfg.sceneReconstruction = .mesh
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        Reporter.log("mesh start: supportsMesh=\(Self.supported) supportsDepth=\(ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)) reconstruction=\(cfg.sceneReconstruction.rawValue)")
        sceneView.session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.tick() }
        scanning = true
        Screen.awake(true)   // keep the screen on while scanning
    }

    private func tick() {
        ticks += 1
        guard let frame = sceneView.session.currentFrame else {
            hud = "Waiting for the camera… (\(ticks))"
            return
        }
        var parts: [String] = []
        var dark = false
        if let light = frame.lightEstimate?.ambientIntensity {
            dark = light < 150
            parts.append(dark ? "Too dark for good colours (\(Int(light)))" : light < 400 ? "Dim light (\(Int(light)))" : "Light OK")
        }
        switch frame.camera.trackingState {
        case .normal: break
        case .notAvailable: parts.append("Tracking lost")
        case .limited(.excessiveMotion): parts.append("Moving too fast")
        case .limited(.insufficientFeatures): parts.append("Not enough detail or light")
        case .limited: parts.append("Tracking limited")
        }
        // "Keep every frame": a photo, the LiDAR depth and the pose twice a second, so each frame can be reviewed on the server.
        let every = UserDefaults.standard.object(forKey: "allFrames") as? Bool ?? true
        if !dark && (every || !ring.capturing) { recorder?.consider(frame, force: every) }
        lock.lock()
        for a in frame.anchors { if let m = a as? ARMeshAnchor { meshAnchors[m.identifier] = m } }
        let all = Array(meshAnchors.values)
        lock.unlock()
        if ticks % 4 == 0 {
            Reporter.log("mesh t=\(ticks / 2)s frameAnchors=\(frame.anchors.count) meshPatches=\(all.count) points=\(cloud.count) tracking=\(frame.camera.trackingState) depth=\(frame.sceneDepth != nil) photos=\(recorder?.count ?? 0)")
        }
        let tris = all.reduce(0) { $0 + $1.geometry.faces.count }
        parts.append("\(cloud.count / 1000)k points · \(all.count) patches · \(tris / 1000)k triangles · \(recorder?.count ?? 0) photos")
        stalled = cloud.count == lastCount ? stalled + 1 : 0
        lastCount = cloud.count
        if stalled >= 4 && !ring.capturing { parts.append("Nothing new captured: move slowly, 0.3 to 3 m from a surface") }
        hud = parts.joined(separator: " · ")
    }

    /// Throw everything away and go back to the start, whatever state the scan is in.
    func discard() {
        timer?.invalidate(); timer = nil
        cloudTimer?.invalidate(); cloudTimer = nil
        coverageTimer?.invalidate(); coverageTimer = nil
        overlayTimer?.invalidate(); overlayTimer = nil
        dots = []; pointCount = 0
        sceneView.session.pause()
        Screen.awake(false)
        ring.end()
        ringActive = false
        cloud = []; cloudColours = []; occupied = []; cloudGen += 1
        lock.lock(); meshAnchors = [:]; lock.unlock()
        dispose(recorder?.dir)
        TempFiles.remove(phoneResult?.stl)
        recorder = nil; savedCopy = nil
        plyURL = nil; photoURLs = []; photosMetaURL = nil; modelScene = nil; serverOnly = false; phoneResult = nil
        scanning = false
        hud = ""; status = ""; stopNote = ""
        Reporter.log("mesh: reset by user")
        live.abandon()
        startPreview()      // back to the live camera at the start screen, not a paused one
    }

    /// `leftScreen`: the tab was switched (or Settings opened) mid-scan. Stop exactly as the button does, so the result is kept,
    /// and say why: the timers and the AR session must not run behind another tab that uses the camera.
    func stop(leftScreen: Bool = false) {
        if leftScreen {
            stopNote = "Stopped because you left the scan screen. What was captured is kept: Upload or Save it."
            Reporter.log("mesh: stopped, the scan screen was left")
        }
        timer?.invalidate(); timer = nil
        cloudTimer?.invalidate(); cloudTimer = nil
        coverageTimer?.invalidate(); coverageTimer = nil
        overlayTimer?.invalidate(); overlayTimer = nil
        cloudGen += 1      // a frame still being read is not added after Stop: the cloud is written to the PLY below
        dots = []
        ring.end()
        ringActive = false
        lock.lock(); let anchors = Array(meshAnchors.values); lock.unlock()
        sceneView.session.pause()
        scanning = false
        Screen.awake(false)
        serverOnly = ring.enabled
        guard !anchors.isEmpty || !cloud.isEmpty || (ring.enabled && (recorder?.count ?? 0) > 0) else {
            let why = sceneView.session.currentFrame == nil ? "The camera never started." : "The camera ran but the LiDAR produced no mesh."
            status = "No surfaces were captured (\(ticks / 2) s). \(why) A report was sent."
            Reporter.report(tab: "mesh", message: "No surfaces captured after \(ticks / 2) s", detail: why)
            return
        }
        status = "Saving…"
        let rec = recorder
        // In the scan's own temp folder, deleted with it (New scan, Save, the next scan, or the clean-up at launch).
        let plyDir = rec?.dir ?? FileManager.default.temporaryDirectory
        DispatchQueue.global().async { [weak self] in
            let url = plyDir.appendingPathComponent("scan.ply")
            // ARKit's mesh when it produced one, otherwise the depth point cloud (the server meshes it).
            let ok = anchors.isEmpty ? self?.writeCloudPLY(to: url) ?? false : Self.writePLY(anchors, to: url)
            let scene = anchors.isEmpty ? self?.cloudScene() ?? SCNScene() : Self.modelScene(anchors)
            DispatchQueue.main.async {
                guard let self else { return }
                self.plyURL = ok ? url : nil
                self.photoURLs = rec?.urls ?? []
                self.photosMetaURL = rec?.writeManifest()
                self.modelScene = scene
                self.status = ok ? "Scan ready" : "Could not save the mesh"
                if ok && self.ring.enabled { self.outlineParts() }
            }
        }
    }

    /// The phone's settings for this scan, stored with the model on the server and shown next to it.
    private var scanInfo: [String: Any] {
        var info: [String: Any] = ["scan type": sweepMode ? "Sweep from one spot" : (ring.enabled ? "LiDAR + ring" : "Free scan"), "app": AppVersion.text,
                "device": Reporter.deviceModel(), "ios": UIDevice.current.systemVersion,
                "keep every frame": UserDefaults.standard.object(forKey: "allFrames") as? Bool ?? true,
                "photo longest side px": ring.enabled ? 1920 : 1280, "frames sent": photoURLs.filter { $0.pathExtension == "jpg" }.count,
                "lidar depth": photoURLs.contains { $0.pathExtension == "depth" },
                "part outlines": maskURLs.count]
        if sweepMode { info["sweep"] = true; info["sweep max movement m"] = (sweep.maxMoved * 100).rounded() / 100 }
        return info
    }

    /// Ring scans: the part's outline in every photo (Vision, on the phone). The server carves the shape from these, which is far
    /// sharper than the LiDAR depth for small parts.
    private func outlineParts() {
        let photos = photoURLs.filter { $0.pathExtension == "jpg" }
        maskURLs = []
        guard !photos.isEmpty else { return }
        outlining = true
        Screen.awake(true)      // the phone must not lock while it works through the photos
        status = "Outlining the part in \(photos.count) photos…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let ctx = CIContext()
            var out: [URL] = []
            for (i, url) in photos.enumerated() {
                autoreleasepool {
                    if let r = SubjectMasker.outlineAndLabelsPNG(url, ctx: ctx) {
                        let dest = url.deletingPathExtension().appendingPathExtension("png")
                        if (try? r.outline.write(to: dest)) != nil { out.append(dest) }
                        if let labels = r.labels {
                            let ldest = url.deletingLastPathComponent().appendingPathComponent(url.deletingPathExtension().lastPathComponent + "-labels.png")
                            if (try? labels.write(to: ldest)) != nil { out.append(ldest) }
                        }
                    }
                }
                if i % 10 == 0 { DispatchQueue.main.async { self?.status = "Outlining the part: \(i + 1) of \(photos.count) photos…" } }
            }
            DispatchQueue.main.async {
                self?.maskURLs = out
                self?.outlining = false
                Screen.awake(false)
                self?.status = "Scan ready (\(out.filter { !$0.lastPathComponent.contains("-labels") }.count) outlines)"
                Reporter.log("ring: \(out.count) outlines of \(photos.count) photos")
            }
        }
    }

    /// Ring scans: make the model here on the phone (no server needed), from the ring markers and the part outlines.
    func buildOnPhone() {
        guard let rec = recorder, canBuildOnPhone else { return }
        buildingOnPhone = true
        Screen.awake(true)
        status = "Building on the phone…"
        let photos = photoURLs.filter { $0.pathExtension == "jpg" }, metas = rec.metas, masks = maskURLs
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let r = OnPhoneRing.build(photos: photos, metas: metas, masks: masks, ring: .current()) { text in
                DispatchQueue.main.async { self?.status = text }
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.buildingOnPhone = false
                Screen.awake(false)
                if let r {
                    self.phoneResult = r
                    TempFiles.hold(r.stl)      // the STL button shares it
                    self.modelScene = r.scene
                    self.serverOnly = false
                    self.status = String(format: "Built on the phone from %d photos: %.1f × %.1f × %.1f mm", r.views, r.sizeMM.x, r.sizeMM.y, r.sizeMM.z)
                    Reporter.log("phone build: \(r.views) views, \(r.triangles) triangles, size \(r.sizeMM)")
                } else {
                    self.status = "Could not build on the phone (\(OnPhoneRing.lastSummary)). Upload it instead."
                    Reporter.report(tab: "ring", message: "Phone build failed", detail: "photos \(photos.count), outlines \(masks.count); \(OnPhoneRing.lastSummary)")
                }
            }
        }
    }

    private var package: ScanPackage? {
        guard let plyURL else { return nil }
        return ScanPackage(name: (ring.enabled ? "Part " : "3D room ") + Date().formatted(), kind: "mesh", mesh: plyURL,
                           photosMeta: photosMetaURL, photos: photoURLs, masks: maskURLs, ringScan: ring.enabled, info: scanInfo)
    }

    func saveOnPhone() {
        if outlining { status = "Still outlining the part in the photos, save again in a moment"; return }
        if savedCopy != nil { status = "Already saved on the phone (Models tab, On this phone)"; return }
        guard let package else { return }
        guard keepOnPhone(package) else { status = "Could not save on the phone"; return }
        status = "Saved on the phone (Models tab, On this phone)"
        // A live-guided scan also has a draft on the server, which would be built there after 10 minutes and then uploaded again
        // from the saved copy: delete the draft. Not once the server has started building it (or has finished: scanID is nil then).
        if live.scanID != nil && !live.finishing {
            live.abandon()
            status += ". The live draft on the server was removed; Upload sends this copy."
        }
    }

    /// Copy into the phone's storage, then work from that copy (Upload, Build here) and delete the temp folder.
    private func keepOnPhone(_ p: ScanPackage) -> Bool {
        guard let s = try? LocalStore.save(p) else { return false }
        savedCopy = s
        let q = LocalStore.package(s)
        plyURL = q.mesh; photoURLs = q.photos; photosMetaURL = q.photosMeta; maskURLs = q.masks
        dispose(recorder?.dir)
        return true
    }

    /// Delete a temp folder, but only once no upload is reading from it.
    private func dispose(_ url: URL?) {
        if let url { toDelete.append(url) }
        guard uploads == 0 else { return }
        toDelete.forEach { TempFiles.remove($0) }
        toDelete = []
    }

    @MainActor func upload(server: String) async {
        uploads += 1
        defer { uploads -= 1; dispose(nil) }
        // Scanned with the live link: the frames are (nearly) all on the server already, so only the build is left. If that cannot be
        // done (the connection never came back), fall through to the ordinary upload from the phone, which always works.
        if live.scanID != nil, let rec = recorder {
            status = "Finishing on the server…"
            do {
                let id = try await live.complete(recorder: rec, info: scanInfo)
                if var s = savedCopy {       // saved while the server was building: that copy is on the server now
                    s.uploadedID = id; s.uploadedAt = Date()
                    try? LocalStore.write(s)
                    savedCopy = s
                    NotificationCenter.default.post(name: .savedScansChanged, object: nil)
                }
                status = "Uploaded: built on the server from the live frames"
                Notifier.scanDone(name: sweepMode ? "Sweep" : "3D room", status: "ok")
                live.end()
                return
            } catch {
                Reporter.log("live finish failed (\(error)), uploading from the phone instead")
                live.abandon()
            }
        }
        if let s = savedCopy {
            status = "Uploading…"
            do {
                _ = try await LocalStore.upload(s, server: server)
                status = "Uploaded"
            } catch {
                status = "Upload failed: \(error.localizedDescription). It is saved on the phone: upload it later from Models, On this phone."
                Reporter.report(tab: "mesh", message: "Upload failed", detail: "\(error)")
            }
            return
        }
        guard let package else { return }
        if outlining { status = "Still outlining the part in the photos, upload again in a moment"; return }
        status = "Uploading…"
        do {
            _ = try await Uploader.upload(package, server: server)
            status = "Uploaded"
        } catch {
            let saved = keepOnPhone(package)
            status = "Upload failed: \(error.localizedDescription)." + (saved ? " Saved on the phone: upload it later from Models, On this phone." : "")
            Reporter.report(tab: "mesh", message: "Upload failed", detail: "\(error)")
        }
    }

    // MARK: depth point cloud
    /// Every 0.25 s: the heavy part (rendering the camera image small, unprojecting the depth map) runs on `cloudQueue`; the new
    /// points are merged here on the main thread, which owns `cloud`, `cloudColours` and `occupied`. A frame is skipped while the
    /// last one is still being read, and a result that arrives after Stop, Reset or a new start is dropped (`cloudGen`).
    private func addPoints() {
        guard !cloudBusy, let frame = sceneView.session.currentFrame, case .normal = frame.camera.trackingState,
              let depth = frame.sceneDepth, let conf = depth.confidenceMap, cloud.count < maxPoints else { return }
        // A turning part would smear in a cloud built in room coordinates: show only the current view, in finer dots.
        let onlyThisView = ring.capturing
        let vox: Float = onlyThisView ? 0.004 : voxel
        let image = frame.capturedImage, dm = depth.depthMap
        let k = frame.camera.intrinsics, res = frame.camera.imageResolution, t = frame.camera.transform
        let gen = cloudGen, ctx = ciContext
        cloudBusy = true
        cloudQueue.async { [weak self] in
            let found = MeshScanModel.depthPoints(image: image, depth: dm, conf: conf, intrinsics: k, resolution: res, transform: t, voxel: vox, ctx: ctx)
            DispatchQueue.main.async {
                guard let self else { return }
                self.cloudBusy = false
                guard self.scanning, gen == self.cloudGen else { return }
                if onlyThisView { self.cloud = []; self.cloudColours = []; self.occupied = [] }
                for f in found where self.cloud.count < self.maxPoints {
                    guard self.occupied.insert(f.key).inserted else { continue }
                    self.cloud.append(f.point)
                    self.cloudColours.append(f.colour)
                }
            }
        }
    }

    /// The depth map's points in world space with their camera colours, one per voxel key (merged into the cloud on the main thread).
    private static func depthPoints(image: CVPixelBuffer, depth dm: CVPixelBuffer, conf: CVPixelBuffer, intrinsics k: simd_float3x3,
                                    resolution: CGSize, transform t: simd_float4x4, voxel vox: Float, ctx: CIContext)
        -> [(key: Int64, point: SIMD3<Float>, colour: SIMD3<Float>)] {
        let dw = CVPixelBufferGetWidth(dm), dh = CVPixelBufferGetHeight(dm)
        // camera colours at the depth map's size
        var rgba = [UInt8](repeating: 0, count: dw * dh * 4)
        let img = CIImage(cvPixelBuffer: image)
        let small = img.transformed(by: CGAffineTransform(scaleX: CGFloat(dw) / img.extent.width, y: CGFloat(dh) / img.extent.height))
        ctx.render(small, toBitmap: &rgba, rowBytes: dw * 4, bounds: CGRect(x: 0, y: 0, width: dw, height: dh),
                   format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB))
        // intrinsics are for the full image; scale to the depth map
        let sx = Float(dw) / Float(resolution.width), sy = Float(dh) / Float(resolution.height)
        let fx = k[0][0] * sx, fy = k[1][1] * sy, cx = k[2][0] * sx, cy = k[2][1] * sy
        CVPixelBufferLockBaseAddress(dm, .readOnly); CVPixelBufferLockBaseAddress(conf, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(dm, .readOnly); CVPixelBufferUnlockBaseAddress(conf, .readOnly) }
        guard let db = CVPixelBufferGetBaseAddress(dm), let cb = CVPixelBufferGetBaseAddress(conf) else { return [] }
        let dbpr = CVPixelBufferGetBytesPerRow(dm), cbpr = CVPixelBufferGetBytesPerRow(conf)
        var seen = Set<Int64>()
        var out: [(key: Int64, point: SIMD3<Float>, colour: SIMD3<Float>)] = []
        for y in stride(from: 0, to: dh, by: 2) {
            let drow = (db + y * dbpr).assumingMemoryBound(to: Float32.self)
            let crow = (cb + y * cbpr).assumingMemoryBound(to: UInt8.self)
            for x in stride(from: 0, to: dw, by: 2) {
                let d = drow[x]
                guard d > 0.15, d < 5, crow[x] >= 1 else { continue }
                // ARKit camera: +X right, +Y up, looks down -Z; image rows go down
                let w = t * SIMD4<Float>((Float(x) - cx) * d / fx, -(Float(y) - cy) * d / fy, -d, 1)
                let key = voxelKey(w, vox)
                guard seen.insert(key).inserted else { continue }
                let o = (y * dw + x) * 4
                out.append((key: key, point: SIMD3(w.x, w.y, w.z), colour: SIMD3(Float(rgba[o]), Float(rgba[o + 1]), Float(rgba[o + 2])) / 255))
            }
        }
        return out
    }

    private static func voxelKey(_ p: SIMD4<Float>, _ size: Float) -> Int64 {
        let ix = Int64((p.x / size).rounded(.down)) & 0x1FFFFF
        let iy = Int64((p.y / size).rounded(.down)) & 0x1FFFFF
        let iz = Int64((p.z / size).rounded(.down)) & 0x1FFFFF
        return (ix << 42) | (iy << 21) | iz
    }

    private func cloudGeometry() -> SCNGeometry? {
        let n = cloud.count
        guard n > 0 else { return nil }
        let stride = MemoryLayout<SIMD3<Float>>.stride
        let pos = cloud.withUnsafeBufferPointer { Data(buffer: $0) }
        let col = cloudColours.withUnsafeBufferPointer { Data(buffer: $0) }
        let vs = SCNGeometrySource(data: pos, semantic: .vertex, vectorCount: n, usesFloatComponents: true,
                                   componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0, dataStride: stride)
        let cs = SCNGeometrySource(data: col, semantic: .color, vectorCount: n, usesFloatComponents: true,
                                   componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0, dataStride: stride)
        let indices = (0..<Int32(n)).map { $0 }
        let el = SCNGeometryElement(data: indices.withUnsafeBufferPointer { Data(buffer: $0) }, primitiveType: .point,
                                    primitiveCount: n, bytesPerIndex: MemoryLayout<Int32>.size)
        el.pointSize = 0.01                                  // world size in metres, so dots shrink with distance
        el.minimumPointScreenSpaceRadius = 2
        el.maximumPointScreenSpaceRadius = 6
        let g = SCNGeometry(sources: [vs, cs], elements: [el])
        let m = SCNMaterial()
        m.lightingModel = .constant
        g.firstMaterial = m
        return g
    }

    private func cloudScene() -> SCNScene {
        let scene = SCNScene()
        scene.background.contents = UIColor(white: 0.1, alpha: 1)
        scene.rootNode.addChildNode(SCNNode(geometry: cloudGeometry()))
        return scene
    }

    /// Binary little-endian PLY of points with colours, ARKit world frame (metres, Y up).
    private func writeCloudPLY(to url: URL) -> Bool {
        var data = Data("ply\nformat binary_little_endian 1.0\nelement vertex \(cloud.count)\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n".utf8)
        var body = [UInt8]()
        body.reserveCapacity(cloud.count * 15)
        for (p, c) in zip(cloud, cloudColours) {
            for f in [p.x, p.y, p.z] { withUnsafeBytes(of: f.bitPattern.littleEndian) { body.append(contentsOf: $0) } }
            body.append(contentsOf: [UInt8(max(0, min(255, c.x * 255))), UInt8(max(0, min(255, c.y * 255))), UInt8(max(0, min(255, c.z * 255)))])
        }
        data.append(contentsOf: body)
        return (try? data.write(to: url)) != nil
    }

    // MARK: session problems are shown instead of failing silently
    func session(_ session: ARSession, didFailWithError error: Error) {
        DispatchQueue.main.async { self.status = "AR error: \(error.localizedDescription)" }
        Reporter.report(tab: "mesh", message: "AR session error", detail: "\(error)")
    }
    func sessionWasInterrupted(_ session: ARSession) {
        DispatchQueue.main.async { self.status = "Camera interrupted" }
    }
    func sessionInterruptionEnded(_ session: ARSession) {
        DispatchQueue.main.async { self.status = "" }
    }

    // MARK: live overlay
    func renderer(_ renderer: SCNSceneRenderer, nodeFor anchor: ARAnchor) -> SCNNode? {
        guard let a = anchor as? ARMeshAnchor else { return nil }
        lock.lock(); meshAnchors[a.identifier] = a; lock.unlock()
        let node = SCNNode()
        Self.fill(node, a)
        return node
    }

    func renderer(_ renderer: SCNSceneRenderer, didUpdate node: SCNNode, for anchor: ARAnchor) {
        guard let a = anchor as? ARMeshAnchor else { return }
        lock.lock(); meshAnchors[a.identifier] = a; lock.unlock()
        Self.fill(node, a)
    }

    /// Wireframe plus a faint fill: the lines show what the LiDAR has covered.
    private static func fill(_ node: SCNNode, _ a: ARMeshAnchor) {
        node.childNodes.forEach { $0.removeFromParentNode() }
        let wire = SCNNode(geometry: geometry(a.geometry))
        wire.geometry?.firstMaterial = material(.cyan, 1, lines: true)
        let face = SCNNode(geometry: geometry(a.geometry))
        face.geometry?.firstMaterial = material(.cyan, 0.15, lines: false)
        node.addChildNode(wire)
        node.addChildNode(face)
    }

    private static func material(_ color: UIColor, _ alpha: CGFloat, lines: Bool) -> SCNMaterial {
        let m = SCNMaterial()
        m.diffuse.contents = color.withAlphaComponent(alpha)
        m.lightingModel = .constant
        m.isDoubleSided = true
        if lines { m.fillMode = .lines }
        if alpha < 1 { m.writesToDepthBuffer = false }
        return m
    }

    private static func geometry(_ g: ARMeshGeometry) -> SCNGeometry {
        let v = g.vertices, n = g.normals, f = g.faces
        let vs = SCNGeometrySource(buffer: v.buffer, vertexFormat: v.format, semantic: .vertex, vertexCount: v.count, dataOffset: v.offset, dataStride: v.stride)
        let ns = SCNGeometrySource(buffer: n.buffer, vertexFormat: n.format, semantic: .normal, vertexCount: n.count, dataOffset: n.offset, dataStride: n.stride)
        let data = Data(bytesNoCopy: f.buffer.contents(), count: f.buffer.length, deallocator: .none)
        let el = SCNGeometryElement(data: data, primitiveType: .triangles, primitiveCount: f.count, bytesPerIndex: f.bytesPerIndex)
        return SCNGeometry(sources: [vs, ns], elements: [el])
    }

    /// The finished mesh as a shaded model, for turning around on the phone.
    private static func modelScene(_ anchors: [ARMeshAnchor]) -> SCNScene {
        let scene = SCNScene()
        scene.background.contents = UIColor(white: 0.1, alpha: 1)
        for a in anchors {
            let node = SCNNode(geometry: geometry(a.geometry))
            let m = SCNMaterial()
            m.diffuse.contents = UIColor(white: 0.8, alpha: 1)
            m.isDoubleSided = true
            node.geometry?.firstMaterial = m
            node.simdTransform = a.transform
            scene.rootNode.addChildNode(node)
        }
        return scene
    }

    /// Binary little-endian PLY in the ARKit world frame (metres, Y up) with normals.
    private static func writePLY(_ anchors: [ARMeshAnchor], to url: URL) -> Bool {
        let vcount = anchors.reduce(0) { $0 + $1.geometry.vertices.count }
        let fcount = anchors.reduce(0) { $0 + $1.geometry.faces.count }
        var data = Data()
        data.append(("ply\nformat binary_little_endian 1.0\nelement vertex \(vcount)\n"
            + "property float x\nproperty float y\nproperty float z\nproperty float nx\nproperty float ny\nproperty float nz\n"
            + "element face \(fcount)\nproperty list uchar int vertex_indices\nend_header\n").data(using: .ascii)!)
        var floats = [Float]()
        floats.reserveCapacity(vcount * 6)
        for a in anchors {
            let g = a.geometry, t = a.transform
            let vb = g.vertices.buffer.contents(), nb = g.normals.buffer.contents()
            for i in 0..<g.vertices.count {
                let vp = (vb + g.vertices.offset + g.vertices.stride * i).assumingMemoryBound(to: Float.self)
                let np = (nb + g.normals.offset + g.normals.stride * i).assumingMemoryBound(to: Float.self)
                let w = t * SIMD4<Float>(vp[0], vp[1], vp[2], 1)
                let n = t * SIMD4<Float>(np[0], np[1], np[2], 0)
                floats.append(contentsOf: [w.x, w.y, w.z, n.x, n.y, n.z])
            }
        }
        floats.withUnsafeBufferPointer { data.append(Data(buffer: $0)) }
        var faces = [UInt8]()
        faces.reserveCapacity(fcount * 13)
        var base: Int32 = 0
        for a in anchors {
            let f = a.geometry.faces, fb = f.buffer.contents()
            for i in 0..<f.count {
                faces.append(3)
                for k in 0..<3 {
                    let idx = fb.advanced(by: f.bytesPerIndex * (i * 3 + k)).assumingMemoryBound(to: UInt32.self).pointee
                    withUnsafeBytes(of: (Int32(bitPattern: idx) + base).littleEndian) { faces.append(contentsOf: $0) }
                }
            }
            base += Int32(a.geometry.vertices.count)
        }
        data.append(contentsOf: faces)
        return (try? data.write(to: url)) != nil
    }
}

struct ARSceneRepresentable: UIViewRepresentable {
    let view: ARSCNView
    func makeUIView(context: Context) -> ARSCNView { view }
    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}

/// Orbit with one finger, pinch to zoom, two fingers to pan, double-tap to reset.
struct OrbitSceneView: UIViewRepresentable {
    let scene: SCNScene

    func makeUIView(context: Context) -> SCNView {
        let v = SCNView()
        v.scene = scene
        v.allowsCameraControl = true
        v.autoenablesDefaultLighting = true
        v.defaultCameraController.interactionMode = .orbitTurntable
        v.antialiasingMode = .multisampling4X
        v.backgroundColor = .black
        let cam = SCNNode()
        cam.camera = SCNCamera()
        cam.camera?.zNear = 0.05
        cam.camera?.zFar = 500
        scene.rootNode.addChildNode(cam)
        v.pointOfView = cam
        DispatchQueue.main.async {
            let (c, r) = scene.rootNode.boundingSphere
            cam.position = SCNVector3(c.x, c.y + r * 0.8, c.z + r * 2.2)
            cam.look(at: c)
            v.defaultCameraController.frameNodes(scene.rootNode.childNodes.filter { $0.geometry != nil })
        }
        return v
    }

    func updateUIView(_ uiView: SCNView, context: Context) {}
}

struct MeshScanView: View {
    var ringScan = false        // Object tab: LiDAR with the printed marker ring; otherwise a room
    var sweepMode = false       // Room tab, "From one spot": turn on the spot, no walking
    var liveGuide = false       // Room tab: use the two-way link to the server when it can be reached
    @StateObject private var model = MeshScanModel()
    @AppStorage("serverURL") private var serverURL = ServerAddress.defaultURL
    @State private var zoom: CGFloat = 1

    private var guide: String {
        if !MeshScanModel.supported { return "Mesh scanning needs an iPhone with LiDAR." }
        if sweepMode && !model.scanning && model.plyURL == nil {
            return "For a small or cluttered room: stand in the middle, tap Start scan and turn slowly on the spot until the outer ring fills (the walls), then tilt up for the ceiling and down for the floor. You get a 360° picture of the room and a model with its measurements. Keep your feet in one place."
        }
        if model.scanning {
            return "The LiDAR is measuring the surfaces in front of the camera. Each coloured dot is a point it measured. Move slowly: dots appear on everything you have seen, and gaps with no dots are not captured yet. Tap Stop when it covers what you need, or Reset to start over."
        }
        if model.plyURL != nil && model.phoneResult != nil {
            return "Built on the phone. Turn it with one finger, pinch to zoom; STL shares the file (for example to Orca). Upload gives the server's version (more careful, with the full web page). New scan throws this one away."
        }
        if model.plyURL != nil && model.serverOnly {
            return "Scan done. Build here makes the model on the phone now (about a minute, no server needed). Upload sends it to the server, which builds it more carefully: see the Models tab. Save keeps it on the phone for later. New scan throws it away."
        }
        if model.plyURL != nil {
            return "Scan done. This is what the phone captured: turn it with one finger, pinch to zoom. Tap Upload for the finished model (solid surface, colours, STL) in the Models tab. New scan throws it away."
        }
        return "What this does: it measures a room or an object with the LiDAR and shows the result as coloured dots over the camera picture while you scan, so you can see what is captured. Tap Start scan, then walk slowly around what you want, 0.5 to 3 m away."
    }

    var body: some View {
        ZStack {
            if let scene = model.modelScene, !model.scanning, !model.serverOnly {
                OrbitSceneView(scene: scene).ignoresSafeArea()
            } else if model.plyURL != nil && model.serverOnly {
                Color(white: 0.1).ignoresSafeArea()
            } else {
                ARSceneRepresentable(view: model.sceneView).ignoresSafeArea().scaleEffect(zoom)
                    .opacity(!model.previewOn && !model.scanning && model.plyURL == nil ? 0 : 1)
            }
            if model.scanning {
                Canvas { ctx, _ in
                    for d in model.dots {
                        ctx.fill(Path(ellipseIn: CGRect(x: CGFloat(d.x) - 2.5, y: CGFloat(d.y) - 2.5, width: 5, height: 5)),
                                 with: .color(Color(red: Double(d.color.x), green: Double(d.color.y), blue: Double(d.color.z))))
                    }
                }
                .ignoresSafeArea().allowsHitTesting(false).scaleEffect(zoom)
            }
            if liveGuide && model.scanning {
                GeometryReader { geo in LiveNeedLayer(live: model.live, view: model.sceneView, size: geo.size, zoom: zoom) }
                    .ignoresSafeArea().allowsHitTesting(false)
            }
            VStack(spacing: 8) {
                HStack(spacing: 8) {
                    if model.modelScene == nil || model.scanning { ZoomControl(zoom: $zoom) }
                    if !model.scanning && model.plyURL == nil {
                        Button { model.setPreview(!model.previewOn) } label: {
                            Image(systemName: model.previewOn ? "video.fill" : "video.slash.fill").padding(8)
                                .background(.thinMaterial, in: Capsule())
                        }
                        .accessibilityLabel(model.previewOn ? "Turn the camera off" : "Turn the camera on")
                    }
                }
                if !model.ringActive && !(sweepMode && model.scanning) {
                    HelpCard(text: guide)
                }
                if model.scanning && !model.ringActive && !sweepMode {
                    VStack(spacing: 2) {
                        Text("\(model.pointCount) points · \(model.framesSoFar) photos").font(.title3.weight(.bold))
                        if model.framesSoFar < 120 { Text("Keep going: a room needs about 120 photos (1 to 2 minutes)").font(.caption) }
                    }
                    .padding(8).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                }
                Spacer()
                if sweepMode && model.scanning { SweepPanel(sweep: model.sweep, frames: model.framesSoFar) }
                if liveGuide && model.scanning { LivePanel(live: model.live) }
                if model.ringActive && model.scanning { RingPanel(tracker: model.ring) }
                if model.scanning, !model.hud.isEmpty { Text(model.hud).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                if !model.stopNote.isEmpty, !model.scanning { Text(model.stopNote).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
                if liveGuide { LiveNote(live: model.live) }
                HStack {
                    if model.scanning {
                        Button("Stop") { model.stop() }
                        Button("Reset", role: .destructive) { model.discard() }
                    } else if MeshScanModel.supported && model.plyURL == nil {
                        Button("Start scan") { model.start() }
                    }
                    if model.plyURL != nil, !model.scanning {
                        if model.canBuildOnPhone { Button("Build here") { model.buildOnPhone() } }
                        if let stl = model.phoneResult?.stl { ShareLink(item: stl) { Text("STL") } }
                        Button("Upload") { Task { await model.upload(server: serverURL) } }
                        Button("Save") { model.saveOnPhone() }
                        Button("New scan", role: .destructive) { model.discard() }
                    }
                }
                .buttonStyle(CaptureButtonStyle())
                Button("Report a problem") { Reporter.report(tab: "mesh", message: "Manual report", detail: model.status) }
                    .font(.caption)
            }
            .padding()
        }
        .pinchZoom($zoom, enabled: model.modelScene == nil || model.scanning)
        .onAppear { model.sweepMode = sweepMode; model.liveWanted = liveGuide; model.ring.enabled = ringScan; model.startPreview() }
        .onDisappear { if model.scanning { model.stop(leftScreen: true) } else { model.pausePreview() } }
    }
}
