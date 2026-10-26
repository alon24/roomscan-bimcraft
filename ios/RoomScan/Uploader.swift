import Foundation

enum Uploader {
    static func upload(json: URL?, usdz: URL?, name: String, server: String, kind: String = "room",
                       photos: [URL] = [], photosMeta: URL? = nil) async throws {
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
        if let json { try file("json", json, "scan.json", "application/json") }
        if let usdz { try file("usdz", usdz, "scan.usdz", "model/vnd.usdz+zip") }
        for p in photos { try file("photos", p, p.lastPathComponent, p.pathExtension == "depth" ? "application/octet-stream" : "image/jpeg") }
        if let photosMeta { try file("photos_meta", photosMeta, "photos.json", "application/json") }
        body.append("--\(boundary)--\r\n".data(using: .utf8)!)

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 300
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (_, resp) = try await URLSession.shared.upload(for: req, from: body)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
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
