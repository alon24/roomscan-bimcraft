import SwiftUI

@main
struct RoomScanApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                TabView {
                    RoomTab().tabItem { Label("Room", systemImage: "square.split.bottomrightquarter") }
                    ObjectTab().tabItem { Label("Object", systemImage: "cube") }
                    ModelsView().tabItem { Label("Models", systemImage: "cube.box") }
                }
                .toolbar { NavigationLink { SettingsView() } label: { Image(systemName: "gear") } }
                .task { Reporter.log("app launched"); await Reporter.flush() }
                .overlay(alignment: .topLeading) {
                    Text(AppVersion.text).font(.system(size: 9)).foregroundStyle(.white)
                        .padding(.horizontal, 4).background(.black.opacity(0.5), in: Capsule())
                        .padding(.leading, 6).allowsHitTesting(false)
                }
            }
        }
    }
}
