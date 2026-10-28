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

/// A UIKit pinch recogniser on the window. SwiftUI's MagnificationGesture lost to the gesture recognisers inside the camera and
/// SceneKit views, so the zoom worked only some of the time; this one recognises together with everything else.
private struct PinchCatcher: UIViewRepresentable {
    @Binding var zoom: CGFloat
    var enabled: Bool

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> Probe {
        let v = Probe()
        v.isUserInteractionEnabled = false
        v.coordinator = context.coordinator
        return v
    }

    func updateUIView(_ v: Probe, context: Context) {
        context.coordinator.zoom = $zoom
        context.coordinator.enabled = enabled
    }

    final class Probe: UIView {
        weak var coordinator: Coordinator?
        override func didMoveToWindow() {
            super.didMoveToWindow()
            coordinator?.attach(to: window)
        }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var zoom: Binding<CGFloat> = .constant(1)
        var enabled = true
        private var start: CGFloat = 1
        private weak var host: UIWindow?
        private lazy var pinch: UIPinchGestureRecognizer = {
            let g = UIPinchGestureRecognizer(target: self, action: #selector(changed(_:)))
            g.cancelsTouchesInView = false
            g.delegate = self
            return g
        }()

        func attach(to window: UIWindow?) {
            host?.removeGestureRecognizer(pinch)
            host = window
            window?.addGestureRecognizer(pinch)
        }

        @objc func changed(_ g: UIPinchGestureRecognizer) {
            guard enabled else { return }
            if g.state == .began { start = zoom.wrappedValue }
            zoom.wrappedValue = min(4, max(1, start * g.scale))
        }

        func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    }
}

private struct PinchZoom: ViewModifier {
    @Binding var zoom: CGFloat
    var enabled: Bool
    @State private var visible = false

    func body(content: Content) -> some View {
        content
            .background(PinchCatcher(zoom: $zoom, enabled: enabled && visible))
            .onAppear { visible = true }
            .onDisappear { visible = false }
    }
}

extension View {
    /// Pinch with two fingers to zoom the display between 1× and 4×, on top of whatever view is underneath. Only while `enabled`
    /// and while this screen is showing (the other tabs stay loaded behind it).
    func pinchZoom(_ zoom: Binding<CGFloat>, enabled: Bool = true) -> some View { modifier(PinchZoom(zoom: zoom, enabled: enabled)) }
}
