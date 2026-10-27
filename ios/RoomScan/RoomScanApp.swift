import SwiftUI

@main
struct RoomScanApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                TabView {
                    ScanView().tabItem { Label("Room", systemImage: "square.split.bottomrightquarter") }
                    MeshScanView().tabItem { Label("Mesh", systemImage: "cube.transparent") }
                    ObjectScanView().tabItem { Label("Object", systemImage: "cube") }
                    ModelsView().tabItem { Label("Models", systemImage: "cube.box") }
                    ViewerView().tabItem { Label("Viewer", systemImage: "view.3d") }
                }
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
