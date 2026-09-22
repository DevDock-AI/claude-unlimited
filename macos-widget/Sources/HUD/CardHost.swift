import SwiftUI

/// The hover card's content, as a view that stays alive between hovers.
///
/// The card used to be a snapshot. `showCard` took a fully-built `DetailCard`
/// and assigned a brand-new `NSHostingView` to the panel on every hover, which
/// had two consequences: the numbers froze at the instant the pointer arrived
/// and never moved again while the card was open, and anything animating
/// inside it restarted from frame zero each time the pointer crossed a tile.
///
/// This observes the same two objects the dock does and derives the profile
/// from `controller.hoveredProfileId`, so one hosting view is built once and
/// SwiftUI morphs the content in place — which is also the project's standing
/// rule for live-updating UI: morph, never flash the container.
struct CardHost: View {
    @ObservedObject var client: PoolClient
    @ObservedObject var controller: DockController
    /// The card is a second window, so it resolves the appearance itself —
    /// but from the same theme and the same rule as the dock, so the two
    /// cannot end up facing different ways.
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        if let profile = hoveredProfile {
            DetailCard(profile: profile,
                       spend: client.spendToday[profile.id],
                       updated: client.lastUpdated,
                       ringWindow: controller.ringWindow(for: profile),
                       palette: DockPalette.resolve(theme: controller.theme,
                                                    transparency: controller.transparency,
                                                    systemIsDark: colorScheme == .dark))
        }
    }

    /// The hovered profile as the pool currently reports it — not as it was
    /// when the pointer arrived. A profile that disappears from the pool
    /// (deleted in the Dashboard while its card is open) renders nothing
    /// rather than a stale copy.
    private var hoveredProfile: Profile? {
        guard let id = controller.hoveredProfileId,
              case .ready(let profiles, _) = client.state else { return nil }
        return profiles.first { $0.id == id }
    }
}
