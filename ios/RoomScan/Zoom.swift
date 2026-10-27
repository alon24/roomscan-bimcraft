import SwiftUI

/// Display zoom for the ARKit camera views. ARKit's lens is fixed (zooming it would change the measurements), so this magnifies
/// what you see, like a magnifying glass over the screen. The scan itself still records the full field of view.
struct ZoomControl: View {
    @Binding var zoom: CGFloat

    var body: some View {
        HStack(spacing: 12) {
            Button { zoom = max(1, zoom - 0.5) } label: { Image(systemName: "minus.magnifyingglass") }
            Text(String(format: "%.1f×", Double(zoom))).font(.caption.monospacedDigit())
            Button { zoom = min(4, zoom + 0.5) } label: { Image(systemName: "plus.magnifyingglass") }
        }
        .padding(8).background(.thinMaterial, in: Capsule())
    }
}

private struct PinchZoom: ViewModifier {
    @Binding var zoom: CGFloat
    @State private var start: CGFloat?

    func body(content: Content) -> some View {
        content.simultaneousGesture(
            MagnificationGesture()
                .onChanged { v in
                    if start == nil { start = zoom }
                    zoom = min(4, max(1, (start ?? 1) * v))
                }
                .onEnded { _ in start = nil })
    }
}

extension View {
    /// Pinch with two fingers to zoom the display between 1× and 4×.
    func pinchZoom(_ zoom: Binding<CGFloat>) -> some View { modifier(PinchZoom(zoom: zoom)) }
}
