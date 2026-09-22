import SwiftUI

/// The "nothing to show yet" states: the dock while it is waiting for its first
/// reply, and the dock when the daemon is not answering at all.
///
/// The effect is a sonar sweep. The fallback differs at every call site — a
/// small spinner in one place, a warning glyph in another — so it is passed in
/// rather than chosen here, which also keeps each site's meaning (and its
/// tooltip) next to the layout it belongs to.
struct HUDLoader<Fallback: View>: View {
    let size: CGFloat
    let color: Color
    /// The surface is on screen.
    let visible: Bool
    @ViewBuilder let fallback: () -> Fallback

    var body: some View {
        #if HUD_EFFECTS
        if ShaderGate.isEnabled(visible: visible, wanted: true) {
            HUDEffects.loader(size: size, color: color)
                .frame(width: size, height: size)
        } else {
            fallback()
        }
        #else
        fallback()
        #endif
    }
}
