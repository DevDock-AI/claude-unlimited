import SwiftUI

/// Real provider marks, drawn from SVG path data.
///
/// Vector rather than bundled images so the mark stays crisp at any tile size,
/// and a *registry* rather than a `switch` so adding a connector later is one
/// entry here — nothing else in the dock needs to know the kind exists.
struct LogoSpec {
    let path: String            // SVG path `d`
    let viewBox: CGFloat        // square viewBox side
    let color: Color
    let stroke: CGFloat?        // nil = filled, otherwise stroke width
    let evenOdd: Bool           // SVG fill-rule="evenodd"

    init(path: String, viewBox: CGFloat = 24, color: Color,
         stroke: CGFloat? = nil, evenOdd: Bool = false) {
        self.path = path
        self.viewBox = viewBox
        self.color = color
        self.stroke = stroke
        self.evenOdd = evenOdd
    }
}

enum ProviderRegistry {
    /// The SAME marks the dashboard ships (see `kindIcon` in app.js): Claude's
    /// starburst from simple-icons, OpenAI's blossom from lobe-icons, a link
    /// glyph for a raw API key. Copied as data, not redrawn, so the dock and
    /// the web UI can never disagree about what a provider looks like.
    ///
    /// Keyed by `Profile.kind`; an unknown kind falls back to `generic`, so a
    /// connector added daemon-side still renders before this table catches up.
    /// Each path stays on ONE line: a trailing backslash in a Swift multiline
    /// string joins lines with no separator and fuses adjacent coordinates.
    static let logos: [String: LogoSpec] = [
        "oauth": LogoSpec(
            path: "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z",
            viewBox: 24, color: Color(red: 0.851, green: 0.467, blue: 0.341)),

        // fill-rule evenodd is load-bearing: one self-overlapping path that
        // fills as a solid blob under the default non-zero winding rule.
        "codex": LogoSpec(
            path: "M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z",
            viewBox: 24, color: Color(red: 0.184, green: 0.851, blue: 0.769), evenOdd: true),

        "api": LogoSpec(
            path: "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71 M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71",
            viewBox: 24, color: Color(red: 0.604, green: 0.604, blue: 0.631), stroke: 1.8),

        "generic": LogoSpec(
            path: "M12 2.6l8.2 4.7v9.4L12 21.4 3.8 16.7V7.3z",
            viewBox: 24, color: Color(red: 0.604, green: 0.604, blue: 0.631), stroke: 1.8),
    ]

    static func spec(for kind: String) -> LogoSpec {
        logos[kind] ?? logos["generic"]!
    }
}

/// A `Shape` that parses SVG path data and scales it into its rect.
struct SVGPath: Shape {
    let data: String
    let viewBox: CGFloat

    func path(in rect: CGRect) -> Path {
        var parser = SVGPathParser(data)
        let parsed = parser.parse()
        let scale = min(rect.width, rect.height) / viewBox
        let dx = rect.midX - viewBox * scale / 2
        let dy = rect.midY - viewBox * scale / 2
        return parsed.applying(CGAffineTransform(scaleX: scale, y: scale)
            .concatenating(CGAffineTransform(translationX: dx, y: dy)))
    }
}

/// Minimal but complete SVG path parser: every command the marks above use,
/// including elliptical arcs, which the OpenAI knot is built almost entirely
/// from. Written out rather than pulled in as a dependency so the widget keeps
/// building with nothing but SwiftPM and the system frameworks.
struct SVGPathParser {
    private let scanner: [Character]
    private var i = 0
    private var current = CGPoint.zero
    private var start = CGPoint.zero
    private var lastControl: CGPoint?
    private var lastCommand: Character = " "

    init(_ data: String) { scanner = Array(data) }

    mutating func parse() -> Path {
        var path = Path()
        while let command = nextCommand() {
            apply(command, to: &path)
        }
        return path
    }

    private mutating func apply(_ command: Character, to path: inout Path) {
        let relative = command.isLowercase
        switch Character(command.uppercased()) {
        case "M":
            guard let x = number(), let y = number() else { return }
            current = point(x, y, relative)
            start = current
            path.move(to: current)
            // Extra coordinate pairs after a moveto are implicit linetos.
            while let nx = number(), let ny = number() {
                current = point(nx, ny, relative)
                path.addLine(to: current)
            }
        case "L":
            while let x = number(), let y = number() {
                current = point(x, y, relative)
                path.addLine(to: current)
            }
        case "H":
            while let x = number() {
                current = CGPoint(x: relative ? current.x + x : x, y: current.y)
                path.addLine(to: current)
            }
        case "V":
            while let y = number() {
                current = CGPoint(x: current.x, y: relative ? current.y + y : y)
                path.addLine(to: current)
            }
        case "C":
            while let x1 = number(), let y1 = number(), let x2 = number(),
                  let y2 = number(), let x = number(), let y = number() {
                let c1 = point(x1, y1, relative), c2 = point(x2, y2, relative)
                current = point(x, y, relative)
                path.addCurve(to: current, control1: c1, control2: c2)
                lastControl = c2
            }
        case "S":
            while let x2 = number(), let y2 = number(), let x = number(), let y = number() {
                let c1 = reflectedControl()
                let c2 = point(x2, y2, relative)
                current = point(x, y, relative)
                path.addCurve(to: current, control1: c1, control2: c2)
                lastControl = c2
            }
        case "Q":
            while let x1 = number(), let y1 = number(), let x = number(), let y = number() {
                let c = point(x1, y1, relative)
                current = point(x, y, relative)
                path.addQuadCurve(to: current, control: c)
                lastControl = c
            }
        case "T":
            while let x = number(), let y = number() {
                let c = reflectedControl()
                current = point(x, y, relative)
                path.addQuadCurve(to: current, control: c)
                lastControl = c
            }
        case "A":
            while let rx = number(), let ry = number(), let rot = number(),
                  let large = flag(), let sweep = flag(), let x = number(), let y = number() {
                let end = point(x, y, relative)
                addArc(&path, rx: rx, ry: ry, rotation: rot,
                       largeArc: large != 0, sweep: sweep != 0, to: end)
                current = end
                lastControl = nil
            }
        case "Z":
            path.closeSubpath()
            current = start
            lastControl = nil
        default:
            break
        }
    }

    /// Endpoint -> centre parameterisation (SVG spec F.6.5), then approximated
    /// with cubic segments of at most 90°.
    private mutating func addArc(_ path: inout Path, rx: CGFloat, ry: CGFloat,
                                 rotation: CGFloat, largeArc: Bool, sweep: Bool, to end: CGPoint) {
        var rx = abs(rx), ry = abs(ry)
        if rx == 0 || ry == 0 { path.addLine(to: end); return }

        let phi = rotation * .pi / 180
        let cosPhi = cos(phi), sinPhi = sin(phi)
        let dx2 = (current.x - end.x) / 2, dy2 = (current.y - end.y) / 2
        let x1p = cosPhi * dx2 + sinPhi * dy2
        let y1p = -sinPhi * dx2 + cosPhi * dy2

        // Scale the radii up if they cannot span the endpoints.
        let lambda = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
        if lambda > 1 { rx *= sqrt(lambda); ry *= sqrt(lambda) }

        let sign: CGFloat = largeArc == sweep ? -1 : 1
        let num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
        let den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
        let co = sign * sqrt(max(0, num) / max(den, .leastNonzeroMagnitude))
        let cxp = co * rx * y1p / ry
        let cyp = -co * ry * x1p / rx
        let cx = cosPhi * cxp - sinPhi * cyp + (current.x + end.x) / 2
        let cy = sinPhi * cxp + cosPhi * cyp + (current.y + end.y) / 2

        func angle(_ ux: CGFloat, _ uy: CGFloat, _ vx: CGFloat, _ vy: CGFloat) -> CGFloat {
            let dot = ux * vx + uy * vy
            let len = sqrt(ux * ux + uy * uy) * sqrt(vx * vx + vy * vy)
            var a = acos(min(max(dot / max(len, .leastNonzeroMagnitude), -1), 1))
            if ux * vy - uy * vx < 0 { a = -a }
            return a
        }

        let theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
        var delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                          (-x1p - cxp) / rx, (-y1p - cyp) / ry)
        if !sweep && delta > 0 { delta -= 2 * .pi }
        if sweep && delta < 0 { delta += 2 * .pi }

        let segments = Int(ceil(abs(delta) / (.pi / 2)))
        let step = delta / CGFloat(max(segments, 1))
        var theta = theta1
        for _ in 0..<max(segments, 1) {
            let next = theta + step
            let t = 4.0 / 3.0 * tan(step / 4)
            let cos1 = cos(theta), sin1 = sin(theta)
            let cos2 = cos(next), sin2 = sin(next)

            func map(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
                CGPoint(x: cosPhi * rx * x - sinPhi * ry * y + cx,
                        y: sinPhi * rx * x + cosPhi * ry * y + cy)
            }
            let p1 = map(cos1 - t * sin1, sin1 + t * cos1)
            let p2 = map(cos2 + t * sin2, sin2 - t * cos2)
            let p = map(cos2, sin2)
            path.addCurve(to: p, control1: p1, control2: p2)
            theta = next
        }
    }

    /// Arc flags are single digits and are commonly written with no
    /// separator: "a4.5 4.5 0 006.8 .5" means flags 0,0 then x=6.8. Reading
    /// them with the ordinary number scanner swallowed "006.8" as one value
    /// and produced a degenerate arc — which is what made every arc-based
    /// mark render as a splat.
    private mutating func flag() -> CGFloat? {
        skipSeparators()
        guard i < scanner.count, scanner[i] == "0" || scanner[i] == "1" else { return nil }
        let v: CGFloat = scanner[i] == "1" ? 1 : 0
        i += 1
        return v
    }

    private func reflectedControl() -> CGPoint {
        guard let last = lastControl,
              "CSQT".contains(Character(lastCommand.uppercased())) else { return current }
        return CGPoint(x: 2 * current.x - last.x, y: 2 * current.y - last.y)
    }

    private func point(_ x: CGFloat, _ y: CGFloat, _ relative: Bool) -> CGPoint {
        relative ? CGPoint(x: current.x + x, y: current.y + y) : CGPoint(x: x, y: y)
    }

    private mutating func nextCommand() -> Character? {
        skipSeparators()
        guard i < scanner.count else { return nil }
        let c = scanner[i]
        if c.isLetter {
            i += 1
            lastCommand = c
            return c
        }
        // A bare coordinate run repeats the previous command, per the spec.
        return lastCommand == " " ? nil : lastCommand
    }

    private mutating func skipSeparators() {
        while i < scanner.count, scanner[i] == " " || scanner[i] == ","
                || scanner[i] == "\n" || scanner[i] == "\t" || scanner[i] == "\r" {
            i += 1
        }
    }

    /// Reads one number, stopping before the next command letter. Returns nil
    /// at a letter, which is how the `while let` loops above end.
    private mutating func number() -> CGFloat? {
        skipSeparators()
        guard i < scanner.count else { return nil }
        var s = ""
        if scanner[i] == "-" || scanner[i] == "+" { s.append(scanner[i]); i += 1 }
        var seenDot = false
        while i < scanner.count {
            let c = scanner[i]
            if c.isNumber {
                s.append(c); i += 1
            } else if c == "." && !seenDot {
                seenDot = true; s.append(c); i += 1
            } else if c == "." && seenDot {
                break                     // "1.5.5" is two numbers
            } else if (c == "e" || c == "E"), i + 1 < scanner.count {
                s.append(c); i += 1
                if scanner[i] == "-" || scanner[i] == "+" { s.append(scanner[i]); i += 1 }
            } else {
                break
            }
        }
        guard !s.isEmpty, let v = Double(s) else {
            if !s.isEmpty { i -= s.count }
            return nil
        }
        return CGFloat(v)
    }
}

/// The provider mark for a profile kind, filled or stroked as the logo needs.
struct ProviderMark: View {
    let kind: String
    var size: CGFloat = 26
    /// The ink to draw the mark in. `nil` keeps the registry's brand colour,
    /// which is tuned for the dark dock; a light dock passes the darkened
    /// counterpart from `DockPalette.provider(_:)`. Geometry never changes —
    /// the path, the fill rule and the stroke width are the logo's identity.
    var color: Color? = nil

    var body: some View {
        let spec = ProviderRegistry.spec(for: kind)
        let ink = color ?? spec.color
        Group {
            if let width = spec.stroke {
                SVGPath(data: spec.path, viewBox: spec.viewBox)
                    .stroke(ink, style: StrokeStyle(lineWidth: width,
                                                    lineCap: .round, lineJoin: .round))
            } else {
                SVGPath(data: spec.path, viewBox: spec.viewBox)
                    .fill(ink, style: FillStyle(eoFill: spec.evenOdd))
            }
        }
        .frame(width: size, height: size)
    }
}
