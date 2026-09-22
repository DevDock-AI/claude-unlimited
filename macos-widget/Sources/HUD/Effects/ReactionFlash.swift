import SwiftUI

/// A one-shot flash over the tile of an account that just started serving —
/// whether the pool rotated to it on its own or someone chose "Take over".
///
/// Both arrive the same way: `DockController.reactions[profileId]` gets a fresh
/// token, and a token that differs from the last one fires the effect exactly
/// once. Nothing animates between tokens, and there is no fallback — without
/// the effect the tile simply changes as it always has.
struct ReactionFlash: View {
    /// The token for this profile, or nil if it has never reacted.
    let token: UUID?
    let size: CGFloat
    let tint: Color
    /// The dock is on screen. A token that arrives while it is hidden is
    /// counted but not drawn, so showing the dock again does not replay it.
    let visible: Bool

    /// How many distinct tokens this tile has seen. The effect keys off the
    /// count, so it restarts on each one; the UUID itself is never shown.
    @State private var count = 0

    var body: some View {
        content
            .onAppear { seen = token }
            // macOS 13 floor: the two-parameter onChange(of:initial:) that
            // replaced this one is 14+.
            .onChange(of: token) { next in
                guard next != seen else { return }
                seen = next
                count += 1
            }
    }

    @State private var seen: UUID?

    // @MainActor: the gate reads live system settings; `body` already is.
    @MainActor @ViewBuilder
    private var content: some View {
        #if HUD_EFFECTS
        if count > 0, ShaderGate.isEnabled(visible: visible, wanted: true) {
            HUDEffects.reaction(label: "", size: CGSize(width: size, height: size),
                                tint: tint, trigger: count)
                .frame(width: size, height: size)
                .clipShape(Circle())
                .allowsHitTesting(false)
        }
        #endif
    }
}
