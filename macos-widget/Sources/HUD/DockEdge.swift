import CoreGraphics

/// A screen edge the dock can attach to.
///
/// The model (an optional docked edge, a snap distance, a candidate with a
/// progress value) follows getagentseal/codeburn's CapacityDock (MIT). This is
/// an independent, much smaller implementation of the same feel: the dock runs
/// its own drag, is pulled onto an edge it comes close to, and holds there
/// until it is pulled clearly away.
enum DockEdge: String, CaseIterable {
    case left, right, top, bottom

    /// Docked to a side, the dock runs vertically; to the top or bottom, across.
    var isVertical: Bool { self == .left || self == .right }
    var opposite: DockEdge {
        switch self {
        case .left: return .right
        case .right: return .left
        case .top: return .bottom
        case .bottom: return .top
        }
    }
    var label: String {
        switch self {
        case .left: return "left"
        case .right: return "right"
        case .top: return "top"
        case .bottom: return "bottom"
        }
    }
}

struct DockSnapCandidate: Equatable {
    let edge: DockEdge
    /// 0 at the edge of the snap zone, 1 touching the screen edge.
    let progress: CGFloat
}

/// Pure geometry — no AppKit, no state — so it can be checked without a screen.
/// All rects are AppKit screen coordinates (y grows upward) and `visible` is
/// the usable part of the screen (see `usableFrame`), so a menu bar or Dock
/// that is actually on screen is never covered.
enum DockSnap {
    /// The part of a screen the dock may use. `visibleFrame` keeps reserving
    /// the menu bar's band even when the menu bar is set to hide itself, which
    /// stopped the dock short of the top of the screen; with auto-hide on, the
    /// band is free. (An auto-hidden Dock is already excluded by the system.)
    /// Which display a frame belongs to: the one it overlaps most, or nil when
    /// it overlaps none. Pure, so the rule is tested rather than inferred.
    ///
    /// Used instead of `NSScreen.main` whenever the window's own `screen` is
    /// not known yet (at launch, before it is on screen): `NSScreen.main` is
    /// the display with KEYBOARD FOCUS, so a dock saved on the laptop's right
    /// edge relaunched onto whichever monitor you happened to be typing on.
    /// The window frame out of an AppKit frame-autosave string
    /// ("x y w h screenX screenY screenW screenH"): the first four numbers,
    /// which are already global coordinates. nil for anything malformed.
    static func savedFrame(from autosave: String?) -> CGRect? {
        guard let parts = autosave?.split(separator: " ").compactMap({ Double($0) }),
              parts.count >= 4, parts[2] > 0, parts[3] > 0 else { return nil }
        return CGRect(x: parts[0], y: parts[1], width: parts[2], height: parts[3])
    }

    static func homeScreenIndex(for frame: CGRect, screens: [CGRect]) -> Int? {
        var best: (index: Int, area: CGFloat)?
        for (i, screen) in screens.enumerated() {
            let overlap = screen.intersection(frame)
            guard !overlap.isNull, overlap.width > 0, overlap.height > 0 else { continue }
            let area = overlap.width * overlap.height
            if best == nil || area > best!.area { best = (i, area) }
        }
        return best?.index
    }

    static func usableFrame(screen: CGRect, visible: CGRect, menuBarAutoHides: Bool) -> CGRect {
        guard menuBarAutoHides, screen.maxY > visible.maxY else { return visible }
        var frame = visible
        frame.size.height = screen.maxY - visible.minY
        return frame
    }

    /// How close to an edge a released drag has to be to attach.
    static let snapDistance: CGFloat = 44
    /// While dragging: this close to an edge, the dock jumps onto it...
    static let stickDistance: CGFloat = 20
    /// ...and stays there until the pointer has pulled it this far away.
    /// Larger than stickDistance, so it never flickers on and off at the line.
    static let releaseDistance: CGFloat = 40
    /// Gap between a docked dock and the edge. Zero: a docked dock sits flush
    /// and its shoulders (DockRail) curve out into the edge, the way the
    /// display notch meets the top of the screen.
    static let inset: CGFloat = 0

    /// The nearest edge within `snapDistance`, or nil to stay floating. Ties
    /// go to the sides, whose order in `allCases` comes first.
    static func candidate(for frame: CGRect, in visible: CGRect) -> DockSnapCandidate? {
        // Past the edge counts as touching it, hence max(_, 0).
        let distances: [(DockEdge, CGFloat)] = [
            (DockEdge.left, max(frame.minX - visible.minX, 0)),
            (DockEdge.right, max(visible.maxX - frame.maxX, 0)),
            (DockEdge.top, max(visible.maxY - frame.maxY, 0)),
            (DockEdge.bottom, max(frame.minY - visible.minY, 0)),
        ]
        guard let nearest = distances.min(by: { $0.1 < $1.1 }), nearest.1 <= snapDistance else { return nil }
        return DockSnapCandidate(edge: nearest.0, progress: min(max(1 - nearest.1 / snapDistance, 0), 1))
    }

    /// Where a dock of `size` sits when attached to `edge`. Along the edge it
    /// keeps the position it was released at (`near`), clamped on screen.
    static func dockedFrame(size: CGSize, edge: DockEdge, near frame: CGRect, in visible: CGRect) -> CGRect {
        let width = min(size.width, visible.width - inset * 2)
        let height = min(size.height, visible.height - inset * 2)
        var x: CGFloat
        var y: CGFloat
        switch edge {
        case .left:   x = visible.minX + inset
        case .right:  x = visible.maxX - inset - width
        case .top, .bottom:
            // Keep the centre where it was released, so a dock rotating from
            // vertical to horizontal does not jump sideways.
            x = frame.midX - width / 2
        }
        switch edge {
        case .top:    y = visible.maxY - inset - height
        case .bottom: y = visible.minY + inset
        case .left, .right:
            // Keep the top where it was released: tiles grow downward.
            y = frame.maxY - height
        }
        x = min(max(x, visible.minX + inset), visible.maxX - inset - width)
        y = min(max(y, visible.minY + inset), visible.maxY - inset - height)
        return CGRect(x: x, y: y, width: width, height: height)
    }

    /// How far `frame` is from sitting docked on `edge` (negative: pushed past).
    static func gap(_ frame: CGRect, to edge: DockEdge, in visible: CGRect) -> CGFloat {
        switch edge {
        case .left:   return frame.minX - (visible.minX + inset)
        case .right:  return (visible.maxX - inset) - frame.maxX
        case .top:    return (visible.maxY - inset) - frame.maxY
        case .bottom: return frame.minY - (visible.minY + inset)
        }
    }

    /// One drag step. `free` is where the pointer alone would put the dock;
    /// the result is where to draw it, and the edge it is stuck to. Pushing
    /// past an edge never moves it off screen.
    static func dragFrame(free: CGRect, stuck: DockEdge?, in visible: CGRect) -> (frame: CGRect, stuck: DockEdge?) {
        var edge = stuck
        if let current = edge, gap(free, to: current, in: visible) > releaseDistance { edge = nil }
        if edge == nil {
            edge = DockEdge.allCases
                .map { ($0, gap(free, to: $0, in: visible)) }
                .filter { $0.1 <= stickDistance }
                .min { $0.1 < $1.1 }?.0
        }
        var frame = free
        let minX = visible.minX + inset, maxX = max(minX, visible.maxX - inset - frame.width)
        let minY = visible.minY + inset, maxY = max(minY, visible.maxY - inset - frame.height)
        frame.origin.x = min(max(frame.origin.x, minX), maxX)
        frame.origin.y = min(max(frame.origin.y, minY), maxY)
        switch edge {
        case .left:   frame.origin.x = minX
        case .right:  frame.origin.x = maxX
        case .top:    frame.origin.y = maxY
        case .bottom: frame.origin.y = minY
        case nil:     break
        }
        return (frame, edge)
    }

    /// A floating dock that changes size grows downward from its top and
    /// stays on screen.
    static func floatingFrame(size: CGSize, from frame: CGRect, in visible: CGRect) -> CGRect {
        var x = frame.minX
        var y = frame.maxY - size.height
        x = min(max(x, visible.minX), max(visible.minX, visible.maxX - size.width))
        y = min(max(y, visible.minY), max(visible.minY, visible.maxY - size.height))
        return CGRect(x: x, y: y, width: size.width, height: size.height)
    }
}


/// The dock's outline — a rounded pill while it floats, and flush against the
/// edge with concave "shoulders" while it is attached, so it reads as part of
/// the screen edge rather than a panel parked next to it.
///
/// Same construction as codeburn's rail (the system-notch technique: one quad
/// curve per corner, control point at the corner). The panel always reserves
/// `shoulder` points at both ends of its run, so attaching changes only the
/// outline, never the window size — which matters mid-drag, under the cursor.
/// Coordinates are y-down (SwiftUI's), for a rect the size of the panel.
enum DockRail {
    /// Along-axis room reserved at each end of the run for a shoulder.
    static let shoulder: CGFloat = 14
    /// Convex radius of the free side (and of the floating pill).
    static let freeRadius: CGFloat = 22

    static func path(in rect: CGRect, attachedTo edge: DockEdge?, vertical: Bool) -> CGPath {
        // A shoulder only makes sense on an edge the dock runs along; a vertical
        // dock stuck to the top edge mid-drag stays a pill until it rotates.
        guard let edge, edge.isVertical == vertical else {
            let body = vertical ? rect.insetBy(dx: 0, dy: shoulder) : rect.insetBy(dx: shoulder, dy: 0)
            let r = min(freeRadius, body.width / 2, body.height / 2)
            return CGPath(roundedRect: body, cornerWidth: r, cornerHeight: r, transform: nil)
        }
        // Built for the right edge in a canonical rect (across = width, along
        // = height), then turned onto the real edge.
        let canonical = CGRect(x: 0, y: 0,
                               width: vertical ? rect.width : rect.height,
                               height: vertical ? rect.height : rect.width)
        let c = min(shoulder, canonical.height / 4)
        let free = min(freeRadius, canonical.width * 0.45, max(0, canonical.height / 2 - c))
        let p = CGMutablePath()
        let (minX, maxX, minY, maxY) = (canonical.minX, canonical.maxX, canonical.minY, canonical.maxY)
        p.move(to: CGPoint(x: maxX, y: minY))
        p.addQuadCurve(to: CGPoint(x: maxX - c, y: minY + c), control: CGPoint(x: maxX, y: minY + c))
        p.addLine(to: CGPoint(x: minX + free, y: minY + c))
        p.addQuadCurve(to: CGPoint(x: minX, y: minY + c + free), control: CGPoint(x: minX, y: minY + c))
        p.addLine(to: CGPoint(x: minX, y: maxY - c - free))
        p.addQuadCurve(to: CGPoint(x: minX + free, y: maxY - c), control: CGPoint(x: minX, y: maxY - c))
        p.addLine(to: CGPoint(x: maxX - c, y: maxY - c))
        p.addQuadCurve(to: CGPoint(x: maxX, y: maxY), control: CGPoint(x: maxX, y: maxY - c))
        p.closeSubpath()

        var t: CGAffineTransform
        switch edge {
        case .right:  t = CGAffineTransform(translationX: rect.minX, y: rect.minY)
        case .left:   t = CGAffineTransform(a: -1, b: 0, c: 0, d: 1, tx: canonical.width + rect.minX, ty: rect.minY)
        case .bottom: t = CGAffineTransform(a: 0, b: 1, c: 1, d: 0, tx: rect.minX, ty: rect.minY)
        case .top:    t = CGAffineTransform(a: 0, b: -1, c: 1, d: 0, tx: rect.minX, ty: canonical.width + rect.minY)
        }
        return p.copy(using: &t) ?? p
    }
}
