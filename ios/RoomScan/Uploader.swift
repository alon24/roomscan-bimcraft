import Foundation

enum Uploader {
    @discardableResult
    static func upload(json: URL?, usdz: URL?, name: String, server: String, kind: String = "room",
                       photos: [URL] = [], photosMeta: URL? = nil, mesh: URL? = nil, footprint: URL? = nil, realHeightMM: Double? = nil, turntableScan: Bool = false, ringScan: Bool = false) async throws -> String? {
        guard let url = URL(string: server)?.appendingPathComponent("api/scans") else {
            throw URLError(.badURL)
        }
        let boundary = UUID().uuidString
        var body = Data()

        func field(_ key: String, _ value: String) {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n".data(using: .utf8)!)
        }
        func file(_ key: String, _ fileURL: URL, _ filename: String, _ type: String) throws {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"; filename=\"\(filename)\"\r\nContent-Type: \(type)\r\n\r\n".data(using: .utf8)!)
            body.append(try Data(contentsOf: fileURL))
            body.append("\r\n".data(using: .utf8)!)
        }

        field("name", name)
        field("kind", kind)
        if turntableScan { field("turntable_scan", "true") }
        if ringScan { field("ring_scan", "true") }
        if let h = realHeightMM, h > 0 { field("real_height_mm", String(h)) }
        field("project", UserDefaults.standard.string(forKey: "project") ?? "")  // the home this room or object belongs to
        if let json { try file("json", json, "scan.json", "application/json") }
        if let usdz { try file("usdz", usdz, "scan.usdz", "model/vnd.usdz+zip") }
        if let mesh { try file("mesh", mesh, "scan.ply", "application/octet-stream") }
        if let footprint { try file("footprint", footprint, "footprint.json", "application/json") }
        for p in photos { try file("photos", p, p.lastPathComponent, p.pathExtension == "depth" ? "application/octet-stream" : "image/jpeg") }
        if let photosMeta { try file("photos_meta", photosMeta, "photos.json", "application/json") }
        body.append("--\(boundary)--\r\n".data(using: .utf8)!)

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 300
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (data, resp) = try await URLSession.shared.upload(for: req, from: body)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["id"] as? String
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
}
