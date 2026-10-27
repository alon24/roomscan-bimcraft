import SwiftUI
import SceneKit
import SceneKit.ModelIO
import ModelIO

/// The server's finished models, in the app: turn them in 3D, see their size and settings, share the STL / 3MF.
struct ScanSummary: Identifiable, Decodable, Hashable {
    let id: String
    let name: String
    let kind: String
    let status: String

    enum CodingKeys: String, CodingKey { case id, name, kind, status }

    /// Old scans may lack some fields: one of them must not hide the whole list.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = (try? c.decode(String.self, forKey: .name)) ?? id
        kind = (try? c.decode(String.self, forKey: .kind)) ?? "room"
        status = (try? c.decode(String.self, forKey: .status)) ?? "?"
    }
}

@MainActor
final class ModelsModel: ObservableObject {
    @Published var scans: [ScanSummary] = []
    @Published var error = ""

    func load(server: String) async {
        guard let url = URL(string: server)?.appendingPathComponent("api/scans") else { error = "Bad server address"; return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            scans = try JSONDecoder().decode([ScanSummary].self, from: data)
            error = ""
        } catch {
            self.error = error is DecodingError ? "The server's list could not be read: \(error)" : "Cannot reach the server at \(server): \(error.localizedDescription)"
        }
    }
}

struct ModelsView: View {
    @StateObject private var model = ModelsModel()
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"

    var body: some View {
        List {
            if !model.error.isEmpty { Text(model.error).foregroundStyle(.red) }
            ForEach(model.scans) { s in
                NavigationLink {
                    ModelDetailView(scan: s)
                } label: {
                    VStack(alignment: .leading) {
                        Text(s.name)
                        Text("\(s.kind) · \(s.status)").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .refreshable { await model.load(server: serverURL) }
        .task { await model.load(server: serverURL) }
    }
}

struct ModelDetailView: View {
    let scan: ScanSummary
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @State private var scene: SCNScene?
    @State private var note = "Downloading the model…"
    @State private var size = ""
    @State private var details: [(String, String)] = []
    @State private var stl: URL?
    @State private var threeMF: URL?

    var body: some View {
        List {
            Section {
                if let scene {
                    OrbitSceneView(scene: scene).frame(height: 380).listRowInsets(EdgeInsets())
                    Text("One finger turns, pinch zooms, two fingers move, double-tap resets").font(.caption2).foregroundStyle(.secondary)
                } else {
                    Text(note)
                }
                if !size.isEmpty { Text(size).font(.callout.weight(.semibold)) }
                HStack {
                    if let stl { ShareLink(item: stl) { Label("STL", systemImage: "square.and.arrow.up") } }
                    if let threeMF { ShareLink(item: threeMF) { Label("3MF", systemImage: "square.and.arrow.up") } }
                }
            }
            Section {
                NavigationLink("Open the full page (floor plan, frames, measure, downloads)") { WebPage(scanID: scan.id) }
            }
            if !details.isEmpty {
                Section("Settings used for this model") {
                    ForEach(details, id: \.0) { k, v in
                        HStack(alignment: .top) { Text(k).foregroundStyle(.secondary); Spacer(); Text(v).multilineTextAlignment(.trailing) }
                            .font(.caption)
                    }
                }
            }
        }
        .navigationTitle(scan.name)
        .task { await load() }
    }

    private func fetch(_ file: String) async -> URL? {
        guard let url = URL(string: serverURL)?.appendingPathComponent("api/scans/\(scan.id)/\(file)"),
              let (tmp, resp) = try? await URLSession.shared.download(from: url),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        let dest = FileManager.default.temporaryDirectory.appendingPathComponent("\(scan.id)-\(file)")
        try? FileManager.default.removeItem(at: dest)
        return (try? FileManager.default.moveItem(at: tmp, to: dest)) != nil ? dest : nil
    }

    private func json(_ file: String) async -> [String: Any]? {
        guard let url = URL(string: serverURL)?.appendingPathComponent(file),
              let (data, _) = try? await URLSession.shared.data(from: url) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    private func load() async {
        if let rep = await json("api/scans/\(scan.id)/report.json") {
            if let mm = rep["size_mm"] as? [Double] { size = "Size " + mm.map { String(format: "%.1f", $0) }.joined(separator: " × ") + " mm" }
            else if let m = rep["size_m"] as? [Double] { size = "Size " + m.map { String(format: "%.0f", $0 * 1000) }.joined(separator: " × ") + " mm" }
        }
        if let meta = await json("api/scans/\(scan.id)") {
            var rows: [(String, String)] = []
            for key in ["settings", "phone"] {
                for (k, v) in (meta[key] as? [String: Any] ?? [:]).sorted(by: { $0.key < $1.key }) {
                    rows.append(((key == "phone" ? "phone: " : "") + k.replacingOccurrences(of: "_", with: " "), "\(v)"))
                }
            }
            details = rows
        }
        stl = await fetch("model.stl")
        threeMF = await fetch("model.3mf")
        // show the coloured model when there is one, else the printable one
        for file in scan.kind == "mesh" ? ["colored.ply", "model.stl"] : ["model.stl"] {
            var local = stl
            if file != "model.stl" { local = await fetch(file) }
            if let local, let s = Self.scene(local) { scene = s; return }
        }
        note = scan.kind == "room" ? "Floor plans are shown on the full page below." : "No 3D model for this scan yet."
    }

    private static func scene(_ url: URL) -> SCNScene? {
        let asset = MDLAsset(url: url)
        guard asset.count > 0 else { return nil }
        let s = SCNScene(mdlAsset: asset)
        s.background.contents = UIColor(white: 0.1, alpha: 1)
        return s
    }
}

/// The server's page for one scan, inside the app.
struct WebPage: View {
    let scanID: String
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @State private var reload = 0

    var body: some View {
        Group {
            if let url = URL(string: serverURL + "/?embed=1&scan=" + scanID) {
                WebViewer(url: url).id(reload).ignoresSafeArea(edges: .bottom)
            } else {
                Text("Set a valid server address in Settings")
            }
        }
        .toolbar { Button("Reload") { reload += 1 } }
    }
}
