import SwiftUI

/// One look for the whole app: a teal accent, soft cards over the camera, big round capture buttons.
enum Theme {
    static let accent = Color(red: 0.05, green: 0.60, blue: 0.62)
}

private struct CardModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(.white.opacity(0.12)))
            .shadow(color: .black.opacity(0.18), radius: 10, y: 3)
    }
}

extension View {
    /// A rounded frosted panel, readable over the camera picture.
    func card() -> some View { modifier(CardModifier()) }
}

/// The scan buttons: Start / Stop / Upload as large capsules; destructive ones (Reset, New scan) in red, greyed out when disabled.
struct CaptureButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Body(configuration: configuration) }

    struct Body: View {
        let configuration: ButtonStyleConfiguration
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            configuration.label
                .font(.headline)
                .foregroundStyle(.white)
                .padding(.horizontal, 20).padding(.vertical, 11)
                .background(configuration.role == .destructive ? Color.red : Theme.accent, in: Capsule())
                .shadow(color: .black.opacity(0.25), radius: 6, y: 2)
                .opacity(enabled ? (configuration.isPressed ? 0.75 : 1) : 0.4)
                .scaleEffect(configuration.isPressed ? 0.96 : 1)
                .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
        }
    }
}

/// The how-to text: the first sentence, with a button for the rest, so it does not cover the camera.
struct HelpCard: View {
    let text: String
    @State private var open = false

    private var first: String {
        guard let r = text.range(of: ". ") else { return text }
        return String(text[..<r.upperBound]).trimmingCharacters(in: .whitespaces)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(open ? text : first).font(.callout).fixedSize(horizontal: false, vertical: true)
            if first != text {
                Button(open ? "Less" : "More") { withAnimation(.easeInOut(duration: 0.2)) { open.toggle() } }
                    .font(.caption.weight(.semibold)).tint(Theme.accent)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }
}

/// Icon and colour for a kind of scan, in the lists.
func kindIcon(_ kind: String, ring: Bool = false) -> String {
    switch kind {
    case "room": return "square.split.bottomrightquarter"
    case "object": return "cube"
    default: return ring ? "scope" : "house"
    }
}
