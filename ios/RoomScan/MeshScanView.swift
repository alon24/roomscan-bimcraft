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
    @Published var modelScene: SCNScene?
    private var recorder: PhotoRecorder?
    private var timer: Timer?

    static var supported: Bool { ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) }

    override init() {
        super.init()
        sceneView.delegate = self
        sceneView.automaticallyUpdatesLighting = true
    }

    func start() {
        plyURL = nil; photoURLs = []; photosMetaURL = nil; modelScene = nil; status = ""
        recorder = PhotoRecorder()
        let cfg = ARWorldTrackingConfiguration()
        cfg.sceneReconstruction = .mesh
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
        sceneView.session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.tick() }
        scanning = true
    }

    private func tick() {
        guard let frame = sceneView.session.currentFrame else { return }
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
        if !dark { recorder?.consider(frame) }
        let tris = frame.anchors.compactMap { $0 as? ARMeshAnchor }.reduce(0) { $0 + $1.geometry.faces.count }
        parts.append("\(tris / 1000)k triangles · \(recorder?.count ?? 0) photos")
        hud = parts.joined(separator: " · ")
    }

    func stop() {
        timer?.invalidate(); timer = nil
        let anchors = (sceneView.session.currentFrame?.anchors ?? []).compactMap { $0 as? ARMeshAnchor }
        sceneView.session.pause()
        scanning = false
        guard !anchors.isEmpty else { status = "No surfaces were captured"; return }
        status = "Saving…"
        let rec = recorder
        DispatchQueue.global().async { [weak self] in
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("scan.ply")
            let ok = Self.writePLY(anchors, to: url)
            let scene = Self.modelScene(anchors)
            DispatchQueue.main.async {
                guard let self else { return }
                self.plyURL = ok ? url : nil
                self.photoURLs = rec?.urls ?? []
                self.photosMetaURL = rec?.writeManifest()
                self.modelScene = scene
                self.status = ok ? "Scan ready" : "Could not save the mesh"
            }
        }
    }

    @MainActor func upload(server: String) async {
        guard let plyURL else { return }
        status = "Uploading…"
        do {
            let id = try await Uploader.upload(json: nil, usdz: nil, name: "Mesh \(Date().formatted())", server: server, kind: "mesh",
                                               photos: photoURLs, photosMeta: photosMetaURL, mesh: plyURL)
            if let id { UserDefaults.standard.set(id, forKey: "lastScanID") }
            status = "Uploaded"
        } catch {
            status = "Upload failed: \(error.localizedDescription) — use Share"
        }
    }

    // MARK: live overlay
    func renderer(_ renderer: SCNSceneRenderer, nodeFor anchor: ARAnchor) -> SCNNode? {
        guard let a = anchor as? ARMeshAnchor else { return nil }
        let node = SCNNode()
        Self.fill(node, a)
        return node
    }

    func renderer(_ renderer: SCNSceneRenderer, didUpdate node: SCNNode, for anchor: ARAnchor) {
        guard let a = anchor as? ARMeshAnchor else { return }
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
    @StateObject private var model = MeshScanModel()
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @State private var mode = 0   // 0 AR overlay, 1 3D model

    private var guide: String {
        if !MeshScanModel.supported { return "Mesh scanning needs an iPhone with LiDAR." }
        if model.scanning {
            return "Move slowly and point at everything you want in the model. The blue grid appears on every surface the LiDAR has seen. Gaps in the grid are not captured yet. Tap Stop when it covers what you need."
        }
        if model.plyURL != nil {
            return "Scan done. Switch to 3D model to turn it around. Upload sends it to the server, which colours it from the photos."
        }
        return "1. Tap Start scan.  2. Walk slowly around the object or room, keeping surfaces 0.5 to 3 m away.  3. Watch the blue grid fill in.  4. Tap Stop, then Upload."
    }

    var body: some View {
        ZStack {
            if mode == 1, let scene = model.modelScene {
                OrbitSceneView(scene: scene).ignoresSafeArea()
            } else {
                ARSceneRepresentable(view: model.sceneView).ignoresSafeArea()
            }
            VStack(spacing: 8) {
                if model.modelScene != nil {
                    Picker("View", selection: $mode) {
                        Text("AR view").tag(0)
                        Text("3D model").tag(1)
                    }
                    .pickerStyle(.segmented)
                }
                Text(guide).font(.caption).padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
                Spacer()
                if model.scanning, !model.hud.isEmpty { Text(model.hud).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
                if !model.status.isEmpty { Text(model.status).padding(6).background(.thinMaterial, in: Capsule()) }
                HStack {
                    if model.scanning {
                        Button("Stop") { model.stop() }
                    } else if MeshScanModel.supported {
                        Button("Start scan") { mode = 0; model.start() }
                    }
                    if let url = model.plyURL, !model.scanning {
                        Button("Upload") { Task { await model.upload(server: serverURL) } }
                        ShareLink(item: url) { Text("Share") }
                    }
                }
                .buttonStyle(.borderedProminent)
            }
            .padding()
        }
    }
}
