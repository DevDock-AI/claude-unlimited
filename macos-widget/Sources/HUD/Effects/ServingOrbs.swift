import SwiftUI

/// The serving tile alternates between its provider logo and Thinking Orbs.
///
/// The logo holds long enough to identify the account, fades into the
/// "Sampling" Layers orbs while it works, then fades back to the logo. Nothing
/// else animates on a serving tile: no glow, no expanding ring and no Liquid
/// Metal beneath it. The disc and usage ring remain still throughout.
///
/// On a public build, macOS 13, Reduce Motion, Low Power, or while the dock is
/// hidden, this collapses to the logo. An unavailable decoration must never
/// make a serving account harder to identify.
struct ServingOrbs: View {
    let diameter: CGFloat
    let accent: Color
    let isDark: Bool
    let visible: Bool
    let fallbackKind: String
    /// The orbiting dots and the logo's ink, both resolved by `DockPalette`
    /// for the appearance the dock is on. The cadence below is unaffected:
    /// only what the frames are painted in changes.
    var dot: Color = Color.white.opacity(0.82)
    var mark: Color? = nil

    /// Each mark is readable before the next transition begins.
    static let logoHold: Double = 2.8
    static let crossfade: Double = 0.45
    static let orbsHold: Double = 3.4
    static var cycle: Double { (logoHold + crossfade) * 2 + orbsHold }

    /// The linked effect's 1.8 / 1.4 values are tuned at 46pt. At the logo's
    /// former 42%-tile footprint its dots merge into an opaque centre blob.
    private var ballSize: CGFloat { diameter * 0.62 }
    private static let dots: Double = 0.34
    private static let dotScale: Double = 1.9

    var body: some View {
        content
            .frame(width: diameter, height: diameter)
            .accessibilityLabel("\(kindLabel(fallbackKind)) serving")
    }

    @MainActor @ViewBuilder
    private var content: some View {
        #if HUD_EFFECTS
        if ShaderGate.isMotionEnabled(visible: visible, wanted: true) {
            TimelineView(.periodic(from: .now, by: ShaderGate.frameInterval)) { context in
                let phase = Self.phase(at: context.date.timeIntervalSinceReferenceDate)
                ZStack {
                    ProviderMark(kind: fallbackKind, size: diameter * 0.42, color: mark)
                        .opacity(Self.logoOpacity(at: phase))
                    HUDEffects.servingOrbs(diameter: ballSize, accent: accent,
                                           dot: dot, isDark: isDark,
                                           dots: Self.dots, dotScale: Self.dotScale)
                        .opacity(Self.orbsOpacity(at: phase))
                }
            }
        } else {
            fallback
        }
        #else
        fallback
        #endif
    }

    private var fallback: some View {
        ProviderMark(kind: fallbackKind, size: diameter * 0.42, color: mark)
    }

    /// 0 begins the logo hold. Pure timing keeps the cadence if the card beside
    /// the tile is rebuilt while the pointer moves across the dock.
    static func phase(at time: Double) -> Double {
        guard cycle > 0 else { return 0 }
        let value = time.truncatingRemainder(dividingBy: cycle)
        return value < 0 ? value + cycle : value
    }

    static func logoOpacity(at phase: Double) -> Double {
        let t = min(max(phase, 0), cycle)
        if t < logoHold { return 1 }
        if t < logoHold + crossfade { return 1 - (t - logoHold) / crossfade }
        let fadeBack = logoHold + crossfade + orbsHold
        if t < fadeBack { return 0 }
        return min(max((t - fadeBack) / crossfade, 0), 1)
    }

    static func orbsOpacity(at phase: Double) -> Double {
        1 - logoOpacity(at: phase)
    }
}
