import SwiftUI

@main
struct RoomScanApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                TabView {
                    ScanView().tabItem { Label("Room", systemImage: "square.split.bottomrightquarter") }
                    ObjectScanView().tabItem { Label("Object", systemImage: "cube") }
                    ViewerView().tabItem { Label("Viewer", systemImage: "view.3d") }
                }
            }
        }
    }
}
