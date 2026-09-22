import Foundation

// The daemon's wire shapes, trimmed to what the dock draws. Everything is
// optional that the daemon can legitimately omit — a profile that has never
// served a request has no usage percentage at all, and the dock must show
// "not yet observed" rather than 0%.
struct Profile: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let kind: String            // oauth | codex | api
    /// How this account authenticates: "oauth" / "chatgpt_subscription" /
    /// "api_key". `kind` alone does not say whether tokens are billed — a
    /// Codex account runs either way — and that is what decides whether a
    /// cost figure is money or an equivalent.
    let authMode: String?
    let enabled: Bool
    let state: String           // eligible | draining | exhausted | cooldown | auth_invalid | disabled
    let statusWord: String?
    let plan: String?
    let switchThreshold: Double?
    let usage5hPercent: Double?
    let usage7dPercent: Double?
    let usage5hResetsAt: String?
    let usage7dResetsAt: String?
    let inUseNow: Bool?
    let liveAgents: Int?
    let forcedForSubagents: Bool?
    // The session block: the latest COMPLETED request, and how long the
    // longest-serving live agent has been on this account.
    let lastModel: String?
    let lastRequestedModel: String?
    let lastProject: String?
    let lastUsedAt: String?
    let agentsSince: String?
    /// Per-model weekly windows, e.g. Fable — Claude accounts only, from the
    /// daemon's background usage reads. Information only; nothing routes on it.
    let modelUsage: [ModelUsage]?
    /// Prepaid ChatGPT credits (issue #6). `creditsHas` is nil until a
    /// response has said anything about them — every Claude account, and a
    /// Codex one whose backend stays silent — which is not the same as false.
    /// `spendingOnCredits` is true only while requests are actually being
    /// charged to them, so money is never spent without the dock saying so.
    let creditsHas: Bool?
    let creditsBalance: Double?
    let spendingOnCredits: Bool?
    /// The colour picked for this account in the Dashboard (`#RRGGBB`), or
    /// nil for none. The dock inks the provider mark with it, as the
    /// Dashboard does its profile icon.
    var tagColor: String? = nil
    /// Lifetime spend and tokens at the provider's list rates, and an API
    /// profile's optional token cap. An API key has no usage windows, so
    /// these are what the dock shows for one instead of a percentage.
    var costUsdTotal: Double? = nil
    var tokensTotal: Int? = nil
    var tokenThreshold: Int? = nil

    enum CodingKeys: String, CodingKey {
        case id, name, kind, enabled, state, plan
        case authMode = "auth_mode"
        case statusWord = "status_word"
        case switchThreshold = "switch_threshold"
        case usage5hPercent = "usage_5h_percent"
        case usage7dPercent = "usage_7d_percent"
        case usage5hResetsAt = "usage_5h_resets_at"
        case usage7dResetsAt = "usage_7d_resets_at"
        case inUseNow = "in_use_now"
        case liveAgents = "live_agents"
        case forcedForSubagents = "forced_for_subagents"
        case lastModel = "last_model"
        case lastRequestedModel = "last_requested_model"
        case lastProject = "last_project"
        case lastUsedAt = "last_used_at"
        case agentsSince = "agents_since"
        case modelUsage = "model_usage"
        case creditsHas = "credits_has"
        case creditsBalance = "credits_balance"
        case spendingOnCredits = "spending_on_credits"
        case tagColor = "tag_color"
        case costUsdTotal = "cost_usd_total"
        case tokensTotal = "tokens_total"
        case tokenThreshold = "token_threshold"
    }

    /// The number the dock ring shows: the window that actually governs this
    /// profile. Codex subscriptions report a weekly window and often no 5h one.
    var headlinePercent: Double? { usage5hPercent ?? usage7dPercent }
    var headlineResetsAt: String? { usage5hPercent != nil ? usage5hResetsAt : usage7dResetsAt }
    var isServing: Bool { inUseNow == true }
    /// Its credentials were refused: nothing is served from it until the
    /// user signs in again, and the dock says so in red.
    var needsReauth: Bool { state == "auth_invalid" || statusWord == "needs re-auth" }
    /// An API key: billed per token, no plan windows to draw.
    var isAPIKey: Bool { kind == "api" }
    /// Share of an API profile's token cap used, 0–100+; nil without a cap.
    var tokenCapPercent: Double? {
        guard let cap = tokenThreshold, cap > 0 else { return nil }
        return Double(tokensTotal ?? 0) / Double(cap) * 100
    }

    /// Shown on the detail card whenever this account's requests are being
    /// paid for per-request rather than by its plan.
    var isOnCredits: Bool { spendingOnCredits == true }
    var creditsBalanceText: String? {
        guard let balance = creditsBalance else { return nil }
        return String(format: "$%.2f", balance)
    }

    /// The window the headline number comes from when nobody has chosen one.
    var headlineWindow: RingWindow { usage5hPercent != nil ? .fiveHour : .weekly }

    func percent(for window: RingWindow) -> Double? {
        window == .fiveHour ? usage5hPercent : usage7dPercent
    }

    /// Does the provider report this window at all? Codex subscriptions often
    /// have no 5-hour window, and offering to show one would draw an empty ring.
    func reports(_ window: RingWindow) -> Bool { percent(for: window) != nil }
}

struct ModelUsage: Decodable, Equatable {
    let name: String
    let percent: Double
    let resetsAt: String?
    let active: Bool?

    enum CodingKeys: String, CodingKey {
        case name, percent, active
        case resetsAt = "resets_at"
    }
}

/// Which usage window a tile's ring shows. Chosen per profile from the tile's
/// right-click menu; unset means the headline window.
enum RingWindow: String, CaseIterable {
    case fiveHour = "5h"
    case weekly = "7d"

    var label: String { self == .fiveHour ? "5-hour" : "Weekly" }
}

struct Status: Decodable {
    let version: String
    let currentProfileId: String?
    let currentProfileName: String?
    let uptimeSeconds: Double?
    /// Per daemon run. Needed for the widget's two writes, "Take over" and
    /// Enable/Disable.
    let csrfToken: String?

    enum CodingKeys: String, CodingKey {
        case version
        case csrfToken = "csrf_token"
        case currentProfileId = "current_profile_id"
        case currentProfileName = "current_profile_name"
        case uptimeSeconds = "uptime_seconds"
    }
}

/// What a profile spent in the selected window — the card's "today" line.
struct Spend: Equatable {
    let cost: Double
    let requests: Int
    let tokens: Int
}

private struct ProfilesEnvelope: Decodable { let profiles: [Profile] }

private struct StatsEnvelope: Decodable {
    struct Row: Decodable {
        let key: String
        let costUsd: Double?
        let requests: Int?
        let tokens: Int?
        enum CodingKeys: String, CodingKey {
            case key, requests, tokens
            case costUsd = "cost_usd"
        }
    }
    let byProfile: [Row]?
    enum CodingKeys: String, CodingKey { case byProfile = "by_profile" }
}

enum PoolState: Equatable {
    case loading
    case ready(profiles: [Profile], status: Status?)
    case unreachable(String)

    static func == (a: PoolState, b: PoolState) -> Bool {
        switch (a, b) {
        case (.loading, .loading): return true
        case let (.ready(p1, _), .ready(p2, _)): return p1 == p2
        case let (.unreachable(m1), .unreachable(m2)): return m1 == m2
        default: return false
        }
    }
}

/// Polls the local daemon. Loopback-only, and no credentials ever leave the
/// daemon — the dock reads what the dashboard already shows.
///
/// It makes exactly TWO writes, both from a tile's right-click menu and both
/// the Dashboard's own endpoints: "Take over"
/// (`POST /api/profiles/<id>/take-over`) and Enable/Disable
/// (`PATCH /api/profiles/<id>` with `{"enabled": <bool>}`). Both are CSRF-gated
/// like every Dashboard write; the widget has no page to read the token from,
/// so it uses the `csrf_token` that `/api/status` already serves to any local
/// caller. Nothing else here changes daemon state.
@MainActor
final class PoolClient: ObservableObject {
    @Published private(set) var state: PoolState = .loading
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var spendToday: [String: Spend] = [:]

    private let base: URL
    /// Injected so tests can stand a `URLProtocol` stub in front of the
    /// daemon; production always gets `.shared`.
    private let session: URLSession
    private var csrfToken: String?
    /// Consecutive failed polls. The dock keeps showing the last good data
    /// until this passes `failuresBeforeUnreachable`: one slow or timed-out
    /// poll (a busy machine, a daemon restart) should not blank the dock back
    /// to its loading state, which is what it used to do every time.
    private var consecutiveFailures = 0
    private let failuresBeforeUnreachable = 3
    private var timer: Timer?
    /// Idle cadence. The daemon answers these reads from memory in tens of
    /// milliseconds, and at 30s the dock visibly lagged the Dashboard (which
    /// polls every second) — an account could fill up and hand over with the
    /// dock still showing the old one as serving.
    static let idleInterval: TimeInterval = 5
    static let activeInterval: TimeInterval = 2
    /// Today's spend is a SQL aggregate, not a memory read, and it moves
    /// slowly — it does not need the tile cadence.
    static let spendInterval: TimeInterval = 30
    private var interval: TimeInterval = PoolClient.idleInterval
    private var spendFetchedAt: Date?
    /// A slow poll (12s timeout) must not stack up behind the fast timer.
    /// A refresh asked for while one is in flight (a Take over) is not
    /// dropped: it runs once more afterwards, so it reads the new state.
    private var refreshing = false
    private var refreshAgain = false

    /// `autoStart` exists for tests only: a client that polls a real port on
    /// `init` makes every assertion race a background refresh.
    /// The daemon's port: 4317, unless `HUD_PORT` says otherwise — used to
    /// point a second copy at a demo daemon without touching the real one.
    nonisolated static var defaultPort: Int {
        ProcessInfo.processInfo.environment["HUD_PORT"].flatMap(Int.init) ?? 4317
    }

    init(port: Int = PoolClient.defaultPort, session: URLSession = .shared, autoStart: Bool = true) {
        self.base = URL(string: "http://127.0.0.1:\(port)")!
        self.session = session
        // Poll from launch, at the idle cadence. Without this the only thing
        // that ever started the timer was the popover appearing, so the menu
        // bar sat on the loading glyph and never showed a number until you
        // clicked it — which is the one moment you don't need it.
        if autoStart { start() }
    }

    /// Fast while someone is looking at it, slow when it is just sitting on
    /// screen — the daemon is a local process, but there is no reason to wake
    /// it twice a second for a dock nobody is hovering.
    func setActive(_ active: Bool) {
        let wanted = active ? Self.activeInterval : Self.idleInterval
        guard wanted != interval else { return }   // restarting the timer would refresh on every hover
        interval = wanted
        if timer != nil { start() }
    }

    /// Stop polling entirely. The dock calls this when it is hidden: a HUD
    /// nobody can see was still waking the daemon every few seconds, and with
    /// animated surfaces on top of it that cost stops being theoretical.
    func stop() {
        timer?.invalidate()
        timer = nil
    }

    /// Whether the poll timer is running. The dock reads it to decide whether
    /// becoming visible needs a restart.
    var isPolling: Bool { timer != nil }

    func start() {
        timer?.invalidate()
        Task { await refresh() }
        let t = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in await self.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func refresh() async {
        guard !refreshing else { refreshAgain = true; return }
        refreshing = true
        await poll()
        refreshing = false
        if refreshAgain {
            refreshAgain = false
            await refresh()
        }
    }

    private func poll() async {
        do {
            async let profiles: [Profile] = get("/api/profiles", as: ProfilesEnvelope.self).profiles
            async let status: Status = get("/api/status", as: Status.self)
            let loaded = try? await status
            state = .ready(profiles: try await profiles, status: loaded)
            if let token = loaded?.csrfToken { csrfToken = token }
            lastUpdated = Date()
            consecutiveFailures = 0
        } catch {
            consecutiveFailures += 1
            let refused = (error as NSError).code == NSURLErrorCannotConnectToHost
            // Nothing to fall back on, or the daemon is really gone: say so.
            // Otherwise hold the last reading — the footer already says how
            // old it is.
            if case .ready = state, !refused, consecutiveFailures < failuresBeforeUnreachable {
                return
            }
            state = .unreachable(refused ? "Daemon not running" : error.localizedDescription)
            return
        }
        // Spend is a nice-to-have on top of a working poll: an older daemon
        // without /api/usage/stats must not knock the dock into its error
        // state, so this is deliberately a separate, swallowed request.
        if let at = spendFetchedAt, Date().timeIntervalSince(at) < Self.spendInterval { return }
        spendFetchedAt = Date()
        if let stats = try? await get("/api/usage/stats?range=1d", as: StatsEnvelope.self) {
            var out: [String: Spend] = [:]
            for row in stats.byProfile ?? [] {
                out[row.key] = Spend(cost: row.costUsd ?? 0,
                                     requests: row.requests ?? 0,
                                     tokens: row.tokens ?? 0)
            }
            spendToday = out
        }
    }

    private func get<T: Decodable>(_ path: String, as: T.Type) async throws -> T {
        // Not appendingPathComponent: it percent-escapes the query string.
        guard let url = URL(string: base.absoluteString + path) else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        // Generous: a busy machine can make the daemon's own poll handler
        // slow, and a timeout here costs the dock its data.
        request.timeoutInterval = 12
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    /// The Dashboard's "Take over": make this profile the one serving, now.
    /// Throws with the daemon's own message (a disabled profile, an unknown
    /// id) so the caller can show it verbatim.
    func takeOver(_ profileId: String) async throws {
        try await write(profileId, suffix: "/take-over", method: "POST", body: nil)
    }

    /// The Dashboard's per-profile Enable/Disable switch. The daemon's PATCH
    /// takes a real JSON boolean — `"false"` is a validation error, not a
    /// falsey value — and the same endpoint serves every `Profile.kind`,
    /// because `kind` is not patchable and nothing here touches credentials.
    func setEnabled(_ enabled: Bool, for profileId: String) async throws {
        try await write(profileId, suffix: "", method: "PATCH", body: ["enabled": enabled])
    }

    /// One CSRF-gated write against `/api/profiles/<id>`, shared by both
    /// mutations so neither can drift into its own retry or error handling.
    private func write(_ profileId: String, suffix: String, method: String,
                       body payload: [String: Any]?) async throws {
        guard let id = profileId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: base.absoluteString + "/api/profiles/\(id)\(suffix)")
        else { throw URLError(.badURL) }
        let encoded = try payload.map { try JSONSerialization.data(withJSONObject: $0) }

        func send(_ token: String?) async throws -> (Int, [String: Any]) {
            var request = URLRequest(url: url)
            request.httpMethod = method
            request.timeoutInterval = 8
            if let token { request.setValue(token, forHTTPHeaderField: "X-CSRF-Token") }
            if let encoded {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = encoded
            }
            let (data, response) = try await session.data(for: request)
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            return ((response as? HTTPURLResponse)?.statusCode ?? 0, body)
        }

        if csrfToken == nil { csrfToken = try? await get("/api/status", as: Status.self).csrfToken }
        var (code, body) = try await send(csrfToken)
        if code == 403, body["error"] as? String == "csrf" {
            // The token rotates per daemon run: a restart since the last poll
            // is the ordinary cause. Fetch the new one and try exactly once more.
            csrfToken = try await get("/api/status", as: Status.self).csrfToken
            (code, body) = try await send(csrfToken)
        }
        guard code == 200 else {
            throw TakeOverError(message: body["message"] as? String ?? "The daemon answered HTTP \(code).")
        }
        await refresh()
    }

    func openDashboard() {
        NSWorkspaceOpen(base)
    }
}

struct TakeOverError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

#if canImport(AppKit)
import AppKit
func NSWorkspaceOpen(_ url: URL) { NSWorkspace.shared.open(url) }
#else
func NSWorkspaceOpen(_ url: URL) {}
#endif

// MARK: - formatting

enum Fmt {
    /// "2d 11h" / "3h 40m" / "51m" — the same shape the dashboard uses, so the
    /// two never disagree about how long is left.
    static func until(_ iso: String?) -> String {
        guard let iso, let date = isoDate(iso) else { return "—" }
        let seconds = Int(date.timeIntervalSinceNow)
        if seconds <= 0 { return "now" }
        let d = seconds / 86400, h = (seconds % 86400) / 3600, m = (seconds % 3600) / 60
        if d > 0 { return "\(d)d \(h)h" }
        if h > 0 { return "\(h)h \(m)m" }
        return "\(m)m"
    }

    /// "42m" / "3h 5m" / "2d 1h" since a moment — how long something has run.
    /// `coarse` keeps only the leading unit ("2h"), for "used 2h ago".
    static func elapsed(since iso: String?, coarse: Bool = false) -> String? {
        guard let iso, let date = isoDate(iso) else { return nil }
        let seconds = max(Int(Date().timeIntervalSince(date)), 0)
        let d = seconds / 86400, h = (seconds % 86400) / 3600, m = (seconds % 3600) / 60
        if d > 0 { return coarse ? "\(d)d" : "\(d)d \(h)h" }
        if h > 0 { return coarse ? "\(h)h" : "\(h)h \(m)m" }
        return m > 0 ? "\(m)m" : "<1m"
    }

    /// "claude-opus-5" -> "opus-5". The vendor prefix repeats what the logo
    /// already says, and the card has one line to spend.
    static func model(_ id: String?) -> String? {
        guard let id, !id.isEmpty else { return nil }
        return id.hasPrefix("claude-") ? String(id.dropFirst("claude-".count)) : id
    }

    /// The last path component of a project, which is what a person calls it.
    static func project(_ path: String?) -> String? {
        guard let path, !path.isEmpty else { return nil }
        let name = (path as NSString).lastPathComponent
        return name.isEmpty ? path : name
    }

    static func percent(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int(value.rounded()))%"
    }

    /// Cents matter here — a dock that rounds $0.04 to $0 looks broken.
    static func money(_ value: Double) -> String {
        String(format: "$%.2f", value)
    }

    /// A cost that has to fit under a tile: cents while it is small, whole
    /// dollars once it is not, thousands abbreviated.
    static func shortMoney(_ value: Double) -> String {
        switch value {
        case ..<10:     return String(format: "$%.2f", value)
        case ..<1000:   return String(format: "$%.0f", value)
        default:        return String(format: "$%.1fk", value / 1000)
        }
    }

    static func compact(_ value: Int) -> String {
        switch value {
        case 1_000_000...:  return String(format: "%.1fM", Double(value) / 1_000_000)
        case 1_000...:      return String(format: "%.0fK", Double(value) / 1_000)
        default:            return "\(value)"
        }
    }

    static func ago(_ date: Date?) -> String {
        guard let date else { return "never updated" }
        let seconds = Int(Date().timeIntervalSince(date))
        if seconds < 5 { return "updated just now" }
        if seconds < 60 { return "updated \(seconds)s ago" }
        return "updated \(seconds / 60)m ago"
    }

    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let isoPlain = ISO8601DateFormatter()

    static func isoDate(_ s: String) -> Date? {
        isoFractional.date(from: s) ?? isoPlain.date(from: s)
    }
}

import SwiftUI

extension Color {
    /// `#RRGGBB` (the only shape the Dashboard's tag picker produces); nil for
    /// anything else, so a malformed value falls back rather than going black.
    init?(tagHex hex: String?) {
        guard var s = hex?.trimmingCharacters(in: .whitespaces) else { return nil }
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        self.init(red: Double((v >> 16) & 0xFF) / 255,
                  green: Double((v >> 8) & 0xFF) / 255,
                  blue: Double(v & 0xFF) / 255)
    }
}
