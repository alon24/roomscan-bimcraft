import SwiftUI

struct SettingsView: View {
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @AppStorage("liveStream") private var liveStream = true
    @AppStorage("project") private var project = ""

    var body: some View {
        Form {
            Section("Server URL") {
                TextField("http://host:8000", text: $serverURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
            Section(header: Text("Home"), footer: Text("New scans are filed under this home. Rename, move or delete them in the Viewer tab.")) {
                TextField("e.g. My house", text: $project)
            }
            Section(footer: Text("Show the room in the web UI while you scan.")) {
                Toggle("Live preview", isOn: $liveStream)
            }
        }
        .navigationTitle("Settings")
    }
}
