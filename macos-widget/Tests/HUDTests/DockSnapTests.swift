import CoreGraphics
import XCTest
@testable import HUD

/// Edge snapping is geometry with no AppKit in it, so it is tested here rather
/// than by dragging a real window (which would need Accessibility permission).
final class DockSnapTests: XCTestCase {
    // A 1512x982 display with a 42pt menu bar above and a 70pt Dock below.
    let visible = CGRect(x: 0, y: 70, width: 1512, height: 870)

    private func rail(x: CGFloat, y: CGFloat, w: CGFloat = 56, h: CGFloat = 150) -> CGRect {
        CGRect(x: x, y: y, width: w, height: h)
    }

    func testTheMiddleOfTheScreenFloats() {
        XCTAssertNil(DockSnap.candidate(for: rail(x: 700, y: 400), in: visible))
    }

    func testTheSnapZoneIsFortyFourPoints() {
        XCTAssertEqual(DockSnap.candidate(for: rail(x: 1512 - 56 - 44, y: 400), in: visible)?.edge, .right)
        XCTAssertNil(DockSnap.candidate(for: rail(x: 1512 - 56 - 45, y: 400), in: visible))
    }

    func testPastTheEdgeCountsAsTouchingIt() {
        XCTAssertEqual(DockSnap.candidate(for: rail(x: -20, y: 400), in: visible),
                       DockSnapCandidate(edge: .left, progress: 1))
    }

    func testEveryEdgeIsReachable() {
        XCTAssertEqual(DockSnap.candidate(for: rail(x: 600, y: 72, w: 200, h: 56), in: visible)?.edge, .bottom)
        XCTAssertEqual(DockSnap.candidate(for: rail(x: 600, y: 940 - 56 - 5, w: 200, h: 56), in: visible)?.edge, .top)
        XCTAssertEqual(DockSnap.candidate(for: rail(x: 5, y: 100), in: visible)?.edge, .left)
    }

    func testOrientationFollowsTheEdge() {
        XCTAssertTrue(DockEdge.left.isVertical && DockEdge.right.isVertical)
        XCTAssertFalse(DockEdge.top.isVertical || DockEdge.bottom.isVertical)
    }

    func testDockedToASideKeepsItsTopAndItsInset() {
        let frame = DockSnap.dockedFrame(size: CGSize(width: 56, height: 150), edge: .right,
                                         near: rail(x: 1400, y: 500), in: visible)
        XCTAssertEqual(frame, CGRect(x: 1512 - DockSnap.inset - 56, y: 500, width: 56, height: 150))
    }

    func testDockingNeverCoversTheMenuBarOrTheDock() {
        let left = DockSnap.dockedFrame(size: CGSize(width: 56, height: 150), edge: .left,
                                        near: rail(x: 3, y: 860), in: visible)
        XCTAssertLessThanOrEqual(left.maxY, visible.maxY - DockSnap.inset)
        let bottom = DockSnap.dockedFrame(size: CGSize(width: 200, height: 70), edge: .bottom,
                                          near: rail(x: 1490, y: 72), in: visible)
        XCTAssertEqual(bottom.minY, visible.minY + DockSnap.inset)
        XCTAssertEqual(bottom.maxX, visible.maxX - DockSnap.inset)
    }

    func testRotatingOntoTheTopKeepsTheCentre() {
        let top = DockSnap.dockedFrame(size: CGSize(width: 200, height: 70), edge: .top,
                                       near: rail(x: 700, y: 800), in: visible)
        XCTAssertEqual(top.midX, 728)
        XCTAssertEqual(top.maxY, visible.maxY - DockSnap.inset)
    }

    func testADockTallerThanTheScreenStillFits() {
        let frame = DockSnap.dockedFrame(size: CGSize(width: 56, height: 2000), edge: .right,
                                         near: rail(x: 1400, y: 500), in: visible)
        XCTAssertEqual(frame.height, visible.height - DockSnap.inset * 2)
    }

    func testAFloatingDockGrowsDownwardAndStaysOnScreen() {
        let grown = DockSnap.floatingFrame(size: CGSize(width: 56, height: 300), from: rail(x: 500, y: 500), in: visible)
        XCTAssertEqual(grown.maxY, 650)
        let low = DockSnap.floatingFrame(size: CGSize(width: 56, height: 300), from: rail(x: 500, y: 80), in: visible)
        XCTAssertEqual(low.minY, visible.minY)
    }
}

/// The drag step: the magnetic pull, the hold, and the release.
final class DockDragTests: XCTestCase {
    let visible = CGRect(x: 0, y: 70, width: 1512, height: 870)
    let size = CGSize(width: 56, height: 150)

    private func free(rightGap: CGFloat, y: CGFloat = 400) -> CGRect {
        CGRect(x: visible.maxX - DockSnap.inset - size.width - rightGap, y: y, width: size.width, height: size.height)
    }

    func testFarFromEveryEdgeItFollowsThePointerExactly() {
        let f = CGRect(x: 700, y: 400, width: 56, height: 150)
        let step = DockSnap.dragFrame(free: f, stuck: nil, in: visible)
        XCTAssertEqual(step.frame, f)
        XCTAssertNil(step.stuck)
    }

    func testComingWithinTwentyPointsPullsItOntoTheEdge() {
        let step = DockSnap.dragFrame(free: free(rightGap: 18), stuck: nil, in: visible)
        XCTAssertEqual(step.stuck, .right)
        XCTAssertEqual(step.frame.maxX, visible.maxX - DockSnap.inset)
        XCTAssertEqual(step.frame.minY, 400, "it still slides along the edge")
        XCTAssertNil(DockSnap.dragFrame(free: free(rightGap: 21), stuck: nil, in: visible).stuck)
    }

    func testOnceStuckItHoldsUntilPulledFortyPointsAway() {
        XCTAssertEqual(DockSnap.dragFrame(free: free(rightGap: 39), stuck: .right, in: visible).stuck, .right)
        XCTAssertEqual(DockSnap.dragFrame(free: free(rightGap: 39), stuck: .right, in: visible).frame.maxX,
                       visible.maxX - DockSnap.inset)
        let released = DockSnap.dragFrame(free: free(rightGap: 41), stuck: .right, in: visible)
        XCTAssertNil(released.stuck)
        XCTAssertEqual(released.frame, free(rightGap: 41))
    }

    func testPushingPastTheEdgeKeepsItOnScreen() {
        let step = DockSnap.dragFrame(free: free(rightGap: -300), stuck: nil, in: visible)
        XCTAssertEqual(step.stuck, .right)
        XCTAssertEqual(step.frame.maxX, visible.maxX - DockSnap.inset)
    }

    func testSlidingAlongAStuckEdgeIntoACornerStaysStuckToTheFirstEdge() {
        let step = DockSnap.dragFrame(free: free(rightGap: 0, y: 60), stuck: .right, in: visible)
        XCTAssertEqual(step.stuck, .right)
        XCTAssertEqual(step.frame.minY, visible.minY + DockSnap.inset)
    }
}

/// The outline: a pill while floating, flush with concave shoulders when attached.
final class DockRailTests: XCTestCase {
    let rect = CGRect(x: 0, y: 0, width: 56, height: 180)
    let s = DockRail.shoulder

    func testDockingIsFlush() {
        XCTAssertEqual(DockSnap.inset, 0)
    }

    func testFloatingIsAPillInsideTheShoulderRoom() {
        let path = DockRail.path(in: rect, attachedTo: nil, vertical: true)
        XCTAssertEqual(path.boundingBox, rect.insetBy(dx: 0, dy: s))
        XCTAssertFalse(path.contains(CGPoint(x: 55, y: 1)), "the shoulder room stays transparent")
    }

    func testDockedRightReachesTheEdgeAtBothEndsButNotOnTheFreeSide() {
        let path = DockRail.path(in: rect, attachedTo: .right, vertical: true)
        XCTAssertEqual(path.boundingBox.integral, rect)
        XCTAssertTrue(path.contains(CGPoint(x: 55.5, y: 7)), "top shoulder meets the edge")
        XCTAssertTrue(path.contains(CGPoint(x: 55.5, y: 173)), "bottom shoulder meets the edge")
        XCTAssertFalse(path.contains(CGPoint(x: 1, y: 1)), "the free side keeps its inset")
        XCTAssertFalse(path.contains(CGPoint(x: 45, y: 7)), "the shoulder is concave, not a square corner")
    }

    func testDockedLeftIsTheMirrorImage() {
        let path = DockRail.path(in: rect, attachedTo: .left, vertical: true)
        XCTAssertTrue(path.contains(CGPoint(x: 0.5, y: 7)))
        XCTAssertFalse(path.contains(CGPoint(x: 55, y: 1)))
    }

    func testTopAndBottomFlareAlongTheirOwnEdge() {
        let wide = CGRect(x: 0, y: 0, width: 180, height: 60)
        let bottom = DockRail.path(in: wide, attachedTo: .bottom, vertical: false)   // y-down: bottom is maxY
        XCTAssertTrue(bottom.contains(CGPoint(x: 7, y: 59.5)))
        XCTAssertFalse(bottom.contains(CGPoint(x: 7, y: 0.5)))
        let top = DockRail.path(in: wide, attachedTo: .top, vertical: false)
        XCTAssertTrue(top.contains(CGPoint(x: 7, y: 0.5)))
        XCTAssertFalse(top.contains(CGPoint(x: 7, y: 59.5)))
    }

    func testAnEdgeAcrossTheRunStaysAPill() {
        // A vertical dock stuck to the top mid-drag rotates only on release.
        XCTAssertEqual(DockRail.path(in: rect, attachedTo: .top, vertical: true).boundingBox,
                       rect.insetBy(dx: 0, dy: s))
    }
}

/// The top of the screen when the menu bar hides itself.
final class UsableFrameTests: XCTestCase {
    let screen = CGRect(x: 0, y: 0, width: 1512, height: 982)
    let visible = CGRect(x: 0, y: 0, width: 1512, height: 950)   // 32pt menu-bar band reserved

    func testAnAutoHidingMenuBarFreesTheTopBand() {
        XCTAssertEqual(DockSnap.usableFrame(screen: screen, visible: visible, menuBarAutoHides: true).maxY, 982)
    }

    func testAVisibleMenuBarKeepsItsBand() {
        XCTAssertEqual(DockSnap.usableFrame(screen: screen, visible: visible, menuBarAutoHides: false), visible)
    }

    func testTheDocksReservationIsLeftAlone() {
        let withDock = CGRect(x: 0, y: 70, width: 1512, height: 880)
        let usable = DockSnap.usableFrame(screen: screen, visible: withDock, menuBarAutoHides: true)
        XCTAssertEqual(usable.minY, 70)
        XCTAssertEqual(usable.maxY, 982)
    }

    func testAStuckTopDockReachesTheScreenTop() {
        let usable = DockSnap.usableFrame(screen: screen, visible: visible, menuBarAutoHides: true)
        let step = DockSnap.dragFrame(free: CGRect(x: 700, y: 940, width: 144, height: 68), stuck: nil, in: usable)
        XCTAssertEqual(step.stuck, .top)
        XCTAssertEqual(step.frame.maxY, 982)
    }
}

final class HomeScreenTests: XCTestCase {
    // A common layout: laptop at the origin, a wide monitor to its right.
    private let laptop = CGRect(x: 0, y: 0, width: 1512, height: 982)
    private let wide = CGRect(x: 1512, y: -360, width: 3440, height: 1440)

    func testADockSavedOnTheLaptopBelongsToTheLaptop() {
        let saved = CGRect(x: 1448, y: 598, width: 64, height: 352)   // docked on its right edge
        XCTAssertEqual(DockSnap.homeScreenIndex(for: saved, screens: [laptop, wide]), 0)
    }

    func testAFrameStraddlingTwoDisplaysGoesToTheBiggerShare() {
        let straddling = CGRect(x: 1480, y: 300, width: 100, height: 300)   // 32pt on the laptop, 68 on the wide one
        XCTAssertEqual(DockSnap.homeScreenIndex(for: straddling, screens: [laptop, wide]), 1)
    }

    func testTheSavedFrameIsReadFromAppKitsAutosaveString() {
        XCTAssertEqual(DockSnap.savedFrame(from: "1448 598 64 352 0 0 1512 950 "),
                       CGRect(x: 1448, y: 598, width: 64, height: 352))
        XCTAssertNil(DockSnap.savedFrame(from: nil))
        XCTAssertNil(DockSnap.savedFrame(from: "garbage"))
        XCTAssertNil(DockSnap.savedFrame(from: "10 10 0 0 0 0 1512 950"))
    }

    func testAFrameOnNoDisplayHasNoHome() {
        XCTAssertNil(DockSnap.homeScreenIndex(for: CGRect(x: -5000, y: 0, width: 64, height: 300),
                                              screens: [laptop, wide]))
    }
}
