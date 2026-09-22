import CoreGraphics
import XCTest
@testable import HUD

/// The session block on the detail card is a progress bar, so its fill obeys
/// the same clamping the meter capsules do.
final class UsageFillTests: XCTestCase {
    func testAPercentageIsItsShareOfTheWidth() {
        XCTAssertEqual(UsageFill.width(61, total: 200), 122, accuracy: 0.001)
    }

    func testAnUnknownPercentageFillsTheBar() {
        XCTAssertEqual(UsageFill.width(nil, total: 200), 200)
    }

    func testZeroDrawsNothing() {
        XCTAssertEqual(UsageFill.width(0, total: 200), 0)
    }

    func testATinyPercentageKeepsAThreePointSliver() {
        XCTAssertEqual(UsageFill.width(0.2, total: 200), 3, accuracy: 0.001)
    }

    func testAboveOneHundredNeverOverflows() {
        XCTAssertEqual(UsageFill.width(140, total: 200), 200)
    }

    func testTheSliverNeverExceedsAVeryNarrowBar() {
        XCTAssertEqual(UsageFill.width(5, total: 2), 2, accuracy: 0.001)
    }

    func testAZeroWidthBarIsEmptyEvenWhenUnknown() {
        XCTAssertEqual(UsageFill.width(nil, total: 0), 0)
    }
}
