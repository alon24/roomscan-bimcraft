import ARKit
import SwiftUI

/// One thing the server wants captured next (docs/live-guide.md). `target` is a point in this session's ARKit world frame.
struct LiveNeed: Decodable, Identifiable, Equatable {
    let id: String
    let kind: String
    let text: String
    let target: [Double]
    let stand: [Double]?
    let look: [Double]?
    var point: SIMD3<Float> { SIMD3(Float(target[0]), Float(target[1]), Float(target[2])) }
}

/// The server's top-down coverage map: 0 not seen, 1 thin, 2 good, 3 free floor.
struct LiveGrid: Equatable {
    var x0: Double, z0: Double, cell: Double
    var w: Int, h: Int
    var cells: [UInt8]
}

enum LiveError: Error { case notLive, timeout, failed(String) }

/// Two-way link to the server while scanning a room: frames go up as they are taken, the server answers with a small map and what is
/// still missing. It is an extra, never a requirement: capture does not wait for it, and without a connection everything is kept on
/// the phone and uploaded afterwards, exactly as before.
final class LiveLink: NSObject, ObservableObject {
    enum State { case off, connecting, live, offline }

    @Published var link: State = .off
    @Published var percent = 0.0
    @Published var summary = ""
    @Published var needs: [LiveNeed] = []
    @Published var grid: LiveGrid?
    @Published var you: SIMD3<Float>?
    @Published var waiting = 0            // frames the server has not confirmed yet
    @Published var note = ""              // what the link is doing, in words (for the finish step)

    private(set) var scanID: String?
    private var server = ""
    private var params: [String: Any] = [:]
    private var task: URLSessionWebSocketTask?
    private let session = URLSession(configuration: .default)
    private let io = DispatchQueue(label: "live.send")      // frames go out in order
    private var acked = 0, sent = 0
    private var active = false
    private var retries = 0
    private var lastPose = Date.distantPast
    private var serverReady = false

    // MARK: start / stop

    /// Start a live session. If the server cannot be reached the scan simply goes on offline and the link keeps trying.
    func begin(server: String, kind: String, name: String, project: String, info: [String: Any], resume: String? = nil) {
        end()
        self.server = server.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        params = ["kind": kind, "name": name, "project": project, "info": info, "resume_scan": resume.map { $0 as Any } ?? NSNull()]
        active = true; retries = 0; acked = 0; sent = 0; serverReady = false
        link = .connecting
        Task { await createSession() }
    }

    /// Leave without finishing (the scan on the server stays as a draft).
    func end() {
        active = false
        task?.cancel(with: .goingAway, reason: nil); task = nil
        scanID = nil; link = .off
        percent = 0; summary = ""; needs = []; grid = nil; you = nil; waiting = 0; note = ""
    }

    /// Reset pressed: throw the draft away on the server too.
    func abandon() {
        let id = scanID, base = server
        end()
        guard let id, let url = URL(string: base)?.appendingPathComponent("api/scans/\(id)") else { return }
        var req = URLRequest(url: url); req.httpMethod = "DELETE"
        URLSession.shared.dataTask(with: req).resume()
    }

    // MARK: connection

    private func createSession() async {
        guard active, let url = URL(string: server)?.appendingPathComponent("api/live") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"; req.timeoutInterval = 6
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: params)
        do {
            let (data, resp) = try await session.data(for: req)
            guard (resp as? HTTPURLResponse)?.statusCode == 200,
                  let o = try JSONSerialization.jsonObject(with: data) as? [String: Any], let id = o["scan_id"] as? String else { throw LiveError.failed("server said no") }
            await MainActor.run { self.scanID = id; self.connect() }
        } catch {
            await MainActor.run { self.link = .offline; self.scheduleRetry() }
        }
    }

    private func connect() {
        guard active, let id = scanID else { return }
        guard var comps = URLComponents(string: server) else { return }
        comps.scheme = comps.scheme == "https" ? "wss" : "ws"
        guard let base = comps.url else { return }
        let t = session.webSocketTask(with: base.appendingPathComponent("ws/live/\(id)"))
        task = t
        t.resume()
        link = .connecting
        sent = acked      // anything not confirmed is sent again; the server ignores numbers it already has
        sendJSON(["type": "hello", "resume_after": acked, "app": AppVersion.text]) { [weak self] ok in
            guard let self else { return }
            DispatchQueue.main.async {
                guard self.task === t else { return }
                if ok { self.link = .live; self.retries = 0; Reporter.log("live: connected to scan \(id), resume after \(self.acked)") } else { self.dropped(t) }
            }
        }
        listen(t)
    }

    private func listen(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let msg):
                if case .string(let s) = msg, let d = s.data(using: .utf8), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                    DispatchQueue.main.async { self.handle(o, raw: d) }
                }
                self.listen(t)
            case .failure:
                DispatchQueue.main.async { self.dropped(t) }
            }
        }
    }

    private func dropped(_ t: URLSessionWebSocketTask) {
        guard task === t else { return }
        task = nil
        link = active ? .offline : .off
        sent = acked
        Reporter.log("live: connection lost, \(waiting) frames waiting")
        scheduleRetry()
    }

    private func scheduleRetry() {
        guard active else { return }
        retries += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + min(10, Double(retries) * 2)) { [weak self] in
            guard let self, self.active, self.link != .live, self.task == nil else { return }
            if self.scanID == nil { Task { await self.createSession() } } else { self.connect() }
        }
    }

    // MARK: messages from the server

    private struct MapMessage: Decodable {
        struct Grid: Decodable { let x0: Double; let z0: Double; let cell: Double; let w: Int; let h: Int; let data: String }
        let percent: Double?
        let summary: String?
        let grid: Grid?
        let you: [Double]?
        let needs: [LiveNeed]?
    }

    private func handle(_ o: [String: Any], raw: Data) {
        switch o["type"] as? String {
        case "ack":
            if let n = o["n"] as? Int { acked = max(acked, n) }
        case "map":
            guard let m = try? JSONDecoder().decode(MapMessage.self, from: raw) else { return }
            if let p = m.percent { percent = p }
            summary = m.summary ?? summary
            needs = m.needs ?? []
            if let y = m.you, y.count == 3 { you = SIMD3(Float(y[0]), Float(y[1]), Float(y[2])) }
            if let g = m.grid, let bytes = Data(base64Encoded: g.data), bytes.count == g.w * g.h {
                grid = LiveGrid(x0: g.x0, z0: g.z0, cell: g.cell, w: g.w, h: g.h, cells: [UInt8](bytes))
            }
        case "state":
            let s = o["status"] as? String ?? ""
            if s == "ready" { serverReady = true }
            if let msg = o["message"] as? String, !msg.isEmpty { note = msg } else if s == "building" { note = "The server is building the model…" }
        case "aligned":
            note = o["message"] as? String ?? note
        default: break
        }
    }

    // MARK: frames out

    private func sendJSON(_ obj: [String: Any], done: ((Bool) -> Void)? = nil) {
        guard let t = task, let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) else { done?(false); return }
        t.send(.string(s)) { err in done?(err == nil) }
    }

    /// Called every 0.2 s while scanning (and while finishing): sends the camera position and any new frames.
    func pump(_ rec: PhotoRecorder?, frame: ARFrame?) {
        guard active, let rec else { return }
        waiting = max(0, rec.count - acked)
        guard link == .live, let t = task else { return }
        if let f = frame, Date().timeIntervalSince(lastPose) > 0.2 {
            lastPose = Date()
            let tr = f.camera.transform
            sendJSON(["type": "pose", "t": Date().timeIntervalSince1970, "transform": (0..<4).map { c in (0..<4).map { tr[c][$0] } }])
        }
        let dir = rec.dir
        while sent < rec.count && sent - acked < 6 {
            let n = sent + 1
            let meta = rec.metas[n - 1]
            sent = n
            io.async { [weak self] in
                guard let self, let msg = Self.frameMessage(n: n, meta: meta, dir: dir) else { return }
                t.send(.data(msg)) { err in
                    if err != nil { DispatchQueue.main.async { self.dropped(t) } }
                }
            }
        }
    }

    /// `[u32 big-endian header length][header JSON][jpeg][depth]`, docs/live-guide.md.
    private static func frameMessage(n: Int, meta: PhotoMeta, dir: URL) -> Data? {
        guard let jpeg = try? Data(contentsOf: dir.appendingPathComponent(meta.file)) else { return nil }
        let depth = meta.depthFile.flatMap { try? Data(contentsOf: dir.appendingPathComponent($0)) } ?? Data()
        let enc = JSONEncoder()
        enc.keyEncodingStrategy = .convertToSnakeCase
        guard let pj = try? enc.encode(meta), let photo = try? JSONSerialization.jsonObject(with: pj),
              let head = try? JSONSerialization.data(withJSONObject: ["type": "frame", "n": n, "jpeg_len": jpeg.count, "depth_len": depth.count, "photo": photo] as [String: Any])
        else { return nil }
        var out = Data()
        var len = UInt32(head.count).bigEndian
        out.append(Data(bytes: &len, count: 4))
        out.append(head); out.append(jpeg); out.append(depth)
        return out
    }

    // MARK: finishing

    /// Stop pressed and Upload tapped: make sure the server has every frame, then ask it to build the model. Throws if that cannot be done,
    /// and the caller falls back to the ordinary upload from the phone.
    @MainActor func complete(recorder rec: PhotoRecorder, info: [String: Any]) async throws -> String {
        guard active, let id = scanID else { throw LiveError.notLive }
        serverReady = false
        let sendDeadline = Date().addingTimeInterval(120)
        while acked < rec.count {
            if Date() > sendDeadline { throw LiveError.timeout }
            pump(rec, frame: nil)
            note = "Sending \(rec.count - acked) frames to the server…"
            try await Task.sleep(nanoseconds: 300_000_000)
        }
        sendJSON(["type": "finish", "info": info])
        note = "The server is building the model…"
        guard let url = URL(string: server)?.appendingPathComponent("api/scans/\(id)") else { throw LiveError.notLive }
        let buildDeadline = Date().addingTimeInterval(900)
        while Date() < buildDeadline {
            try await Task.sleep(nanoseconds: 2_000_000_000)
            if serverReady { return id }
            if let (data, _) = try? await session.data(from: url), let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let status = (o["status"] as? String) ?? ((o["meta"] as? [String: Any])?["status"] as? String) {
                if status == "ok" { return id }
                if status == "error" { throw LiveError.failed("the server could not build the model") }
            }
        }
        throw LiveError.timeout
    }
}
