import SwiftUI
import ARKit

/// Ring scans (Object tab, "With the ring"): the markers in each photo place the frame, so nothing is tracked here except what
/// helps the user: every frame is kept while the tray turns, the tray speed and camera angle are read from the markers, and the
/// angles seen round the ring at three heights fill a dial (the same bands as the server's quality report).
final class RingTracker: ObservableObject {
    var enabled = false                        // set once by the view: the Room tab's scans never use the ring
    @Published private(set) var capturing = false
    @Published var framesTaken = 0
    @Published var speed: RingSpeedMonitor.Reading?       // how fast the tray is turning, read from the markers
    private let speedMonitor = RingSpeedMonitor()
    // Which angles round the ring have been seen at which height (3 heights x 24 sectors of 15 degrees)
    @Published var cells = [Bool](repeating: false, count: 72)
    @Published var hint = ""
    static let sectors = 24
    // Camera angle above the ring plane that counts as low / middle / high. Real scans only ever reached 33 to 44 degrees (holding the
    // phone by hand with the markers in view), so a 25 degree edge for "low" was never reached: the outer ring would not fill.
    static let lowMax = 32.0, midMax = 50.0
    static func band(_ el: Double) -> Int { el < lowMax ? 0 : (el < midMax ? 1 : 2) }
    static func bandName(_ b: Int) -> String { ["low", "middle", "high"][b] }
    private var readings = 0
    private var ticks = 0

    func begin() {
        speedMonitor.onReading = { [weak self] r in self?.speed = r; self?.note(r) }
        speed = nil
        cells = [Bool](repeating: false, count: 72); hint = ""
        framesTaken = 0; ticks = 0
        capturing = enabled
        if enabled { speedMonitor.reset() }
    }

    func end() {
        speed = nil
        capturing = false
    }

    /// About 5 times a second: keep every other frame (about 2.5 a second) and read the markers.
    func update(frame: ARFrame?, recorder: PhotoRecorder?) {
        guard capturing, let frame, case .normal = frame.camera.trackingState else { return }
        ticks += 1
        speedMonitor.feed(frame)
        if ticks % 2 == 0 { recorder?.consider(frame, force: true); framesTaken = recorder?.count ?? 0 }
    }

    private func note(_ r: RingSpeedMonitor.Reading) {
        guard let az = r.az, let el = r.elev else { return }
        readings += 1
        if readings % 8 == 0 { Reporter.log("ring angle \(Int(el))° az \(Int(az))° band \(Self.bandName(Self.band(el)))") }   // so a report shows which angles were reached
        cells[Self.band(el) * Self.sectors + min(Self.sectors - 1, Int(az / (360 / Double(Self.sectors))))] = true
        hint = Self.hint(for: cells)
    }

    static func hint(for cells: [Bool]) -> String {
        func share(_ b: Int) -> Double { Double(cells[b * sectors..<(b + 1) * sectors].filter { $0 }.count) / Double(sectors) }
        let low = share(0), mid = share(1), high = share(2)
        if mid < 0.85 { return "Turn the tray slowly: the first turn is \(Int(mid * 100))% done." }
        if low < 0.7 { return "Now lower the phone until the angle reads under \(Int(lowMax))°, and turn the tray once more." }
        if high < 0.5 { return "Last: raise the phone until the angle reads over \(Int(midMax))°, looking down from above, for a quarter turn or more." }
        return "The whole ring is covered at every height. Tap Stop."
    }
}

/// What a ring scan shows while filming: the instruction, the coverage dial with the camera angle, the tray speed, the frame count.
struct RingPanel: View {
    @ObservedObject var tracker: RingTracker

    var body: some View {
        VStack(spacing: 8) {
            Text("Keep the part big in the picture with at least 4 markers in view (the rest of the ring may be cut off). Turn the spool slowly for one turn looking down at it, then one more turn with the phone low, nearly level with the top of the part. Then tap Stop.")
                .font(.caption).padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
            HStack(spacing: 12) {
                RingCoverageDial(cells: tracker.cells).frame(width: 96, height: 96)
                VStack(alignment: .leading, spacing: 4) {
                    Text(tracker.hint.isEmpty ? "Waiting for the ring…" : tracker.hint).font(.caption.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                    if let e = tracker.speed?.elev {   // the number to steer by: low is under 32, middle 32 to 50, high over 50
                        let b = RingTracker.band(e)
                        Text("Camera angle \(Int(e))° (\(RingTracker.bandName(b)))").font(.caption2.monospacedDigit())
                            .padding(.horizontal, 8).padding(.vertical, 2)
                            .background(Theme.accent.opacity(b == 0 ? 0.9 : 0.5), in: Capsule()).foregroundStyle(.white)
                    }
                }
            }
            .padding(10).card()
            if let s = tracker.speed {
                Text(s.text).font(.callout.weight(.semibold)).foregroundStyle(.white).padding(.horizontal, 12).padding(.vertical, 8)
                    .background(s.level >= 3 ? Color.red : (s.level == 2 ? Color.orange : (s.level == 1 ? Color.green : Color.gray)), in: Capsule())
            }
            Text("\(tracker.framesTaken) frames").font(.system(size: 34, weight: .bold, design: .rounded))
                .padding(10).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
        }
    }
}
