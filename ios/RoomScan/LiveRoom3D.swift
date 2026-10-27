import SwiftUI
import SceneKit
import RoomPlan

/// A 3D model of what RoomPlan has captured so far, rebuilt as the scan grows.
/// Walls are solid-edged where the scanner has finished that edge and dashed where it has not.
final class LiveRoomScene: ObservableObject {
    let scene = SCNScene()
    let cameraNode = SCNNode()
    weak var view: SCNView?
    @Published var summary = "Nothing scanned yet"
    private let content = SCNNode()
    private let phone = SCNNode()
    private var fitted = false

    init() {
        scene.background.contents = UIColor(white: 0.08, alpha: 1)
        scene.rootNode.addChildNode(content)
        let cam = SCNCamera()
        cam.zNear = 0.05
        cam.zFar = 200
        cameraNode.camera = cam
        cameraNode.position = SCNVector3(0, 5, 7)
        cameraNode.look(at: SCNVector3(0, 0, 0))
        scene.rootNode.addChildNode(cameraNode)

        let cone = SCNNode(geometry: SCNCone(topRadius: 0, bottomRadius: 0.1, height: 0.25))
        cone.geometry?.firstMaterial = Self.material(.systemYellow)
        cone.eulerAngles.x = -.pi / 2   // apex along the camera's viewing direction (-Z)
        phone.addChildNode(cone)
        phone.isHidden = true
        scene.rootNode.addChildNode(phone)
    }

    func reset() {
        content.childNodes.forEach { $0.removeFromParentNode() }
        phone.isHidden = true
        fitted = false
        summary = "Nothing scanned yet"
    }

    /// Where the phone is, so you can see which part of the model you are pointing at.
    func setPhone(_ transform: simd_float4x4) {
        phone.simdTransform = transform
        phone.isHidden = false
    }

    func update(_ room: CapturedRoom) {
        let root = SCNNode()
        for w in room.walls { root.addChildNode(surface(w, edge: .systemTeal, fill: .systemTeal, progress: true)) }
        for d in room.doors { root.addChildNode(surface(d, edge: .systemOrange, fill: .systemOrange, progress: false)) }
        for w in room.windows { root.addChildNode(surface(w, edge: .systemBlue, fill: .systemBlue, progress: false)) }
        for o in room.openings { root.addChildNode(surface(o, edge: .systemPurple, fill: .systemPurple, progress: false)) }
        for o in room.objects {
            let box = SCNBox(width: CGFloat(o.dimensions.x), height: CGFloat(o.dimensions.y), length: CGFloat(o.dimensions.z), chamferRadius: 0)
            box.firstMaterial = Self.material(.systemGray, alpha: 0.35)
            let n = SCNNode(geometry: box)
            n.simdTransform = o.transform
            root.addChildNode(n)
        }
        content.childNodes.forEach { $0.removeFromParentNode() }
        content.addChildNode(root)
        let longest = room.walls.map { $0.dimensions.x }.max() ?? 0
        let height = room.walls.map { $0.dimensions.y }.max() ?? 0
        summary = String(format: "%d walls · %d doors · %d windows · %d objects · longest %.2f m · height %.2f m",
                         room.walls.count, room.doors.count, room.windows.count, room.objects.count, longest, height)
        if !fitted, !room.walls.isEmpty { fitted = true; recenter() }
    }

    /// Frame the whole model in the view.
    func recenter() {
        guard content.childNodes.first?.childNodes.isEmpty == false else { return }
        if let view {
            view.pointOfView = cameraNode
            view.defaultCameraController.frameNodes([content])
        } else {
            let (c, r) = content.boundingSphere
            cameraNode.position = SCNVector3(c.x, c.y + r * 1.2, c.z + r * 2)
            cameraNode.look(at: c)
        }
    }

    private static func material(_ color: UIColor, alpha: CGFloat = 1) -> SCNMaterial {
        let m = SCNMaterial()
        m.diffuse.contents = color.withAlphaComponent(alpha)
        m.lightingModel = .constant
        m.isDoubleSided = true
        if alpha < 1 { m.writesToDepthBuffer = false }
        return m
    }

    private func surface(_ s: CapturedRoom.Surface, edge: UIColor, fill: UIColor, progress: Bool) -> SCNNode {
        let w = CGFloat(s.dimensions.x), h = CGFloat(s.dimensions.y)
        let hw = Float(w / 2), hh = Float(h / 2)
        let node = SCNNode()
        let plane = SCNPlane(width: w, height: h)
        plane.firstMaterial = Self.material(fill, alpha: 0.12)
        node.addChildNode(SCNNode(geometry: plane))
        let edges: [(CapturedRoom.Surface.Edge, SCNVector3, SCNVector3)] = [
            (.top, SCNVector3(-hw, hh, 0), SCNVector3(hw, hh, 0)),
            (.bottom, SCNVector3(-hw, -hh, 0), SCNVector3(hw, -hh, 0)),
            (.left, SCNVector3(-hw, -hh, 0), SCNVector3(-hw, hh, 0)),
            (.right, SCNVector3(hw, -hh, 0), SCNVector3(hw, hh, 0)),
        ]
        for (e, a, b) in edges {
            let done = !progress || s.completedEdges.contains(e)
            node.addChildNode(line(a, b, dashed: !done, color: done ? edge : .white))
        }
        node.simdTransform = s.transform
        return node.flattenedClone()
    }

    /// An axis-aligned line, solid or made of short dashes.
    private func line(_ a: SCNVector3, _ b: SCNVector3, dashed: Bool, color: UIColor) -> SCNNode {
        let node = SCNNode()
        let horizontal = abs(b.x - a.x) > abs(b.y - a.y)
        let length = horizontal ? abs(b.x - a.x) : abs(b.y - a.y)
        let t: CGFloat = dashed ? 0.012 : 0.02
        let mat = Self.material(color)
        func segment(_ from: Float, _ len: Float) {
            let box = SCNBox(width: horizontal ? CGFloat(len) : t, height: horizontal ? t : CGFloat(len), length: t, chamferRadius: 0)
            box.firstMaterial = mat
            let n = SCNNode(geometry: box)
            let mid = from + len / 2
            n.position = horizontal ? SCNVector3(min(a.x, b.x) + mid, a.y, 0) : SCNVector3(a.x, min(a.y, b.y) + mid, 0)
            node.addChildNode(n)
        }
        if !dashed { segment(0, length); return node }
        let dash: Float = 0.1, gap: Float = 0.07
        var p: Float = 0
        while p < length {
            segment(p, min(dash, length - p))
            p += dash + gap
        }
        return node
    }
}

/// Orbit with one finger, pinch to zoom, two fingers to pan, double-tap to reset.
struct LiveRoom3DView: UIViewRepresentable {
    let model: LiveRoomScene

    func makeUIView(context: Context) -> SCNView {
        let v = SCNView()
        v.scene = model.scene
        v.pointOfView = model.cameraNode
        v.allowsCameraControl = true
        v.defaultCameraController.interactionMode = .orbitTurntable
        v.antialiasingMode = .multisampling4X
        v.backgroundColor = .black
        model.view = v
        DispatchQueue.main.async { model.recenter() }
        return v
    }

    func updateUIView(_ uiView: SCNView, context: Context) {}
}

/// A finished USDZ model (the Object Capture result) you can turn around and zoom.
struct ModelPreview: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> SCNView {
        let v = SCNView()
        v.scene = try? SCNScene(url: url, options: nil)
        v.allowsCameraControl = true
        v.autoenablesDefaultLighting = true
        v.antialiasingMode = .multisampling4X
        v.backgroundColor = UIColor(white: 0.12, alpha: 1)
        return v
    }

    func updateUIView(_ uiView: SCNView, context: Context) {}
}
