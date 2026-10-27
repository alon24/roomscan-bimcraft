import UIKit
import AVFoundation

/// Keeps a short log and posts problem reports to the server, so they show up in the web page's Issues list.
/// A report that cannot be sent (server unreachable) is kept and retried the next time the app starts or reports.
enum Reporter {
    private static let queue = DispatchQueue(label: "reporter")
    private static var lines: [String] = []
    private static let pendingKey = "pendingIssues"

    static func log(_ s: String) {
        let t = Date().formatted(date: .omitted, time: .standard)
        queue.async {
            lines.append("\(t) \(s)")
            if lines.count > 300 { lines.removeFirst(lines.count - 300) }
        }
    }

    /// Send a report now, with the recent log.
    static func report(tab: String, message: String, detail: String = "") {
        log("REPORT \(tab): \(message)")
        let server = UserDefaults.standard.string(forKey: "serverURL") ?? ""
        let info = Bundle.main.infoDictionary
        queue.async {
            let payload: [String: Any] = [
                "tab": tab, "message": message, "detail": detail, "server": server,
                "app": "v\(info?["CFBundleShortVersionString"] as? String ?? "?") build \(info?["CFBundleVersion"] as? String ?? "?")",
                "device": machine(), "os": UIDevice.current.systemVersion,
                "camera_permission": AVCaptureDevice.authorizationStatus(for: .video).rawValue,  // 0 ask, 1 restricted, 2 denied, 3 allowed
                "log": Array(lines.suffix(120)),
            ]
            guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
            var pending = UserDefaults.standard.stringArray(forKey: pendingKey) ?? []
            pending.append(data.base64EncodedString())
            UserDefaults.standard.set(Array(pending.suffix(20)), forKey: pendingKey)
            Task.detached { await flush() }
        }
    }

    /// Try to deliver every saved report. Called at launch and after each new report.
    static func flush() async {
        let server = UserDefaults.standard.string(forKey: "serverURL") ?? ""
        guard let url = URL(string: server)?.appendingPathComponent("api/issues") else { return }
        for b64 in UserDefaults.standard.stringArray(forKey: pendingKey) ?? [] {
            guard let body = Data(base64Encoded: b64) else { continue }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.timeoutInterval = 8
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            guard let (_, resp) = try? await URLSession.shared.upload(for: req, from: body),
                  (resp as? HTTPURLResponse)?.statusCode == 200 else { return }  // still unreachable: keep the rest
            var left = UserDefaults.standard.stringArray(forKey: pendingKey) ?? []
            if let i = left.firstIndex(of: b64) { left.remove(at: i) }
            UserDefaults.standard.set(left, forKey: pendingKey)
        }
    }

    static func deviceModel() -> String { machine() }

    private static func machine() -> String {
        var sys = utsname()
        uname(&sys)
        return withUnsafePointer(to: &sys.machine) { $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) } }
    }
}
