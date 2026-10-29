import SwiftUI
import SceneKit

/// Everything one scan uploads, so it can be sent now or kept on the phone and sent later.
struct ScanPackage {
    var name: String
    var kind: String
    var json: URL? = nil
    var usdz: URL? = nil
    var mesh: URL? = nil
    var footprint: URL? = nil
    var photosMeta: URL? = nil
    var photos: [URL] = []
    var masks: [URL] = []
    var realHeightMM: Double? = nil
    var turntableScan = false
    var ringScan = false
    var info: [String: Any]? = nil
}

extension Uploader {
    static func upload(_ p: ScanPackage, server: String) async throws -> String? {
        try await upload(json: p.json, usdz: p.usdz, name: p.name, server: server, kind: p.kind, photos: p.photos, photosMeta: p.photosMeta,
                         mesh: p.mesh, footprint: p.footprint, realHeightMM: p.realHeightMM, turntableScan: p.turntableScan,
                         ringScan: p.ringScan, info: p.info, masks: p.masks)
    }
}

/// A scan kept on the phone (Documents/SavedScans/<id>/), with its files and what is needed to upload it.
struct SavedScan: Codable, Identifiable, Hashable {
    var id: String
    var name: String
    var kind: String
    var created: Date
    var json: String?
    var usdz: String?
    var mesh: String?
    var footprint: String?
    var photosMeta: String?
    var photos: [String]
    var masks: [String]
    var realHeightMM: Double?
    var turntableScan: Bool
    var ringScan: Bool
    var info: Data?
    var uploadedID: String?
    var uploadedAt: Date?

    var kindLabel: String {
        switch kind {
        case "room": return "Floor plan"
        case "object": return "Object (without the ring)"
        default: return ringScan ? "Object with the ring" : "3D room"
        }
    }
}

/// Manifests written by an older app may lack keys added later: every key but the id has a default, so such a scan still lists and uploads.
extension SavedScan {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? "Saved scan"
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "mesh"
        created = (try? c.decodeIfPresent(Date.self, forKey: .created)) ?? Date.distantPast
        json = try? c.decodeIfPresent(String.self, forKey: .json)
        usdz = try? c.decodeIfPresent(String.self, forKey: .usdz)
        mesh = try? c.decodeIfPresent(String.self, forKey: .mesh)
        footprint = try? c.decodeIfPresent(String.self, forKey: .footprint)
        photosMeta = try? c.decodeIfPresent(String.self, forKey: .photosMeta)
        photos = (try? c.decodeIfPresent([String].self, forKey: .photos)) ?? []
        masks = (try? c.decodeIfPresent([String].self, forKey: .masks)) ?? []
        realHeightMM = try? c.decodeIfPresent(Double.self, forKey: .realHeightMM)
        turntableScan = (try? c.decodeIfPresent(Bool.self, forKey: .turntableScan)) ?? false
        ringScan = (try? c.decodeIfPresent(Bool.self, forKey: .ringScan)) ?? false
        info = try? c.decodeIfPresent(Data.self, forKey: .info)
        uploadedID = try? c.decodeIfPresent(String.self, forKey: .uploadedID)
        uploadedAt = try? c.decodeIfPresent(Date.self, forKey: .uploadedAt)
    }
}

enum LocalStore {
    static var root: URL {
        let r = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("SavedScans")
        try? FileManager.default.createDirectory(at: r, withIntermediateDirectories: true)
        return r
    }

    static func dir(_ id: String) -> URL { root.appendingPathComponent(id) }

    /// Copy a scan's files into the phone's own storage. They stay until you delete them.
    @discardableResult
    static func save(_ p: ScanPackage) throws -> SavedScan {
        let id = UUID().uuidString
        let d = dir(id)
        let fm = FileManager.default
        try fm.createDirectory(at: d.appendingPathComponent("photos"), withIntermediateDirectories: true)
        try fm.createDirectory(at: d.appendingPathComponent("masks"), withIntermediateDirectories: true)
        func keep(_ u: URL?, _ sub: String = "") throws -> String? {
            guard let u else { return nil }
            let rel = sub.isEmpty ? u.lastPathComponent : sub + "/" + u.lastPathComponent
            try fm.copyItem(at: u, to: d.appendingPathComponent(rel))
            return rel
        }
        let s = SavedScan(id: id, name: p.name, kind: p.kind, created: Date(),
                          json: try keep(p.json), usdz: try keep(p.usdz), mesh: try keep(p.mesh), footprint: try keep(p.footprint),
                          photosMeta: try keep(p.photosMeta), photos: try p.photos.compactMap { try keep($0, "photos") },
                          masks: try p.masks.compactMap { try keep($0, "masks") }, realHeightMM: p.realHeightMM,
                          turntableScan: p.turntableScan, ringScan: p.ringScan,
                          info: p.info.flatMap { try? JSONSerialization.data(withJSONObject: $0) }, uploadedID: nil, uploadedAt: nil)
        try write(s)
        NotificationCenter.default.post(name: .savedScansChanged, object: nil)
        return s
    }

    static func write(_ s: SavedScan) throws {
        let enc = JSONEncoder()
        enc.dateEncodingStrategy = .iso8601
        try enc.encode(s).write(to: dir(s.id).appendingPathComponent("manifest.json"))
    }

    static func list() -> [SavedScan] {
        let dec = JSONDecoder()
        dec.dateDecodingStrategy = .iso8601
        let dirs = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        return dirs.compactMap { d -> SavedScan? in
            guard var s = try? dec.decode(SavedScan.self, from: Data(contentsOf: d.appendingPathComponent("manifest.json"))) else { return nil }
            s.id = d.lastPathComponent      // the folder is the truth: a folder copied or renamed in Files still opens and uploads
            return s
        }
        .sorted { $0.created > $1.created }
    }

    static func delete(_ s: SavedScan) {
        try? FileManager.default.removeItem(at: dir(s.id))
        NotificationCenter.default.post(name: .savedScansChanged, object: nil)
    }

    static func rename(_ s: SavedScan, to name: String) {
        var t = s; t.name = name
        try? write(t)
        NotificationCenter.default.post(name: .savedScansChanged, object: nil)
    }

    static func package(_ s: SavedScan) -> ScanPackage {
        let d = dir(s.id)
        // A file that is gone (deleted in Files, or never written by an older app) is left out instead of failing the whole upload.
        func u(_ rel: String?) -> URL? {
            guard let rel else { return nil }
            let url = d.appendingPathComponent(rel)
            return FileManager.default.fileExists(atPath: url.path) ? url : nil
        }
        return ScanPackage(name: s.name, kind: s.kind, json: u(s.json), usdz: u(s.usdz), mesh: u(s.mesh), footprint: u(s.footprint),
                           photosMeta: u(s.photosMeta), photos: s.photos.compactMap { u($0) }, masks: s.masks.compactMap { u($0) },
                           realHeightMM: s.realHeightMM, turntableScan: s.turntableScan, ringScan: s.ringScan,
                           info: s.info.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] })
    }

    static func upload(_ s: SavedScan, server: String) async throws -> String? {
        let id = try await Uploader.upload(package(s), server: server)
        var t = s; t.uploadedID = id; t.uploadedAt = Date()
        try? write(t)
        NotificationCenter.default.post(name: .savedScansChanged, object: nil)
        return id
    }

    static func bytes(_ s: SavedScan) -> Int64 {
        let e = FileManager.default.enumerator(at: dir(s.id), includingPropertiesForKeys: [.fileSizeKey])
        var total: Int64 = 0
        while let f = e?.nextObject() as? URL { total += Int64((try? f.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
        return total
    }

    /// The whole scan folder as one zip file, to share or keep in Files.
    static func zip(_ s: SavedScan) -> URL? {
        var out: URL?
        var err: NSError?
        NSFileCoordinator().coordinate(readingItemAt: dir(s.id), options: .forUploading, error: &err) { tmp in
            let dest = FileManager.default.temporaryDirectory.appendingPathComponent("\(s.name.replacingOccurrences(of: "/", with: "-")).zip")
            try? FileManager.default.removeItem(at: dest)
            if (try? FileManager.default.copyItem(at: tmp, to: dest)) != nil { out = dest }
        }
        return out
    }
}

extension Notification.Name {
    static let savedScansChanged = Notification.Name("savedScansChanged")
}

@MainActor
final class SavedStore: ObservableObject {
    @Published var items: [SavedScan] = []
    @Published var busy: Set<String> = []
    @Published var message = ""
    private var observer: Any?

    init() {
        reload()
        observer = NotificationCenter.default.addObserver(forName: .savedScansChanged, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.reload() }
        }
    }

    func reload() { items = LocalStore.list() }

    func upload(_ s: SavedScan, server: String) async {
        busy.insert(s.id)
        do {
            _ = try await LocalStore.upload(s, server: server)
            message = "Uploaded \(s.name)"
        } catch {
            message = "Could not upload \(s.name): \(error.localizedDescription)"
        }
        busy.remove(s.id)
        reload()
    }

    func uploadAll(server: String) async {
        for s in items where s.uploadedID == nil { await upload(s, server: server) }
    }
}

/// "On this phone": scans saved for later, at the top of the Models tab.
struct SavedScansSection: View {
    @StateObject private var store = SavedStore()
    @AppStorage("serverURL") private var serverURL = ServerAddress.defaultURL

    var body: some View {
        Section {
            if store.items.isEmpty {
                Text("Nothing saved. After a scan, tap Save to keep it here and upload it later.").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(store.items) { s in
                NavigationLink { SavedScanDetail(scan: s, store: store) } label: {
                    HStack(spacing: 12) {
                        Image(systemName: kindIcon(s.kind, ring: s.ringScan)).font(.title3).foregroundStyle(Theme.accent).frame(width: 28)
                        VStack(alignment: .leading) {
                            Text(s.name)
                            Text("\(s.kindLabel) · \(s.photos.filter { $0.hasSuffix(".jpg") }.count) photos · \(ByteCountFormatter.string(fromByteCount: LocalStore.bytes(s), countStyle: .file))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if store.busy.contains(s.id) { ProgressView() }
                        else if s.uploadedID != nil { Image(systemName: "checkmark.icloud").foregroundStyle(.green) }
                        else { Image(systemName: "icloud.and.arrow.up").foregroundStyle(.orange) }
                    }
                }
                .swipeActions {
                    Button("Delete", role: .destructive) { LocalStore.delete(s) }
                    Button("Upload") { Task { await store.upload(s, server: serverURL) } }.tint(.blue)
                }
            }
            if store.items.contains(where: { $0.uploadedID == nil }) {
                Button("Upload all that are not uploaded") { Task { await store.uploadAll(server: serverURL) } }
            }
            if !store.message.isEmpty { Text(store.message).font(.caption) }
        } header: {
            Text("On this phone")
        } footer: {
            Text("Saved scans stay on the phone until you delete them. Swipe left on one to upload or delete it.")
        }
    }
}

struct SavedScanDetail: View {
    let scan: SavedScan
    @ObservedObject var store: SavedStore
    @AppStorage("serverURL") private var serverURL = ServerAddress.defaultURL
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var zipURL: URL?
    @State private var preview: SCNScene?
    @State private var previewNote = "Building the preview…"
    @State private var showAR = false

    var body: some View {
        Form {
            Section {
                if let preview {
                    OrbitSceneView(scene: preview).frame(height: 360).listRowInsets(EdgeInsets())
                    Text("What the phone captured: one finger turns, pinch zooms, two fingers move. Upload makes the finished model.")
                        .font(.caption2).foregroundStyle(.secondary)
                } else {
                    Text(previewNote)
                }
            }
            if let usdz = scan.usdz {
                Section { Button { showAR = true } label: { Label("View in AR (real size)", systemImage: "arkit") } }
                    .fullScreenCover(isPresented: $showAR) { ARPreview(url: LocalStore.dir(scan.id).appendingPathComponent(usdz)).ignoresSafeArea() }
            }
            Section("Name") {
                TextField("Name", text: $name).onSubmit { LocalStore.rename(scan, to: name) }
                Button("Save the name") { LocalStore.rename(scan, to: name) }.disabled(name == scan.name || name.isEmpty)
            }
            Section("What is in it") {
                LabeledContent("Type", value: scan.kindLabel)
                LabeledContent("Made", value: scan.created.formatted())
                LabeledContent("Photos", value: "\(scan.photos.filter { $0.hasSuffix(".jpg") }.count)")
                LabeledContent("LiDAR depth frames", value: "\(scan.photos.filter { $0.hasSuffix(".depth") }.count)")
                if !scan.masks.isEmpty { LabeledContent("Part outlines", value: "\(scan.masks.count)") }
                LabeledContent("Size on the phone", value: ByteCountFormatter.string(fromByteCount: LocalStore.bytes(scan), countStyle: .file))
                LabeledContent("Uploaded", value: scan.uploadedAt.map { "yes, \($0.formatted())" } ?? "not yet")
            }
            Section {
                Button(store.busy.contains(scan.id) ? "Uploading…" : (scan.uploadedID == nil ? "Upload now" : "Upload again")) {
                    Task { await store.upload(scan, server: serverURL) }
                }
                .disabled(store.busy.contains(scan.id))
                if let zipURL { ShareLink(item: zipURL) { Label("Share as zip", systemImage: "square.and.arrow.up") } }
                else { Button("Make a zip to share") { zipURL = LocalStore.zip(scan) } }
                Button("Delete from the phone", role: .destructive) { LocalStore.delete(scan); dismiss() }
            }
            if !store.message.isEmpty { Text(store.message).font(.caption) }
        }
        .navigationTitle(scan.name)
        .onAppear { name = scan.name }
        .task {
            let id = scan
            let built = await Task.detached(priority: .userInitiated) { SavedPreview.scene(for: id) }.value
            preview = built
            if built == nil { previewNote = "No 3D preview for this scan: it has no model file, only photos and depth. Upload it to see the server's model." }
        }
    }
}
