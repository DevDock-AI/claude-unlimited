import SwiftUI

/// The two places the card draws "this much of the window is used".
///
/// Both are the same idea at different sizes: a track, and a fill that reaches
/// as far as the percentage printed beside it. The effect replaces the flat
/// fill with a liquid one that flows toward a new value instead of jumping to
/// it; the fallbacks are the rectangles that were there before.
///
/// Neither takes a percentage straight from the API: `UsageFill.width` holds
/// the clamping rules (over 100 never overflows, a non-zero percentage keeps a
/// visible sliver, an unknown percentage fills the bar) and stays the single
/// definition of how far a fill reaches — the effect gets the same number as a
/// 0...1 fraction of the width it was given.
///
/// Two things about the effect are the same at both sizes:
///
/// - It draws its own full-width bar, background included, and reaches
///   `fraction` by itself. So it is never framed to the fill width — that would
///   scale the bar down and then fill a fraction OF a fraction.
/// - That background is opaque black, and these bars sit on a translucent card.
///   `.screen` is what removes it: black contributes nothing, and the liquid
///   adds light over whatever track is underneath.
///
/// That last point is also why both bars take the card's `DockPalette` and fall
/// back to the plain fill on a LIGHT one. `.screen` only ever adds light, and
/// adding light to a white card gives white: the liquid would be invisible and
/// the percentage unreadable. The plain rectangles have no such problem, so a
/// light card gets those and a dark card keeps the effect.

/// The session block's background — the whole "serving now" panel doubles as a
/// progress bar, so a serving account at 61% is 61% filled rather than a slab.
struct SessionFillBar: View {
    let percent: Double?
    /// Only the serving account's block is filled; the others show the track.
    let serving: Bool
    /// The card's resolved colours, for the track and for whether the liquid
    /// fill can run at all — see the note above about `.screen`.
    let palette: DockPalette
    var cornerRadius: CGFloat = 12

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                (palette.isDark ? Color.white : Color.black).opacity(palette.isDark ? 0.035 : 0.06)
                if serving {
                    fill(width: UsageFill.width(percent, total: geo.size.width),
                         total: geo.size.width)
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
    }

    // @MainActor because the gate reads live system settings, and `body` is
    // main-actor isolated already.
    @MainActor @ViewBuilder
    private func fill(width: CGFloat, total: CGFloat) -> some View {
        #if HUD_EFFECTS
        // The card is only ever on screen while the dock is, and it is built
        // on hover, so there is no separate visibility to consult here.
        if palette.isDark, ShaderGate.isEnabled(visible: true, wanted: true) {
            HUDEffects.usageFill(fraction: UsageFill.fraction(width, of: total),
                                 tint: palette.good.opacity(0.55),
                                 track: .black,
                                 cornerRadius: cornerRadius)
                .blendMode(.screen)
        } else {
            plain(width: width)
        }
        #else
        plain(width: width)
        #endif
    }

    private func plain(width: CGFloat) -> some View {
        ZStack(alignment: .trailing) {
            palette.sessionFill
            // The fill is deliberately soft; a stronger hairline at its end
            // keeps a small percentage readable. Light mode always lands here
            // (`.screen` over white does nothing), so both are weighted for the
            // appearance rather than left at the dark dock's values.
            Rectangle().fill(palette.sessionEdge).frame(width: 1)
        }
        .frame(width: width)
    }
}

/// One meter under the card: a 4pt capsule per usage window.
struct MeterBar: View {
    let percent: Double?
    /// The account's own switch threshold, so the colour changes where rotation
    /// actually happens rather than at an arbitrary band.
    let threshold: Double?
    /// The card's resolved colours — see `SessionFillBar`.
    let palette: DockPalette
    var height: CGFloat = 4
    /// The colour of the figure printed above this meter. When given, the bar
    /// uses it, so a weekly 92% cannot be a red number over an amber bar.
    var color: Color? = nil

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(palette.track)
                fill(width: barWidth(geo.size.width), total: geo.size.width)
            }
            .clipShape(Capsule())
        }
        .frame(height: height)
    }

    private var tint: Color { color ?? palette.usage(percent, threshold: threshold) }

    /// An unknown percentage is an empty meter here, not a full one: the meter
    /// is printed next to its own figure, and "—" over a full bar reads wrong.
    private func barWidth(_ total: CGFloat) -> CGFloat {
        guard let percent else { return 0 }
        return UsageFill.width(percent, total: total)
    }

    // @MainActor because the gate reads live system settings, and `body` is
    // main-actor isolated already.
    @MainActor @ViewBuilder
    private func fill(width: CGFloat, total: CGFloat) -> some View {
        #if HUD_EFFECTS
        if palette.isDark, ShaderGate.isEnabled(visible: true, wanted: percent != nil) {
            HUDEffects.usageFill(fraction: UsageFill.fraction(width, of: total),
                                 tint: tint, track: .black,
                                 cornerRadius: height / 2)
                .blendMode(.screen)
        } else {
            Capsule().fill(tint).frame(width: width)
        }
        #else
        Capsule().fill(tint).frame(width: width)
        #endif
    }
}
