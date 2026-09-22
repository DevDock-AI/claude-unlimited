import SwiftUI
import XCTest
@testable import HUD

/// `DockPalette`'s *meaning* colours — the usage bands, the kind chip, the
/// provider ink and the serving/session accents.
///
/// The surface tokens were always resolved against the appearance; these were
/// not, which is why a light dock painted the dark dock's bright green onto
/// white. The invariant worth pinning is not any particular hex value but the
/// two rules: dark is unchanged, and light actually reads on white.
final class DockSemanticPaletteTests: XCTestCase {

    private func light(_ theme: DockTheme = .light,
                       _ transparency: DockTransparency = .medium) -> DockPalette {
        DockPalette.resolve(theme: theme, transparency: transparency, systemIsDark: false)
    }

    private func dark(_ theme: DockTheme = .dark,
                      _ transparency: DockTransparency = .medium) -> DockPalette {
        DockPalette.resolve(theme: theme, transparency: transparency, systemIsDark: true)
    }

    // MARK: Contrast

    /// Relative luminance per WCAG 2.1, from the colour's sRGB components.
    private func luminance(_ color: Color) -> Double {
        guard let srgb = NSColor(color).usingColorSpace(.sRGB) else {
            XCTFail("colour is not representable in sRGB")
            return 0
        }
        func channel(_ value: CGFloat) -> Double {
            let v = Double(value)
            return v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(srgb.redComponent)
             + 0.7152 * channel(srgb.greenComponent)
             + 0.0722 * channel(srgb.blueComponent)
    }

    /// Contrast ratio against white, which is the background a light dock is
    /// complained about on.
    private func contrastOnWhite(_ color: Color) -> Double {
        (1.0 + 0.05) / (luminance(color) + 0.05)
    }

    private func assertReadable(_ color: Color, _ label: String,
                                file: StaticString = #filePath, line: UInt = #line) {
        let ratio = contrastOnWhite(color)
        XCTAssertGreaterThanOrEqual(ratio, 4.5,
            "\(label) is \(String(format: "%.2f", ratio)):1 on white — below the 4.5:1 floor",
            file: file, line: line)
    }

    func testLightUsageBandsReadOnWhite() {
        let palette = light()
        // The percentage under a tile and beside a meter is text, not a hint.
        assertReadable(palette.usage(10, threshold: 98), "the good band")
        assertReadable(palette.usage(80, threshold: 98), "the warn band")
        assertReadable(palette.usage(99, threshold: 98), "the bad band")
    }

    func testLightProviderInkReadsOnWhite() {
        let palette = light()
        for kind in ["oauth", "codex", "api", "something-new"] {
            assertReadable(palette.provider(kind), "the \(kind) mark")
        }
    }

    func testLightKindChipReadsOnWhite() {
        let palette = light()
        for kind in ["oauth", "codex", "api", "something-new"] {
            assertReadable(palette.kind(kind), "the \(kind) chip")
        }
    }

    // MARK: Dark is untouched

    func testDarkKeepsThePalettesOwnValues() {
        let palette = dark()
        XCTAssertEqual(palette.good, Palette.good)
        XCTAssertEqual(palette.warn, Palette.warn)
        XCTAssertEqual(palette.bad, Palette.bad)
        XCTAssertEqual(palette.usage(10, threshold: 98), Palette.usage(10, threshold: 98))
        XCTAssertEqual(palette.usage(80, threshold: 98), Palette.usage(80, threshold: 98))
        XCTAssertEqual(palette.usage(99, threshold: 98), Palette.usage(99, threshold: 98))
        XCTAssertEqual(palette.kind("codex"), Palette.kind("codex"))
        XCTAssertEqual(palette.kind("oauth"), Palette.kind("oauth"))
    }

    func testDarkProviderInkStaysTheBrandColour() {
        let palette = dark()
        for kind in ["oauth", "codex", "api"] {
            XCTAssertEqual(palette.provider(kind), ProviderRegistry.spec(for: kind).color,
                           "\(kind) lost its brand colour in the dark dock")
        }
    }

    // MARK: The bands are still the bands

    func testTheThresholdsDidNotMove() {
        // The routing threshold decides the band in both appearances; only the
        // paint differs. A profile that switches at 80 warns from 65 and is
        // red from 75 — the Dashboard's bands.
        for palette in [light(), dark()] {
            XCTAssertEqual(palette.usage(64, threshold: 80), palette.good)
            XCTAssertEqual(palette.usage(65, threshold: 80), palette.warn)
            XCTAssertEqual(palette.usage(74, threshold: 80), palette.warn)
            XCTAssertEqual(palette.usage(75, threshold: 80), palette.bad)
            // No threshold means the default 98.
            XCTAssertEqual(palette.usage(92, threshold: nil), palette.warn)
            XCTAssertEqual(palette.usage(93, threshold: nil), palette.bad)
        }
    }

    private func profile(_ json: String) -> Profile {
        let base = #"{"id":"a","name":"a","kind":"oauth","enabled":true,"state":"eligible","#
        return try! JSONDecoder().decode(Profile.self, from: Data((base + json + "}").utf8))
    }

    func testTheWeeklyRingIsBlueUntilTheDashboardsRed() {
        for palette in [light(), dark()] {
            let calm = profile(#""status_word":"healthy","usage_5h_percent":10,"usage_7d_percent":89"#)
            XCTAssertNotEqual(palette.weeklyRing(calm), palette.bad)
            XCTAssertNotEqual(palette.weeklyRing(calm), palette.warn)
            let spent = profile(#""status_word":"healthy","usage_5h_percent":10,"usage_7d_percent":90"#)
            XCTAssertEqual(palette.weeklyRing(spent), palette.bad)
            XCTAssertEqual(palette.ring(spent, window: .fiveHour), palette.good)
            let reauth = profile(##""status_word":"needs re-auth","usage_7d_percent":5,"tag_color":"#C97BFF""##)
            XCTAssertEqual(palette.weeklyRing(reauth), palette.bad)
            XCTAssertEqual(palette.ring(reauth, window: .fiveHour), palette.bad)
            XCTAssertEqual(palette.mark(reauth), Color(tagHex: "#C97BFF"))
            XCTAssertEqual(palette.mark(calm), palette.provider("oauth"))
        }
    }

    func testAnUnknownPercentageIsFaintNotGreen() {
        for palette in [light(), dark()] {
            XCTAssertEqual(palette.usage(nil, threshold: 98), palette.textFaint)
        }
    }

    // MARK: Light and dark actually differ

    func testEveryMeaningColourChangesBetweenAppearances() {
        let lit = light(), drk = dark()
        XCTAssertNotEqual(lit.good, drk.good)
        XCTAssertNotEqual(lit.warn, drk.warn)
        XCTAssertNotEqual(lit.bad, drk.bad)
        XCTAssertNotEqual(lit.orbDot, drk.orbDot)
        XCTAssertNotEqual(lit.sessionFill, drk.sessionFill)
        XCTAssertNotEqual(lit.sessionEdge, drk.sessionEdge)
        XCTAssertNotEqual(lit.provider("oauth"), drk.provider("oauth"))
        XCTAssertNotEqual(lit.provider("codex"), drk.provider("codex"))
    }

    /// Light mode never takes the shader branch, so the plain session fill is
    /// all it has — it must be laid on harder than the dark dock's, which has a
    /// glow behind it.
    func testTheLightSessionFillIsStrongerThanTheDarkOne() {
        func alpha(_ color: Color, file: StaticString = #filePath, line: UInt = #line) -> Double {
            guard let srgb = NSColor(color).usingColorSpace(.sRGB) else {
                XCTFail("colour is not representable in sRGB", file: file, line: line)
                return 0
            }
            return Double(srgb.alphaComponent)
        }
        XCTAssertGreaterThan(alpha(light().sessionEdge), alpha(light().sessionFill),
                             "the leading edge should be the stronger of the two")
        XCTAssertGreaterThan(alpha(light().sessionEdge), alpha(dark().sessionEdge))
        XCTAssertGreaterThan(alpha(light().sessionFill), alpha(dark().sessionFill))
    }

    // MARK: Liquid Glass follows the appearance, not the theme name

    func testGlassOnALightDesktopGetsTheLightMeanings() {
        // Glass resolves `isDark` from the system; the meaning colours have to
        // follow that same answer, or a light glass dock paints dark-dock green.
        for transparency in DockTransparency.allCases {
            let glass = DockPalette.resolve(theme: .glass, transparency: transparency,
                                            systemIsDark: false)
            XCTAssertFalse(glass.isDark)
            XCTAssertEqual(glass.good, light().good)
            XCTAssertEqual(glass.provider("codex"), light().provider("codex"))
            assertReadable(glass.usage(10, threshold: 98), "glass-light good at \(transparency)")

            let dim = DockPalette.resolve(theme: .glass, transparency: transparency,
                                          systemIsDark: true)
            XCTAssertTrue(dim.isDark)
            XCTAssertEqual(dim.good, Palette.good)
        }
    }

    /// Transparency changes the scrim, never what a colour means.
    func testTransparencyDoesNotMoveTheMeaningColours() {
        for transparency in DockTransparency.allCases {
            let palette = light(.light, transparency)
            XCTAssertEqual(palette.good, light().good)
            XCTAssertEqual(palette.usage(99, threshold: 98), light().bad)
            XCTAssertEqual(palette.provider("oauth"), light().provider("oauth"))
        }
    }
}
