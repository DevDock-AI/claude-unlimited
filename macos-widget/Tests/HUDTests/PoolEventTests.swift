import XCTest
import SwiftUI
@testable import HUD

/// The detector is what gives a one-shot effect something to fire on. Its
/// whole job is telling "this changed" from "this is what it has always been",
/// so that is what these check.
final class PoolEventTests: XCTestCase {

    private func profile(_ id: String, usage5h: Double?, threshold: Double? = 98) -> Profile {
        Profile(id: id, name: id, kind: "oauth", authMode: "oauth", enabled: true, state: "eligible",
                statusWord: nil, plan: nil, switchThreshold: threshold,
                usage5hPercent: usage5h, usage7dPercent: nil,
                usage5hResetsAt: nil, usage7dResetsAt: nil,
                inUseNow: nil, liveAgents: nil, forcedForSubagents: nil,
                lastModel: nil, lastRequestedModel: nil, lastProject: nil,
                lastUsedAt: nil, agentsSince: nil, modelUsage: nil,
                creditsHas: nil, creditsBalance: nil, spendingOnCredits: nil)
    }

    // MARK: bands

    // The Dashboard's barColor: amber within 15 points of the threshold, red
    // within 5. If these move, app.js must move with them.
    func testBandsFollowTheThreshold() {
        XCTAssertEqual(UsageBand.of(10, threshold: 100), .good)
        XCTAssertEqual(UsageBand.of(84, threshold: 100), .good)
        XCTAssertEqual(UsageBand.of(85, threshold: 100), .warn)
        XCTAssertEqual(UsageBand.of(94, threshold: 100), .warn)
        XCTAssertEqual(UsageBand.of(95, threshold: 100), .bad)
        XCTAssertEqual(UsageBand.of(nil, threshold: 100), .unknown)
    }

    func testAMissingThresholdFallsBackToNinetyEight() {
        XCTAssertEqual(UsageBand.of(92, threshold: nil), .warn)
        XCTAssertEqual(UsageBand.of(93, threshold: nil), .bad)
    }

    // The Dashboard's weeklyBarColor: fixed 75 / 90.
    func testWeeklyBandsAreFixed() {
        XCTAssertEqual(UsageBand.weekly(74), .good)
        XCTAssertEqual(UsageBand.weekly(75), .warn)
        XCTAssertEqual(UsageBand.weekly(90), .bad)
        XCTAssertEqual(UsageBand.weekly(nil), .unknown)
    }

    func testAnAlertingStatusIsRedWhateverThePercentage() {
        let reauth = Profile(from: profile("a", usage5h: nil), usage7d: nil, state: "auth_invalid")
        XCTAssertTrue(reauth.needsReauth)
        XCTAssertEqual(UsageBand.of(reauth, window: .fiveHour), .bad)
        XCTAssertEqual(UsageBand.of(reauth, window: .weekly), .bad)
        let exhausted = Profile(from: profile("b", usage5h: 10), usage7d: nil, statusWord: "exhausted")
        XCTAssertEqual(UsageBand.of(exhausted, window: .fiveHour), .bad)
        let cooling = Profile(from: profile("c", usage5h: 10), usage7d: nil, statusWord: "cooldown")
        XCTAssertEqual(UsageBand.of(cooling, window: .fiveHour), .warn)
        let healthy = Profile(from: profile("d", usage5h: 10), usage7d: 91, statusWord: "healthy")
        XCTAssertFalse(healthy.needsReauth)
        XCTAssertEqual(UsageBand.of(healthy, window: .fiveHour), .good)
        XCTAssertEqual(UsageBand.of(healthy, window: .weekly), .bad)
        XCTAssertEqual(UsageBand.model(80, on: healthy), .warn)
    }

    func testTagColourParsesOnlyHex() {
        XCTAssertNotNil(Color(tagHex: "#43C6FF"))
        XCTAssertNotNil(Color(tagHex: "35D07F"))
        XCTAssertNil(Color(tagHex: nil))
        XCTAssertNil(Color(tagHex: "#FFF"))
        XCTAssertNil(Color(tagHex: "blue"))
    }

    // MARK: the first snapshot

    func testTheFirstPoolProducesNothing() {
        var detector = PoolEventDetector()
        let events = detector.events(for: [profile("a", usage5h: 99)], servingId: "a")
        XCTAssertTrue(events.isEmpty, "the pool arriving is not something changing")
    }

    // MARK: serving

    func testADifferentServingProfileIsAnEvent() {
        var detector = PoolEventDetector()
        _ = detector.events(for: [profile("a", usage5h: 10), profile("b", usage5h: 10)], servingId: "a")
        let events = detector.events(for: [profile("a", usage5h: 10), profile("b", usage5h: 10)], servingId: "b")
        XCTAssertEqual(events, [.servingChanged(from: "a", to: "b")])
    }

    func testTheSameServingProfileIsNotAnEvent() {
        var detector = PoolEventDetector()
        _ = detector.events(for: [profile("a", usage5h: 10)], servingId: "a")
        XCTAssertTrue(detector.events(for: [profile("a", usage5h: 10)], servingId: "a").isEmpty)
    }

    func testNobodyServingProducesNoEvent() {
        var detector = PoolEventDetector()
        _ = detector.events(for: [profile("a", usage5h: 10)], servingId: "a")
        XCTAssertTrue(detector.events(for: [profile("a", usage5h: 10)], servingId: nil).isEmpty)
    }

    // MARK: bands over time

    func testCrossingIntoAnotherBandIsAnEvent() {
        var detector = PoolEventDetector()
        _ = detector.events(for: [profile("a", usage5h: 10)], servingId: "a")
        let events = detector.events(for: [profile("a", usage5h: 88)], servingId: "a")
        XCTAssertEqual(events, [.bandChanged(profileId: "a", from: .good, to: .warn)])
    }

    func testMovingWithinABandIsNotAnEvent() {
        var detector = PoolEventDetector()
        _ = detector.events(for: [profile("a", usage5h: 10)], servingId: "a")
        XCTAssertTrue(detector.events(for: [profile("a", usage5h: 11.4)], servingId: "a").isEmpty,
                      "a reaction per changed decimal would fire on every poll")
    }

    func testANewProfileHasNotCrossedAnything() {
        var detector = PoolEventDetector()
        _ = detector.events(for: [profile("a", usage5h: 10)], servingId: "a")
        let events = detector.events(for: [profile("a", usage5h: 10), profile("b", usage5h: 99)],
                                     servingId: "a")
        XCTAssertTrue(events.isEmpty, "adding an account must not set off a reaction on it")
    }

    // MARK: credits (issue #6)

    func testAProfileOnlyReadsAsOnCreditsWhenTheDaemonSaysSo() {
        // nil is "this backend never mentioned credits" — every Claude
        // account — and must not render as an amber money warning.
        XCTAssertFalse(profile("a", usage5h: 100).isOnCredits)
        XCTAssertNil(profile("a", usage5h: 100).creditsBalanceText)
    }

    func testTheBalanceIsShownToTheCent() {
        let spending = Profile(from: profile("a", usage5h: 100), usage7d: nil,
                               creditsHas: true, creditsBalance: 12.5, spendingOnCredits: true)
        XCTAssertTrue(spending.isOnCredits)
        XCTAssertEqual(spending.creditsBalanceText, "$12.50")
    }

    func testTheWeeklyWindowIsWatchedWhenAsked() {
        var detector = PoolEventDetector()
        var low = profile("a", usage5h: 10); var high = profile("a", usage5h: 10)
        low = Profile(from: low, usage7d: 10); high = Profile(from: high, usage7d: 99)
        _ = detector.events(for: [low], servingId: "a", window: .weekly)
        XCTAssertEqual(detector.events(for: [high], servingId: "a", window: .weekly),
                       [.bandChanged(profileId: "a", from: .good, to: .bad)])
    }
}

private extension Profile {
    /// A copy with a weekly percentage — Profile is Decodable-only, so the
    /// test builds its variants here rather than through JSON.
    init(from other: Profile, usage7d: Double?,
         creditsHas: Bool? = nil, creditsBalance: Double? = nil, spendingOnCredits: Bool? = nil,
         state: String? = nil, statusWord: String? = nil) {
        self.init(id: other.id, name: other.name, kind: other.kind, authMode: other.authMode, enabled: other.enabled,
                  state: state ?? other.state, statusWord: statusWord ?? other.statusWord, plan: other.plan,
                  switchThreshold: other.switchThreshold,
                  usage5hPercent: other.usage5hPercent, usage7dPercent: usage7d,
                  usage5hResetsAt: other.usage5hResetsAt, usage7dResetsAt: other.usage7dResetsAt,
                  inUseNow: other.inUseNow, liveAgents: other.liveAgents,
                  forcedForSubagents: other.forcedForSubagents,
                  lastModel: other.lastModel, lastRequestedModel: other.lastRequestedModel,
                  lastProject: other.lastProject, lastUsedAt: other.lastUsedAt,
                  agentsSince: other.agentsSince, modelUsage: other.modelUsage,
                  creditsHas: creditsHas ?? other.creditsHas,
                  creditsBalance: creditsBalance ?? other.creditsBalance,
                  spendingOnCredits: spendingOnCredits ?? other.spendingOnCredits)
    }
}

final class DockDefaultsTests: XCTestCase {
    private func store() -> UserDefaults {
        let name = "hud.tests.\(UUID().uuidString)"
        let d = UserDefaults(suiteName: name)!
        addTeardownBlock { d.removePersistentDomain(forName: name) }
        return d
    }

    func testAFirstLaunchIsOnTopGlassAndLarge() {
        let d = store()
        XCTAssertTrue(DockDefaults.pinned(d))
        XCTAssertEqual(DockDefaults.theme(d, glassAvailable: true), .glass)
        XCTAssertEqual(DockDefaults.theme(d, glassAvailable: false), .dark)
        XCTAssertNil(DockDefaults.savedTileSize(d))
    }

    func testASavedChoiceAlwaysWins() {
        let d = store()
        d.set(false, forKey: "alwaysOnTop"); d.set(1.0, forKey: "theme"); d.set(28.0, forKey: "tileSize")
        XCTAssertFalse(DockDefaults.pinned(d))
        XCTAssertEqual(DockDefaults.theme(d, glassAvailable: true), .light)
        XCTAssertEqual(DockDefaults.savedTileSize(d), 28)
    }

    func testSavedGlassWithoutGlassFallsBackToDark() {
        let d = store(); d.set(2.0, forKey: "theme")
        XCTAssertEqual(DockDefaults.theme(d, glassAvailable: false), .dark)
    }
}

final class APIProfileTests: XCTestCase {
    private func api(_ json: String) -> Profile {
        let base = #"{"id":"k","name":"k","kind":"api","enabled":true,"state":"eligible","#
        return try! JSONDecoder().decode(Profile.self, from: Data((base + json + "}").utf8))
    }

    func testCostFitsUnderATile() {
        XCTAssertEqual(Fmt.shortMoney(0.4095), "$0.41")
        XCTAssertEqual(Fmt.shortMoney(96.49), "$96")
        XCTAssertEqual(Fmt.shortMoney(1234), "$1.2k")
    }

    func testTheTokenCapDrivesTheRingOnlyWhenSet() {
        let capped = api(#""cost_usd_total":0.41,"tokens_total":450,"token_threshold":500"#)
        XCTAssertTrue(capped.isAPIKey)
        XCTAssertEqual(capped.tokenCapPercent!, 90, accuracy: 1e-9)
        XCTAssertNil(api(#""cost_usd_total":3.2,"tokens_total":900"#).tokenCapPercent)
        XCTAssertNil(api(#""token_threshold":0"#).tokenCapPercent)
    }
}
