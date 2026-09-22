import Combine
import Foundation

/// The three bands a usage percentage can be in, named the way the dock
/// already colours them (`Palette.usage`).
///
/// The rules are the Dashboard's (`barColor` / `weeklyBarColor` in app.js),
/// so a bar that is red in the web UI is red on the dock too:
/// - the 5-hour window is relative to the switch threshold — amber within
///   15 points of it, red within 5;
/// - the weekly and per-model windows have no threshold: amber at 75%, red
///   at 90%;
/// - a status that is already alerting (exhausted, almost exhausted, needs
///   re-auth) is red whatever the percentage; cooldown is amber.
///
/// The colour function recomputes a colour during render and remembers
/// nothing, so "usage just crossed into warn" was not something the HUD could
/// know. It is a band, not a percentage, on purpose: a reaction that fired on
/// every changed decimal would fire on every poll.
enum UsageBand: String, Equatable {
    case unknown, good, warn, bad

    static let almostExhaustedBand = 5.0
    static let nearThresholdBand = 15.0

    /// The 5-hour window, against the profile's switch threshold.
    static func of(_ percent: Double?, threshold: Double?) -> UsageBand {
        guard let percent else { return .unknown }
        let remaining = (threshold ?? 98) - percent
        if remaining <= almostExhaustedBand { return .bad }
        if remaining <= nearThresholdBand { return .warn }
        return .good
    }

    /// Weekly and per-model windows: fixed bands.
    static let weeklyRed = 90.0

    static func weekly(_ percent: Double?) -> UsageBand {
        guard let percent else { return .unknown }
        if percent >= weeklyRed { return .bad }
        if percent >= 75 { return .warn }
        return .good
    }

    /// What the Dashboard would colour this profile's `window` bar.
    static func of(_ profile: Profile, window: RingWindow) -> UsageBand {
        if let alert = status(profile) { return alert }
        let percent = profile.percent(for: window)
        return window == .weekly ? weekly(percent) : of(percent, threshold: profile.switchThreshold)
    }

    /// A per-model window (e.g. Fable's week) on this profile.
    static func model(_ percent: Double?, on profile: Profile) -> UsageBand {
        status(profile) ?? weekly(percent)
    }

    /// The band a status forces regardless of percentage; nil when the
    /// status is neutral (healthy, disabled, unknown) and the number decides.
    static func status(_ profile: Profile) -> UsageBand? {
        if profile.needsReauth { return .bad }
        switch profile.statusWord {
        case "exhausted", "almost exhausted": return .bad
        case "cooldown": return .warn
        default: return nil
        }
    }
}

/// Something worth reacting to, once.
enum PoolEvent: Equatable {
    /// A different profile is now serving. `from` is nil on the first pool
    /// that ever loads — see `PoolEventDetector.events(for:)`.
    case servingChanged(from: String?, to: String)
    /// A profile's usage moved into a different band.
    case bandChanged(profileId: String, from: UsageBand, to: UsageBand)
}

/// Turns successive pool snapshots into the events above.
///
/// Pure and synchronous: it holds the previous serving id and the previous
/// band per profile, and answers what changed. Nothing here touches a view, a
/// clock or the network, so the rules are unit-tested rather than inferred
/// from watching the dock.
struct PoolEventDetector {
    private var servingId: String?
    private var bands: [String: UsageBand] = [:]
    private var seenAPool = false

    init() {}

    /// The events between the last snapshot and this one.
    ///
    /// The first snapshot produces none. It is the pool arriving, not anything
    /// changing — reacting to it would flash every tile once per launch, and
    /// again every time the daemon comes back after being unreachable.
    mutating func events(for profiles: [Profile], servingId currentServing: String?,
                         window: RingWindow = .fiveHour) -> [PoolEvent] {
        var out: [PoolEvent] = []
        let first = !seenAPool
        seenAPool = true

        if let currentServing, currentServing != servingId, !first {
            out.append(.servingChanged(from: servingId, to: currentServing))
        }
        servingId = currentServing

        for profile in profiles {
            let band = UsageBand.of(profile, window: window)
            let previous = bands[profile.id]
            bands[profile.id] = band
            // A profile appearing for the first time has no previous band, so
            // it has not crossed anything: adding an account must not set off
            // a reaction on it.
            guard let previous, previous != band, !first else { continue }
            out.append(.bandChanged(profileId: profile.id, from: previous, to: band))
        }
        return out
    }
}
