import UIKit

/// While the phone films or builds: the screen stays on and at full brightness, and goes back to the user's brightness afterwards
/// (also when the app is left, so the brightness is never left at full).
enum Screen {
    private static var saved: CGFloat?
    private static var wanted = false
    private static var observers: [NSObjectProtocol] = []

    static func awake(_ on: Bool) {
        wanted = on
        UIApplication.shared.isIdleTimerDisabled = on
        if on { brighten() } else { restore() }
        if observers.isEmpty {
            let nc = NotificationCenter.default
            observers = [
                nc.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { _ in restore() },
                nc.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in if wanted { brighten() } },
            ]
        }
    }

    private static func brighten() {
        if saved == nil { saved = UIScreen.main.brightness }
        UIScreen.main.brightness = 1.0
    }

    private static func restore() {
        guard let s = saved else { return }
        UIScreen.main.brightness = s
        saved = nil
    }
}
