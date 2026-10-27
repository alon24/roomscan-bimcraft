import SwiftUI

@main
struct RoomScanApp: App {
    /// The measured ring sizes live on the server; keep a copy so ring scans can also be built on the phone with no network.
    static func syncRing() async {
        guard let server = UserDefaults.standard.string(forKey: "serverURL"), let url = URL(string: server)?.appendingPathComponent("api/ring"),
              let (data, _) = try? await URLSession.shared.data(from: url),
              let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let m = o["marker_size_mm"] as? Double, let r = o["centre_radius_mm"] as? Double else { return }
        UserDefaults.standard.set(m, forKey: "ringMarkerMM")
        UserDefaults.standard.set(r, forKey: "ringRadiusMM")
    }

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                TabView {
                    RoomTab().tabItem { Label("Room", systemImage: "square.split.bottomrightquarter") }
                    ObjectTab().tabItem { Label("Object", systemImage: "cube") }
                    ModelsView().tabItem { Label("Models", systemImage: "cube.box") }
                }
                .toolbar { NavigationLink { SettingsView() } label: { Image(systemName: "gear") } }
                .task { Reporter.log("app launched"); await Reporter.flush(); await Self.syncRing() }
                .overlay(alignment: .bottom) { UploadProgressLine().padding(.bottom, 56).allowsHitTesting(false) }
                .overlay(alignment: .topLeading) {
                    Text(AppVersion.text).font(.system(size: 9)).foregroundStyle(.white)
                        .padding(.horizontal, 4).background(.black.opacity(0.5), in: Capsule())
                        .padding(.leading, 6).allowsHitTesting(false)
                }
            }
        }
    }
}
