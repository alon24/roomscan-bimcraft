import SwiftUI
import WebKit

/// The server's web viewer (WebGL) inside the app: same 3D preview, live scan view and downloads as the browser.
struct WebViewer: UIViewRepresentable {
    let url: URL
    func makeUIView(context: Context) -> WKWebView {
        let w = WKWebView()
        w.load(URLRequest(url: url))
        return w
    }
    func updateUIView(_ w: WKWebView, context: Context) {}
}

struct ViewerView: View {
    @AppStorage("serverURL") private var serverURL = "http://192.168.1.10:8000"
    @AppStorage("lastScanID") private var lastScanID = ""
    @State private var reload = 0

    var body: some View {
        let tail = lastScanID.isEmpty ? "" : "&scan=\(lastScanID)"
        Group {
            if let url = URL(string: serverURL + "/?embed=1" + tail) {
                WebViewer(url: url).id(reload).ignoresSafeArea(edges: .bottom)
            } else {
                Text("Set a valid server URL in Settings")
            }
        }
        .toolbar { Button("Reload") { reload += 1 } }
    }
}
