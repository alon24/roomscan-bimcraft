import SwiftUI

struct SettingsView: View {
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @AppStorage("liveStream") private var liveStream = true
    @AppStorage("project") private var project = ""
    @AppStorage("allFrames") private var allFrames = true
    @State private var serverVersion = "checking…"

    var body: some View {
        Form {
            Section("Server URL") {
                TextField("http://host:8000", text: $serverURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
            Section(header: Text("Home"), footer: Text("New scans are filed under this home. Rename, move or delete them on the full page of a model (Models tab).")) {
                TextField("e.g. My house", text: $project)
            }
            Section("Version") {
                LabeledContent("App", value: AppVersion.text)
                LabeledContent("Server", value: serverVersion)
            }
            Section(footer: Text("Sends the recent app log to the server. It appears under Issues on the web page.")) {
                Button("Send problem report") { Reporter.report(tab: "settings", message: "Manual report") }
            }
            Section(footer: Text("After printing the marker ring: measure it with calipers, take a few photos and let the server check it.")) {
                NavigationLink("Calibrate the printed ring") { RingCalibrationView() }
            }
            Section(footer: Text("Mesh scans keep a photo, the LiDAR depth and the position of every half second (up to 400 frames), so each frame can be checked and corrected on the web page. Uses more storage and upload time.")) {
                Toggle("Keep every frame", isOn: $allFrames)
            }
            Section(footer: Text("Show the room in the web UI while you scan.")) {
                Toggle("Live preview", isOn: $liveStream)
            }
        }
        .navigationTitle("Settings")
        .task(id: serverURL) { serverVersion = await Self.fetchServerVersion(serverURL) }
    }

    /// Also a quick connection test: if this says "not reachable", uploads will fail too.
    static func fetchServerVersion(_ server: String) async -> String {
        guard let url = URL(string: server)?.appendingPathComponent("api/version") else { return "bad URL" }
        var req = URLRequest(url: url)
        req.timeoutInterval = 5
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let v = o["version"] as? String else { return "not reachable" }
        return "v\(v) · build \(o["build"] as? String ?? "?")"
    }
}
