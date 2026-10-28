import SwiftUI
import simd

/// A spot pinned in the room. Backed by an ARKit anchor named `pin-N`, so it stays put while the phone moves.
struct Pin: Identifiable, Equatable {
    let name: String
    var pos: SIMD3<Float>
    var id: String { name }
    var number: Int { Int(name.dropFirst(4)) ?? 0 }
}

/// A line the user chose to keep between two pins. Pins are independent; nothing joins them unless it is here.
struct PinLink: Codable, Hashable {
    var a: String
    var b: String
}

/// Numbers for the selected pins, in the order they were selected. All lengths are raw sensor metres;
/// the caller applies the calibration (`leg`, and its square for the area).
struct SelStats {
    var distance: Double?
    var horizontal: Double?   // along the floor
    var vertical: Double?     // height difference (ARKit world Y is up)
    var path: Double = 0      // 1 to 2 to 3 ... open
    var perimeter: Double?    // path closed back to the first
    var area: Double?         // polygon in selection order, flattened onto its own plane
    var angles: [Double] = [] // at each pin, degrees, polygon order

    init(_ p: [SIMD3<Float>]) {
        guard p.count >= 2 else { return }
        func len(_ a: SIMD3<Float>, _ b: SIMD3<Float>) -> Double { Double(simd_length(a - b)) }
        for i in 1..<p.count { path += len(p[i - 1], p[i]) }
        if p.count == 2 {
            distance = len(p[0], p[1])
            let d = p[1] - p[0]
            vertical = Double(abs(d.y))
            horizontal = Double(simd_length(SIMD3(d.x, 0, d.z)))
        }
        if p.count >= 3 {
            perimeter = path + len(p[p.count - 1], p[0])
            var n = SIMD3<Float>(0, 0, 0)
            for i in 0..<p.count { n += simd_cross(p[i] - p[0], p[(i + 1) % p.count] - p[0]) }
            area = Double(simd_length(n)) / 2
            for i in 0..<p.count {
                let prev = p[(i + p.count - 1) % p.count] - p[i], next = p[(i + 1) % p.count] - p[i]
                let den = simd_length(prev) * simd_length(next)
                guard den > 1e-6 else { angles.append(0); continue }
                angles.append(Double(acos(max(-1, min(1, simd_dot(prev, next) / den)))) * 180 / .pi)
            }
        }
    }
}

/// Manage the pins: choose which ones to measure, name them, link pairs you want to keep, delete what you don't need.
struct PinsView: View {
    @ObservedObject var model: MeasureModel
    @Environment(\.dismiss) private var dismiss
    @State private var renaming: Pin?
    @State private var newName = ""
    @State private var confirmClear = false

    private func dist(_ p: Pin) -> String {
        MeasureView.text(Double(simd_length(p.pos - model.camPos)), .wall).big
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("Tick the pins you want to measure. Two ticked: the distance between them. Three or more: perimeter, area and angles, in the order you ticked them. Pins are not joined unless you link them.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section("Pins (\(model.pins.count))") {
                    if model.pins.isEmpty { Text("Aim the bullseye at a spot on the camera picture and tap it to pin.").foregroundStyle(.secondary) }
                    ForEach(model.pins) { p in
                        Button { model.toggleSelect(p.name) } label: {
                            HStack {
                                Image(systemName: model.selected.contains(p.name) ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(model.selected.contains(p.name) ? Color.orange : .secondary)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text("Pin \(p.number)" + (model.labels[p.name].map { $0.isEmpty ? "" : " · " + $0 } ?? "")).foregroundStyle(.primary)
                                    Text("\(dist(p)) from the camera now").font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if let i = model.selected.firstIndex(of: p.name) { Text("\(i + 1)").font(.caption.bold()).foregroundStyle(.orange) }
                            }
                        }
                        .swipeActions(edge: .trailing) {
                            Button(role: .destructive) { model.deletePin(p.name) } label: { Label("Delete", systemImage: "trash") }
                            Button { renaming = p; newName = model.labels[p.name] ?? "" } label: { Label("Name", systemImage: "pencil") }.tint(Theme.accent)
                        }
                    }
                }
                Section("Linked pairs") {
                    if model.selected.count == 2 {
                        Button(model.isLinked(model.selected[0], model.selected[1]) ? "Unlink the two ticked pins" : "Link the two ticked pins") { model.toggleLink() }
                    }
                    if model.links.isEmpty { Text("None. A link keeps a measured line drawn between two pins.").font(.caption).foregroundStyle(.secondary) }
                    ForEach(model.links, id: \.self) { l in
                        if let a = model.pin(l.a), let b = model.pin(l.b) {
                            Text("Pin \(a.number) — pin \(b.number)  ·  " + MeasureView.text(Double(simd_length(a.pos - b.pos)), .span).big)
                        } else {
                            Text("\(l.a) — \(l.b)  ·  waiting to find the saved place").foregroundStyle(.secondary)
                        }
                    }
                    .onDelete { model.links.remove(atOffsets: $0); model.saveMeta() }
                }
                Section {
                    Button("Untick all") { model.selected = [] }.disabled(model.selected.isEmpty)
                    Button("Delete all pins", role: .destructive) { confirmClear = true }.disabled(model.pins.isEmpty)
                }
            }
            .navigationTitle("Pins")
            .toolbar { Button("Done") { dismiss() } }
            .alert("Name this pin", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
                TextField("e.g. left wall, window top", text: $newName)
                Button("Save") { if let r = renaming { model.rename(r.name, newName) }; renaming = nil }
                Button("Cancel", role: .cancel) { renaming = nil }
            }
            .confirmationDialog("Delete every pin and forget the saved room?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Delete all pins", role: .destructive) { model.clearPins() }
            }
        }
    }
}
