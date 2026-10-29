import SceneKit
import SwiftUI

/// Top-down map from the server: grey = not seen, orange = thin, green = good, blue = free floor. White dot = you, numbered dots = what to capture next.
struct LiveMiniMap: View {
    let grid: LiveGrid
    let you: SIMD3<Float>?
    let needs: [LiveNeed]

    var body: some View {
        Canvas { ctx, size in
            let s = min(size.width / CGFloat(grid.w), size.height / CGFloat(grid.h))
            let ox = (size.width - s * CGFloat(grid.w)) / 2, oy = (size.height - s * CGFloat(grid.h)) / 2
            for r in 0..<grid.h {
                for c in 0..<grid.w {
                    let v = grid.cells[r * grid.w + c]
                    guard v > 0 else { continue }
                    let col: Color = v == 1 ? .orange : (v == 2 ? Theme.accent : Color.blue.opacity(0.35))
                    ctx.fill(Path(CGRect(x: ox + CGFloat(c) * s, y: oy + CGFloat(r) * s, width: s + 0.5, height: s + 0.5)), with: .color(col))
                }
            }
            func at(_ x: Double, _ z: Double) -> CGPoint { CGPoint(x: ox + CGFloat((x - grid.x0) / grid.cell) * s, y: oy + CGFloat((z - grid.z0) / grid.cell) * s) }
            for (i, n) in needs.enumerated() {
                let p = at(n.target[0], n.target[2])
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - 6, y: p.y - 6, width: 12, height: 12)), with: .color(.red))
                ctx.draw(Text("\(i + 1)").font(.system(size: 9, weight: .bold)).foregroundColor(.white), at: p)
            }
            if let y = you {
                let p = at(Double(y.x), Double(y.z))
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - 5, y: p.y - 5, width: 10, height: 10)), with: .color(.white))
                ctx.stroke(Path(ellipseIn: CGRect(x: p.x - 8, y: p.y - 8, width: 16, height: 16)), with: .color(Theme.accent), lineWidth: 2)
            }
        }
        .accessibilityLabel("Map of what has been captured")
    }
}

/// While scanning with a server: connection, progress, the map and what to do next. Offline it says so and the scan carries on.
struct LivePanel: View {
    @ObservedObject var live: LiveLink

    private var pill: (String, Color) {
        switch live.link {
        case .live: return ("Live · \(Int(live.percent))%" + (live.waiting > 3 ? " · \(live.waiting) waiting" : ""), .green)
        case .connecting: return ("Connecting to the server…", .orange)
        case .offline: return ("Offline: saving on the phone · \(live.waiting) to send later", .gray)
        case .off: return ("", .clear)
        }
    }

    var body: some View {
        if live.link != .off {
            HStack(alignment: .top, spacing: 10) {
                if let g = live.grid {
                    LiveMiniMap(grid: g, you: live.you, needs: live.needs).frame(width: 110, height: 110)
                        .background(Color.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 8))
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(pill.0).font(.caption2.weight(.bold)).padding(.horizontal, 8).padding(.vertical, 2)
                        .background(pill.1, in: Capsule()).foregroundStyle(.white)
                    if let n = live.needs.first {
                        Text(n.text).font(.caption.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                        if live.needs.count > 1 { Text("+ \(live.needs.count - 1) more").font(.caption2).foregroundStyle(.secondary) }
                    } else if live.link == .live {
                        Text(live.summary.isEmpty ? "Keep scanning. The server will say what is missing." : live.summary)
                            .font(.caption).fixedSize(horizontal: false, vertical: true)
                    } else {
                        Text("No connection: keep scanning, everything is kept on the phone and can be uploaded afterwards.")
                            .font(.caption).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(10).card()
        }
    }
}

/// What the live link is doing, mostly after Stop (sending the last frames, building, a server error). A separate view because
/// MeshScanView does not redraw for changes inside the nested LiveLink.
struct LiveNote: View {
    @ObservedObject var live: LiveLink
    var body: some View {
        if !live.note.isEmpty { Text(live.note).font(.caption).padding(6).background(.thinMaterial, in: Capsule()) }
    }
}

/// The server's "capture here next" points, drawn on the camera picture; an arrow at the edge when one is out of view.
struct LiveNeedLayer: View {
    @ObservedObject var live: LiveLink
    let view: SCNView
    let size: CGSize
    let zoom: CGFloat

    private var screen: ScreenProjector {
        ScreenProjector(view: view, size: size, zoom: zoom, side: 20, top: 120, bottom: 260, arrowBand: 100, arrowInset: 150)
    }
    private func project(_ w: SIMD3<Float>) -> (pt: CGPoint, front: Bool) { screen.project(w) }
    private func onScreen(_ q: (pt: CGPoint, front: Bool)) -> Bool { screen.onScreen(q) }
    private func edge(_ q: (pt: CGPoint, front: Bool)) -> (CGPoint, Double) { screen.edge(q) }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.1)) { _ in
            ZStack {
                ForEach(Array(live.needs.enumerated()), id: \.element.id) { i, n in
                    let q = project(n.point)
                    if onScreen(q) {
                        ZStack {
                            Circle().stroke(.white, lineWidth: 3).frame(width: 34, height: 34)
                            Circle().stroke(Color.red, lineWidth: 3).frame(width: 22, height: 22)
                            Text("\(i + 1)").font(.caption2.bold()).foregroundStyle(.white)
                        }
                        .shadow(color: .black.opacity(0.6), radius: 2).position(q.pt)
                    } else {
                        let e = edge(q)
                        VStack(spacing: 0) {
                            Image(systemName: "location.north.fill").rotationEffect(.radians(e.1)).font(.title2).foregroundStyle(.red)
                            Text("\(i + 1)").font(.caption2.bold()).foregroundStyle(.white).padding(.horizontal, 5).background(.black.opacity(0.7), in: Capsule())
                        }.position(e.0)
                    }
                }
            }
        }
    }
}
