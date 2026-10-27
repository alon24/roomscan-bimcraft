import SwiftUI

enum AppVersion {
    /// "v0.3.0 · build 52". Version comes from the VERSION file and the build is the git commit count (set in CI).
    static var text: String {
        let info = Bundle.main.infoDictionary
        let v = info?["CFBundleShortVersionString"] as? String ?? "?"
        let b = info?["CFBundleVersion"] as? String ?? "?"
        return "v\(v) · build \(b)"
    }
}
