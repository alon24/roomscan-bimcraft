import SwiftUI
import Foundation

enum Uploader {
    @discardableResult
    static func upload(json: URL?, usdz: URL?, name: String, server: String, kind: String = "room",
                       photos: [URL] = [], photosMeta: URL? = nil, mesh: URL? = nil, footprint: URL? = nil, realHeightMM: Double? = nil, turntableScan: Bool = false, ringScan: Bool = false, info: [String: Any]? = nil, masks: [URL] = []) async throws -> String? {
        guard let url = URL(string: server)?.appendingPathComponent("api/scans") else {
            throw URLError(.badURL)
        }
        let boundary = UUID().uuidString
        // The body goes to a file and is sent from there: a 400-frame ring scan is about 240 MB, too much to hold in memory.
        let bodyURL = FileManager.default.temporaryDirectory.appendingPathComponent("upload-\(boundary).multipart")
        guard FileManager.default.createFile(atPath: bodyURL.path, contents: nil) else { throw URLError(.cannotCreateFile) }
        TempFiles.hold(bodyURL)
        defer { TempFiles.remove(bodyURL) }
        let out = try FileHandle(forWritingTo: bodyURL)
        var closed = false
        defer { if !closed { try? out.close() } }

        func put(_ text: String) throws { try out.write(contentsOf: Data(text.utf8)) }
        func field(_ key: String, _ value: String) throws {
            try put("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n")
        }
        func file(_ key: String, _ fileURL: URL, _ filename: String, _ type: String) throws {
            try put("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"; filename=\"\(filename)\"\r\nContent-Type: \(type)\r\n\r\n")
            let input = try FileHandle(forReadingFrom: fileURL)
            defer { try? input.close() }
            // 1 MB at a time; the pool frees each chunk before the next is read
            while try autoreleasepool(invoking: { () throws -> Bool in
                guard let chunk = try input.read(upToCount: 1 << 20), !chunk.isEmpty else { return false }
                try out.write(contentsOf: chunk)
                return true
            }) {}
            try put("\r\n")
        }

        try field("name", name)
        try field("kind", kind)
        if turntableScan { try field("turntable_scan", "true") }
        if ringScan { try field("ring_scan", "true") }
        if let info, let data = try? JSONSerialization.data(withJSONObject: info), let text = String(data: data, encoding: .utf8) { try field("info", text) }
        if let h = realHeightMM, h > 0 { try field("real_height_mm", String(h)) }
        try field("project", UserDefaults.standard.string(forKey: "project") ?? "")  // the home this room or object belongs to
        if let json { try file("json", json, "scan.json", "application/json") }
        if let usdz { try file("usdz", usdz, "scan.usdz", "model/vnd.usdz+zip") }
        if let mesh { try file("mesh", mesh, "scan.ply", "application/octet-stream") }
        if let footprint { try file("footprint", footprint, "footprint.json", "application/json") }
        for p in photos { try file("photos", p, p.lastPathComponent, p.pathExtension == "depth" ? "application/octet-stream" : "image/jpeg") }
        for m in masks { try file("masks", m, m.lastPathComponent, "image/png") }
        if let photosMeta { try file("photos_meta", photosMeta, "photos.json", "application/json") }
        try put("--\(boundary)--\r\n")
        try out.close()
        closed = true

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 300
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let progress = UploadSpeed()
        defer { DispatchQueue.main.async { UploadProgress.shared.active = false } }
        let (data, resp) = try await URLSession.shared.upload(for: req, fromFile: bodyURL, delegate: progress)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        let reply = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        Notifier.scanDone(name: name, status: reply?["status"] as? String ?? "ok")
        return reply?["id"] as? String
    }

    /// Fire-and-forget partial scan for the live web view. Failures are ignored: the final upload is what counts.
    static func live(json: Data, server: String) async {
        guard let url = URL(string: server)?.appendingPathComponent("api/live") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 5
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        _ = try? await URLSession.shared.upload(for: req, from: json)
    }

    /// Photos of the printed ring plus the caliper readings; the server says whether they agree and saves them if so.
    static func calibrateRing(server: String, markerMM: Double, centreDistanceMM: Double?, photos: [URL], meta: URL) async throws -> [String: Any] {
        guard let url = URL(string: server)?.appendingPathComponent("api/ring/calibrate") else { throw URLError(.badURL) }
        let boundary = UUID().uuidString
        var body = Data()
        func field(_ key: String, _ value: String) {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n".data(using: .utf8)!)
        }
        func file(_ key: String, _ fileURL: URL, _ type: String) throws {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"; filename=\"\(fileURL.lastPathComponent)\"\r\nContent-Type: \(type)\r\n\r\n".data(using: .utf8)!)
            body.append(try Data(contentsOf: fileURL))
            body.append("\r\n".data(using: .utf8)!)
        }
        field("marker_mm", String(markerMM))
        if let c = centreDistanceMM { field("centre_distance_mm", String(c)) }
        for p in photos { try file("photos", p, p.pathExtension == "depth" ? "application/octet-stream" : "image/jpeg") }
        try file("photos_meta", meta, "application/json")
        body.append("--\(boundary)--\r\n".data(using: .utf8)!)
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 120
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (data, resp) = try await URLSession.shared.upload(for: req, from: body)
        guard (resp as? HTTPURLResponse)?.statusCode == 200,
              let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw URLError(.badServerResponse) }
        return obj
    }
}


/// How the current upload is going, shown at the bottom of every tab.
final class UploadProgress: ObservableObject {
    static let shared = UploadProgress()
    @Published var text = ""
    @Published var active = false
}

/// Counts the bytes as they are sent: amount, speed per second (smoothed) and time left.
final class UploadSpeed: NSObject, URLSessionTaskDelegate {
    private var lastBytes: Int64 = 0
    private var lastTime = Date()
    private var rate = 0.0

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64, totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        let now = Date(), dt = now.timeIntervalSince(lastTime)
        guard dt >= 0.5 || totalBytesSent == totalBytesExpectedToSend else { return }
        let instant = Double(totalBytesSent - lastBytes) / max(dt, 0.001)
        rate = rate == 0 ? instant : 0.6 * rate + 0.4 * instant
        lastBytes = totalBytesSent; lastTime = now
        let f = ByteCountFormatter()
        f.countStyle = .file
        let left = rate > 0 ? Double(totalBytesExpectedToSend - totalBytesSent) / rate : 0
        let text = "Uploading \(f.string(fromByteCount: totalBytesSent)) of \(f.string(fromByteCount: totalBytesExpectedToSend))"
            + " · \(f.string(fromByteCount: Int64(rate)))/s" + (totalBytesSent < totalBytesExpectedToSend ? " · \(Int(left.rounded())) s left" : " · the server is building the model…")
        DispatchQueue.main.async { UploadProgress.shared.text = text; UploadProgress.shared.active = true }
    }
}

struct UploadProgressLine: View {
    @ObservedObject private var p = UploadProgress.shared
    var body: some View {
        if p.active {
            Text(p.text).font(.caption.monospacedDigit()).padding(.horizontal, 10).padding(.vertical, 6)
                .background(.regularMaterial, in: Capsule())
        }
    }
}
