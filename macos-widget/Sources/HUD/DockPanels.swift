import AppKit
import Combine
import SwiftUI

/// The dock is a real floating window, not a menu-bar popover.
///
/// The popover version could not do the three things the dock is for: it
/// vanished the moment you looked at anything else, it could not be dragged
/// (the system owns that window's placement), and "always on top" had nothing
/// to set a level on. An `NSPanel` we own fixes all three, because position,
/// level and lifetime become ours.
final class DockPanel: NSPanel {
    init(content: NSView, autosave: String) {
        super.init(contentRect: NSRect(x: 0, y: 0, width: 80, height: 300),
                   // .borderless kills the titlebar; .nonactivatingPanel is what
                   // lets you click the dock without stealing focus from the
                   // editor you are actually working in.
                   styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        hidesOnDeactivate = false
        becomesKeyOnlyIfNeeded = true
        backgroundColor = .clear
        isOpaque = false
        // No shadow: around a rounded translucent panel its edge reads as a
        // border, which is the one thing this dock must not have.
        hasShadow = false
        // Not AppKit's background drag: that moves the window in the window
        // server and only reports it afterwards, so nothing can pull the dock
        // onto an edge while it moves. The dock drives its own drag through
        // `onDrag` (sendEvent below); the card sets no handler and never moves.
        isMovableByWindowBackground = false
        // Follow you between Spaces and sit over full-screen apps, which is the
        // whole point of a dock you keep on screen.
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        contentView = content
        // The hosting view must not paint its own background or edge.
        content.wantsLayer = true
        content.layer?.backgroundColor = .clear
        content.layer?.borderWidth = 0
        // A fresh NSHostingView reports a zero fittingSize until it has laid
        // out, and a 0x0 window is invisible AND absent from the window list —
        // which looks exactly like "the app didn't start".
        content.layoutSubtreeIfNeeded()
        let fitted = content.fittingSize
        setContentSize(NSSize(width: max(fitted.width, 74), height: max(fitted.height, 160)))
        if !autosave.isEmpty {
            // Read the saved frame BEFORE AppKit restores it: with several
            // displays AppKit may re-seat it on another one (a dock saved on
            // the laptop's right edge came back on the external monitor's), and
            // then autosaves over the original. Put it back where it was saved,
            // provided that spot is still on a connected display.
            let key = "NSWindow Frame \(autosave)"
            let saved = DockSnap.savedFrame(from: UserDefaults.standard.string(forKey: key))
            setFrameAutosaveName(autosave)
            if let saved, DockSnap.homeScreenIndex(for: saved, screens: NSScreen.screens.map(\.frame)) != nil {
                setFrame(saved, display: false)
            }
        }
    }

    // A borderless window refuses key status by default, which would break the
    // cog menu and any future text field.
    override var canBecomeKey: Bool { true }

    /// AppKit keeps every titled-or-not window below the menu bar, even one
    /// set to auto-hide, which stopped a top-docked dock 32pt short of the top.
    /// The dock computes its own on-screen frame (DockSnap), so take it as is.
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }

    enum DragPhase { case began, moved, ended }
    /// Set on the dock only. Receives screen-coordinate pointer positions.
    var onDrag: ((DragPhase, NSPoint) -> Void)?
    private var pressedAt: NSPoint?
    private var dragging = false

    /// A press that moves more than 3pt becomes a drag of the whole dock, from
    /// anywhere on it. Anything shorter stays an ordinary click or hover.
    override func sendEvent(_ event: NSEvent) {
        guard let onDrag else { return super.sendEvent(event) }
        switch event.type {
        case .leftMouseDown:
            pressedAt = NSEvent.mouseLocation
            dragging = false
        case .leftMouseDragged:
            if let start = pressedAt {
                let now = NSEvent.mouseLocation
                if !dragging, hypot(now.x - start.x, now.y - start.y) > 3 {
                    dragging = true
                    onDrag(.began, start)
                }
                if dragging {
                    onDrag(.moved, now)
                    return
                }
            }
        case .leftMouseUp:
            pressedAt = nil
            if dragging {
                dragging = false
                onDrag(.ended, NSEvent.mouseLocation)
                return
            }
        default:
            break
        }
        super.sendEvent(event)
    }
}

/// The three dock sizes. Three, not a continuum: the dock is glanceable
/// furniture, and every step has to fit its container at every profile count.
enum DockSize: Double, CaseIterable {
    case small = 28
    case medium = 34
    case large = 42

    var label: String {
        switch self {
        case .small:  return "Small"
        case .medium: return "Medium"
        case .large:  return "Large"
        }
    }
}

/// Which way the dock runs. The hover card follows: beside a vertical dock,
/// above or below a horizontal one.
enum DockOrientation: Double, CaseIterable {
    case vertical = 0
    case horizontal = 1

    var label: String { self == .vertical ? "Vertical" : "Horizontal" }
    var isVertical: Bool { self == .vertical }
}

/// What the user picked from the Background submenu, and nothing else.
///
/// Liquid Glass is a THIRD value, not a replacement for Light: Light has no
/// glass equivalent. Dark and Light say what they are; Glass
/// says only "use the system's glass", and which way round it is drawn is not
/// this type's business — see `DockPalette`.
enum DockTheme: Double, CaseIterable {
    case dark = 0
    case light = 1
    case glass = 2

    var label: String {
        switch self {
        case .dark:  return "Dark"
        case .light: return "Light"
        case .glass: return "Liquid Glass"
        }
    }
    /// Offered only where the system has it. A saved `.glass` on an older
    /// macOS still renders — through the blur — so nothing turns unreadable.
    static var available: [DockTheme] { GlassBackground.isAvailable ? allCases : [.dark, .light] }

    var isLight: Bool { self == .light }
}

/// The dock's colours, resolved. `DockTheme` is what the user picked; this is
/// what that choice means right now, given the system appearance.
///
/// Everything that has to stay legible on the dock — labels, tile fills, ring
/// tracks, the scrim itself — comes from here rather than being hard coded, so
/// no combination can leave light text on a light dock.
///
/// That last part is why this type exists. Glass used to be defined as "always
/// light text over a fixed dark scrim, so it reads on any wallpaper without
/// sampling what is behind it". `NSGlassEffectView` does not work that way: in
/// Light Mode it renders LIGHT glass, and a black scrim over it with light text
/// on top gave a grey panel nobody could read. So Glass now follows the system
/// appearance — dark glass and light text in Dark Mode, light glass and dark
/// text in Light Mode — which is both legible and what the rest of the system
/// is doing at that moment.
///
/// Still not handled, deliberately: a white WALLPAPER while the system is in
/// Dark Mode. Only sampling the desktop would catch that, and that costs a
/// Screen Recording permission the HUD does not otherwise need.
///
/// `resolve` is pure and takes the appearance as an argument, so every
/// combination is a unit test with no screen and no preference to flip — the
/// same split `ShaderGate` makes between its rule and `NSWorkspace`.
struct DockPalette: Equatable {
    let isDark: Bool
    let base: Color
    let text: Color
    let textDim: Color
    let textFaint: Color
    let tileFill: Color
    let tileFillActive: Color
    let ringTrack: Color
    /// Opacity of the `base`-coloured layer over the glass or the blur. More of
    /// it reads as less transparent.
    let scrim: Double

    // The hover card's surfaces. It is the same dock in a different window, so
    // it resolves from the same palette rather than keeping its own opinion —
    // a light dock that opened a black card was the visible half of the bug
    // this type exists to fix.

    /// The card's own tint over its material, and the bar tracks inside it.
    let cardFill: Color
    let hairline: Color
    let hairSoft: Color
    let track: Color
    /// The plan/kind chips in the card header.
    let chipFill: Color

    static func resolve(theme: DockTheme, transparency: DockTransparency,
                        systemIsDark: Bool) -> DockPalette {
        // Dark and Light mean themselves whatever the system is doing; only
        // Glass defers to the appearance.
        let dark: Bool
        switch theme {
        case .dark:  dark = true
        case .light: dark = false
        case .glass: dark = systemIsDark
        }
        return DockPalette(
            isDark: dark,
            base: dark ? .black : .white,
            text: dark ? Palette.text : Color(white: 0.11),
            textDim: dark ? Palette.textDim : Color(white: 0.36),
            textFaint: dark ? Palette.textFaint : Color(white: 0.52),
            tileFill: dark ? Color.white.opacity(0.06) : Color.black.opacity(0.05),
            tileFillActive: dark ? Color.white.opacity(0.12) : Color.black.opacity(0.10),
            ringTrack: dark ? Color.white.opacity(0.08) : Color.black.opacity(0.11),
            scrim: theme == .glass ? transparency.glassScrim(dark: dark) : transparency.tint,
            cardFill: dark ? Palette.bg.opacity(0.62) : Color.white.opacity(0.72),
            hairline: dark ? Color(white: 1, opacity: 0.09) : Color(white: 0, opacity: 0.12),
            hairSoft: dark ? Color(white: 1, opacity: 0.055) : Color(white: 0, opacity: 0.075),
            track: dark ? Color(white: 1, opacity: 0.09) : Color(white: 0, opacity: 0.10),
            chipFill: dark ? Color.white.opacity(0.08) : Color.black.opacity(0.06))
    }
}

/// The *meanings* — usage bands and provider identity — resolved for the
/// appearance the dock is actually on.
///
/// `Palette`'s tokens are tuned for the dark dock, and a colour bright enough
/// to glow on #0A0A0C is the same colour that disappears on white: the 98%-safe
/// green, the Codex teal and the usage percentages were all washing out on a
/// light desktop. These are the darkened counterparts, each at or above 4.5:1
/// against white so a percentage reads as text, not as a hint.
///
/// Dark deliberately returns `Palette`'s existing values unchanged — this adds
/// a light branch, it does not retune the dark dock.
extension DockPalette {
    private enum Lit {
        static let good   = Color(red: 0.059, green: 0.478, blue: 0.271)   // #0F7A45
        static let warn   = Color(red: 0.639, green: 0.353, blue: 0.0)     // #A35A00
        static let bad    = Color(red: 0.753, green: 0.149, blue: 0.149)   // #C02626
        static let accent = Color(red: 0.039, green: 0.431, blue: 0.588)   // #0A6E96
        static let codex  = Color(red: 0.059, green: 0.478, blue: 0.431)   // #0F7A6E
        static let claude = Color(red: 0.659, green: 0.271, blue: 0.165)   // #A8452A
    }

    var good: Color { isDark ? Palette.good : Lit.good }
    var warn: Color { isDark ? Palette.warn : Lit.warn }
    var bad: Color { isDark ? Palette.bad : Lit.bad }

    /// `Palette.usage`'s bands, in this appearance's colours. The thresholds
    /// are the routing thresholds and do not move.
    func usage(_ percent: Double?, threshold: Double?) -> Color {
        color(UsageBand.of(percent, threshold: threshold))
    }

    /// The colour the Dashboard gives this profile's `window` bar.
    func usage(_ profile: Profile, window: RingWindow) -> Color {
        color(UsageBand.of(profile, window: window))
    }

    /// The weekly ring (the inner one when both windows are reported): one
    /// calm blue until it matters, red from the Dashboard's weekly red (90%)
    /// or when the status is already alerting. No amber step — the outer
    /// 5-hour ring carries the graded warning.
    func weeklyRing(_ profile: Profile) -> Color {
        if UsageBand.status(profile) == .bad { return bad }
        guard let percent = profile.usage7dPercent else { return textFaint }
        return percent >= UsageBand.weeklyRed ? bad : (isDark ? Palette.accent : Lit.accent)
    }

    /// A window's colour on the dock: the 5-hour one by the Dashboard's
    /// bands, the weekly one by `weeklyRing`.
    func ring(_ profile: Profile, window: RingWindow) -> Color {
        window == .weekly ? weeklyRing(profile) : usage(profile, window: .fiveHour)
    }

    /// An API key's ring and figure: by its token cap when it has one (red
    /// from 95% — past the cap it is out of rotation), neutral text otherwise.
    func apiRing(_ profile: Profile) -> Color {
        guard let percent = profile.tokenCapPercent else { return text }
        return color(UsageBand.of(percent, threshold: 100))
    }

    func color(_ band: UsageBand) -> Color {
        switch band {
        case .unknown: return textFaint
        case .good:    return good
        case .warn:    return warn
        case .bad:     return bad
        }
    }

    /// The provider mark's ink for this account: its Dashboard tag colour
    /// when it has one, the provider's own otherwise.
    func mark(_ profile: Profile) -> Color {
        Color(tagHex: profile.tagColor) ?? provider(profile.kind)
    }

    /// The kind chip in the card header.
    func kind(_ kind: String) -> Color {
        switch kind {
        case "codex": return isDark ? Palette.codex : Lit.codex
        case "api":   return textDim
        default:      return isDark ? Palette.accent : Lit.accent
        }
    }

    /// The provider mark's ink. Dark keeps each brand's own colour from
    /// `ProviderRegistry`; light uses a darkened version of the same hue, so a
    /// logo stays recognisably Claude's or Codex's rather than turning grey.
    func provider(_ kind: String) -> Color {
        guard !isDark else { return ProviderRegistry.spec(for: kind).color }
        switch kind {
        case "codex": return Lit.codex
        case "oauth": return Lit.claude
        default:      return Color(white: 0.36)
        }
    }

    /// The serving animation's orbiting dots.
    var orbDot: Color { isDark ? Color.white.opacity(0.82) : Color.black.opacity(0.70) }

    /// The session block's progress fill and its leading edge. Light mode never
    /// takes the shader branch (`.screen` over white does nothing), so the
    /// plain fill is all there is and has to carry the weight on its own.
    var sessionFill: Color { good.opacity(isDark ? 0.13 : 0.22) }
    var sessionEdge: Color { good.opacity(isDark ? 0.22 : 0.60) }
}

/// How much of the desktop shows through the dock.
enum DockTransparency: Double, CaseIterable {
    case low = 0
    case medium = 1
    case high = 2

    var label: String {
        switch self {
        case .low:    return "Low"
        case .medium: return "Medium"
        case .high:   return "High"
        }
    }
    /// Alpha of the blur layer itself; the content on top stays fully opaque.
    var blurAlpha: CGFloat {
        switch self {
        case .low:    return 0.96
        case .medium: return 0.78
        case .high:   return 0.52
        }
    }
    /// Tint over the blur, in the theme's own base colour — more tint reads as
    /// less transparent.
    var tint: Double {
        switch self {
        case .low:    return 0.34
        case .medium: return 0.12
        case .high:   return 0.0
        }
    }
    /// The scrim over Liquid Glass. Never zero, and never below 0.16: glass
    /// alone shows whatever is behind it straight through, and the dock's text
    /// has to survive that. Slightly lighter on the light side, where the text
    /// it protects is dark and starts with more contrast to spend.
    func glassScrim(dark: Bool) -> Double {
        switch self {
        case .low:    return dark ? 0.38 : 0.34
        case .medium: return dark ? 0.24 : 0.22
        case .high:   return dark ? 0.18 : 0.16
        }
    }
}

/// First-launch defaults for the dock's settings, as pure reads of a
/// defaults store so they can be tested against a throwaway suite.
/// `bool`/`double(forKey:)` return false/0 for a key never written, which
/// silently meant off / Dark / Medium — each rule checks for absence instead.
/// A saved choice always wins.
enum DockDefaults {
    nonisolated static func pinned(_ d: UserDefaults) -> Bool {
        d.object(forKey: "alwaysOnTop") == nil ? true : d.bool(forKey: "alwaysOnTop")
    }

    nonisolated static func theme(_ d: UserDefaults, glassAvailable: Bool) -> DockTheme {
        guard d.object(forKey: "theme") != nil else { return glassAvailable ? .glass : .dark }
        let theme = DockTheme(rawValue: d.double(forKey: "theme")) ?? .dark
        // A saved Glass on a system without it (a downgrade) would draw the
        // blur fallback labelled as glass; Dark says what it is.
        return theme == .glass && !glassAvailable ? .dark : theme
    }

    /// The saved size, or nil on a first launch (the caller then uses Large).
    nonisolated static func savedTileSize(_ d: UserDefaults) -> Double? {
        d.object(forKey: "tileSize") == nil ? nil : d.double(forKey: "tileSize")
    }
}

/// Owns the dock window, the hover card beside it, and the pinned state.
@MainActor
final class DockController: NSObject, ObservableObject {
    static let shared: DockController = {
        migrateLegacyDefaults()   // before any property below reads UserDefaults
        return DockController()
    }()

    /// This app shipped as "Capacity Widget" (bundle id …capacitydock). Its
    /// saved position, theme, size and ring choices live in that old domain;
    /// copy them across once so the rename does not reset anyone's setup.
    static func migrateLegacyDefaults(_ defaults: UserDefaults = .standard) {
        let flag = "migratedFromCapacityWidget"
        guard !defaults.bool(forKey: flag) else { return }
        defaults.set(true, forKey: flag)
        guard let old = defaults.persistentDomain(forName: "ai.devdock.claude-unlimited.capacitydock") else { return }
        for (key, value) in old {
            let renamed = key == "NSWindow Frame CapacityDock.dock" ? "NSWindow Frame HUD.dock" : key
            if defaults.object(forKey: renamed) == nil { defaults.set(value, forKey: renamed) }
        }
    }

    // Defaults for a first launch: on top, Liquid Glass where the system has
    // it, Large. `bool`/`double(forKey:)` return false/0 for a key that was
    // never written, which silently meant off / Dark / Medium — so each of
    // these checks for the key's absence instead. A saved choice always wins.
    @Published var pinned: Bool = DockDefaults.pinned(.standard) {
        didSet {
            UserDefaults.standard.set(pinned, forKey: "alwaysOnTop")
            applyLevel()
        }
    }
    @Published var visible: Bool = true

    /// Owned here for the same reason as `tileSize`: it changes how many rows
    /// the dock draws, so the window has to be resized with it. Left in the
    /// view it grew the content inside a window that kept its old frame, and
    /// the dock stayed clipped until the next poll — up to 30s later.
    @Published var orientation: DockOrientation = {
        let saved = UserDefaults.standard.double(forKey: "orientation")
        return DockOrientation(rawValue: saved) ?? .vertical
    }() {
        didSet {
            UserDefaults.standard.set(orientation.rawValue, forKey: "orientation")
            resizeDock()
        }
    }

    @Published var theme: DockTheme = {
        DockDefaults.theme(.standard, glassAvailable: GlassBackground.isAvailable)
    }() {
        didSet { UserDefaults.standard.set(theme.rawValue, forKey: "theme") }
    }

    @Published var transparency: DockTransparency = {
        let saved = UserDefaults.standard.double(forKey: "transparency")
        return DockTransparency(rawValue: saved) ?? .medium
    }() {
        didSet { UserDefaults.standard.set(transparency.rawValue, forKey: "transparency") }
    }

    @Published var hideDisabled: Bool = UserDefaults.standard.bool(forKey: "hideDisabled") {
        didSet {
            UserDefaults.standard.set(hideDisabled, forKey: "hideDisabled")
            resizeDock()
        }
    }

    /// Owned here, not in the view: the window's size is derived from this, so
    /// a setting that lived only in the view could change the content while
    /// the window kept its old frame — which is how profiles ended up spilling
    /// outside their container.
    @Published var tileSize: Double = {
        guard let saved = DockDefaults.savedTileSize(.standard) else { return DockSize.large.rawValue }
        let sizes = DockSize.allCases.map(\.rawValue)
        if sizes.contains(saved) { return saved }
        // A value from an earlier scale: keep the user's relative choice by
        // snapping to the nearest current size rather than silently resetting.
        guard saved > 0, let nearest = sizes.min(by: {
            abs($0 - saved * 0.5) < abs($1 - saved * 0.5)
        }) else { return DockSize.medium.rawValue }
        return nearest
    }() {
        didSet {
            UserDefaults.standard.set(tileSize, forKey: "tileSize")
            resizeDock()
        }
    }

    /// Per profile id, which window its ring shows. Only explicit choices are
    /// stored; a profile nobody has switched follows its headline window.
    @Published private(set) var ringWindows: [String: String] =
        UserDefaults.standard.dictionary(forKey: "ringWindows") as? [String: String] ?? [:]

    /// The window a tile actually draws. A saved choice the provider has
    /// stopped reporting falls back to the headline rather than an empty ring.
    func ringWindow(for profile: Profile) -> RingWindow {
        if let raw = ringWindows[profile.id], let chosen = RingWindow(rawValue: raw),
           profile.reports(chosen) {
            return chosen
        }
        return profile.headlineWindow
    }

    func setRingWindow(_ window: RingWindow, for profileId: String) {
        ringWindows[profileId] = window.rawValue
        UserDefaults.standard.set(ringWindows, forKey: "ringWindows")
    }

    /// The tile under the pointer, kept by the dock view's hover tracking. A
    /// right-click acts on this rather than hit-testing SwiftUI from AppKit.
    /// Published because the hover card's content is derived from it: changing
    /// it is what moves the card from one profile to the next.
    @Published var hoveredProfileId: String?

    private var dock: DockPanel?
    private var card: DockPanel?
    private var cardHost: NSHostingView<CardHost>?
    /// Whether the card should be on screen at all. `settleCard` runs a tick
    /// late and from the poll sink as well as from a hover, so it has to know
    /// the difference between "not shown yet" and "deliberately hidden" — a
    /// right-click hides the card while a tile is still hovered, and without
    /// this the next poll would put it back up over the context menu.
    private var cardWanted = false
    /// The last size `settleCard` measured, and how many times it has re-measured
    /// waiting for two in a row to agree. A hosting view that has just been given
    /// new content reports an intermediate `fittingSize` on the first pass — and
    /// since the card is centred on the dock, a wrong height puts it visibly too
    /// high before the next pass drops it into place. So it is not revealed until
    /// the measurement has stopped changing.
    private var cardFitted: NSSize = .zero
    private var cardSettleTries = 0

    /// One-shot reaction tokens, per profile id. An effect that should play
    /// once — a tile whose account just started serving, a meter that just
    /// crossed into warn — watches its own id here and replays whenever the
    /// token changes. A token rather than a Bool: two reactions in a row must
    /// be two animations, and nothing has to reset it afterwards.
    @Published private(set) var reactions: [String: UUID] = [:]
    private var detector = PoolEventDetector()
    private var rightClickMonitor: Any?

    /// The screen edge the dock is attached to, or nil while it floats. Set by
    /// releasing a drag within `DockSnap.snapDistance` of an edge; the dock's
    /// orientation then follows the edge. Persisted, so it comes back docked.
    @Published private(set) var dockedEdge: DockEdge? =
        DockEdge(rawValue: UserDefaults.standard.string(forKey: "dockedEdge") ?? "") {
        didSet {
            UserDefaults.standard.set(dockedEdge?.rawValue ?? "", forKey: "dockedEdge")
            if !isDragging { attachedEdge = dockedEdge }
        }
    }
    private var animateNextResize = false
    /// The edge the outline flares into: the one it is stuck to while being
    /// dragged, otherwise the one it is docked to.
    @Published private(set) var attachedEdge: DockEdge? =
        DockEdge(rawValue: UserDefaults.standard.string(forKey: "dockedEdge") ?? "")
    private var isDragging = false
    private var dragStartFrame: CGRect = .zero
    private var dragStartPointer: NSPoint = .zero
    private var dragStuckEdge: DockEdge?

    /// The usable frame of a screen for the dock — up to the very top when the
    /// menu bar auto-hides. Read every time: the setting can change at any moment.
    /// The display this frame is on, else the menu-bar display — never
    /// "whichever has keyboard focus". See `DockSnap.homeScreenIndex`.
    static func home(for frame: NSRect) -> NSScreen? {
        let screens = NSScreen.screens
        if let i = DockSnap.homeScreenIndex(for: frame, screens: screens.map(\.frame)) { return screens[i] }
        return screens.first
    }

    static func usableFrame(of screen: NSScreen) -> CGRect {
        // "Automatically hide and show the menu bar" (on the desktop) writes
        // this global default; System Settings changes it live.
        let autoHides = UserDefaults.standard.persistentDomain(forName: UserDefaults.globalDomain)?["_HIHideMenuBar"] as? Bool ?? false
        return DockSnap.usableFrame(screen: screen.frame, visible: screen.visibleFrame, menuBarAutoHides: autoHides)
    }

    func undock() {
        dockedEdge = nil
        resizeDock()
    }
    private let client = PoolClient()
    private var bag = Set<AnyCancellable>()

    var pool: PoolClient { client }

    func showDock() {
        if dock == nil { buildDock() }
        visible = true
        if !client.isPolling { client.start() }   // hideDock stopped it
        dock?.orderFrontRegardless()
        applyLevel()
        if ProcessInfo.processInfo.environment["HUD_DEBUG"] == "1", let dock {
            FileHandle.standardError.write(
                "[dock] frame=\(dock.frame) visible=\(dock.isVisible) level=\(dock.level.rawValue)\n"
                    .data(using: .utf8)!)
            // HUD_DEBUG_DRAG="<dx>,<dy>[;<dx>,<dy>...]" drags the dock
            // through the REAL drag handler: press at its centre, move by each
            // offset in 12 steps (logging where it is drawn and what it is
            // stuck to), release. Synthesising real input needs Accessibility.
            if let raw = ProcessInfo.processInfo.environment["HUD_DEBUG_DRAG"] {
                let legs = raw.split(separator: ";").map { $0.split(separator: ",").compactMap { Double($0) } }
                    .filter { $0.count == 2 }
                Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 3_000_000_000)
                    var pointer = NSPoint(x: dock.frame.midX, y: dock.frame.midY)
                    self.handleDrag(.began, at: pointer)
                    for leg in legs {
                        for _ in 0..<12 {
                            pointer.x += leg[0] / 12; pointer.y += leg[1] / 12
                            self.handleDrag(.moved, at: pointer)
                            try? await Task.sleep(nanoseconds: 16_000_000)
                        }
                        FileHandle.standardError.write("[dock] mid-drag: pointer=\(pointer) frame=\(dock.frame) stuck=\(self.dragStuckEdge?.rawValue ?? "none")\n".data(using: .utf8)!)
                    }
                    self.handleDrag(.ended, at: pointer)
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    let visible = dock.screen.map(Self.usableFrame(of:)) ?? .zero
                    FileHandle.standardError.write("[dock] after drag: frame=\(dock.frame) visible=\(visible) docked=\(self.dockedEdge?.rawValue ?? "floating") orientation=\(self.orientation.label)\n".data(using: .utf8)!)
                }
            }
            // Which background actually rendered: glass cannot be checked
            // from a window capture, since its backdrop is composited later.
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                @MainActor func walk(_ v: NSView) -> [String] { [String(describing: type(of: v))] + v.subviews.flatMap(walk) }
                let backgrounds = walk(dock.contentView ?? NSView())
                    .filter { $0.contains("Glass") || $0.contains("VisualEffect") }
                FileHandle.standardError.write("[dock] backgrounds=\(backgrounds)\n".data(using: .utf8)!)
            }
        }
    }

    func hideDock() {
        visible = false
        card?.orderOut(nil)
        dock?.orderOut(nil)
        // Nothing on screen to update: stop waking the daemon until it is back.
        client.stop()
    }

    func toggleDock() { visible ? hideDock() : showDock() }

    private func buildDock() {
        let view = DockColumn(client: client, controller: self)
        let host = NSHostingView(rootView: view)
        host.sizingOptions = [.intrinsicContentSize]
        let panel = DockPanel(content: host, autosave: "HUD.dock")  // renaming it resets the saved position; see migrateLegacyDefaults
        dock = panel
        let debug = ProcessInfo.processInfo.environment["HUD_DEBUG"] == "1"
        if debug { FileHandle.standardError.write("[dock] restored frame=\(panel.frame) screen=\(panel.screen?.localizedName ?? "nil")\n".data(using: .utf8)!) }
        placeIfNeeded(panel)
        if debug { FileHandle.standardError.write("[dock] after place frame=\(panel.frame)\n".data(using: .utf8)!) }
        installRightClickMenu()
        panel.onDrag = { [weak self] phase, pointer in self?.handleDrag(phase, at: pointer) }
        // A display added, removed or rearranged: re-seat a docked dock on its edge.
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                                               name: NSApplication.didChangeScreenParametersNotification, object: nil)
        // The panel is built while the pool is still loading, so its first
        // size is the empty one. Resize whenever the data changes, or it stays
        // stuck at the height of a dock with no profiles in it.
        client.$state
            .receive(on: RunLoop.main)
            .sink { [weak self] state in
                self?.resizeDock()
                self?.settleCard()   // the open card is live now, so its height can change under it
                self?.noteEvents(in: state)
                self?.debugPopMenuOnce()
            }
            .store(in: &bag)
    }

    /// Feed a pool snapshot to the detector and turn what changed into
    /// reaction tokens the effects can watch.
    private func noteEvents(in state: PoolState) {
        guard case .ready(let profiles, let status) = state else { return }
        for event in detector.events(for: profiles, servingId: status?.currentProfileId) {
            switch event {
            case .servingChanged(_, let to):            reactions[to] = UUID()
            case .bandChanged(let profileId, _, _):     reactions[profileId] = UUID()
            }
        }
    }

    func resizeDock() {
        // Next runloop tick: when this is called from a setting's didSet the
        // SwiftUI view has not re-laid-out yet, so fittingSize would still be
        // the OLD size and the window would resize to the wrong thing.
        DispatchQueue.main.async { [weak self] in self?.applyDockSize() }
    }

    private func applyDockSize() {
        guard let dock, let host = dock.contentView else { return }
        host.layoutSubtreeIfNeeded()
        let fitted = host.fittingSize
        guard fitted.width > 1, fitted.height > 1 else { return }
        // Mid-drag, a poll landing here would pull the dock away from under
        // the cursor. finishDrag() resizes once the drag ends.
        if isDragging { return }
        guard let visible = (dock.screen ?? Self.home(for: dock.frame)).map(Self.usableFrame(of:)) else {
            let top = dock.frame.maxY                 // grow downward, not off the top
            dock.setContentSize(fitted)
            dock.setFrameOrigin(NSPoint(x: dock.frame.origin.x, y: top - dock.frame.height))
            return
        }
        let target = dockedEdge.map { DockSnap.dockedFrame(size: fitted, edge: $0, near: dock.frame, in: visible) }
            ?? DockSnap.floatingFrame(size: fitted, from: dock.frame, in: visible)
        let animate = animateNextResize
        animateNextResize = false
        guard target != dock.frame else { return }
        dock.setFrame(target, display: true, animate: animate)
    }

    // MARK: edge snapping

    private func handleDrag(_ phase: DockPanel.DragPhase, at pointer: NSPoint) {
        guard let dock else { return }
        switch phase {
        case .began:
            isDragging = true
            hideCard()
            dragStartFrame = dock.frame
            dragStartPointer = pointer
            dragStuckEdge = dockedEdge
        case .moved:
            let free = dragStartFrame.offsetBy(dx: pointer.x - dragStartPointer.x,
                                               dy: pointer.y - dragStartPointer.y)
            // The display under the pointer, so a drag can cross monitors.
            let screen = NSScreen.screens.first { $0.frame.contains(pointer) } ?? dock.screen ?? NSScreen.main
            guard let visible = screen.map(Self.usableFrame(of:)) else { return }
            let step = DockSnap.dragFrame(free: free, stuck: dragStuckEdge, in: visible)
            dragStuckEdge = step.stuck
            if attachedEdge != step.stuck { attachedEdge = step.stuck }
            dock.setFrame(step.frame, display: true)
        case .ended:
            isDragging = false
            finishDrag()
        }
    }

    @objc private func screensChanged(_ note: Notification) { resizeDock() }

    /// Released while stuck to an edge: dock there, turning to run along it.
    /// Released anywhere else: float where it was dropped.
    func finishDrag() {
        dockedEdge = dragStuckEdge
        animateNextResize = dragStuckEdge != nil
        if let edge = dragStuckEdge {
            let wanted: DockOrientation = edge.isVertical ? .vertical : .horizontal
            // Setting it resizes (its didSet); otherwise resize explicitly so
            // the frame is re-seated on the edge.
            if orientation != wanted { orientation = wanted } else { resizeDock() }
        } else {
            resizeDock()
        }
        if ProcessInfo.processInfo.environment["HUD_DEBUG"] == "1" {
            FileHandle.standardError.write("[dock] drag ended: docked=\(dockedEdge?.rawValue ?? "floating")\n".data(using: .utf8)!)
        }
    }

    /// Put it on the main screen's right edge unless a *valid* saved frame
    /// exists. A restored frame is only honoured if it still lands on a
    /// connected display — otherwise unplugging a monitor hides the dock.
    private func placeIfNeeded(_ panel: NSPanel) {
        let onAScreen = NSScreen.screens.contains { $0.frame.intersects(panel.frame) }
        // The menu-bar display, not NSScreen.main (the one with keyboard focus).
        guard panel.frame.origin == .zero || !onAScreen, let screen = NSScreen.screens.first else { return }
        let f = Self.usableFrame(of: screen)
        panel.setFrameOrigin(NSPoint(x: f.maxX - panel.frame.width - 24,
                                     y: f.midY - panel.frame.height / 2))
    }

    /// The hover card is a child window: AppKit then moves it with the dock
    /// when you drag, so the two never come apart.
    ///
    /// Its content comes from `CardHost`, which reads `hoveredProfileId`, so
    /// this only ever builds the hosting view once. Replacing it per hover —
    /// which is what this did — reset every animation inside the card and
    /// froze its numbers at the moment the pointer arrived.
    func showCard() {
        guard !isDragging else { return }
        cardWanted = true
        if cardHost == nil {
            let host = NSHostingView(rootView: CardHost(client: client, controller: self))
            // The card's geometry has exactly one owner: `settleCard`. As the
            // window's own contentView, the hosting view is a second owner — it
            // sizes the window from its content, and an NSWindow resize keeps
            // its bottom-left corner, so a card that grew taller pushed its top
            // upward, above where it belongs, until the next settle recentred
            // it. That is the flash. (codeburn's CapacityDockController splits
            // the same two roles: "Panel geometry has one owner".)
            //
            // So the hosting view goes inside a plain container instead. It can
            // still be measured — `sizingOptions = []` would silence that, and
            // a card measured at 0x0 is never shown at all — but it now resizes
            // with the window rather than the other way round.
            host.translatesAutoresizingMaskIntoConstraints = true
            host.autoresizingMask = [.width, .height]
            let container = NSView()
            container.autoresizesSubviews = true
            host.frame = container.bounds
            container.addSubview(host)
            cardHost = host
            let panel = DockPanel(content: container, autosave: "")
            panel.isMovableByWindowBackground = false   // it follows the dock
            panel.alphaValue = 0                       // revealed once it is placed
            card = panel
            // NOT attached to the dock yet. `addChildWindow` orders the child
            // in immediately, and this panel has never been laid out — it is
            // still the placeholder 80x300 at the screen origin, which is the
            // card appearing "somewhere up" for one frame before it lands.
            // `settleCard` attaches it once it has a real size and place.
        }
        card?.level = dock?.level ?? .floating
        // Deliberately neither sized, placed nor ordered in here. Whatever
        // triggered this has only just changed `hoveredProfileId`, so the card's
        // content is still the previous profile's — and a card that has never
        // been shown is still `DockPanel`'s placeholder 80x300 at the screen
        // origin. Showing it now and correcting it a tick later is exactly the
        // flash: the card appearing somewhere random, then jumping left, and
        // jumping again for every tile the pointer crosses. `settleCard` does
        // all three in one pass, once the content it is measuring is the right
        // content.
        settleCard()
    }

    /// Size the card to its content, put it beside the dock and only then show
    /// it — all on the next runloop tick.
    ///
    /// Same reason as `resizeDock`: the hover that triggers this has only just
    /// changed `hoveredProfileId`, so SwiftUI has not laid the new content out
    /// yet and `fittingSize` would still describe the previous profile's card.
    func settleCard() {
        DispatchQueue.main.async { [weak self] in
            guard let self, cardWanted, let card, let host = cardHost else { return }
            host.layoutSubtreeIfNeeded()
            let fitted = host.fittingSize
            guard fitted.width > 1, fitted.height > 1 else { return }
            card.setContentSize(fitted)
            // The container does not lay its subview out for us: an autoresizing
            // mask scales proportionally, and this one starts from a zero frame.
            if let content = card.contentView { host.frame = content.bounds }
            if let dock { positionCard(card, beside: dock) }

            if card.parent != nil {
                cardFitted = fitted
                if !card.isVisible { card.orderFront(nil) }
                return
            }
            // Not on screen yet. Attaching is what makes it visible, so it waits
            // until two passes measure the same size: the first measurement after
            // new content is an intermediate one, and revealing on it is the card
            // appearing too high and then dropping. Bounded, so a card whose
            // content genuinely never settles still shows up.
            let agreed = fitted == cardFitted
            cardFitted = fitted
            if agreed || cardSettleTries >= 4 {
                cardSettleTries = 0
                dock?.addChildWindow(card, ordered: .above)
                card.alphaValue = 1
            } else {
                cardSettleTries += 1
                settleCard()
            }
        }
    }

    func hideCard() {
        cardWanted = false
        // Detached, not just ordered out: AppKit orders child windows in with
        // their parent, so a hidden-but-still-attached card comes back by
        // itself the next time the dock is ordered front — at whatever frame it
        // last had. `settleCard` re-attaches it once it is sized and placed.
        cardSettleTries = 0
        if let card {
            card.parent?.removeChildWindow(card)
            card.orderOut(nil)
        }
    }

    // MARK: right-click menu on a tile

    /// A local monitor rather than a view overlay: an NSView laid over the
    /// SwiftUI tiles to catch right-clicks also swallowed the hover tracking
    /// that drives the detail card. The monitor sees the event first, and lets
    /// every other click through untouched.
    private func installRightClickMenu() {
        guard rightClickMonitor == nil else { return }
        rightClickMonitor = NSEvent.addLocalMonitorForEvents(matching: [.rightMouseDown, .leftMouseDown]) { [weak self] event in
            guard let self, let dock = self.dock, event.window === dock else { return event }
            let isContextClick = event.type == .rightMouseDown
                || (event.type == .leftMouseDown && event.modifierFlags.contains(.control))
            guard isContextClick, let id = self.hoveredProfileId,
                  case .ready(let profiles, let status) = self.client.state,
                  let profile = profiles.first(where: { $0.id == id }),
                  let view = dock.contentView
            else { return event }
            self.hideCard()
            NSMenu.popUpContextMenu(self.tileMenu(for: profile, current: status?.currentProfileId),
                                    with: event, for: view)
            return nil
        }
    }

    func tileMenu(for profile: Profile, current: String?) -> NSMenu {
        let menu = NSMenu(title: profile.name)
        menu.autoenablesItems = false

        let takeOver = NSMenuItem(title: "Take over", action: #selector(MenuActions.takeOver(_:)), keyEquivalent: "")
        takeOver.target = MenuActions.shared
        takeOver.representedObject = profile.id
        if !profile.enabled {
            // The daemon refuses this too; saying why beats a greyed-out mystery.
            takeOver.title = "Take over — profile is off"
            takeOver.isEnabled = false
        } else if profile.id == current {
            takeOver.title = "Take over — already serving"
        }
        menu.addItem(takeOver)

        // The Dashboard's per-profile switch, as one item that names what it
        // will do rather than what the profile currently is. The target state
        // is captured here, when the menu is built: re-reading `enabled` at
        // click time would let a poll landing mid-menu invert the action.
        let toggle = NSMenuItem(title: profile.enabled ? "Disable" : "Enable",
                                action: #selector(MenuActions.setProfileEnabled(_:)),
                                keyEquivalent: "")
        toggle.target = MenuActions.shared
        toggle.representedObject = [profile.id, !profile.enabled] as [Any]
        menu.addItem(toggle)

        // An API key has no windows to choose between — no section at all
        // rather than two greyed-out rows.
        if profile.isAPIKey { return menu }
        menu.addItem(.separator())
        let header = NSMenuItem(title: "Percentage shows", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        let shown = ringWindow(for: profile)
        for window in RingWindow.allCases {
            let item = NSMenuItem(title: window.label, action: #selector(MenuActions.setRingWindow(_:)), keyEquivalent: "")
            item.target = MenuActions.shared
            item.representedObject = [profile.id, window.rawValue]
            // Never tick a window with no data: a profile reporting neither
            // (an API key, a disabled account) would show a false choice.
            item.state = window == shown && profile.reports(window) ? .on : .off
            item.indentationLevel = 1
            if !profile.reports(window) {
                item.title = "\(window.label) — not reported"
                item.isEnabled = false
            }
            menu.addItem(item)
        }
        return menu
    }

    /// `HUD_DEBUG_MENU=<profile name>` opens that tile's menu, and
    /// `HUD_DEBUG_CARD=<profile name>` its detail card, once the pool
    /// loads. Synthesising a real right-click or hover needs Accessibility
    /// permission; these let both be checked without granting it.
    private var debugMenuShown = false
    private func debugPopMenuOnce() {
        let env = ProcessInfo.processInfo.environment
        if let name = env["HUD_DEBUG_CARD"], !debugMenuShown,
           case .ready(let profiles, _) = client.state,
           let profile = profiles.first(where: { $0.name == name }) {
            debugMenuShown = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
                guard let self else { return }
                self.hoveredProfileId = profile.id
                self.showCard()
            }
            return
        }
        guard !debugMenuShown,
              let name = env["HUD_DEBUG_MENU"],
              case .ready(let profiles, let status) = client.state,
              let profile = profiles.first(where: { $0.name == name }),
              let view = dock?.contentView else { return }
        debugMenuShown = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self else { return }
            self.tileMenu(for: profile, current: status?.currentProfileId)
                .popUp(positioning: nil, at: NSPoint(x: view.bounds.midX, y: view.bounds.midY), in: view)
        }
    }

    func takeOver(_ profileId: String) {
        mutate("Couldn't take over") { try await self.client.takeOver(profileId) }
    }

    /// Enable or disable a profile, exactly as the Dashboard's own switch
    /// does. `enabled` is the state the menu asked for, not a toggle of
    /// whatever the pool says now.
    func setProfileEnabled(_ enabled: Bool, for profileId: String) {
        mutate(enabled ? "Couldn't enable profile" : "Couldn't disable profile") {
            try await self.client.setEnabled(enabled, for: profileId)
        }
    }

    /// No toast surface on a floating dock; an alert is rare and carries the
    /// daemon's own message, so a refusal says exactly why.
    private func mutate(_ failure: String, _ body: @escaping () async throws -> Void) {
        Task { @MainActor in
            do {
                try await body()
            } catch {
                let alert = NSAlert()
                alert.messageText = failure
                alert.informativeText = error.localizedDescription
                alert.alertStyle = .warning
                NSApp.activate(ignoringOtherApps: true)
                alert.runModal()
            }
        }
    }

    /// Vertical dock: the card sits to its left, flipping right when the dock
    /// is near the left edge. Horizontal dock: above it, flipping below when
    /// there is no room overhead. Either way the card stays on screen.
    private func positionCard(_ card: NSWindow, beside dock: NSWindow) {
        let gap: CGFloat = 12
        let screen = dock.screen ?? Self.home(for: dock.frame)
        let bounds = screen.map(Self.usableFrame(of:)) ?? .zero
        var origin: NSPoint

        if orientation.isVertical {
            var x = dock.frame.minX - card.frame.width - gap
            if x < bounds.minX + 8 { x = dock.frame.maxX + gap }
            let y = dock.frame.midY - card.frame.height / 2
            origin = NSPoint(x: x, y: y)
        } else {
            // AppKit's y grows upward, so "above" is the dock's maxY.
            var y = dock.frame.maxY + gap
            if y + card.frame.height > bounds.maxY - 8 {
                y = dock.frame.minY - card.frame.height - gap
            }
            let x = dock.frame.midX - card.frame.width / 2
            origin = NSPoint(x: x, y: y)
        }

        origin.x = min(max(origin.x, bounds.minX + 8), bounds.maxX - card.frame.width - 8)
        origin.y = min(max(origin.y, bounds.minY + 8), bounds.maxY - card.frame.height - 8)
        card.setFrameOrigin(origin)
    }

    private func applyLevel() {
        // This is what "always on top" actually means, and why it did nothing
        // before: a level on a window we own.
        let level: NSWindow.Level = pinned ? .floating : .normal
        dock?.level = level
        card?.level = level
    }
}
