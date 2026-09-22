import XCTest
@testable import HUD

/// The gate decides whether a decoration runs. Every one of these is a case
/// where it must not — a GPU is not needed to check that.
final class ShaderGateTests: XCTestCase {

    private func allows(hasLibrary: Bool = true, supportsShaderAPI: Bool = true,
                        reduceMotion: Bool = false, reduceTransparency: Bool = false,
                        lowPower: Bool = false, visible: Bool = true,
                        wanted: Bool = true) -> Bool {
        ShaderGate.allows(hasLibrary: hasLibrary, supportsShaderAPI: supportsShaderAPI,
                          reduceMotion: reduceMotion, reduceTransparency: reduceTransparency,
                          lowPower: lowPower, visible: visible, wanted: wanted)
    }

    func testEverythingSatisfiedRuns() {
        XCTAssertTrue(allows())
    }

    func testABuildWithoutShadersFallsBack() {
        XCTAssertFalse(allows(hasLibrary: false),
                       "a public checkout ships no metallib and must still work")
    }

    func testAnOlderMacOSFallsBack() {
        XCTAssertFalse(allows(supportsShaderAPI: false))
    }

    func testReduceMotionWins() {
        XCTAssertFalse(allows(reduceMotion: true))
    }

    func testReduceTransparencyWins() {
        XCTAssertFalse(allows(reduceTransparency: true))
    }

    func testLowPowerModeWins() {
        XCTAssertFalse(allows(lowPower: true))
    }

    func testAHiddenDockAnimatesNothing() {
        XCTAssertFalse(allows(visible: false))
    }

    func testAnEffectWithNoReasonToRunDoesNot() {
        XCTAssertFalse(allows(wanted: false), "e.g. this account is not serving")
    }

    // MARK: the motion-only gate

    private func allowsMotion(reduceMotion: Bool = false, lowPower: Bool = false,
                              visible: Bool = true, wanted: Bool = true) -> Bool {
        ShaderGate.allowsMotion(reduceMotion: reduceMotion, lowPower: lowPower,
                                visible: visible, wanted: wanted)
    }

    func testMotionRunsWithoutAShaderLibrary() {
        // The logo/orb alternation is plain SwiftUI, so a build with no
        // .metallib still animates it.
        XCTAssertTrue(allowsMotion())
    }

    func testMotionStopsForReduceMotion() {
        XCTAssertFalse(allowsMotion(reduceMotion: true))
    }

    func testMotionStopsForLowPower() {
        XCTAssertFalse(allowsMotion(lowPower: true))
    }

    func testMotionStopsWhenTheDockIsHidden() {
        XCTAssertFalse(allowsMotion(visible: false))
    }

    func testMotionStopsWhenNothingWantsIt() {
        XCTAssertFalse(allowsMotion(wanted: false), "e.g. this account is not serving")
    }

    func testMotionIgnoresReduceTransparency() {
        // Reduce Transparency is a statement about translucency; the logo/orb
        // alternation only changes opacity, so it is not that setting's business.
        XCTAssertTrue(allowsMotion())
        XCTAssertFalse(allows(reduceTransparency: true),
                       "the shader gate still honours it")
    }

    func testTheFrameRateIsCapped() {
        XCTAssertLessThanOrEqual(ShaderGate.framesPerSecond, 30)
        XCTAssertEqual(ShaderGate.frameInterval, 1 / ShaderGate.framesPerSecond, accuracy: 1e-9)
    }
}
