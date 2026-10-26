import SwiftUI

@main
struct RoomScanApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                ScanView()
            }
        }
    }
}
