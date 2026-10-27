import CoreImage
import CoreMotion
import SwiftUI
import ARKit
import SceneKit
import Metal

/// Free-form scan: ARKit's LiDAR mesh is drawn over the camera while you scan, so you see exactly what has been
/// captured. Photos with poses and depth go along; the server colours the mesh from them.
final class MeshScanModel: NSObject, ObservableObject, ARSCNViewDelegate {
    let sceneView = ARSCNView(frame: .zero)
    @Published var scanning = false
    @Published var status = ""
    @Published var hud = ""
    @Published var plyURL: URL?
    @Published var photoURLs: [URL] = []
    @Published var photosMetaURL: URL?
    private var footprintURL: URL?
    private var maskURLs: [URL] = []
    private var outlining = false
    private var wasTurntable = false
    private var wasRing = false
    @Published var serverOnly = false     // turntable and ring scans: only the server can put the frames together
    private var lastMode = 0
    @Published var modelScene: SCNScene?
    private var recorder: PhotoRecorder?
    private var timer: Timer?
    private var ticks = 0
    struct Dot { var x: Float; var y: Float; var color: SIMD3<Float> }
    @Published var dots: [Dot] = []                // the captured points as the camera sees them now, drawn flat on top of the picture
    @Published var pointCount = 0
    private var overlayTimer: Timer?
    private var lastCount = 0
    private var stalled = 0
    let guided = CoverageTracker()
    @Published var guidedActive = false
    private var coverageTimer: Timer?
    // Depth point cloud, built from the LiDAR depth map of every camera frame. This does not depend on ARKit's mesh
    // anchors, which never arrived on the test phone.
    private var cloud: [SIMD3<Float>] = []
    private var cloudColours: [SIMD3<Float>] = []
    private var occupied = Set<Int64>()
    private var cloudTimer: Timer?
    private var shownPoints = 0
    private let cloudNode = SCNNode()
    private let ciContext = CIContext()
    private let maxPoints = 1_000_000
    private let voxel: Float = 0.015
    private let lock = NSLock()
    private var meshAnchors: [UUID: ARMeshAnchor] = [:]   // kept from the delegate, so Stop does not depend on currentFrame

    static var supported: Bool { ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) }

    override init() {
        super.init()
        sceneView.delegate = self
        sceneView.automaticallyUpdatesLighting = true
        sceneView.scene.rootNode.addChildNode(cloudNode)
        guided.attach(to: sceneView.scene)
    }

    /// Show the camera as soon as the tab opens, so it is clear the app is alive before anything is scanned.
    func startPreview() {
        guard !scanning, plyURL == nil else { return }
        let cfg = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        sceneView.session.run(cfg)
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
    }

    func start() {
        plyURL = nil; photoURLs = []; photosMetaURL = nil; modelScene = nil; status = ""; maskURLs = []; serverOnly = false
        recorder = PhotoRecorder()
        recorder?.maxPhotos = UserDefaults.standard.object(forKey: "allFrames") as? Bool ?? true ? 400 : 120
        guided.begin()
        if guided.ring { recorder?.maxSide = 1920 }       // the markers must be sharp enough to read
        guidedActive = guided.isActive
        overlayTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in self?.refreshOverlay() }
        lastCount = 0; stalled = 0; pointCount = 0; dots = []
        coverageTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.guided.update(frame: self.sceneView.session.currentFrame, recorder: self.recorder)
        }
        ticks = 0
        cloud = []; cloudColours = []; occupied = []; shownPoints = 0; cloudNode.geometry = nil
        cloudTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.addPoints() }
        lock.lock(); meshAnchors = [:]; lock.unlock()
        let cfg = ARWorldTrackingConfiguration()
        cfg.sceneReconstruction = .mesh
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        Reporter.log("mesh start: supportsMesh=\(Self.supported) supportsDepth=\(ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)) reconstruction=\(cfg.sceneReconstruction.rawValue)")
        sceneView.session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.tick() }
        scanning = true
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
        if !dark && (every || !guided.isActive) { recorder?.consider(frame, force: every && (!guided.isActive || guided.phase == .capturing)) }
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
        if stalled >= 4 && !guided.isActive { parts.append("Nothing new captured: move slowly, 0.3 to 3 m from a surface") }
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
        guided.end()
        guidedActive = false
        cloud = []; cloudColours = []; occupied = []; shownPoints = 0
        cloudNode.geometry = nil
        lock.lock(); meshAnchors = [:]; lock.unlock()
        recorder = nil
        plyURL = nil; photoURLs = []; photosMetaURL = nil; footprintURL = nil; modelScene = nil; serverOnly = false
        scanning = false
        hud = ""; status = ""
        Reporter.log("mesh: reset by user")
    }

    func stop() {
        timer?.invalidate(); timer = nil
        cloudTimer?.invalidate(); cloudTimer = nil
        coverageTimer?.invalidate(); coverageTimer = nil
        overlayTimer?.invalidate(); overlayTimer = nil
        dots = []
        guided.end()
        guidedActive = false
        lock.lock(); let anchors = Array(meshAnchors.values); lock.unlock()
        sceneView.session.pause()
        scanning = false
        wasTurntable = guided.turntable
        wasRing = guided.ring
        serverOnly = guided.turntable || guided.ring
        lastMode = guided.mode
        guard !anchors.isEmpty || !cloud.isEmpty || ((guided.turntable || guided.ring) && (recorder?.count ?? 0) > 0) else {
            let why = sceneView.session.currentFrame == nil ? "The camera never started." : "The camera ran but the LiDAR produced no mesh."
            status = "No surfaces were captured (\(ticks / 2) s). \(why) A report was sent."
            Reporter.report(tab: "mesh", message: "No surfaces captured after \(ticks / 2) s", detail: why)
            return
        }
        status = "Saving…"
        let rec = recorder
        DispatchQueue.global().async { [weak self] in
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("scan.ply")
            // ARKit's mesh when it produced one, otherwise the depth point cloud (the server meshes it).
            let ok = anchors.isEmpty ? self?.writeCloudPLY(to: url) ?? false : Self.writePLY(anchors, to: url)
            let scene = anchors.isEmpty ? self?.cloudScene() ?? SCNScene() : Self.modelScene(anchors)
            DispatchQueue.main.async {
                guard let self else { return }
                self.plyURL = ok ? url : nil
                self.photoURLs = rec?.urls ?? []
                self.photosMetaURL = rec?.writeManifest()
                if let fp = self.guided.footprintJSON() {
                    let u = FileManager.default.temporaryDirectory.appendingPathComponent("footprint.json")
                    self.footprintURL = (try? fp.write(to: u)) != nil ? u : nil
                } else { self.footprintURL = nil }
                self.modelScene = scene
                self.status = ok ? "Scan ready" : "Could not save the mesh"
                if ok && self.wasRing { self.outlineParts() }
            }
        }
    }

    /// The phone's settings for this scan, stored with the model on the server and shown next to it.
    private var scanInfo: [String: Any] {
        let names = ["Free scan", "Guided object", "LiDAR turntable (no ring)", "LiDAR + ring"]
        let mode = lastMode
        return ["scan type": names[min(max(mode, 0), 3)], "app": AppVersion.text,
                "device": Reporter.deviceModel(), "ios": UIDevice.current.systemVersion,
                "keep every frame": UserDefaults.standard.object(forKey: "allFrames") as? Bool ?? true,
                "photo longest side px": mode == 3 ? 1920 : 1280, "frames sent": photoURLs.filter { $0.pathExtension == "jpg" }.count,
                "lidar depth": photoURLs.contains { $0.pathExtension == "depth" },
                "traced outline": footprintURL != nil, "part outlines": maskURLs.count]
    }

    /// Ring scans: the part's outline in every photo (Vision, on the phone). The server carves the shape from these, which is far
    /// sharper than the LiDAR depth for small parts.
    private func outlineParts() {
        let photos = photoURLs.filter { $0.pathExtension == "jpg" }
        maskURLs = []
        guard !photos.isEmpty else { return }
        outlining = true
        status = "Outlining the part in \(photos.count) photos…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let ctx = CIContext()
            var out: [URL] = []
            for (i, url) in photos.enumerated() {
                autoreleasepool {
                    if let png = SubjectMasker.outlinePNG(url, ctx: ctx) {
                        let dest = url.deletingPathExtension().appendingPathExtension("png")
                        if (try? png.write(to: dest)) != nil { out.append(dest) }
                    }
                }
                if i % 10 == 0 { DispatchQueue.main.async { self?.status = "Outlining the part: \(i + 1) of \(photos.count) photos…" } }
            }
            DispatchQueue.main.async {
                self?.maskURLs = out
                self?.outlining = false
                self?.status = "Scan ready (\(out.count) outlines)"
                Reporter.log("ring: \(out.count) outlines of \(photos.count) photos")
            }
        }
    }

    @MainActor func upload(server: String) async {
        guard let plyURL else { return }
        if outlining { status = "Still outlining the part in the photos, upload again in a moment"; return }
        status = "Uploading…"
        do {
            let id = try await Uploader.upload(json: nil, usdz: nil, name: "Mesh \(Date().formatted())", server: server, kind: "mesh",
                                               photos: photoURLs, photosMeta: photosMetaURL, mesh: plyURL, footprint: footprintURL, turntableScan: wasTurntable, ringScan: wasRing, info: scanInfo, masks: maskURLs)
            if let id { UserDefaults.standard.set(id, forKey: "lastScanID") }
            status = "Uploaded"
        } catch {
            status = "Upload failed: \(error.localizedDescription) — use Share"
            Reporter.report(tab: "mesh", message: "Upload failed", detail: "\(error)")
        }
    }

    // MARK: depth point cloud
    private func addPoints() {
        // A turning part would smear in a cloud built in room coordinates: show only the current view.
        if (guided.turntable || guided.ring) && guided.phase == .capturing { cloud = []; cloudColours = []; occupied = [] }
        guard let frame = sceneView.session.currentFrame, case .normal = frame.camera.trackingState,
              guided.acceptsPoints, let depth = frame.sceneDepth, let conf = depth.confidenceMap, cloud.count < maxPoints else { return }
        let vox = guided.phase == .capturing ? max(0.004, min(voxel, (guided.hasOutline ? guided.fitRadius : guided.radius) / 50)) : voxel
        let dm = depth.depthMap
        let dw = CVPixelBufferGetWidth(dm), dh = CVPixelBufferGetHeight(dm)
        // camera colours at the depth map's size
        var rgba = [UInt8](repeating: 0, count: dw * dh * 4)
        let img = CIImage(cvPixelBuffer: frame.capturedImage)
        let small = img.transformed(by: CGAffineTransform(scaleX: CGFloat(dw) / img.extent.width, y: CGFloat(dh) / img.extent.height))
        ciContext.render(small, toBitmap: &rgba, rowBytes: dw * 4, bounds: CGRect(x: 0, y: 0, width: dw, height: dh),
                         format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB))
        // intrinsics are for the full image; scale to the depth map
        let k = frame.camera.intrinsics
        let sx = Float(dw) / Float(frame.camera.imageResolution.width), sy = Float(dh) / Float(frame.camera.imageResolution.height)
        let fx = k[0][0] * sx, fy = k[1][1] * sy, cx = k[2][0] * sx, cy = k[2][1] * sy
        let t = frame.camera.transform
        CVPixelBufferLockBaseAddress(dm, .readOnly); CVPixelBufferLockBaseAddress(conf, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(dm, .readOnly); CVPixelBufferUnlockBaseAddress(conf, .readOnly) }
        guard let db = CVPixelBufferGetBaseAddress(dm), let cb = CVPixelBufferGetBaseAddress(conf) else { return }
        let dbpr = CVPixelBufferGetBytesPerRow(dm), cbpr = CVPixelBufferGetBytesPerRow(conf)
        for y in stride(from: 0, to: dh, by: 2) {
            let drow = (db + y * dbpr).assumingMemoryBound(to: Float32.self)
            let crow = (cb + y * cbpr).assumingMemoryBound(to: UInt8.self)
            for x in stride(from: 0, to: dw, by: 2) {
                let d = drow[x]
                guard d > 0.15, d < 5, crow[x] >= 1 else { continue }
                // ARKit camera: +X right, +Y up, looks down -Z; image rows go down
                let w = t * SIMD4<Float>((Float(x) - cx) * d / fx, -(Float(y) - cy) * d / fy, -d, 1)
                guard guided.contains(SIMD3(w.x, w.y, w.z)) else { continue }
                let key = Self.voxelKey(w, vox)
                guard occupied.insert(key).inserted else { continue }
                cloud.append(SIMD3(w.x, w.y, w.z))
                let o = (y * dw + x) * 4
                cloudColours.append(SIMD3(Float(rgba[o]), Float(rgba[o + 1]), Float(rgba[o + 2])) / 255)
            }
        }
    }

    private static func voxelKey(_ p: SIMD4<Float>, _ size: Float) -> Int64 {
        let ix = Int64((p.x / size).rounded(.down)) & 0x1FFFFF
        let iy = Int64((p.y / size).rounded(.down)) & 0x1FFFFF
        let iz = Int64((p.z / size).rounded(.down)) & 0x1FFFFF
        return (ix << 42) | (iy << 21) | iz
    }

    /// Redraw the cloud as coloured dots over the camera picture.
    private func refreshCloud() {
        guard cloud.count != shownPoints else { return }
        shownPoints = cloud.count
        cloudNode.geometry = cloudGeometry()
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
    var initialMode = 0     // 0 free scan, 1 guided object, 2 turntable, 3 ring
    var fixedMode: Int? = nil   // the Room and Object tabs each use one kind of scan, without the picker
    @StateObject private var model = MeshScanModel()
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @State private var mode = 0   // 0 AR overlay, 1 3D model
    @State private var zoom: CGFloat = 1
    @State private var didSetMode = false

    private var guide: String {
        if !MeshScanModel.supported { return "Mesh scanning needs an iPhone with LiDAR." }
        if model.scanning {
            return "The LiDAR is measuring the surfaces in front of the camera. Each coloured dot is a point it measured. Move slowly: dots appear on everything you have seen, and gaps with no dots are not captured yet. Tap Stop when it covers what you need, or Reset to start over."
        }
        if model.plyURL != nil && model.serverOnly {
            return "Scan done. The phone cannot join turntable or ring frames itself: tap Upload, then open the Models tab to see the finished model (it takes a minute). New scan throws this one away."
        }
        if model.plyURL != nil {
            return "Scan done. This is what the phone captured: turn it with one finger, pinch to zoom. Tap Upload for the finished model (solid surface, colours, STL) in the Models tab. New scan throws it away."
        }
        return "What this does: it measures a room or an object with the LiDAR and shows the result as coloured dots over the camera picture while you scan, so you can see what is captured. Tap Start scan, then walk slowly around what you want, 0.5 to 3 m away. For one small part on a table, pick Guided object."
    }

    var body: some View {
        ZStack {
            if let scene = model.modelScene, !model.scanning, !model.serverOnly {
                OrbitSceneView(scene: scene).ignoresSafeArea()
            } else if model.plyURL != nil && model.serverOnly {
                Color(white: 0.1).ignoresSafeArea()
            } else {
                ARSceneRepresentable(view: model.sceneView).ignoresSafeArea().scaleEffect(zoom)
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
            TraceTapLayer(tracker: model.guided, view: model.sceneView, zoom: zoom)
            VStack(spacing: 8) {
                if fixedMode == nil && !model.scanning && model.plyURL == nil { ScanTypePicker(tracker: model.guided) }
                if model.modelScene == nil || model.scanning { ZoomControl(zoom: $zoom) }
                if !model.guidedActive {
                    Text(guide).font(.callout).padding(10).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                }
                if model.scanning && !model.guidedActive {
                    Text("\(model.pointCount) points captured").font(.title3.weight(.bold)).padding(8)
                        .background(.regularMaterial, in: Capsule())
                }
                Spacer()
                if model.guidedActive && model.scanning {
                    GuidedPanel(tracker: model.guided) { model.guided.setObject(model.sceneView) }
                }
                if model.scanning, !model.hud.isEmpty { Text(model.hud).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                HStack {
                    if model.scanning {
                        Button("Stop") { model.stop() }
                        Button("Reset", role: .destructive) { model.discard() }
                    } else if MeshScanModel.supported && model.plyURL == nil {
                        Button("Start scan") { mode = 0; model.start() }
                    }
                    if let url = model.plyURL, !model.scanning {
                        Button("Upload") { Task { await model.upload(server: serverURL) } }
                        ShareLink(item: url) { Text("Share") }
                        Button("New scan", role: .destructive) { mode = 0; model.discard() }
                    }
                }
                .buttonStyle(.borderedProminent)
                Button("Report a problem") { Reporter.report(tab: "mesh", message: "Manual report", detail: model.status) }
                    .font(.caption)
            }
            .padding()
        }
        .pinchZoom($zoom)
        .onAppear { if !model.scanning && model.plyURL == nil && !didSetMode { model.guided.mode = fixedMode ?? initialMode; didSetMode = true }; model.startPreview() }
        .onDisappear { model.pausePreview() }
    }
}

struct ScanTypePicker: View {
    @ObservedObject var tracker: CoverageTracker
    var body: some View {
        VStack(spacing: 4) {
            Picker("Scan type", selection: $tracker.mode) {
                Text("Free scan").tag(0)
                Text("Guided object").tag(1)
                Text("LiDAR turn").tag(2)
                Text("LiDAR+Ring").tag(3)
            }
            .pickerStyle(.segmented)
            if tracker.mode == 1 {
                Text("For a part on a table: you mark where it is, and the app shows which views are still missing.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if tracker.mode == 3 {
                Text("LiDAR AND the printed marker ring together: the LiDAR measures the shape, the ring markers place every frame exactly. The phone and the spool can both move, and the size is real millimetres.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if tracker.mode == 2 {
                Text("LiDAR only, no ring needed. Phone on a stand, part on a turning plate. You trace the plate; the server works out the turning speed and joins the frames. Plain white parts work.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }
}
