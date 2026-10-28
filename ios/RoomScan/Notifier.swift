import UserNotifications

/// A banner when the server has finished a scan (an upload returns after the conversion), also while the app is in the foreground.
/// A phone app installed without a paid Apple account cannot get pushes from the server, so this only works while the app is alive.
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    private static let shared = Notifier()

    static func setUp() {
        let c = UNUserNotificationCenter.current()
        c.delegate = shared
        c.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func scanDone(name: String, status: String) {
        let c = UNMutableNotificationContent()
        c.title = status == "ok" ? "Model ready" : "Conversion failed"
        c.body = status == "ok" ? "\(name) is on the server: open it in Models." : "\(name) was uploaded but the server could not build it. Open it in Models for the log."
        c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
}
