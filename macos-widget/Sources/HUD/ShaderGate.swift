import AppKit
import Foundation
import SwiftUI

/// Whether a Metal effect may run, and where its library comes from.
///
/// Every effect in `Effects/` is written as `gated { shader } fallback { the
/// view that was there before }`, and this decides which side runs. The rule
/// is deliberately conservative: an effect is a decoration, so anything that
/// suggests it is unwanted or unaffordable turns it off, and the HUD looks
/// exactly as it did before.
///
/// The shader sources are not in this repository — they are licensed to
/// compile and ship but not to publish — so a build made from a
/// public checkout has no `.metallib` at all and every effect falls back. That
/// is a supported configuration, not a broken one.
enum ShaderGate {

    /// The compiled library, or nil when this build shipped without one.
    ///
    /// Loaded once. `build.sh` puts it in the bundle's `Resources`; it is not
    /// a SwiftPM resource, so there is no `Bundle.module` to ask. The name is
    /// fixed: `ShaderLibrary.someShader` resolves against Bundle.main's
    /// DEFAULT library, which is the one called `default.metallib`.
    static let library: URL? = {
        guard let url = Bundle.main.url(forResource: "default", withExtension: "metallib"),
              FileManager.default.fileExists(atPath: url.path) else { return nil }
        return url
    }()

    /// The pure rule, separated from the system it reads so it can be tested
    /// without a GPU, a screen or a preference to flip.
    ///
    /// - Parameters:
    ///   - hasLibrary: the build shipped a compiled shader library.
    ///   - supportsShaderAPI: macOS 14+, where `Shader` and `.colorEffect` exist.
    ///   - reduceMotion: the accessibility setting. The strongest no there is.
    ///   - reduceTransparency: likewise — these effects are all translucent.
    ///   - lowPower: Low Power Mode; a decoration is the first thing to drop.
    ///   - visible: the dock is on screen. A hidden HUD animates nothing.
    ///   - wanted: the effect's own reason to run, e.g. this account is serving.
    static func allows(hasLibrary: Bool, supportsShaderAPI: Bool, reduceMotion: Bool,
                       reduceTransparency: Bool, lowPower: Bool, visible: Bool,
                       wanted: Bool) -> Bool {
        guard hasLibrary, supportsShaderAPI, visible, wanted else { return false }
        return !reduceMotion && !reduceTransparency && !lowPower
    }

    /// The rule for an animation that is not a shader.
    ///
    /// Not every moving thing in the HUD needs the Metal library: the serving
    /// logo/orb alternation is plain SwiftUI, so it runs on a build that shipped
    /// without one and on macOS 13. What it still has to honour is the part of
    /// the rule that is about the person rather than the machine — Reduce Motion,
    /// and a dock that nobody is looking at.
    ///
    /// Reduce Transparency is deliberately not consulted here: it is a
    /// statement about translucency, and the alternation only changes opacity.
    static func allowsMotion(reduceMotion: Bool, lowPower: Bool, visible: Bool,
                             wanted: Bool) -> Bool {
        guard visible, wanted else { return false }
        return !reduceMotion && !lowPower
    }

    /// The same rule against the live system.
    @MainActor
    static func isMotionEnabled(visible: Bool, wanted: Bool) -> Bool {
        allowsMotion(reduceMotion: NSWorkspace.shared.accessibilityDisplayShouldReduceMotion,
                     lowPower: ProcessInfo.processInfo.isLowPowerModeEnabled,
                     visible: visible, wanted: wanted)
    }

    /// The same rule against the live system.
    @MainActor
    static func isEnabled(visible: Bool, wanted: Bool) -> Bool {
        let workspace = NSWorkspace.shared
        return allows(hasLibrary: library != nil,
                      supportsShaderAPI: supportsShaderAPI,
                      reduceMotion: workspace.accessibilityDisplayShouldReduceMotion,
                      reduceTransparency: workspace.accessibilityDisplayShouldReduceTransparency,
                      lowPower: ProcessInfo.processInfo.isLowPowerModeEnabled,
                      visible: visible, wanted: wanted)
    }

    static var supportsShaderAPI: Bool {
        if #available(macOS 14, *) { return true }
        return false
    }

    /// Frames per second for the animated effects.
    ///
    /// Capped, and not a round 60: this is furniture that sits on screen all
    /// day next to the editor the user is actually working in, and the point
    /// of the cap is that it never competes with it for the GPU.
    static let framesPerSecond: Double = 30
    static var frameInterval: TimeInterval { 1 / framesPerSecond }
}
