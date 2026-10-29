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

/// Only one screen's camera session may run. A tab switch can run the new tab's onAppear before the old tab's onDisappear, so a
/// screen claims the camera before it starts its session: the screen that had it is stopped first (session paused, screen-awake
/// off), then the new one runs. A late onDisappear of a screen that no longer owns the camera does nothing, so it can never pause or
/// dim the screen that is now showing. Main thread only.
enum CameraOwner {
    private static var owner: ObjectIdentifier?
    private static var leave: (() -> Void)?

    /// Call before starting a session. `leave` stops this screen's camera work; it runs when another screen claims the camera, or
    /// when this screen calls `release`.
    static func claim(_ who: AnyObject, leave: @escaping () -> Void) {
        let id = ObjectIdentifier(who)
        if let o = owner, o != id, let previous = Self.leave {
            owner = nil; Self.leave = nil
            previous()
        }
        owner = id
        Self.leave = leave
    }

    static func owns(_ who: AnyObject) -> Bool { owner == ObjectIdentifier(who) }

    /// The screen went away: run its `leave`, but only if it still owns the camera.
    static func release(_ who: AnyObject) {
        guard owns(who) else { return }
        let l = leave
        owner = nil; leave = nil
        l?()
    }

    /// Give the camera up without running `leave` (the screen already stopped itself).
    static func drop(_ who: AnyObject) {
        if owns(who) { owner = nil; leave = nil }
    }
}
