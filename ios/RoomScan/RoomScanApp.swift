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

    init() {
        // The scan views draw under the tab bar; without a background its icons vanish into the camera picture until it is tapped.
        let bar = UITabBarAppearance()
        bar.configureWithOpaqueBackground()
        UITabBar.appearance().standardAppearance = bar
        UITabBar.appearance().scrollEdgeAppearance = bar
        // CI screenshots launch with -uiShots 1: no permission pop-up over the picture.
        if !UserDefaults.standard.bool(forKey: "uiShots") { Notifier.setUp() }
    }

    /// `-startTab N` on launch opens that tab (used by the CI screenshot job).
    @State private var tab = UserDefaults.standard.integer(forKey: "startTab")

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                TabView(selection: $tab) {
                    RoomTab().tabItem { Label("Room", systemImage: "square.split.bottomrightquarter") }.tag(0)
                    ObjectTab().tabItem { Label("Object", systemImage: "cube") }.tag(1)
                    MeasureView().tabItem { Label("Measure", systemImage: "ruler") }.tag(2)
                    ModelsView().tabItem { Label("Models", systemImage: "cube.box") }.tag(3)
                }
                .toolbarBackground(.visible, for: .tabBar)
                .tint(Theme.accent)
                .toolbar { NavigationLink { SettingsView() } label: { Image(systemName: "gear") } }
                .task { Reporter.log("app launched"); await Reporter.flush(); await Self.syncRing() }
                .overlay(alignment: .bottom) { UploadProgressLine().padding(.bottom, 56).allowsHitTesting(false) }
                .overlay(alignment: .bottomLeading) {
                    Text(AppVersion.text).font(.system(size: 9)).foregroundStyle(.white)
                        .padding(.horizontal, 4).background(.black.opacity(0.5), in: Capsule())
                        .padding(.leading, 6).padding(.bottom, 2).allowsHitTesting(false)
                }
            }
        }
    }
}
