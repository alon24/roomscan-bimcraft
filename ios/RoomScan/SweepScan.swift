import ARKit
import SwiftUI

/// "Sweep from one spot": stand in the middle of a room (or a cluttered one where walking round is awkward), turn on the spot and
/// tilt the phone up and down. The phone keeps a photo, the LiDAR depth and the camera's direction every half second; the server
/// turns them into a 360 degree picture and a model of the room seen from that spot.
///
/// This tracks where the camera has looked, round the circle and at three heights, so the user can fill the dial.
final class SweepCoverage: ObservableObject {
    static let sectors = RingTracker.sectors   // 24 sectors of 15 degrees of turn
    static let reach = 25.0                            // a cell counts as seen when the camera points within this many degrees of its centre
    /// Where the phone may be. From real sweeps: 20 cm typical / 50 cm worst drift and 44 cm of height change gave doubled edges,
    /// because a photo taken off the centre sees round things the one-spot model cannot hold.
    static let driftOk = 0.10, driftBad = 0.25          // metres from the start, sideways
    static let liftOk = 0.08, liftBad = 0.18            // metres up or down from the start height
    /// Turn speed: at 20 deg/s a 10 ms exposure smears about 4 px; aim for half that.
    static let speedOk = 13.0, speedBad = 20.0          // degrees per second
    static let poleAt = 70.0                            // "straight up / down" counts above this tilt
    /// A frame with this share of its LiDAR depth nearer than 0.8 m looks at you (face, lap, legs): the same rule as the server's
    /// sweep.looks_at_you, which leaves such frames out. Seen on a real sweep: sitting at a desk, the phone filmed the user's legs and face.
    static let selfNear: Float = 0.8, selfShare = 0.4
    static let startHint = "Stand up, hold the phone out in front of you and turn your whole body slowly, so you stay behind it."
    /// Cell layout matches RingCoverageDial: band 0 outer ring, 1 middle, 2 inner.
    static let bandPitch: [Double] = [0, 55, -50]       // walls (level), ceiling (up), floor (down), degrees above the horizon

    enum Level { case good, warn, bad }

    @Published var cells = [Bool](repeating: false, count: 72)
    @Published var hint = SweepCoverage.startHint
    @Published var moved = 0.0                // metres from the start, sideways, now
    @Published var maxMoved = 0.0             // the most it wandered during this scan
    @Published var lift = 0.0                 // metres above (+) or below (-) the start height, now
    @Published var maxLift = 0.0
    @Published var speed = 0.0                // degrees per second of turn, smoothed
    @Published var upDone = false             // looked straight up
    @Published var downDone = false           // and straight down
    @Published var yaw = 0.0                 // where the camera points now, degrees round
    @Published var pitch = 0.0               // and degrees up
    @Published var onYou = false             // the camera is looking at you, not the room
    private var origin: SIMD3<Float>?
    private var last: (t: TimeInterval, f: SIMD3<Float>)?
    private var speeds: [Double] = []
    private var lastWarn: String?
    private var lastSpoken = ""

    var driftLevel: Level { moved > Self.driftBad ? .bad : moved > Self.driftOk ? .warn : .good }
    var liftLevel: Level { abs(lift) > Self.liftBad ? .bad : abs(lift) > Self.liftOk ? .warn : .good }
    var speedLevel: Level { speed > Self.speedBad ? .bad : speed > Self.speedOk ? .warn : .good }
    /// Photos are only kept while they are any use to the server: from the start spot, of the room. It drops the others anyway
    /// (sweep.main_spot, sweep.looks_at_you), and a frame taken 1.2 m away doubles the walls if it is kept.
    var keepFrames: Bool { driftLevel != .bad && !onYou }
    /// Median turn speed over the scan, sent with the upload.
    var medianSpeed: Double { speeds.isEmpty ? 0 : speeds.sorted()[speeds.count / 2] }

    func reset() {
        cells = [Bool](repeating: false, count: 72)
        hint = Self.startHint
        moved = 0; maxMoved = 0; lift = 0; maxLift = 0; speed = 0; upDone = false; downDone = false; onYou = false
        origin = nil; last = nil; speeds = []; lastWarn = nil; lastSpoken = ""
    }

    func share(_ band: Int) -> Double {
        Double(cells[band * Self.sectors..<(band + 1) * Self.sectors].filter { $0 }.count) / Double(Self.sectors)
    }

    func update(_ frame: ARFrame) {
        guard case .normal = frame.camera.trackingState else { last = nil; return }
        let t = frame.camera.transform
        let pos = SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        if origin == nil { origin = pos }
        let d = pos - origin!
        moved = Double(simd_length(SIMD2(d.x, d.z)))
        lift = Double(d.y)
        maxMoved = max(maxMoved, moved)
        maxLift = max(maxLift, abs(lift))
        onYou = Self.looksAtYou(frame)

        let f = -SIMD3(t.columns.2.x, t.columns.2.y, t.columns.2.z)        // where the camera looks (world, Y up)
        let fn = simd_normalize(f)
        if let l = last, frame.timestamp - l.t > 0.05 {
            let ang = Double(acos(max(-1, min(1, simd_dot(fn, l.f))))) * 180 / .pi
            let now = ang / (frame.timestamp - l.t)
            speed = speed * 0.6 + now * 0.4                                   // smoothed over about half a second
            if now > 1 { speeds.append(now) }                                  // standing still does not count as slow turning
        }
        last = (frame.timestamp, fn)
        pitch = Double(asin(max(-1, min(1, fn.y)))) * 180 / .pi
        if pitch > Self.poleAt { upDone = true }
        if pitch < -Self.poleAt { downDone = true }
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
        let (h, warning) = text()
        // spoken once per new instruction: the numbers in the text change all the time, the first words and the warning do not
        let words = warning ?? String(h.prefix(14))
        if words != lastSpoken {
            lastSpoken = words
            Voice.say(warning == "drift" ? "You moved. Go back to where you started." : String(h.split(whereSeparator: { ".:".contains($0) }).first ?? ""), every: 0)
        }
        hint = h
        // one buzz when a new problem starts, not every 0.2 s while it lasts
        if let warning, warning != lastWarn { UINotificationFeedbackGenerator().notificationOccurred(.warning) }
        lastWarn = warning
    }

    /// Share of the LiDAR depth (every 4th pixel) nearer than `selfNear`, at least `selfShare` of it.
    static func looksAtYou(_ frame: ARFrame) -> Bool {
        guard let map = frame.sceneDepth?.depthMap, CVPixelBufferGetPixelFormatType(map) == kCVPixelFormatType_DepthFloat32 else { return false }
        CVPixelBufferLockBaseAddress(map, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return false }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map), row = CVPixelBufferGetBytesPerRow(map)
        var near = 0, all = 0
        for y in stride(from: 0, to: h, by: 4) {
            let line = (base + y * row).assumingMemoryBound(to: Float32.self)
            for x in stride(from: 0, to: w, by: 4) where line[x] > 0.1 {
                all += 1
                if line[x] < selfNear { near += 1 }
            }
        }
        return all > 0 && Double(near) / Double(all) >= selfShare
    }

    /// The most useful thing to say now, and the name of the problem when it is a warning.
    private func text() -> (String, String?) {
        if driftLevel == .bad {
            return (String(format: "You moved %.0f cm, photos paused. Go back over the start: turn your body, don't walk.", moved * 100), "drift")
        }
        if onYou {
            return ("That's you (closer than 80 cm), photos paused. Hold the phone out and turn your whole body, so you stay behind it.", "self")
        }
        if driftLevel != .good {
            return (String(format: "You moved %.0f cm. Step back over the start and turn the phone round itself, not round you.", moved * 100), "drift")
        }
        if liftLevel != .good {
            return (String(format: "Phone %.0f cm %@ than at the start. Keep it at one height: tip it, don't lift it.", abs(lift) * 100, lift > 0 ? "higher" : "lower"), "lift")
        }
        if speedLevel == .bad { return (String(format: "Slow down: %.0f°/s. About 10°/s keeps the photos sharp.", speed), "speed") }
        let walls = share(0), up = share(1), down = share(2)
        if walls < 0.85 { return ("Turn slowly on the spot: the walls are \(Int(walls * 100))% done.", nil) }
        if up < 0.7 { return ("Now tip the phone up about 45° and turn again: ceiling and wall tops (\(Int(up * 100))%).", nil) }
        if down < 0.7 { return ("Now tip it down about 45° and turn again: floor and wall bottoms (\(Int(down * 100))%).", nil) }
        if !upDone { return ("Point straight up at the ceiling for a moment.", nil) }
        if !downDone { return ("Point straight down at the floor under you for a moment.", nil) }
        return ("Everything is covered. Tap Stop.", nil)
    }
}

/// What the user sees while sweeping.
struct SweepPanel: View {
    @ObservedObject var sweep: SweepCoverage
    var frames: Int

    private func colour(_ l: SweepCoverage.Level) -> Color { l == .good ? .green : l == .warn ? .orange : .red }

    private func chip(_ icon: String, _ text: String, _ l: SweepCoverage.Level) -> some View {
        Label(text, systemImage: icon).font(.caption2.weight(.semibold).monospacedDigit()).lineLimit(1)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(colour(l).opacity(0.22), in: Capsule()).overlay(Capsule().strokeBorder(colour(l).opacity(0.7)))
    }

    var body: some View {
        HStack(spacing: 12) {
            ZStack {
                RingCoverageDial(cells: sweep.cells)
                VStack(spacing: 0) {   // straight up / straight down, in the middle of the dial
                    Image(systemName: sweep.upDone ? "arrow.up.circle.fill" : "arrow.up.circle").foregroundStyle(sweep.upDone ? Theme.accent : .secondary)
                    Image(systemName: sweep.downDone ? "arrow.down.circle.fill" : "arrow.down.circle").foregroundStyle(sweep.downDone ? Theme.accent : .secondary)
                }.font(.system(size: 11, weight: .bold))
            }
            .frame(width: 96, height: 96)
            VStack(alignment: .leading, spacing: 5) {
                Text(sweep.hint).font(.caption.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 5) {
                    chip("scope", String(format: "%.0f cm", sweep.moved * 100), sweep.driftLevel)
                    chip("arrow.up.and.down", String(format: "%+.0f cm", sweep.lift * 100), sweep.liftLevel)
                    chip("gauge.with.needle", String(format: "%.0f°/s", sweep.speed), sweep.speedLevel)
                }
                Text("\(Int(sweep.pitch))° up · \(frames) photos · rings: walls, ceiling, floor").font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
            }
        }
        .padding(10).card()
    }
}
