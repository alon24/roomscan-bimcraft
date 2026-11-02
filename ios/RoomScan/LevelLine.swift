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

/// A fixed mark in the middle of the screen and the real horizon: when the line lies on the mark, the phone is level. Green when it is.
struct LevelLine: View {
    let level: PhoneLevel?
    private let pointsPerDegree: CGFloat = 8

    var body: some View {
        GeometryReader { geo in
            if let level {
                let colour = level.isLevel ? Color.green : Color.yellow
                let cx = geo.size.width / 2, cy = geo.size.height / 2
                ZStack {
                    if !level.isSteep {
                        // the fixed mark: two short lines either side of the crosshair
                        Path { p in
                            p.move(to: CGPoint(x: cx - 120, y: cy)); p.addLine(to: CGPoint(x: cx - 50, y: cy))
                            p.move(to: CGPoint(x: cx + 50, y: cy)); p.addLine(to: CGPoint(x: cx + 120, y: cy))
                        }
                        .stroke(.white.opacity(0.9), lineWidth: 2)
                        // the horizon: below the middle when the camera points above it
                        Rectangle().fill(colour).frame(width: geo.size.width * 0.8, height: 2).shadow(radius: 1)
                            .rotationEffect(.degrees(level.roll))
                            .position(x: cx, y: cy + max(-160, min(160, CGFloat(level.pitch) * pointsPerDegree)))
                    }
                    Text(words(level)).font(.caption.bold().monospacedDigit())
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .foregroundStyle(.white)
                        .background((level.isLevel ? Color.green : Color.black).opacity(0.6), in: Capsule())
                        .position(x: cx, y: cy - 58)
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
