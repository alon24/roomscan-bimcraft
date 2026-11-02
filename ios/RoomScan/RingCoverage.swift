import SwiftUI

/// Top view of the ring scan: three rings, outer = phone low, middle = medium, inner = from above; a wedge turns green once the part
/// has been seen from that angle round the ring at that height.
struct RingCoverageDial: View {
    let cells: [Bool]
    var old: [Bool]? = nil       // adding to a scan: grey = it had this already, red = still missing, the usual colour = filled just now
    var yaw: Double? = nil       // where the camera points now: a green line on the dial, to steer it into what is still empty
    var pitch: Double = 0
    private let sectors = RingTracker.sectors

    var body: some View {
        Canvas { ctx, size in
            let c = CGPoint(x: size.width / 2, y: size.height / 2), R = min(size.width, size.height) / 2
            let radii: [(CGFloat, CGFloat)] = [(0.68, 1.0), (0.38, 0.66), (0.08, 0.36)]      // band 0 (low) outermost
            for band in 0..<3 {
                let (r0, r1) = radii[band]
                for i in 0..<sectors {
                    let a0 = Angle.degrees(Double(i) * 360 / Double(sectors) - 90 + 0.8), a1 = Angle.degrees(Double(i + 1) * 360 / Double(sectors) - 90 - 0.8)
                    var p = Path()
                    p.addArc(center: c, radius: R * r1, startAngle: a0, endAngle: a1, clockwise: false)
                    p.addArc(center: c, radius: R * r0, startAngle: a1, endAngle: a0, clockwise: true)
                    p.closeSubpath()
                    let k = band * sectors + i
                    let colour: Color
                    if let old, k < old.count {
                        colour = old[k] ? Color.gray.opacity(0.55) : cells[k] ? Theme.accent : Color.red.opacity(0.6)
                    } else {
                        colour = cells[k] ? Theme.accent : Color.gray.opacity(0.35)
                    }
                    ctx.fill(p, with: .color(colour))
                }
            }
            if let yaw {
                let a = (yaw - 90) * .pi / 180
                func at(_ r: CGFloat) -> CGPoint { CGPoint(x: c.x + cos(a) * R * r, y: c.y + sin(a) * R * r) }
                var line = Path()
                line.move(to: at(0.04))
                line.addLine(to: at(1.0))
                ctx.stroke(line, with: .color(.black.opacity(0.6)), lineWidth: 4)
                ctx.stroke(line, with: .color(.green), lineWidth: 2.5)
                // the dot sits on the ring the camera is at now: walls outer, ceiling middle, floor inner
                let ring: CGFloat = pitch > 28 ? 0.52 : pitch < -25 ? 0.22 : 0.84
                let p = at(ring)
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - 5, y: p.y - 5, width: 10, height: 10)), with: .color(.green))
                ctx.stroke(Path(ellipseIn: CGRect(x: p.x - 5, y: p.y - 5, width: 10, height: 10)), with: .color(.white), lineWidth: 1)
            }
        }
        .accessibilityLabel("Coverage round the ring at three heights")
    }
}
