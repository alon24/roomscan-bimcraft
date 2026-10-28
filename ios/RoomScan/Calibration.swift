import SwiftUI

/// What a reading is: camera to a wall (a distance), or between two pins (a leg), or already final (no correction).
enum CalKind: String, Codable { case wall, span, none }

/// One tape check: what the app read (`raw`) against what the tape says (`truth`), both in metres.
struct CalPoint: Codable, Identifiable {
    var id = UUID()
    var kind: CalKind
    var raw: Double
    var truth: Double
}

/// The user's own calibration. `truth = a * raw + b` is fitted from their tape checks; nothing is hidden: points can be
/// deleted, the correction switched off, and a fit that looks wrong is not applied unless they say so.
final class CalibrationStore: ObservableObject {
    static let shared = CalibrationStore()

    struct Fit { var a: Double; var b: Double; var n: Int; var rmsBefore: Double; var rmsAfter: Double
        var suspicious: Bool { abs(a - 1) > 0.1 }
    }

    @Published var points: [CalPoint] { didSet { save() } }
    @Published var enabled: Bool { didSet { UserDefaults.standard.set(enabled, forKey: "calEnabled") } }
    @Published var allowOdd: Bool { didSet { UserDefaults.standard.set(allowOdd, forKey: "calAllowOdd") } }

    init() {
        let d = UserDefaults.standard
        points = d.data(forKey: "calPoints").flatMap { try? JSONDecoder().decode([CalPoint].self, from: $0) } ?? []
        enabled = d.object(forKey: "calEnabled") as? Bool ?? true
        allowOdd = d.bool(forKey: "calAllowOdd")
    }

    private func save() {
        if let d = try? JSONEncoder().encode(points) { UserDefaults.standard.set(d, forKey: "calPoints") }
    }

    /// Least-squares fit for one kind of check. A camera-to-wall fit gets a scale and an offset when the checks cover at
    /// least 30 cm of range; otherwise (and always for pin-to-pin) only a scale, so one point cannot invent an offset.
    func fit(_ kind: CalKind) -> Fit? {
        let p = points.filter { $0.kind == kind }
        guard !p.isEmpty else { return nil }
        let n = Double(p.count)
        let sx = p.reduce(0) { $0 + $1.raw }, sy = p.reduce(0) { $0 + $1.truth }
        let sxx = p.reduce(0) { $0 + $1.raw * $1.raw }, sxy = p.reduce(0) { $0 + $1.raw * $1.truth }
        let range = (p.map(\.raw).max() ?? 0) - (p.map(\.raw).min() ?? 0)
        var a = sxy / max(sxx, 1e-9), b = 0.0
        if kind == .wall, p.count >= 2, range >= 0.3 {
            let den = n * sxx - sx * sx
            if abs(den) > 1e-9 { a = (n * sxy - sx * sy) / den; b = (sy - a * sx) / n }
        }
        func rms(_ f: (CalPoint) -> Double) -> Double { (p.reduce(0) { $0 + f($1) * f($1) } / n).squareRoot() }
        return Fit(a: a, b: b, n: p.count, rmsBefore: rms { $0.raw - $0.truth }, rmsAfter: rms { a * $0.raw + b - $0.truth })
    }

    private func usable(_ f: Fit?) -> Fit? {
        guard enabled, let f, !f.suspicious || allowOdd else { return nil }
        return f
    }

    /// Camera-to-wall distance, corrected.
    func wall(_ raw: Double) -> Double {
        guard let f = usable(fit(.wall)) else { return raw }
        return f.a * raw + f.b
    }

    /// Pin-to-pin length, corrected. Uses the pin fit, else the wall fit's scale (the same depth sensor made both).
    func leg(_ raw: Double) -> Double {
        if let f = usable(fit(.span)) { return f.a * raw }
        if let f = usable(fit(.wall)) { return f.a * raw }
        return raw
    }

    /// Short line shown under every reading.
    var summary: String {
        guard enabled else { return "correction off · tap to calibrate" }
        let f = fit(.wall) ?? fit(.span)
        guard let f else { return "uncalibrated · tap to calibrate" }
        if f.suspicious && !allowOdd { return "correction looks wrong, not applied · tap" }
        return f.n >= 2 ? String(format: "calibrated · typical error ±%.1f cm · tap", max(f.rmsAfter, 0.001) * 100) : "calibrated on 1 point · tap"
    }
}

/// Calibrate against a tape measure. Two kinds of check; add several at different distances.
struct CalibrationView: View {
    @ObservedObject var model: MeasureModel
    @ObservedObject private var cal = CalibrationStore.shared
    @Environment(\.dismiss) private var dismiss
    @State private var kind: CalKind = .wall
    @State private var tape = ""
    @State private var unit = 0   // 0 cm, 1 mm, 2 m

    private var raw: Double? { kind == .wall ? model.distance : model.pinSpan }

    private var truth: Double? {
        guard let v = Double(tape.replacingOccurrences(of: ",", with: ".")), v > 0 else { return nil }
        return [0.01, 0.001, 1][unit] * v
    }

    private func cm(_ m: Double) -> String { String(format: "%.2f", m * 100) }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Check", selection: $kind) {
                        Text("Camera to wall").tag(CalKind.wall)
                        Text("Between two pins").tag(CalKind.span)
                    }.pickerStyle(.segmented)
                    if kind == .wall {
                        Text("Hold the tape's zero at the **back camera lens** (the round lenses on the back, not the phone edge) and point the crosshair at the wall straight on. Wait for the green dot, read the tape, type it below. Repeat at 0.5 m, 1 m, 2 m, 3 m.")
                    } else {
                        Text("Pin the two ends of something you can measure with a tape (a door width, a tape laid flat), then tap both pins so they are selected. Type the tape length below. This also checks how well the phone tracked while you moved.")
                    }
                }.font(.footnote)

                Section("Now") {
                    HStack {
                        Text("App reads")
                        Spacer()
                        Text(raw.map { String(format: "%.3f m", $0) } ?? (kind == .wall ? "point at a wall" : "select 2 pins")).monospacedDigit()
                    }
                    if kind == .wall, raw != nil {
                        Label(model.steady ? "Steady" : "Hold still…", systemImage: "circle.fill").foregroundStyle(model.steady ? .green : .orange).font(.caption)
                    }
                    HStack {
                        TextField("Tape says", text: $tape).keyboardType(.decimalPad)
                        Picker("", selection: $unit) { Text("cm").tag(0); Text("mm").tag(1); Text("m").tag(2) }
                            .pickerStyle(.segmented).frame(width: 130)
                    }
                    Button("Add this check") {
                        if let r = raw, let t = truth { cal.points.append(CalPoint(kind: kind, raw: r, truth: t)); tape = "" }
                    }
                    .disabled(raw == nil || truth == nil || (kind == .wall && !model.steady))
                }

                Section("Your checks") {
                    if cal.points.isEmpty { Text("None yet.").foregroundStyle(.secondary) }
                    ForEach(cal.points) { p in
                        let f = cal.fit(p.kind)
                        let after = f.map { $0.a * p.raw + $0.b } ?? p.raw
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(p.kind == .wall ? "Wall" : "Pins")  app \(cm(p.raw)) cm · tape \(cm(p.truth)) cm").font(.subheadline).monospacedDigit()
                            Text("error \(String(format: "%+.2f", (p.raw - p.truth) * 100)) cm → \(String(format: "%+.2f", (after - p.truth) * 100)) cm after correction")
                                .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                        }
                    }
                    .onDelete { cal.points.remove(atOffsets: $0) }
                }

                Section("Result") {
                    ForEach([CalKind.wall, .span], id: \.rawValue) { k in
                        if let f = cal.fit(k) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(k == .wall ? "Camera to wall" : "Between pins").font(.subheadline.bold())
                                Text(String(format: "%d check(s): error before ±%.2f cm, after ±%.2f cm", f.n, f.rmsBefore * 100, f.rmsAfter * 100)).font(.caption)
                                Text(String(format: "scale %.4f", f.a) + (k == .wall && f.b != 0 ? String(format: ", offset %+.1f mm", f.b * 1000) : "")).font(.caption).foregroundStyle(.secondary)
                                if f.suspicious {
                                    Text("The scale is more than 10% from 1. That is almost always a mistyped tape value. Delete the bad check, or turn on the switch below.")
                                        .font(.caption).foregroundStyle(.red)
                                }
                            }
                        }
                    }
                    Toggle("Use the correction", isOn: $cal.enabled)
                    Toggle("Apply even if it looks wrong", isOn: $cal.allowOdd)
                    Button("Delete all checks", role: .destructive) { cal.points = [] }.disabled(cal.points.isEmpty)
                }
            }
            .navigationTitle("Calibrate")
            .toolbar { Button("Done") { dismiss() } }
        }
    }
}
