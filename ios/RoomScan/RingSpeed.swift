import ARKit
import UIKit

/// Live check of how fast the tray turns, from the ring markers in the camera picture (the phone stays still, so ARKit's own motion
/// cannot see it). Limits come from the six ring scans so far: above about 30 degrees a second the markers smear more than 3 px
/// and photos start to be dropped; under about 20 the result is clean; under 5 the extra photos add nothing.
final class RingSpeedMonitor {
    struct Reading {
        var text: String
        var level: Int          // 0 starting, 1 good, 2 careful, 3 problem
        var turns: Double
    }

    static let fast = 30.0, ideal = 20.0, slow = 5.0

    var onReading: ((Reading) -> Void)?
    private let queue = DispatchQueue(label: "ringspeed", qos: .userInitiated)
    private var busy = false
    // used on the queue only
    private var flipped: Bool?
    private var lastAz: Double?
    private var lastTime = 0.0
    private var lastSeen = 0.0
    private var started = 0.0
    private var rate = 0.0
    private var travelled = 0.0
    private var slowSince: Double?
    private var warnedAt = 0.0

    func reset() {
        queue.async {
            self.lastAz = nil; self.lastSeen = 0; self.started = 0; self.rate = 0; self.travelled = 0; self.slowSince = nil; self.warnedAt = 0
        }
        busy = false
        onReading?(Reading(text: "Looking for the ring…", level: 0, turns: 0))
    }

    /// Call about 5 times a second. Frames that arrive while the last one is still being read are skipped.
    func feed(_ frame: ARFrame) {
        guard !busy else { return }
        let buf = frame.capturedImage
        CVPixelBufferLockBaseAddress(buf, .readOnly)
        let w = CVPixelBufferGetWidthOfPlane(buf, 0) / 2, h = CVPixelBufferGetHeightOfPlane(buf, 0) / 2
        let bpr = CVPixelBufferGetBytesPerRowOfPlane(buf, 0)
        guard w > 0, h > 0, let base = CVPixelBufferGetBaseAddressOfPlane(buf, 0) else { CVPixelBufferUnlockBaseAddress(buf, .readOnly); return }
        var gray = [UInt8](repeating: 0, count: w * h)
        let src = base.assumingMemoryBound(to: UInt8.self)
        for y in 0..<h { for x in 0..<w { gray[y * w + x] = src[2 * y * bpr + 2 * x] } }     // half size is plenty to read 20 mm markers at 25 cm
        CVPixelBufferUnlockBaseAddress(buf, .readOnly)
        let k = frame.camera.intrinsics
        let fx = Double(k[0][0]) / 2, fy = Double(k[1][1]) / 2, cx = Double(k[2][0]) / 2, cy = Double(k[2][1]) / 2
        let now = frame.timestamp
        busy = true
        queue.async { [weak self] in
            guard let self else { return }
            let pose = OnPhoneRing.livePose(gray: gray, w: w, h: h, fx: fx, fy: fy, cx: cx, cy: cy, ring: .current(), flipped: &self.flipped)
            let reading = self.update(pose, now)
            DispatchQueue.main.async {
                self.busy = false
                self.onReading?(reading)
                if reading.level == 3 && now - self.warnedAt > 4 { self.warnedAt = now; UINotificationFeedbackGenerator().notificationOccurred(.warning) }
            }
        }
    }

    private func update(_ pose: (SIMD3<Double>, Int, Double)?, _ now: Double) -> Reading {
        if started == 0 { started = now }
        let turns = travelled / 360
        guard let (c, markers, _) = pose else {
            if now - max(lastSeen, started) > 1.2 { return Reading(text: "Ring lost: keep 4 or more markers in view", level: 3, turns: turns) }
            return Reading(text: Self.describe(rate, turns), level: Self.level(rate), turns: turns)
        }
        lastSeen = now
        let az = atan2(c.y, c.x)
        if let last = lastAz, now - lastTime < 1.0, now - lastTime > 0.05 {
            var d = az - last
            while d > .pi { d -= 2 * .pi }
            while d < -.pi { d += 2 * .pi }
            let inst = abs(d) * 180 / .pi / (now - lastTime)
            if inst < 150 {                                   // a jump larger than this is a bad fit, not the tray
                rate = rate == 0 ? inst : 0.6 * rate + 0.4 * inst
                travelled += abs(d) * 180 / .pi
            }
            lastAz = az; lastTime = now
        } else if lastAz == nil || now - lastTime >= 1.0 {
            lastAz = az; lastTime = now
        }
        if rate < Self.slow { if slowSince == nil { slowSince = now } } else { slowSince = nil }
        let dist = simd_length(c)
        if dist < 180 { return Reading(text: "Too close: move the phone back a little", level: 2, turns: turns) }
        if dist > 420 { return Reading(text: "Too far: move the phone closer", level: 2, turns: turns) }
        if markers < 16 && rate < Self.fast { return Reading(text: Self.describe(rate, turns) + " · few markers in view", level: max(2, Self.level(rate)), turns: turns) }
        if let s = slowSince, now - s > 3, started > 0, travelled > 20 || now - started > 6 {
            return Reading(text: "Too slow or stopped: keep the tray turning", level: 2, turns: turns)
        }
        return Reading(text: Self.describe(rate, turns), level: Self.level(rate), turns: turns)
    }

    private static func level(_ r: Double) -> Int { r > fast ? 3 : (r > ideal ? 2 : 1) }

    private static func describe(_ r: Double, _ turns: Double) -> String {
        let t = String(format: " · %.1f turns", turns)
        if r == 0 { return "Turn the tray slowly" + t }
        let v = String(format: "%.0f°/s", r)
        if r > fast { return "Too fast (\(v)): slow the tray down" + t }
        if r > ideal { return "A little fast (\(v))" + t }
        return "Good speed (\(v))" + t
    }
}
