import SceneKit
import SwiftUI

/// World points on a zoomed AR camera picture, and an arrow on the edge of the usable area for points out of view.
/// Shared by the Measure tab's pins and the live guide's "capture here next" points; only the margins differ.
struct ScreenProjector {
    let view: SCNView
    let size: CGSize
    let zoom: CGFloat
    let side: CGFloat         // a point counts as visible this far in from the left and right edges
    let top: CGFloat          // and below this
    let bottom: CGFloat       // and above this distance from the bottom
    let arrowBand: CGFloat    // the arrows' area is centred at (height - arrowBand) / 2
    let arrowInset: CGFloat   // and its half-height is that centre minus this

    func project(_ w: SIMD3<Float>) -> (pt: CGPoint, front: Bool) {
        let p = view.projectPoint(SCNVector3(w.x, w.y, w.z))
        // Same magnification as the picture, about the screen centre.
        let c = CGPoint(x: size.width / 2, y: size.height / 2)
        return (CGPoint(x: c.x + (CGFloat(p.x) - c.x) * zoom, y: c.y + (CGFloat(p.y) - c.y) * zoom), p.z > 0 && p.z < 1)
    }

    func onScreen(_ q: (pt: CGPoint, front: Bool)) -> Bool {
        q.front && q.pt.x > side && q.pt.x < size.width - side && q.pt.y > top && q.pt.y < size.height - bottom
    }

    /// Where the arrow sits: on the edge of the usable area, in the direction of the point. Also returns its rotation.
    func edge(_ q: (pt: CGPoint, front: Bool)) -> (CGPoint, Double) {
        let c = CGPoint(x: size.width / 2, y: (size.height - arrowBand) / 2)
        var dx = q.pt.x - c.x, dy = q.pt.y - c.y
        if !q.front { dx = -dx; dy = -dy }
        if abs(dx) < 1 && abs(dy) < 1 { dy = -1 }
        let hx = size.width / 2 - 36, hy = (size.height - arrowBand) / 2 - arrowInset
        let s = min(hx / max(abs(dx), 0.001), hy / max(abs(dy), 0.001))
        return (CGPoint(x: c.x + dx * s, y: c.y + dy * s), Double(atan2(dy, dx)) + .pi / 2)
    }
}
