import SceneKit
import UIKit

/// A 3D look at a scan kept on the phone, built from its own files, so it can be checked before it is uploaded.
/// This is only what the phone captured; the server's model (solid surface, colours, STL) is made after Upload.
enum SavedPreview {
    static func scene(for s: SavedScan) -> SCNScene? {
        let d = LocalStore.dir(s.id)
        func url(_ rel: String?) -> URL? { rel.map { d.appendingPathComponent($0) } }
        let scene: SCNScene?
        if let u = url(s.usdz), let sc = try? SCNScene(url: u) { scene = sc }
        else if let u = url(s.mesh) { scene = ply(u) }
        else if let u = url(s.json) { scene = room(u) }
        else { scene = nil }
        scene?.background.contents = UIColor(white: 0.1, alpha: 1)
        return scene
    }

    // MARK: PLY (the free scan's mesh with normals, or the depth point cloud with colours)

    private static func size(_ type: String) -> Int {
        switch type {
        case "char", "uchar", "int8", "uint8": return 1
        case "short", "ushort", "int16", "uint16": return 2
        case "double", "float64": return 8
        default: return 4
        }
    }

    private static func ply(_ url: URL) -> SCNScene? {
        guard let data = try? Data(contentsOf: url), let end = data.range(of: Data("end_header\n".utf8)) else { return nil }
        let header = String(decoding: data[..<end.lowerBound], as: UTF8.self).split(separator: "\n").map(String.init)
        var vertexCount = 0, faceCount = 0, inVertex = false
        var props: [(name: String, type: String, offset: Int)] = []
        var stride = 0
        for line in header {
            let w = line.split(separator: " ").map(String.init)
            if w.first == "element", w.count == 3 {
                inVertex = w[1] == "vertex"
                if inVertex { vertexCount = Int(w[2]) ?? 0 } else if w[1] == "face" { faceCount = Int(w[2]) ?? 0 }
            } else if w.first == "property", inVertex, w.count == 3 {
                props.append((w[2], w[1], stride))
                stride += size(w[1])
            }
        }
        func offset(_ name: String) -> Int? { props.first { $0.name == name }?.offset }
        guard vertexCount > 0, stride > 0, let ox = offset("x"), let oy = offset("y"), let oz = offset("z") else { return nil }
        let body = data.subdata(in: end.upperBound..<data.count)
        guard body.count >= vertexCount * stride else { return nil }
        let colourOffsets = [offset("red"), offset("green"), offset("blue")].compactMap { $0 }
        let hasColour = colourOffsets.count == 3
        var pos = [SIMD3<Float>](), col = [SIMD3<Float>]()
        pos.reserveCapacity(vertexCount)
        body.withUnsafeBytes { raw in
            for i in 0..<vertexCount {
                let b = i * stride
                pos.append(SIMD3(raw.loadUnaligned(fromByteOffset: b + ox, as: Float.self),
                                 raw.loadUnaligned(fromByteOffset: b + oy, as: Float.self),
                                 raw.loadUnaligned(fromByteOffset: b + oz, as: Float.self)))
                if hasColour {
                    col.append(SIMD3(Float(raw[b + colourOffsets[0]]), Float(raw[b + colourOffsets[1]]), Float(raw[b + colourOffsets[2]])) / 255)
                }
            }
        }
        let vs = SCNGeometrySource(data: pos.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: .vertex, vectorCount: vertexCount,
                                   usesFloatComponents: true, componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0,
                                   dataStride: MemoryLayout<SIMD3<Float>>.stride)
        var sources = [vs]
        if hasColour {
            sources.append(SCNGeometrySource(data: col.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: .color, vectorCount: vertexCount,
                                             usesFloatComponents: true, componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0,
                                             dataStride: MemoryLayout<SIMD3<Float>>.stride))
        }
        let geometry: SCNGeometry
        let material = SCNMaterial()
        if faceCount > 0 {
            var idx = [Int32]()
            idx.reserveCapacity(faceCount * 3)
            body.withUnsafeBytes { raw in
                var p = vertexCount * stride
                for _ in 0..<faceCount {
                    guard p + 13 <= raw.count, raw[p] == 3 else { break }
                    for k in 0..<3 { idx.append(raw.loadUnaligned(fromByteOffset: p + 1 + 4 * k, as: Int32.self)) }
                    p += 13
                }
            }
            guard !idx.isEmpty else { return nil }
            let el = SCNGeometryElement(data: idx.withUnsafeBufferPointer { Data(buffer: $0) }, primitiveType: .triangles,
                                        primitiveCount: idx.count / 3, bytesPerIndex: 4)
            geometry = SCNGeometry(sources: sources, elements: [el])
            material.diffuse.contents = UIColor(white: 0.8, alpha: 1)
            material.isDoubleSided = true
        } else {
            let idx = (0..<Int32(vertexCount)).map { $0 }
            let el = SCNGeometryElement(data: idx.withUnsafeBufferPointer { Data(buffer: $0) }, primitiveType: .point,
                                        primitiveCount: vertexCount, bytesPerIndex: 4)
            el.pointSize = 0.01
            el.minimumPointScreenSpaceRadius = 2
            el.maximumPointScreenSpaceRadius = 6
            geometry = SCNGeometry(sources: sources, elements: [el])
            material.lightingModel = .constant
        }
        geometry.firstMaterial = material
        let scene = SCNScene()
        scene.rootNode.addChildNode(SCNNode(geometry: geometry))
        return scene
    }

    // MARK: floor plan (RoomPlan's scan.json: walls, doors, windows, openings and furniture as boxes)

    private static func room(_ url: URL) -> SCNScene? {
        guard let data = try? Data(contentsOf: url), let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        func matrix(_ any: Any?) -> simd_float4x4? {
            var f = [Float]()
            if let flat = any as? [Double], flat.count == 16 { f = flat.map(Float.init) }
            else if let cols = any as? [[Double]], cols.count == 4 { f = cols.flatMap { $0 }.map(Float.init) }
            guard f.count == 16 else { return nil }
            return simd_float4x4(columns: (SIMD4(f[0], f[1], f[2], f[3]), SIMD4(f[4], f[5], f[6], f[7]),
                                           SIMD4(f[8], f[9], f[10], f[11]), SIMD4(f[12], f[13], f[14], f[15])))
        }
        let scene = SCNScene()
        let kinds: [(String, UIColor, CGFloat)] = [("walls", .systemTeal, 0.1), ("doors", .systemOrange, 0.08), ("windows", .systemBlue, 0.06),
                                                   ("openings", .systemPurple, 0.06), ("objects", .systemGray, -1)]
        for (key, colour, thickness) in kinds {
            for item in json[key] as? [[String: Any]] ?? [] {
                guard let dim = item["dimensions"] as? [Double], dim.count == 3, let m = matrix(item["transform"]) else { continue }
                let box = SCNBox(width: CGFloat(dim[0]), height: CGFloat(dim[1]), length: thickness < 0 ? CGFloat(dim[2]) : thickness, chamferRadius: 0)
                let mat = SCNMaterial()
                mat.diffuse.contents = colour.withAlphaComponent(thickness < 0 ? 0.45 : 0.7)
                mat.isDoubleSided = true
                box.firstMaterial = mat
                let node = SCNNode(geometry: box)
                node.simdTransform = m
                scene.rootNode.addChildNode(node)
            }
        }
        return scene.rootNode.childNodes.isEmpty ? nil : scene
    }
}
