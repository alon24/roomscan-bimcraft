import ARKit
import SwiftUI

/// "Sweep from one spot": stand in the middle of a room (or a cluttered one where walking round is awkward), turn on the spot and
/// tilt the phone up and down. The phone keeps a photo, the LiDAR depth and the camera's direction every half second; the server
/// turns them into a 360 degree picture and a model of the room seen from that spot.
///
/// This tracks where the camera has looked, round the circle and at three heights, so the user can fill the dial.
final class SweepCoverage: ObservableObject {
    static let sectors = CoverageTracker.ringSectors   // 24 sectors of 15 degrees of turn
    static let reach = 25.0                            // a cell counts as seen when the camera points within this many degrees of its centre
    static let stayWithin = 0.35                       // metres of wandering before we say "turn on the spot"
    /// Cell layout matches RingCoverageDial: band 0 outer ring, 1 middle, 2 inner.
    static let bandPitch: [Double] = [0, 55, -50]       // walls (level), ceiling (up), floor (down), degrees above the horizon
    static let bandName = ["the walls", "the ceiling", "the floor"]

    @Published var cells = [Bool](repeating: false, count: 72)
    @Published var hint = "Stand in the middle and turn slowly on the spot."
    @Published var moved = 0.0                // metres from the start, now
    @Published var maxMoved = 0.0             // the most it wandered during this scan
    @Published var yaw = 0.0                 // where the camera points now, degrees round
    @Published var pitch = 0.0               // and degrees up
    private var origin: SIMD3<Float>?

    func reset() {
        cells = [Bool](repeating: false, count: 72)
        hint = "Stand in the middle and turn slowly on the spot."
        moved = 0; maxMoved = 0; origin = nil
    }

    func share(_ band: Int) -> Double {
        Double(cells[band * Self.sectors..<(band + 1) * Self.sectors].filter { $0 }.count) / Double(Self.sectors)
    }

    func update(_ frame: ARFrame) {
        guard case .normal = frame.camera.trackingState else { return }
        let t = frame.camera.transform
        let pos = SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        if origin == nil { origin = pos }
        moved = Double(simd_length(pos - origin!))
        maxMoved = max(maxMoved, moved)

        let f = -SIMD3(t.columns.2.x, t.columns.2.y, t.columns.2.z)        // where the camera looks (world, Y up)
        let fn = simd_normalize(f)
        pitch = Double(asin(max(-1, min(1, fn.y)))) * 180 / .pi
        var y = Double(atan2(fn.x, -fn.z)) * 180 / .pi
        if y < 0 { y += 360 }
        yaw = y
        for band in 0..<3 {
            let p = Self.bandPitch[band] * .pi / 180
            for i in 0..<Self.sectors where !cells[band * Self.sectors + i] {
                let a = (Double(i) + 0.5) * 360 / Double(Self.sectors) * .pi / 180
                let c = SIMD3<Float>(Float(sin(a) * cos(p)), Float(sin(p)), Float(-cos(a) * cos(p)))   // direction of the cell's centre
                let ang = Double(acos(max(-1, min(1, simd_dot(fn, c))))) * 180 / .pi
                if ang < Self.reach { cells[band * Self.sectors + i] = true }
            }
        }
        hint = text()
    }

    private func text() -> String {
        if moved > Self.stayWithin { return String(format: "You are %.0f cm from where you started. Come back and turn on the spot (small steps are fine).", moved * 100) }
        let walls = share(0), up = share(1), down = share(2)
        if walls < 0.85 { return "Turn slowly on the spot: the walls are \(Int(walls * 100))% done." }
        if up < 0.7 { return "Now tilt up and sweep the ceiling and the tops of the walls (\(Int(up * 100))%)." }
        if down < 0.7 { return "Now tilt down and sweep the floor and the bottoms of the walls (\(Int(down * 100))%)." }
        return "Everything is covered. Tap Stop."
    }
}

/// What the user sees while sweeping.
struct SweepPanel: View {
    @ObservedObject var sweep: SweepCoverage
    var frames: Int

    var body: some View {
        HStack(spacing: 12) {
            RingCoverageDial(cells: sweep.cells).frame(width: 96, height: 96)
            VStack(alignment: .leading, spacing: 4) {
                Text(sweep.hint).font(.caption.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                Text("Looking \(Int(sweep.yaw))° round, \(Int(sweep.pitch))° up · \(frames) photos")
                    .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                Text("Outer ring: walls · middle: ceiling · inner: floor").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(10).card()
    }
}
