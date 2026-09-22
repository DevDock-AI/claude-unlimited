import SwiftUI

/// The disc a tile's logo or serving-orbs sit on.
///
/// This is intentionally still. Liquid Metal was tried here for serving tiles,
/// but it added a second animated concentric form underneath the orbs. At tile
/// size it read as a small border inside the usage circle rather than a surface,
/// and made the serving mark ambiguous. The serving logo and orbs now
/// alternate above it; the disc stays the same quiet base under every tile.
struct TileDisc: View {
    let diameter: CGFloat
    let fill: Color

    var body: some View {
        Circle().fill(fill)
    }
}
