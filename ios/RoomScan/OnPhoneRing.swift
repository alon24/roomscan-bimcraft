import UIKit
import Vision
import SceneKit
import simd

/// Builds a ring scan's model on the phone itself, without the server: finds the printed markers in each photo (Vision
/// rectangles, then the 6 x 6 pattern is read), works out where the camera was from all marker corners, and carves the part
/// from its outline in every photo (the outlines are made right after Stop). The server does the same with more care; this
/// gives a model and an STL straight away, also with no network.
enum OnPhoneRing {
    /// The 18 markers of the printed ring (ArUco DICT_4X4_50, ids 0 to 17), 6 x 6 cells each, row by row, 1 = black.
    static let bits: [String] = [
        "111111101001110101111001111011111111", "111111111111100001101101101011111111", "111111111001111001111011100101111111",
        "111111101101101101110111110011111111", "111111110101110111101101100011111111", "111111110001101101100111100101111111",
        "111111101101100011111011100011111111", "111111100111110111100001111011111111", "111111100001100011100101101011111111",
        "111111100111100001110101110011111111", "111111100001101101101101111101111111", "111111111101111101101011110001111111",
        "111111111111100011101001110001111111", "111111111011101011111111100001111111", "111111111011110111101001111101111111",
        "111111111011110011111001100011111111", "111111110111110011110011110101111111", "111111110011110011111111111111111111",
    ]

    struct Ring {
        var markerMM: Double
        var radiusMM: Double
        var stageRadiusMM = 62.5

        /// The sizes measured for your printed ring (Settings, or taken from the server), else the design.
        static func current() -> Ring {
            let d = UserDefaults.standard
            return Ring(markerMM: d.object(forKey: "ringMarkerMM") as? Double ?? 22.0, radiusMM: d.object(forKey: "ringRadiusMM") as? Double ?? 81.0)
        }

        /// Marker i's outer corners in ring millimetres: top-left, top-right, bottom-right, bottom-left (its top points outward).
        func corners(_ i: Int) -> [SIMD2<Double>] {
            let a = Double(i) * 20 * .pi / 180
            let u = SIMD2(cos(a), sin(a)), r = SIMD2(sin(a), -cos(a)), c = radiusMM * u, h = markerMM / 2
            return [c + h * (-r + u), c + h * (r + u), c + h * (r - u), c + h * (-r - u)]
        }
    }

    struct Result {
        let scene: SCNScene
        let stl: URL
        let sizeMM: SIMD3<Double>
        let views: Int
        let triangles: Int
    }

    private struct View {
        let R: simd_double3x3, t: SIMD3<Double>
        let fx: Double, fy: Double, cx: Double, cy: Double
        let w: Int, h: Int
        let mask: [UInt8], mw: Int, mh: Int
        let rgb: [UInt8], cw: Int, ch: Int
    }

    // MARK: images

    private static func pixels(_ cg: CGImage, width: Int, height: Int, gray: Bool) -> [UInt8] {
        let bpp = gray ? 1 : 4
        var px = [UInt8](repeating: 0, count: width * height * bpp)
        px.withUnsafeMutableBytes { buf in
            guard let ctx = CGContext(data: buf.baseAddress, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * bpp,
                                      space: gray ? CGColorSpaceCreateDeviceGray() : CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: gray ? CGImageAlphaInfo.none.rawValue : CGImageAlphaInfo.noneSkipLast.rawValue) else { return }
            ctx.interpolationQuality = .medium
            ctx.draw(cg, in: CGRect(x: 0, y: 0, width: width, height: height))
        }
        return px
    }

    private static func cgImage(_ url: URL) -> CGImage? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(src, 0, nil)
    }

    // MARK: small linear algebra

    /// Solve A x = b (n x n) by Gaussian elimination with pivoting.
    private static func solve(_ a: [[Double]], _ b: [Double]) -> [Double]? {
        var m = a, v = b
        let n = v.count
        for col in 0..<n {
            guard let p = (col..<n).max(by: { abs(m[$0][col]) < abs(m[$1][col]) }), abs(m[p][col]) > 1e-12 else { return nil }
            m.swapAt(col, p); v.swapAt(col, p)
            for r in 0..<n where r != col {
                let f = m[r][col] / m[col][col]
                if f == 0 { continue }
                for c in col..<n { m[r][c] -= f * m[col][c] }
                v[r] -= f * v[col]
            }
        }
        return (0..<n).map { v[$0] / m[$0][$0] }
    }

    /// The 3 x 3 homography taking the unit square's corners to the four given points.
    private static func squareTo(_ q: [SIMD2<Double>]) -> [Double]? {
        let src: [SIMD2<Double>] = [SIMD2(0, 0), SIMD2(1, 0), SIMD2(1, 1), SIMD2(0, 1)]
        var a: [[Double]] = [], b: [Double] = []
        for (s, d) in zip(src, q) {
            a.append([s.x, s.y, 1, 0, 0, 0, -d.x * s.x, -d.x * s.y]); b.append(d.x)
            a.append([0, 0, 0, s.x, s.y, 1, -d.y * s.x, -d.y * s.y]); b.append(d.y)
        }
        guard let h = solve(a, b) else { return nil }
        return h + [1]
    }

    /// Eigenvector of the smallest eigenvalue of a symmetric matrix (cyclic Jacobi).
    private static func smallestEigenvector(_ s: [[Double]]) -> [Double] {
        let n = s.count
        var a = s
        var v = (0..<n).map { i in (0..<n).map { $0 == i ? 1.0 : 0.0 } }
        for _ in 0..<60 {
            var off = 0.0
            for p in 0..<n { for q in (p + 1)..<n { off += a[p][q] * a[p][q] } }
            if off < 1e-18 { break }
            for p in 0..<n {
                for q in (p + 1)..<n where abs(a[p][q]) > 1e-15 {
                    let theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
                    let t = (theta >= 0 ? 1.0 : -1.0) / (abs(theta) + (theta * theta + 1).squareRoot())
                    let c = 1 / (t * t + 1).squareRoot(), s = t * c
                    for k in 0..<n {
                        let akp = a[k][p], akq = a[k][q]
                        a[k][p] = c * akp - s * akq; a[k][q] = s * akp + c * akq
                    }
                    for k in 0..<n {
                        let apk = a[p][k], aqk = a[q][k]
                        a[p][k] = c * apk - s * aqk; a[q][k] = s * apk + c * aqk
                    }
                    for k in 0..<n {
                        let vkp = v[k][p], vkq = v[k][q]
                        v[k][p] = c * vkp - s * vkq; v[k][q] = s * vkp + c * vkq
                    }
                }
            }
        }
        let i = (0..<n).min(by: { a[$0][$0] < a[$1][$1] })!
        return (0..<n).map { v[$0][i] }
    }

    // MARK: markers and camera position

    /// Read the 6 x 6 pattern inside a quad; returns (marker id, quarter turns) or nil.
    private static func decode(_ gray: [UInt8], _ w: Int, _ h: Int, _ quad: [SIMD2<Double>]) -> (Int, Int)? {
        guard let H = squareTo(quad) else { return nil }
        var vals = [Double](repeating: 0, count: 36)
        for r in 0..<6 {
            for c in 0..<6 {
                var sum = 0.0, n = 0.0
                for dy in [-0.15, 0, 0.15] {
                    for dx in [-0.15, 0, 0.15] {
                        let x = (Double(c) + 0.5 + dx) / 6, y = (Double(r) + 0.5 + dy) / 6
                        let z = H[6] * x + H[7] * y + H[8]
                        let u = Int((H[0] * x + H[1] * y + H[2]) / z), v = Int((H[3] * x + H[4] * y + H[5]) / z)
                        if u >= 0 && u < w && v >= 0 && v < h { sum += Double(gray[v * w + u]); n += 1 }
                    }
                }
                vals[r * 6 + c] = n > 0 ? sum / n : 255
            }
        }
        guard let lo = vals.min(), let hi = vals.max(), hi - lo > 40 else { return nil }
        var g = vals.map { $0 < (lo + hi) / 2 ? UInt8(1) : UInt8(0) }
        var border = 0
        for i in 0..<6 { border += Int(g[i]) + Int(g[30 + i]) + Int(g[i * 6]) + Int(g[i * 6 + 5]) }
        guard border >= 23 else { return nil }
        for k in 0..<4 {
            for (id, pattern) in bits.enumerated() {
                var same = true
                let p = Array(pattern.utf8)
                for r in 1..<5 where same { for c in 1..<5 where g[r * 6 + c] != (p[r * 6 + c] == 49 ? 1 : 0) { same = false; break } }
                if same { return (id, k) }
            }
            // turn the grid a quarter turn clockwise
            var t = g
            for r in 0..<6 { for c in 0..<6 { t[r * 6 + c] = g[(5 - c) * 6 + r] } }
            g = t
        }
        return nil
    }

    /// Camera pose (ring millimetres -> camera, x right, y down, looking along +z) from marker corners: plane homography,
    /// then the marker that fits worst is dropped while it is off by more than 3 pixels.
    private static func pose(_ obj: [SIMD2<Double>], _ pix: [SIMD2<Double>], _ owner: [Int], fx: Double, fy: Double, cx: Double, cy: Double)
        -> (simd_double3x3, SIMD3<Double>, Int, Double)? {
        var keep = [Bool](repeating: true, count: obj.count)
        while true {
            var m = [[Double]](repeating: [Double](repeating: 0, count: 9), count: 9)
            for i in obj.indices where keep[i] {
                let X = obj[i].x / 100, Y = obj[i].y / 100
                let x = (pix[i].x - cx) / fx, y = (pix[i].y - cy) / fy
                for row in [[X, Y, 1, 0, 0, 0, -x * X, -x * Y, -x], [0, 0, 0, X, Y, 1, -y * X, -y * Y, -y]] {
                    for a in 0..<9 { for b in 0..<9 { m[a][b] += row[a] * row[b] } }
                }
            }
            let h = smallestEigenvector(m)
            var h1 = SIMD3(h[0] / 100, h[3] / 100, h[6] / 100), h2 = SIMD3(h[1] / 100, h[4] / 100, h[7] / 100), h3 = SIMD3(h[2], h[5], h[8])
            var lam = 1 / simd_length(h1)
            if (lam * h3).z < 0 { lam = -lam }
            h1 *= lam; h2 *= lam; h3 *= lam
            let r1 = simd_normalize(h1)
            let r2 = simd_normalize(h2 - r1 * simd_dot(r1, h2))
            let R = simd_double3x3(columns: (r1, r2, simd_cross(r1, r2)))
            var err = [Int: (Double, Int)]()
            for i in obj.indices where keep[i] {
                let p = R * SIMD3(obj[i].x, obj[i].y, 0) + h3
                let e = simd_length(SIMD2(fx * p.x / p.z + cx, fy * p.y / p.z + cy) - pix[i])
                err[owner[i], default: (0, 0)].0 += e
                err[owner[i], default: (0, 0)].1 += 1
            }
            let mean = err.mapValues { $0.0 / Double($0.1) }
            guard let worst = mean.max(by: { $0.value < $1.value }) else { return nil }
            if worst.value <= 3 || mean.count <= 4 {
                let all = err.values.reduce(0) { $0 + $1.0 } / Double(err.values.reduce(0) { $0 + $1.1 })
                return mean.count >= 4 ? (R, h3, mean.count, all) : nil
            }
            for i in obj.indices where owner[i] == worst.key { keep[i] = false }
        }
    }

    // MARK: building

    static func build(photos: [URL], metas: [PhotoMeta], masks: [URL], ring: Ring, progress: (String) -> Void) -> Result? {
        let maskFor = Dictionary(masks.map { ($0.deletingPathExtension().lastPathComponent, $0) }, uniquingKeysWith: { a, _ in a })
        let metaFor = Dictionary(metas.map { ($0.file, $0) }, uniquingKeysWith: { a, _ in a })
        var candidates = photos.filter { maskFor[$0.deletingPathExtension().lastPathComponent] != nil && metaFor[$0.lastPathComponent] != nil }
        if candidates.count > 60 { let step = Double(candidates.count) / 60; candidates = (0..<60).map { candidates[Int(Double($0) * step)] } }
        var views: [View] = []
        let rect = VNDetectRectanglesRequest()
        rect.maximumObservations = 40
        rect.minimumSize = 0.02
        rect.minimumAspectRatio = 0.2
        rect.quadratureTolerance = 40
        rect.minimumConfidence = 0.3
        for (n, url) in candidates.enumerated() {
            if n % 5 == 0 { progress("Finding the ring: photo \(n + 1) of \(candidates.count)…") }
            guard let meta = metaFor[url.lastPathComponent], let cg = cgImage(url),
                  let mURL = maskFor[url.deletingPathExtension().lastPathComponent], let mcg = cgImage(mURL) else { continue }
            let w = cg.width, h = cg.height
            let gray = pixels(cg, width: w, height: h, gray: true)
            guard (try? VNImageRequestHandler(cgImage: cg).perform([rect])) != nil else { continue }
            var obj: [SIMD2<Double>] = [], pix: [SIMD2<Double>] = [], owner: [Int] = []
            var seen = Set<Int>()
            for o in rect.results ?? [] {
                let q = [o.topLeft, o.topRight, o.bottomRight, o.bottomLeft].map { SIMD2(Double($0.x) * Double(w), (1 - Double($0.y)) * Double(h)) }
                guard let (id, k) = decode(gray, w, h, q), !seen.contains(id) else { continue }
                seen.insert(id)
                let cs = ring.corners(id)
                for i in 0..<4 { obj.append(cs[i]); pix.append(q[(i - k + 4) % 4]); owner.append(id) }
            }
            guard seen.count >= 4 else { continue }
            let K = meta.intrinsics                     // columns: [fx,0,0],[0,fy,0],[cx,cy,1] for the photo size
            let sx = Double(w) / Double(meta.width), sy = Double(h) / Double(meta.height)
            let fx = Double(K[0][0]) * sx, fy = Double(K[1][1]) * sy, cx = Double(K[2][0]) * sx, cy = Double(K[2][1]) * sy
            guard let (R, t, _, err) = pose(obj, pix, owner, fx: fx, fy: fy, cx: cx, cy: cy), err <= 4 else { continue }
            let mw = 480, mh = Int(Double(480) * Double(h) / Double(w))
            let mask = pixels(mcg, width: mw, height: mh, gray: true)
            let rgb = pixels(cg, width: mw, height: mh, gray: false)
            views.append(View(R: R, t: t, fx: fx, fy: fy, cx: cx, cy: cy, w: w, h: h, mask: mask, mw: mw, mh: mh, rgb: rgb, cw: mw, ch: mh))
        }
        guard views.count >= 6 else { return nil }
        progress("Carving the part from \(views.count) photos…")
        return carve(views, ring: ring)
    }

    private static func carve(_ views: [View], ring: Ring) -> Result? {
        let vox = 2.0, r0 = ring.stageRadiusMM, top = 320.0
        let nx = Int(2 * r0 / vox), nz = Int(top / vox)
        let count = nx * nx * nz
        var miss = [UInt8](repeating: 0, count: count), seen = [UInt8](repeating: 0, count: count)
        func centre(_ i: Int, _ j: Int, _ k: Int) -> SIMD3<Double> {
            SIMD3(-r0 + (Double(i) + 0.5) * vox, -r0 + (Double(j) + 0.5) * vox, (Double(k) + 0.5) * vox)
        }
        for v in views {
            let su = Double(v.mw) / Double(v.w), sv = Double(v.mh) / Double(v.h)
            for k in 0..<nz {
                for j in 0..<nx {
                    for i in 0..<nx {
                        let p0 = centre(i, j, k)
                        if p0.x * p0.x + p0.y * p0.y > r0 * r0 { continue }
                        let p = v.R * p0 + v.t
                        guard p.z > 1 else { continue }
                        let u = (v.fx * p.x / p.z + v.cx) * su, w = (v.fy * p.y / p.z + v.cy) * sv
                        guard u >= 0, w >= 0, u < Double(v.mw - 1), w < Double(v.mh - 1) else { continue }
                        let idx = (k * nx + j) * nx + i
                        if seen[idx] < 255 { seen[idx] += 1 }
                        if v.mask[Int(w) * v.mw + Int(u)] < 128, miss[idx] < 255 { miss[idx] += 1 }
                    }
                }
            }
        }
        let allowedMiss = UInt8(max(0, Int((0.05 * Double(views.count)).rounded())))
        let needSeen = UInt8(max(4, views.count / 2))
        var occ = [Bool](repeating: false, count: count)
        for idx in 0..<count { occ[idx] = seen[idx] >= needSeen && miss[idx] <= allowedMiss }
        // keep the biggest connected piece
        var label = [Int32](repeating: 0, count: count)
        var bestLabel: Int32 = 0, bestSize = 0, next: Int32 = 0
        for start in 0..<count where occ[start] && label[start] == 0 {
            next += 1
            var stack = [start], size = 0
            label[start] = next
            while let idx = stack.popLast() {
                size += 1
                let i = idx % nx, j = (idx / nx) % nx, k = idx / (nx * nx)
                for (di, dj, dk) in [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)] {
                    let a = i + di, b = j + dj, c = k + dk
                    guard a >= 0, b >= 0, c >= 0, a < nx, b < nx, c < nz else { continue }
                    let n2 = (c * nx + b) * nx + a
                    if occ[n2] && label[n2] == 0 { label[n2] = next; stack.append(n2) }
                }
            }
            if size > bestSize { bestSize = size; bestLabel = next }
        }
        guard bestSize > 50 else { return nil }
        // smooth field (box blur of the occupancy), padded by one cell all round
        let px = nx + 2, pz = nz + 2
        var field = [Float](repeating: 0, count: px * px * pz)
        func fidx(_ i: Int, _ j: Int, _ k: Int) -> Int { (k * px + j) * px + i }
        for k in 0..<nz { for j in 0..<nx { for i in 0..<nx where label[(k * nx + j) * nx + i] == bestLabel { field[fidx(i + 1, j + 1, k + 1)] = 1 } } }
        var blurred = field
        for k in 1..<(pz - 1) {
            for j in 1..<(px - 1) {
                for i in 1..<(px - 1) {
                    var s: Float = 0
                    for dk in -1...1 { for dj in -1...1 { for di in -1...1 { s += field[fidx(i + di, j + dj, k + dk)] } } }
                    blurred[fidx(i, j, k)] = s / 27
                }
            }
        }
        // surface nets: one vertex per cell that the 0.5 level passes through, quads across every crossing edge
        let iso: Float = 0.5
        var vertIndex = [Int32](repeating: -1, count: px * px * pz)
        var positions: [SIMD3<Double>] = []
        func world(_ g: SIMD3<Double>) -> SIMD3<Double> { SIMD3(-r0 + (g.x - 1 + 0.5) * vox, -r0 + (g.y - 1 + 0.5) * vox, (g.z - 1 + 0.5) * vox) }
        let cornerOffsets = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0), (0, 0, 1), (1, 0, 1), (0, 1, 1), (1, 1, 1)]
        let edges = [(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)]
        for k in 0..<(pz - 1) {
            for j in 0..<(px - 1) {
                for i in 0..<(px - 1) {
                    let vals = cornerOffsets.map { blurred[fidx(i + $0.0, j + $0.1, k + $0.2)] }
                    let inside = vals.map { $0 > iso }
                    if inside.allSatisfy({ $0 }) || inside.allSatisfy({ !$0 }) { continue }
                    var sum = SIMD3<Double>(0, 0, 0), n = 0.0
                    for (a, b) in edges where inside[a] != inside[b] {
                        let ta = Double((iso - vals[a]) / (vals[b] - vals[a]))
                        let ca = cornerOffsets[a], cb = cornerOffsets[b]
                        let pa = SIMD3(Double(ca.0), Double(ca.1), Double(ca.2)), pb = SIMD3(Double(cb.0), Double(cb.1), Double(cb.2))
                        sum += pa + (pb - pa) * ta; n += 1
                    }
                    vertIndex[fidx(i, j, k)] = Int32(positions.count)
                    positions.append(world(SIMD3(Double(i), Double(j), Double(k)) + sum / n))
                }
            }
        }
        var tris: [UInt32] = []
        func quad(_ a: Int, _ b: Int, _ c: Int, _ d: Int, _ flip: Bool) {
            let q = [vertIndex[a], vertIndex[b], vertIndex[c], vertIndex[d]]
            guard q.allSatisfy({ $0 >= 0 }) else { return }
            let v = q.map { UInt32($0) }
            tris += flip ? [v[0], v[2], v[1], v[0], v[3], v[2]] : [v[0], v[1], v[2], v[0], v[2], v[3]]
        }
        for k in 1..<(pz - 1) {
            for j in 1..<(px - 1) {
                for i in 1..<(px - 1) {
                    let here = blurred[fidx(i, j, k)] > iso
                    if i + 1 < px, here != (blurred[fidx(i + 1, j, k)] > iso) {
                        quad(fidx(i, j - 1, k - 1), fidx(i, j, k - 1), fidx(i, j, k), fidx(i, j - 1, k), here)
                    }
                    if j + 1 < px, here != (blurred[fidx(i, j + 1, k)] > iso) {
                        quad(fidx(i - 1, j, k - 1), fidx(i - 1, j, k), fidx(i, j, k), fidx(i, j, k - 1), here)
                    }
                    if k + 1 < pz, here != (blurred[fidx(i, j, k + 1)] > iso) {
                        quad(fidx(i - 1, j - 1, k), fidx(i, j - 1, k), fidx(i, j, k), fidx(i - 1, j, k), here)
                    }
                }
            }
        }
        guard tris.count >= 30 else { return nil }
        // triangles must face outward: if the enclosed volume comes out negative, turn them all over
        var vol = 0.0
        for t in stride(from: 0, to: tris.count, by: 3) {
            vol += simd_dot(positions[Int(tris[t])], simd_cross(positions[Int(tris[t + 1])], positions[Int(tris[t + 2])]))
        }
        if vol < 0 { for t in stride(from: 0, to: tris.count, by: 3) { tris.swapAt(t + 1, t + 2) } }
        // normals and colours
        var normals = [SIMD3<Double>](repeating: .zero, count: positions.count)
        for t in stride(from: 0, to: tris.count, by: 3) {
            let a = Int(tris[t]), b = Int(tris[t + 1]), c = Int(tris[t + 2])
            let n = simd_cross(positions[b] - positions[a], positions[c] - positions[a])
            normals[a] += n; normals[b] += n; normals[c] += n
        }
        normals = normals.map { simd_length($0) > 0 ? simd_normalize($0) : $0 }
        var colours = [SIMD3<Float>](repeating: SIMD3(0.7, 0.7, 0.7), count: positions.count)
        var best = [Double](repeating: -1, count: positions.count)
        for v in views {
            let cam = -(v.R.transpose * v.t)
            let su = Double(v.cw) / Double(v.w), sv = Double(v.ch) / Double(v.h)
            for (n, p0) in positions.enumerated() {
                let facing = simd_dot(normals[n], simd_normalize(cam - p0))
                guard facing > best[n] else { continue }
                let p = v.R * p0 + v.t
                let u = (v.fx * p.x / p.z + v.cx) * su, w = (v.fy * p.y / p.z + v.cy) * sv
                guard p.z > 1, u >= 0, w >= 0, u < Double(v.cw - 1), w < Double(v.ch - 1) else { continue }
                let o = (Int(w) * v.cw + Int(u)) * 4
                colours[n] = SIMD3(Float(v.rgb[o]), Float(v.rgb[o + 1]), Float(v.rgb[o + 2])) / 255
                best[n] = facing
            }
        }
        // on the bed: centred, standing on z = 0
        let lo = positions.reduce(SIMD3(repeating: Double.greatestFiniteMagnitude)) { simd_min($0, $1) }
        let hi = positions.reduce(SIMD3(repeating: -Double.greatestFiniteMagnitude)) { simd_max($0, $1) }
        let shift = SIMD3((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z)
        let bed = positions.map { $0 - shift }
        guard let stl = writeSTL(bed, tris) else { return nil }
        let scene = makeScene(bed, normals, colours, tris)
        return Result(scene: scene, stl: stl, sizeMM: hi - lo, views: views.count, triangles: tris.count / 3)
    }

    private static func writeSTL(_ p: [SIMD3<Double>], _ tris: [UInt32]) -> URL? {
        var data = Data(count: 80)
        var n = UInt32(tris.count / 3)
        withUnsafeBytes(of: &n) { data.append(contentsOf: $0) }
        func f(_ x: Double) { var v = Float(x); withUnsafeBytes(of: &v) { data.append(contentsOf: $0) } }
        for t in stride(from: 0, to: tris.count, by: 3) {
            let a = p[Int(tris[t])], b = p[Int(tris[t + 1])], c = p[Int(tris[t + 2])]
            let nn = simd_length(simd_cross(b - a, c - a)) > 0 ? simd_normalize(simd_cross(b - a, c - a)) : SIMD3(0, 0, 1)
            for v in [nn, a, b, c] { f(v.x); f(v.y); f(v.z) }
            data.append(contentsOf: [0, 0])
        }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("part-\(Int(Date().timeIntervalSince1970)).stl")
        return (try? data.write(to: url)) != nil ? url : nil
    }

    private static func makeScene(_ p: [SIMD3<Double>], _ nrm: [SIMD3<Double>], _ col: [SIMD3<Float>], _ tris: [UInt32]) -> SCNScene {
        // millimetres, Z up -> SceneKit, Y up
        let verts = p.map { SCNVector3(Float($0.x), Float($0.z), Float(-$0.y)) }
        let norms = nrm.map { SCNVector3(Float($0.x), Float($0.z), Float(-$0.y)) }
        let colourData = col.withUnsafeBufferPointer { Data(buffer: $0) }
        let cs = SCNGeometrySource(data: colourData, semantic: .color, vectorCount: col.count, usesFloatComponents: true,
                                   componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0, dataStride: MemoryLayout<SIMD3<Float>>.stride)
        let el = SCNGeometryElement(indices: tris, primitiveType: .triangles)
        let g = SCNGeometry(sources: [SCNGeometrySource(vertices: verts), SCNGeometrySource(normals: norms), cs], elements: [el])
        let m = SCNMaterial()
        m.lightingModel = .blinn
        m.isDoubleSided = true
        g.firstMaterial = m
        let scene = SCNScene()
        scene.background.contents = UIColor(white: 0.1, alpha: 1)
        scene.rootNode.addChildNode(SCNNode(geometry: g))
        return scene
    }
}
