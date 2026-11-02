import SwiftUI
import simd

/// How the phone is held, from gravity. In ARKit the camera's +x points to the bottom of a phone held upright, +y to its right, +z out of the screen.
struct PhoneLevel: Equatable {
    var roll: Double      // degrees the horizon is turned on the screen, clockwise
    var pitch: Double     // degrees the camera points above the horizon

    init(_ t: simd_float4x4) {
        let right = Double(t.columns.1.y), up = -Double(t.columns.0.y), back = Double(t.columns.2.y)
        roll = atan2(right, up) * 180 / .pi
        pitch = asin(max(-1, min(1, -back))) * 180 / .pi
    }

    static let within = 1.0      // degrees that count as level
    var isLevel: Bool { abs(roll) < Self.within && abs(pitch) < Self.within }
    var isSteep: Bool { abs(pitch) > 60 }      // pointing at the floor or the ceiling: there is no horizon to show
}

/// A level like a phone camera's. The long line is the horizon's roll: it lies flat across the screen when the phone isn't leaning. Inside the
/// circle, the ladder of marks moves with the tilt: when its middle mark is on the fixed dash, the phone isn't tipped up or down. Green when both are right.
struct LevelLine: View {
    let level: PhoneLevel?
    private let radius: CGFloat = 40
    private let pointsPerDegree: CGFloat = 4

    var body: some View {
        GeometryReader { geo in
            if let level {
                let colour = level.isLevel ? Color.green : Color.yellow
                let centre = CGPoint(x: geo.size.width / 2, y: geo.size.height / 2)
                ZStack {
                    if !level.isSteep {
                        Circle().stroke(.white.opacity(0.9), lineWidth: 1.5).frame(width: radius * 2, height: radius * 2).position(centre)
                        ZStack {
                            ForEach([-10, -5, 0, 5, 10], id: \.self) { deg in
                                Rectangle().fill(.white).frame(width: deg == 0 ? 36 : (abs(deg) == 10 ? 30 : 18), height: 1.5)
                                    .offset(y: CGFloat(deg) * pointsPerDegree)
                            }
                        }
                        .frame(width: radius * 2, height: radius * 2)
                        .offset(y: max(-radius, min(radius, CGFloat(level.pitch) * pointsPerDegree)))
                        .frame(width: radius * 2, height: radius * 2)
                        .clipShape(Circle())
                        .position(centre)
                        Rectangle().fill(colour).frame(width: 14, height: 4).position(centre)        // the fixed dash
                        Rectangle().fill(colour).frame(width: geo.size.width * 0.55, height: 2.5).shadow(radius: 1)
                            .rotationEffect(.degrees(level.roll)).position(centre)
                    }
                    Text(words(level)).font(.caption.bold().monospacedDigit())
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .foregroundStyle(.white)
                        .background((level.isLevel ? Color.green : Color.black).opacity(0.6), in: Capsule())
                        .position(x: centre.x, y: centre.y - radius - 18)
                }
            }
        }
        .allowsHitTesting(false)
    }

    private func words(_ l: PhoneLevel) -> String {
        if l.isSteep { return String(format: "%.0f° off straight %@", 90 - abs(l.pitch), l.pitch > 0 ? "up" : "down") }
        if l.isLevel { return "Level" }
        var parts: [String] = []
        if abs(l.roll) >= PhoneLevel.within { parts.append(String(format: "Roll %.0f°", abs(l.roll))) }
        if abs(l.pitch) >= PhoneLevel.within { parts.append(String(format: "Tilt %@ %.0f°", l.pitch > 0 ? "down" : "up", abs(l.pitch))) }
        return parts.joined(separator: " · ")
    }
}
