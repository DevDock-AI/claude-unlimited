import SwiftUI
import AppKit

/// The product name, everywhere a person reads it.
let hudName = "HUD - Heads-Up Display"

// The dashboard's own tokens, so the dock and the web UI never drift apart.
enum Palette {
    static let bg        = Color(red: 0.039, green: 0.039, blue: 0.047)   // #0A0A0C
    static let panel     = Color(red: 0.075, green: 0.075, blue: 0.086)   // #131316
    static let text      = Color(red: 0.949, green: 0.949, blue: 0.953)   // #F2F2F3
    static let textDim   = Color(red: 0.604, green: 0.604, blue: 0.631)   // #9A9AA1
    static let textFaint = Color(red: 0.431, green: 0.431, blue: 0.463)   // #6E6E76
    static let accent    = Color(red: 0.263, green: 0.776, blue: 1.0)     // #43C6FF
    static let warn      = Color(red: 1.0,   green: 0.690, blue: 0.125)   // #FFB020
    static let good      = Color(red: 0.208, green: 0.816, blue: 0.498)   // #35D07F
    static let bad       = Color(red: 1.0,   green: 0.361, blue: 0.361)   // #FF5C5C
    static let codex     = Color(red: 0.184, green: 0.851, blue: 0.769)   // #2FD9C4
    static let claude    = Color(red: 0.851, green: 0.467, blue: 0.341)   // #D97757

    // `track`, `hairline` and `hairSoft` used to live here as fixed
    // white-on-dark values. They are surfaces, not meanings, so they moved to
    // `DockPalette`, which has both the dark and the light version of each —
    // keeping a copy here would be a second definition to drift from.

    /// Colour by how close the window is to the switch threshold, not by an
    /// arbitrary band: 98% is where rotation actually happens.
    static func usage(_ percent: Double?, threshold: Double?) -> Color {
        switch UsageBand.of(percent, threshold: threshold) {
        case .unknown: return textFaint
        case .good:    return good
        case .warn:    return warn
        case .bad:     return bad
        }
    }

    static func kind(_ kind: String) -> Color {
        switch kind {
        case "codex": return codex
        case "api":   return textDim
        default:      return accent
        }
    }
}

/// How far a usage fill reaches across a bar of a given width. Pure, so the
/// clamping rules are tested rather than eyeballed: a percentage above 100 never
/// overflows, anything above zero keeps a 3pt sliver so it does not read as
/// empty, and an unknown percentage fills the bar (there is no progress to draw,
/// only the fact that this account is the one serving).
enum UsageFill {
    static func width(_ percent: Double?, total: CGFloat) -> CGFloat {
        guard total > 0 else { return 0 }
        guard let percent else { return total }
        guard percent > 0 else { return 0 }
        return min(max(total * CGFloat(percent) / 100, 3), total)
    }

    /// The same reach as a 0...1 fraction, which is what a progress effect
    /// wants. Derived from `width` rather than from the percentage again, so
    /// the two arms of an effect cannot disagree about where a fill ends.
    static func fraction(_ width: CGFloat, of total: CGFloat) -> Double {
        guard total > 0 else { return 0 }
        return min(max(Double(width / total), 0), 1)
    }
}

/// Logo in a soft disc with its usage ring drawn concentric around it.
struct ProfileTile: View {
    let profile: Profile
    let selected: Bool
    var diameter: CGFloat = 60
    var palette: DockPalette
    /// Which usage window the ring draws — the tile's right-click choice.
    var window: RingWindow? = nil
    /// The dock is on screen. Effects animate nothing when it is not.
    var dockVisible: Bool = true
    /// The latest reaction token for this account, from `DockController`. A new
    /// one means it just became the account that serves.
    var reaction: UUID? = nil

    private var shown: Double? { window.map(profile.percent(for:)) ?? profile.headlinePercent }
    /// Both windows reported: 5-hour outside, weekly inside. Otherwise the
    /// one window there is gets the single outer ring.
    private var dual: Bool { profile.usage5hPercent != nil && profile.usage7dPercent != nil }
    private var outerWindow: RingWindow { dual ? .fiveHour : profile.headlineWindow }
    private var outerPercent: Double {
        profile.isAPIKey ? (profile.tokenCapPercent ?? 0) : (profile.percent(for: outerWindow) ?? 0)
    }
    private var innerPercent: Double { profile.usage7dPercent ?? 0 }
    private func tint(_ c: Color) -> Color { profile.enabled ? c : palette.textFaint }
    /// The colour of the window the label shows — the serving orbs use it too.
    private var ringColor: Color { tint(palette.ring(profile, window: window ?? profile.headlineWindow)) }
    private var ringWidth: CGFloat { max(diameter * 0.055, 3.5) }
    private var innerWidth: CGFloat { max(ringWidth * 0.6, 2.2) }
    /// Inner ring's inset from the tile edge: clear of the outer ring by a
    /// hairline gap.
    private var innerInset: CGFloat { ringWidth + 1.5 + innerWidth / 2 }

    var body: some View {
        ZStack {
            TileDisc(diameter: diameter,
                     fill: selected || profile.isServing ? palette.tileFillActive : palette.tileFill)

            // A refused sign-in has no usage to draw; the track itself goes
            // red so the tile reads as broken, not as empty.
            Circle().stroke(profile.needsReauth ? palette.bad.opacity(0.55) : palette.ringTrack,
                            lineWidth: ringWidth)
                .padding(ringWidth / 2)
            Circle().trim(from: 0, to: min(outerPercent / 100, 1))
                .stroke(tint(profile.isAPIKey ? palette.apiRing(profile) : palette.ring(profile, window: outerWindow)),
                        style: StrokeStyle(lineWidth: ringWidth, lineCap: .round))
                .padding(ringWidth / 2)
                .rotationEffect(.degrees(-90))
                .animation(.easeOut(duration: 0.4), value: outerPercent)

            if dual {
                Circle().stroke(palette.ringTrack.opacity(0.7), lineWidth: innerWidth)
                    .padding(innerInset)
                Circle().trim(from: 0, to: min(innerPercent / 100, 1))
                    .stroke(tint(palette.weeklyRing(profile)),
                            style: StrokeStyle(lineWidth: innerWidth, lineCap: .round))
                    .padding(innerInset)
                    .rotationEffect(.degrees(-90))
                    .animation(.easeOut(duration: 0.4), value: innerPercent)
            }

            // Serving accounts alternate their logo with the orbs. Accounts
            // that are not serving keep a static logo.
            if profile.isServing {
                ServingOrbs(diameter: diameter, accent: ringColor,
                            isDark: palette.isDark, visible: dockVisible,
                            fallbackKind: profile.kind,
                            dot: palette.orbDot, mark: palette.mark(profile))
            } else {
                ProviderMark(kind: profile.kind, size: diameter * 0.42,
                             color: palette.mark(profile))
                    .opacity(profile.enabled ? 1 : 0.4)
                    .saturation(profile.enabled ? 1 : 0)
            }
        }
        .animation(.easeOut(duration: 0.35), value: profile.isServing)
        .frame(width: diameter, height: diameter)
        .scaleEffect(selected ? 1.04 : 1)
        .animation(.spring(response: 0.3, dampingFraction: 0.8), value: selected)
    }
}

// MARK: - the dock: a slim column that stays on screen

/// Internal kind -> what a person calls it.
func kindLabel(_ kind: String) -> String {
    switch kind {
    case "oauth": return "Claude"
    case "codex": return "Codex"
    case "api":   return "API"
    default:      return kind.isEmpty ? "" : kind.prefix(1).uppercased() + kind.dropFirst()
    }
}

struct DockColumn: View {
    @ObservedObject var client: PoolClient
    @ObservedObject var controller: DockController
    @State private var hovered: String?
    @State private var hoveringDock = false
    @State private var hoveringCog = false
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    /// Liquid Glass follows the system appearance, so the dock's colours are
    /// resolved per render rather than fixed by the theme. SwiftUI re-renders
    /// this on its own when the appearance changes; no observer is needed.
    @Environment(\.colorScheme) private var colorScheme

    /// What the chosen theme means right now. Every colour below comes from
    /// here — nothing reads `controller.theme` for a colour.
    private var palette: DockPalette {
        DockPalette.resolve(theme: controller.theme, transparency: controller.transparency,
                            systemIsDark: colorScheme == .dark)
    }

    var body: some View {
        // One stack that flips axis: the dock is the same thing either way,
        // only the direction it runs in changes.
        Group {
            if controller.orientation.isVertical {
                VStack(spacing: 4) {
                    content
                }
                .padding(.horizontal, 8)
                .padding(.top, 9)
                .padding(.bottom, 9)
                // Overlaid, not stacked: a reserved row cost vertical space
                // permanently for chrome that is only wanted on hover.
                .overlay(alignment: .top) { head }
                .padding(.vertical, DockRail.shoulder)   // room for the edge shoulders
                .frame(width: controller.tileSize + 22)
            } else {
                HStack(spacing: 6) {
                    content
                }
                .padding(.vertical, 6)
                .padding(.horizontal, 9)
                .overlay(alignment: .trailing) { head }
                .padding(.horizontal, DockRail.shoulder)
                .frame(height: controller.tileSize + 34)
            }
        }
        .background(background)
        .onHover { inside in
            hoveringDock = inside
            // Someone is looking at it: poll at the fast cadence while the
            // pointer is on the dock, and drop back to idle when it leaves.
            // `setActive` was written for this and had no caller at all, so
            // the dock had been polling at 30s in every state.
            client.setActive(inside)
            if !inside { hovered = nil; controller.hoveredProfileId = nil; controller.hideCard() }
        }
    }

    /// Liquid Glass where the system has it and the user has not asked for
    /// less transparency; the frosted blur everywhere else.
    ///
    /// Both branches draw the SAME scrim — `palette.base` at `palette.scrim` —
    /// so glass falling back to the blur cannot end up a different colour from
    /// the text sitting on it. That was two separate expressions before, and
    /// keeping them apart is how they would drift.
    @ViewBuilder
    private var background: some View {
        let shape = RailShape(edge: controller.attachedEdge, vertical: controller.orientation.isVertical)
        let scrim = shape.fill(palette.base.opacity(palette.scrim))
        if controller.theme == .glass && GlassBackground.isAvailable && !reduceTransparency {
            GlassBackground(shape: shape).overlay(scrim)
        } else {
            DesktopBlur(shape: shape, dark: palette.isDark,
                        opacity: controller.transparency.blurAlpha)
                .overlay(scrim)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch client.state {
        case .loading:
            HUDLoader(size: 26, color: palette.textFaint,
                      visible: controller.visible) {
                ProgressView().controlSize(.small)
            }
            .frame(width: 40, height: 40)
        case .unreachable:
            HUDLoader(size: 26, color: Palette.warn, visible: controller.visible) {
                Image(systemName: "bolt.horizontal.circle")
                    .font(.system(size: 20)).foregroundStyle(Palette.warn)
            }
            .frame(width: 40, height: 40)
            .help("Daemon not running — start it with `cu start`")
        case .ready(let profiles, _):
            tiles(visible(profiles))
        }
    }

    private var head: some View {
        let cog = CogButton(hovering: $hoveringCog, palette: palette) { showMenu($0) }
            .frame(width: 24, height: 16)
            .opacity(hoveringDock ? 1 : 0)
            .animation(.easeInOut(duration: 0.18), value: hoveringDock)
        return Group {
            if controller.orientation.isVertical {
                HStack { Spacer(); cog }.padding(.trailing, 2)
            } else {
                VStack { cog; Spacer() }.padding(.top, 2)
            }
        }
    }

    /// Built as an NSMenu so the cog can be an ordinary button: SwiftUI's Menu
    /// intercepts pointer tracking, which is why the hover effect never fired.
    ///
    /// Anything with more than one choice is a submenu, so the root stays a
    /// short list of what you can change rather than a wall of every value.
    private func showMenu(_ sender: NSView) {
        let menu = NSMenu()

        menu.addItem(toggle("Always on top", on: controller.pinned,
                            action: #selector(MenuActions.togglePin)))
        menu.addItem(toggle("Hide disabled", on: controller.hideDisabled,
                            action: #selector(MenuActions.toggleHideDisabled)))

        menu.addItem(.separator())
        menu.addItem(submenu("Size", options: DockSize.allCases.map {
            ($0.label, $0.rawValue, controller.tileSize == $0.rawValue)
        }, action: #selector(MenuActions.setSize(_:))))
        let orientation = submenu("Orientation", options: DockOrientation.allCases.map {
            ($0.label, $0.rawValue, controller.orientation == $0)
        }, action: #selector(MenuActions.setOrientation(_:)))
        if let edge = controller.dockedEdge {
            // Docked, the edge decides which way the dock runs; a choice here
            // would contradict it. Undocking hands the choice back.
            orientation.title = "Orientation — follows the \(edge.label) edge"
            orientation.isEnabled = false
            orientation.submenu = nil
        }
        menu.autoenablesItems = false
        menu.addItem(orientation)
        menu.addItem(submenu("Background", options: DockTheme.available.map {
            ($0.label, $0.rawValue, controller.theme == $0)
        }, action: #selector(MenuActions.setTheme(_:))))
        menu.addItem(submenu("Transparency", options: DockTransparency.allCases.map {
            ($0.label, $0.rawValue, controller.transparency == $0)
        }, action: #selector(MenuActions.setTransparency(_:))))

        if let edge = controller.dockedEdge {
            menu.addItem(plain("Undock from the \(edge.label) edge", action: #selector(MenuActions.undock)))
        }

        menu.addItem(.separator())
        menu.addItem(plain("Open dashboard", action: #selector(MenuActions.openDashboard)))
        menu.addItem(.separator())
        menu.addItem(plain("Hide dock", action: #selector(MenuActions.hideDock)))
        menu.addItem(plain("Quit \(hudName)", action: #selector(MenuActions.quit)))

        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: sender.bounds.height + 4), in: sender)
    }

    private func plain(_ title: String, action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = MenuActions.shared
        return item
    }

    private func toggle(_ title: String, on: Bool, action: Selector) -> NSMenuItem {
        let item = plain(title, action: action)
        item.state = on ? .on : .off
        return item
    }

    /// A parent item whose submenu opens on hover, the way macOS menus work.
    private func submenu(_ title: String,
                         options: [(String, Double, Bool)],
                         action: Selector) -> NSMenuItem {
        let parent = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let sub = NSMenu(title: title)
        for (label, value, isOn) in options {
            let item = NSMenuItem(title: label, action: action, keyEquivalent: "")
            item.state = isOn ? .on : .off
            item.representedObject = value
            item.target = MenuActions.shared
            sub.addItem(item)
        }
        parent.submenu = sub
        return parent
    }

    @ViewBuilder
    private func tiles(_ profiles: [Profile]) -> some View {
        let cells = ForEach(profiles) { profile in
            let window = controller.ringWindow(for: profile)
            let shown = profile.percent(for: window)
            VStack(spacing: 5) {
                ProfileTile(profile: profile, selected: hovered == profile.id,
                            diameter: controller.tileSize, palette: palette, window: window,
                            dockVisible: controller.visible,
                            reaction: controller.reactions[profile.id])
                Group {
                    if profile.needsReauth {
                        Text("Reauth").foregroundStyle(palette.bad)
                    } else if !profile.enabled {
                        Text("off").foregroundStyle(palette.textFaint)
                    } else if profile.isAPIKey {
                        // No plan windows: what it has cost so far, at list
                        // rates. The ring, when there is one, is the token cap.
                        Text(Fmt.shortMoney(profile.costUsdTotal ?? 0))
                            .foregroundStyle(palette.apiRing(profile))
                    } else {
                        // Subscript names the window the number is: the rings
                        // show both, the number only one (right-click picks).
                        (Text(Fmt.percent(shown))
                         + Text(window == .weekly ? "w" : "5h")
                            .font(.system(size: 8.5, weight: .semibold))
                            .baselineOffset(-2.5))
                            .foregroundStyle(palette.ring(profile, window: window))
                    }
                }
                    .font(.system(size: 13, weight: .bold)).monospacedDigit()
                    .lineLimit(1).minimumScaleFactor(0.75)
            }
            .frame(width: controller.orientation.isVertical ? nil : max(controller.tileSize, Self.labelWidth))
            .frame(maxWidth: controller.orientation.isVertical ? .infinity : nil)
            .contentShape(Rectangle())
            .onHover { inside in
                if inside {
                    hovered = profile.id
                    controller.hoveredProfileId = profile.id
                    controller.showCard()
                } else if controller.hoveredProfileId == profile.id {
                    // Only clear our own id: moving tile to tile can deliver
                    // the next tile's enter before this one's exit.
                    controller.hoveredProfileId = nil
                }
            }
        }

        if controller.orientation.isVertical {
            ScrollView(.vertical, showsIndicators: false) {
                VStack(spacing: 14) { cells }.padding(.vertical, 2)
            }
            .frame(height: Self.runLength(for: profiles.count, tile: controller.tileSize, vertical: true))
        } else {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 14) { cells }.padding(.horizontal, 2)
            }
            .frame(width: Self.runLength(for: profiles.count, tile: controller.tileSize, vertical: false))
        }
    }

    /// The run length of the tile strip. The two axes do NOT share a metric:
    /// stacked vertically a cell is tile + gap + label height, but laid out in
    /// a row a cell is only as wide as the tile (or the widest label it has to
    /// hold). Using the vertical figure for the row's width left dead space
    /// on the trailing edge.
    static func runLength(for count: Int, tile: CGFloat, vertical: Bool) -> CGFloat {
        guard count > 0 else { return 0 }
        let cell = vertical ? tile + 6 + 17 : max(tile, Self.labelWidth)
        let gaps = CGFloat(count - 1) * 14
        return min(CGFloat(count) * cell + gaps + 4, vertical ? 1000 : 1400)
    }

    /// Room for the widest percentage a tile can show ("100%").
    /// Wide enough for the longest label a tile can show — "100%" plus its
    /// `5h` subscript, or "Reauth". At 40 a horizontal dock cut both off
    /// ("Rea…"); the label may still shrink a little rather than truncate.
    static let labelWidth: CGFloat = 50

    private func visible(_ profiles: [Profile]) -> [Profile] {
        let shown = controller.hideDisabled ? profiles.filter(\.enabled) : profiles
        // Whoever is serving leads, then the enabled ones: the thing you keep
        // the dock on screen to watch should never need scrolling to.
        return shown.sorted { a, b in
            if a.isServing != b.isServing { return a.isServing }
            if a.enabled != b.enabled { return a.enabled }
            return (a.headlinePercent ?? 0) > (b.headlinePercent ?? 0)
        }
    }
}

// MARK: - the hover card

struct DetailCard: View {
    let profile: Profile
    let spend: Spend?
    let updated: Date?
    var ringWindow: RingWindow = .fiveHour
    /// The dock's resolved colours. The card is the same dock in a second
    /// window, so it takes the same palette rather than being fixed dark —
    /// which is what made a light dock open a black card.
    ///
    /// Only the SURFACE colours come from here. The meaningful ones —
    /// `Palette.usage`, `Palette.good`, `Palette.warn`, the provider marks —
    /// mean the same thing on either background and stay as they are.
    let palette: DockPalette

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            creditsBanner
            session
            today
            meters
            footer
        }
        .padding(16)
        .frame(width: 320, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .fill(palette.cardFill))
                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .strokeBorder(palette.hairline, lineWidth: 1))
        )
    }

    private var header: some View {
        HStack(spacing: 9) {
            ProviderMark(kind: profile.kind, size: 20, color: palette.mark(profile))
                .frame(width: 22, height: 22)
            Text(profile.name)
                .font(.system(size: 16, weight: .bold)).foregroundStyle(palette.text)
                .lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 6)
            if let plan = planLabel(profile) {
                Text(plan)
                    .font(.system(size: 10.5, weight: .bold))
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(palette.chipFill, in: RoundedRectangle(cornerRadius: 6))
                    .foregroundStyle(palette.text)
            }
            Text(profile.enabled ? kindLabel(profile.kind) : "disabled")
                .font(.system(size: 10.5, weight: .bold))
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(tagColor.opacity(0.15), in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(tagColor.opacity(0.4), lineWidth: 1))
                .foregroundStyle(tagColor)
        }
    }

    /// This account's plan window is spent and its requests are being charged
    /// to purchased credits. Amber, never the ordinary chip treatment: it is
    /// the one state in the pool that costs money per request, so it says so
    /// on the card with the balance beside it.
    @ViewBuilder private var creditsBanner: some View {
        if profile.isOnCredits {
            HStack(spacing: 6) {
                Text("ON CREDITS")
                    .font(.system(size: 9.5, weight: .bold)).kerning(0.3)
                if let balance = profile.creditsBalanceText {
                    Text(balance).font(.system(size: 9.5, weight: .bold)).monospacedDigit()
                }
                Spacer(minLength: 0)
            }
            .foregroundStyle(palette.warn)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(palette.warn.opacity(0.13), in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(palette.warn.opacity(0.35), lineWidth: 1))
            .padding(.top, 10)
        }
    }

    /// "Max", not "max" — and never a 5x/20x multiplier: no provider exposes it.
    private func planLabel(_ p: Profile) -> String? {
        guard p.enabled, let raw = p.plan?.trimmingCharacters(in: .whitespaces), !raw.isEmpty else { return nil }
        switch raw.lowercased() {
        case "max": return "Max"
        case "pro": return "Pro"
        case "plus": return "Plus"
        case "team": return "Team"
        case "free": return "Free"
        case "prolite": return "Pro Lite"
        default: return raw.prefix(1).uppercased() + raw.dropFirst()
        }
    }

    private var tagColor: Color { profile.enabled ? palette.kind(profile.kind) : palette.textFaint }

    private var session: some View {
        VStack(alignment: .leading, spacing: 0) {
            Divider().overlay(palette.hairSoft).padding(.vertical, 12)
            Text("SESSION").font(.system(size: 10.5, weight: .bold)).kerning(0.7)
                .foregroundStyle(palette.textFaint)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    if profile.isServing {
                        Circle().fill(palette.good).frame(width: 6, height: 6)
                            .alignmentGuide(.firstTextBaseline) { $0[.bottom] - 1 }
                    }
                    Text(statusLine).font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(profile.needsReauth ? palette.bad
                                         : profile.isServing ? palette.text : palette.textDim)
                    Spacer()
                    Text(Fmt.percent(profile.percent(for: ringWindow)))
                        .font(.system(size: 13, weight: .bold)).monospacedDigit()
                        .foregroundStyle(palette.ring(profile, window: ringWindow))
                }
                if let detail = detailLine {
                    Text(detail).font(.system(size: 11)).monospacedDigit()
                        .foregroundStyle(profile.isServing ? palette.textDim : palette.textFaint)
                        .lineLimit(1).truncationMode(.tail)
                }
            }
            .padding(.horizontal, 11).padding(.vertical, 10)
            .background(SessionFillBar(percent: profile.percent(for: ringWindow),
                                       serving: profile.isServing, palette: palette))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(
                profile.isServing ? palette.good.opacity(0.28) : palette.hairSoft, lineWidth: 1))
            .padding(.top, 9)
        }
    }

    private var statusLine: String {
        if !profile.enabled { return "turned off" }
        if profile.needsReauth { return "needs re-auth · sign in from the Dashboard" }
        if profile.isServing {
            let n = profile.liveAgents ?? 0
            var line = n > 0 ? "serving now · \(n) agent\(n == 1 ? "" : "s")" : "serving now"
            // How long agents have been on this account, not one request.
            if let since = Fmt.elapsed(since: profile.agentsSince) { line += " · \(since)" }
            return line
        }
        var line = profile.statusWord ?? "idle"
        if let ago = Fmt.elapsed(since: profile.lastUsedAt, coarse: true) { line += " · used \(ago) ago" }
        return line
    }

    /// Model · project of the latest COMPLETED request. The time lives on the
    /// status line, so this line only has to fit what ran and where.
    private var detailLine: String? {
        var model = Fmt.model(profile.lastModel)
        if let asked = Fmt.model(profile.lastRequestedModel), let served = model, asked != served {
            model = "\(asked) → \(served)"      // a codex account translating a Claude model
        }
        let parts = [model, Fmt.project(profile.lastProject)].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// Whether this account is billed per token at all.
    ///
    /// An OAuth (Claude subscription) account is not, and neither is a Codex
    /// account on a ChatGPT subscription — `kind` alone cannot tell, since a
    /// Codex account also runs on an API key. For the ones that are not, the
    /// figure below is what the same traffic would have cost at the provider's
    /// list rates, so it is marked as an equivalent rather than printed like a
    /// charge. The dashboard's cost cards draw the same line.
    private var billedPerToken: Bool {
        profile.kind == "api" || (profile.kind == "codex" && profile.authMode == "api_key")
    }

    private var today: some View {
        HStack(alignment: .bottom) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text((billedPerToken ? "" : "≈") + (spend.map { Fmt.money($0.cost) } ?? "$0.00"))
                    .font(.system(size: 28, weight: .bold, design: .rounded)).monospacedDigit()
                    .foregroundStyle(palette.text)
                Text(billedPerToken ? "today" : "today, at list rates")
                    .font(.system(size: 13)).foregroundStyle(palette.textDim)
            }
            Spacer()
            if let spend {
                Text("\(spend.requests) calls\n\(Fmt.compact(spend.tokens)) tokens")
                    .font(.system(size: 10.5)).monospacedDigit().multilineTextAlignment(.trailing)
                    .foregroundStyle(palette.textFaint)
            }
        }
        .padding(.top, 13)
    }

    @ViewBuilder
    private var meters: some View {
        if profile.isAPIKey { apiMeters } else { windowMeters }
    }

    /// An API key has no plan windows: its lifetime cost and tokens, and its
    /// token cap when one is set — the same figures the Profiles page shows.
    private var apiMeters: some View {
        HStack(alignment: .top, spacing: 12) {
            stat(Fmt.money(profile.costUsdTotal ?? 0), "Total cost", "est., list rates")
            stat(Fmt.compact(profile.tokensTotal ?? 0), "Tokens", "all time")
            if let cap = profile.tokenThreshold, cap > 0 {
                meterView(title: "Token cap \(Fmt.compact(cap))", percent: profile.tokenCapPercent,
                          resets: nil, isRing: true, color: palette.apiRing(profile))
            }
        }
        .padding(.top, 13)
    }

    private func stat(_ value: String, _ title: String, _ note: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(value).font(.system(size: 21, weight: .bold)).monospacedDigit()
                .foregroundStyle(palette.text).lineLimit(1).minimumScaleFactor(0.7)
            Text(title).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(palette.textDim)
            Text(note).font(.system(size: 10.5)).foregroundStyle(palette.textFaint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var windowMeters: some View {
        HStack(alignment: .top, spacing: 12) {
            meter(.fiveHour, profile.usage5hPercent, profile.usage5hResetsAt)
            meter(.weekly, profile.usage7dPercent, profile.usage7dResetsAt)
            // The third slot: a model's own weekly limit (e.g. Fable), which
            // can run out long before the account's. The one the provider
            // marks as binding wins, then the fullest.
            if let model = topModel {
                meterView(title: model.name, percent: model.percent, resets: model.resetsAt, isRing: false,
                          color: palette.color(UsageBand.model(model.percent, on: profile)))
            }
        }
        .padding(.top, 13)
    }

    private var topModel: ModelUsage? {
        (profile.modelUsage ?? []).max { a, b in
            if (a.active ?? false) != (b.active ?? false) { return !(a.active ?? false) }
            return a.percent < b.percent
        }
    }

    private func meter(_ window: RingWindow, _ percent: Double?, _ resets: String?) -> some View {
        meterView(title: window.label, percent: percent, resets: resets, isRing: window == ringWindow,
                  color: palette.ring(profile, window: window))
    }

    private func meterView(title: String, percent: Double?, resets: String?, isRing: Bool,
                           color: Color) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(percent == nil ? "—" : Fmt.percent(percent))
                .font(.system(size: 21, weight: .bold)).monospacedDigit()
                .foregroundStyle(percent == nil ? palette.textFaint : color)
            // Names the meter the tile's percentage is showing, since
            // right-click can switch it and both rings are always drawn.
            HStack(spacing: 4) {
                Text(title).font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(palette.textDim).lineLimit(1)
                if isRing {
                    Text("· shown").font(.system(size: 10.5))
                        .foregroundStyle(palette.textFaint)
                }
            }
            .padding(.top, 4)
            Text(Fmt.until(resets)).font(.system(size: 10.5)).monospacedDigit()
                .foregroundStyle(palette.textFaint).padding(.top, 1)
            MeterBar(percent: percent, threshold: profile.switchThreshold, palette: palette, color: color)
                .padding(.top, 7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var footer: some View {
        VStack(spacing: 0) {
            Divider().overlay(palette.hairSoft).padding(.vertical, 12)
            HStack {
                Text(Fmt.ago(updated)).font(.system(size: 10.5)).monospacedDigit()
                    .foregroundStyle(palette.textFaint)
                Spacer()
            }
        }
    }
}

// MARK: - app entry

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // The dock is the product; show it at launch rather than waiting for
        // someone to find the menu-bar item.
        DockController.shared.showDock()
    }
}

@main
struct HUDApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @ObservedObject private var controller = DockController.shared
    // Observed separately: the label reads pool state, and PoolClient is its
    // own ObservableObject — without this the menu bar would freeze on
    // whatever it showed first.
    @ObservedObject private var pool = DockController.shared.pool

    var body: some Scene {
        MenuBarExtra {
            Button(controller.visible ? "Hide dock" : "Show dock") { controller.toggleDock() }
            Toggle("Always on top", isOn: Binding(
                get: { controller.pinned }, set: { controller.pinned = $0 }))
            Divider()
            Button("Open dashboard") { controller.pool.openDashboard() }
            Divider()
            Button("Quit \(hudName)") { NSApp.terminate(nil) }
        } label: {
            // The small piece of info that lives in the top bar: the account
            // actually serving, and how loaded it is.
            switch pool.state {
            case .ready(let profiles, let status):
                let serving = profiles.first(where: { $0.isServing })
                    ?? profiles.first(where: { $0.id == status?.currentProfileId })
                    ?? profiles.first(where: \.enabled)
                HStack(spacing: 3) {
                    Image(systemName: serving?.isServing == true ? "circle.fill" : "circle")
                        .font(.system(size: 6))
                    Text(Fmt.percent(serving?.headlinePercent))
                        .font(.system(size: 11, weight: .semibold)).monospacedDigit()
                }
            case .unreachable:
                Image(systemName: "bolt.horizontal.circle")
            case .loading:
                // The only effect in the menu bar, and only here: this state
                // lasts until the first reply arrives. Everything else up here
                // stays a glyph, because the menu bar is on screen always.
                HUDLoader(size: 13, color: Palette.textDim, visible: true) {
                    Image(systemName: "circle.dotted")
                }
            }
        }
    }
}


/// Desktop-sampling blur. `blendingMode = .behindWindow` is the whole point:
/// it is what makes the dock translucent against the screen rather than
/// against its own empty background.
struct DesktopBlur: NSViewRepresentable {
    var shape: RailShape
    /// The blur itself has to switch appearance too, or a light dock keeps a
    /// dark frosted panel behind its light tint.
    var dark: Bool = true
    /// Applied to the BACKGROUND only — the icons and text sit on top of this
    /// view, so the dock gets more see-through without dimming its content.
    var opacity: CGFloat = 1

    func makeNSView(context: Context) -> ShapedEffectView {
        let view = ShapedEffectView()
        view.blendingMode = .behindWindow
        // .underWindowBackground has no edge highlight; .hudWindow draws one,
        // which reads as a 1pt border around the dock.
        view.material = .underWindowBackground
        view.state = .active
        view.isEmphasized = false
        view.wantsLayer = true
        view.layer?.borderWidth = 0
        updateNSView(view, context: context)
        return view
    }

    func updateNSView(_ view: ShapedEffectView, context: Context) {
        view.shape = shape
        view.alphaValue = opacity
        view.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    }
}

/// The frosted blur, cut to the rail outline. Shaped with `maskImage` rather
/// than a SwiftUI clipShape: clipping the effect view leaves an antialiased
/// seam against the window's transparent background, which reads as a border.
/// The mask is redrawn at the view's real size on every layout.
final class ShapedEffectView: NSVisualEffectView {
    var shape: RailShape? { didSet { if shape != oldValue { needsLayout = true } } }
    private var maskedSize: CGSize = .zero
    private var maskedShape: RailShape?

    override func layout() {
        super.layout()
        guard let shape, bounds.width > 0, bounds.height > 0,
              bounds.size != maskedSize || shape != maskedShape else { return }
        maskedSize = bounds.size
        maskedShape = shape
        let path = shape.cgPath(in: CGRect(origin: .zero, size: bounds.size))
        maskImage = NSImage(size: bounds.size, flipped: true) { _ in
            guard let context = NSGraphicsContext.current?.cgContext else { return false }
            context.addPath(path)
            context.setFillColor(NSColor.black.cgColor)
            context.fillPath()
            return true
        }
    }
}

/// The rail outline as a SwiftUI shape, for the scrim and tint drawn over the
/// blur or glass. `DockRail` holds the geometry.
struct RailShape: Shape, Equatable {
    var edge: DockEdge?
    var vertical: Bool

    func cgPath(in rect: CGRect) -> CGPath { DockRail.path(in: rect, attachedTo: edge, vertical: vertical) }
    func path(in rect: CGRect) -> Path { Path(cgPath(in: rect)) }
}

/// macOS 26's Liquid Glass, reached by name.
///
/// `NSGlassEffectView` is not in the SDK this builds against (by design:
/// nobody should need Xcode 26 to build the widget). Looking the class up
/// at runtime compiles everywhere and activates only where the class exists;
/// its properties are set through KVC for the same reason. On an older macOS
/// `isAvailable` is false and the caller draws the frosted blur instead.
struct GlassBackground: NSViewRepresentable {
    var shape: RailShape

    fileprivate static let glassClass: NSView.Type? = NSClassFromString("NSGlassEffectView") as? NSView.Type
    static var isAvailable: Bool { glassClass != nil }

    func makeNSView(context: Context) -> ShapedGlassHost {
        let host = ShapedGlassHost()
        host.shape = shape
        return host
    }

    func updateNSView(_ host: ShapedGlassHost, context: Context) { host.shape = shape }
}

/// Holds the glass view and cuts it to the rail outline with a layer mask —
/// the glass view's own `cornerRadius` can only round all four corners the
/// same way, and the docked outline has concave shoulders.
final class ShapedGlassHost: NSView {
    private let glass: NSView? = GlassBackground.glassClass.map { $0.init(frame: .zero) }
    private let maskLayer = CAShapeLayer()
    var shape: RailShape? { didSet { if shape != oldValue { needsLayout = true } } }

    // y-down, like the SwiftUI coordinates DockRail builds its path in.
    override var isFlipped: Bool { true }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        if let glass {
            glass.autoresizingMask = [.width, .height]
            addSubview(glass)
        }
        layer?.mask = maskLayer
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func layout() {
        super.layout()
        glass?.frame = bounds
        guard let shape else { return }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        maskLayer.frame = bounds
        var path = shape.cgPath(in: bounds)
        // The backing layer is not geometry-flipped with the view, so turn the
        // y-down path over for it.
        if layer?.isGeometryFlipped == false {
            var flip = CGAffineTransform(translationX: 0, y: bounds.height).scaledBy(x: 1, y: -1)
            path = path.copy(using: &flip) ?? path
        }
        maskLayer.path = path
        CATransaction.commit()
    }
}

/// A cog that is a real button, so it receives hover tracking.
struct CogButton: NSViewRepresentable {
    @Binding var hovering: Bool
    let palette: DockPalette
    let onClick: (NSView) -> Void

    func makeNSView(context: Context) -> NSView {
        let host = NSHostingView(rootView: CogFace(hovering: hovering, palette: palette))
        let view = TrackingHost(frame: NSRect(x: 0, y: 0, width: 26, height: 22))
        view.addSubview(host)
        host.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            host.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            host.centerYAnchor.constraint(equalTo: view.centerYAnchor),
        ])
        view.onHover = { hovering = $0 }
        view.onClick = { onClick(view) }
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        (view.subviews.first as? NSHostingView<CogFace>)?.rootView = CogFace(hovering: hovering, palette: palette)
    }
}

struct CogFace: View {
    let hovering: Bool
    let palette: DockPalette
    var body: some View {
        Image(systemName: "gearshape.fill")
            .font(.system(size: 13))
            .foregroundStyle(hovering ? palette.text : palette.textDim)
            .padding(4)
            .background(Circle().fill((palette.isDark ? Color.white : Color.black)
                .opacity(hovering ? 0.16 : 0)))
            .rotationEffect(.degrees(hovering ? 45 : 0))
            .animation(.easeOut(duration: 0.2), value: hovering)
    }
}

final class TrackingHost: NSView {
    var onHover: ((Bool) -> Void)?
    var onClick: (() -> Void)?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds,
                                       options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }
    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }
    override func mouseDown(with event: NSEvent) { onClick?() }
}

/// NSMenu needs an ObjC target; the dock's state lives on the controller.
@MainActor
final class MenuActions: NSObject {
    static let shared = MenuActions()
    @objc func togglePin() { DockController.shared.pinned.toggle() }
    @objc func toggleHideDisabled() { DockController.shared.hideDisabled.toggle() }
    @objc func setSize(_ sender: NSMenuItem) {
        if let value = sender.representedObject as? Double { DockController.shared.tileSize = value }
    }
    @objc func setOrientation(_ sender: NSMenuItem) {
        if let raw = sender.representedObject as? Double, let value = DockOrientation(rawValue: raw) {
            DockController.shared.orientation = value
        }
    }
    @objc func setTheme(_ sender: NSMenuItem) {
        if let raw = sender.representedObject as? Double, let value = DockTheme(rawValue: raw) {
            DockController.shared.theme = value
        }
    }
    @objc func setTransparency(_ sender: NSMenuItem) {
        if let raw = sender.representedObject as? Double,
           let level = DockTransparency(rawValue: raw) {
            DockController.shared.transparency = level
        }
    }
    @objc func takeOver(_ sender: NSMenuItem) {
        if let id = sender.representedObject as? String { DockController.shared.takeOver(id) }
    }
    @objc func setProfileEnabled(_ sender: NSMenuItem) {
        guard let pair = sender.representedObject as? [Any], pair.count == 2,
              let id = pair[0] as? String, let enabled = pair[1] as? Bool else { return }
        DockController.shared.setProfileEnabled(enabled, for: id)
    }
    @objc func setRingWindow(_ sender: NSMenuItem) {
        if let pair = sender.representedObject as? [String], pair.count == 2,
           let window = RingWindow(rawValue: pair[1]) {
            DockController.shared.setRingWindow(window, for: pair[0])
        }
    }
    @objc func undock() { DockController.shared.undock() }
    @objc func openDashboard() { DockController.shared.pool.openDashboard() }
    @objc func hideDock() { DockController.shared.hideDock() }
    @objc func quit() { NSApp.terminate(nil) }
}
