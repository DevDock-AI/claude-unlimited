import XCTest
@testable import HUD

/// A serving tile has exactly two visual states: its provider mark and the
/// Sampling Layers orbs. The two opacities must always cover the same space.
final class ServingOrbsTests: XCTestCase {
    func testCycleStartsWithTheLogo() {
        XCTAssertEqual(ServingOrbs.logoOpacity(at: 0), 1, accuracy: 1e-9)
        XCTAssertEqual(ServingOrbs.orbsOpacity(at: 0), 0, accuracy: 1e-9)
    }

    func testLogoRemainsVisibleThroughItsHold() {
        XCTAssertEqual(ServingOrbs.logoOpacity(at: ServingOrbs.logoHold - 0.001),
                       1, accuracy: 1e-9)
        XCTAssertEqual(ServingOrbs.orbsOpacity(at: ServingOrbs.logoHold - 0.001),
                       0, accuracy: 1e-9)
    }

    func testFirstCrossfadeIsReciprocal() {
        let midpoint = ServingOrbs.logoHold + ServingOrbs.crossfade / 2
        XCTAssertEqual(ServingOrbs.logoOpacity(at: midpoint), 0.5, accuracy: 1e-9)
        XCTAssertEqual(ServingOrbs.orbsOpacity(at: midpoint), 0.5, accuracy: 1e-9)
    }

    func testOrbsRemainVisibleThroughTheirHold() {
        let midpoint = ServingOrbs.logoHold + ServingOrbs.crossfade + ServingOrbs.orbsHold / 2
        XCTAssertEqual(ServingOrbs.logoOpacity(at: midpoint), 0, accuracy: 1e-9)
        XCTAssertEqual(ServingOrbs.orbsOpacity(at: midpoint), 1, accuracy: 1e-9)
    }

    func testFinalCrossfadeReturnsToTheLogo() {
        let midpoint = ServingOrbs.logoHold + ServingOrbs.crossfade + ServingOrbs.orbsHold
            + ServingOrbs.crossfade / 2
        XCTAssertEqual(ServingOrbs.logoOpacity(at: midpoint), 0.5, accuracy: 1e-9)
        XCTAssertEqual(ServingOrbs.orbsOpacity(at: midpoint), 0.5, accuracy: 1e-9)
    }

    func testOpacitiesAlwaysCoverTheTile() {
        for phase in stride(from: 0.0, through: ServingOrbs.cycle, by: 0.01) {
            XCTAssertEqual(ServingOrbs.logoOpacity(at: phase) + ServingOrbs.orbsOpacity(at: phase),
                           1, accuracy: 1e-9)
        }
    }

    func testPhaseWrapsInBothDirections() {
        XCTAssertEqual(ServingOrbs.phase(at: ServingOrbs.cycle + 0.2), 0.2, accuracy: 1e-9)
        XCTAssertEqual(ServingOrbs.phase(at: -0.2), ServingOrbs.cycle - 0.2, accuracy: 1e-9)
    }
}
