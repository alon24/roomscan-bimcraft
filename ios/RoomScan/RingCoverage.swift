import SwiftUI

/// Top view of the ring scan: three rings, outer = phone low, middle = medium, inner = from above; a wedge turns green once the part
/// has been seen from that angle round the ring at that height.
struct RingCoverageDial: View {
    let cells: [Bool]
    private let sectors = CoverageTracker.ringSectors

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
                    ctx.fill(p, with: .color(cells[band * sectors + i] ? Theme.accent : Color.gray.opacity(0.35)))
                }
            }
        }
        .accessibilityLabel("Coverage round the ring at three heights")
    }
}
