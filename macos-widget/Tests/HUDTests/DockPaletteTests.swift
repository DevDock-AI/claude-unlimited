import SwiftUI
import XCTest
@testable import HUD

/// `DockPalette.resolve` decides which way round the dock is drawn. The whole
/// point of it being pure is that every combination is checkable here, with no
/// screen and no system preference to flip.
final class DockPaletteTests: XCTestCase {

    private func resolve(_ theme: DockTheme, _ transparency: DockTransparency = .medium,
                         systemIsDark: Bool) -> DockPalette {
        DockPalette.resolve(theme: theme, transparency: transparency, systemIsDark: systemIsDark)
    }

    private let everyCombination: [(DockTheme, DockTransparency, Bool)] = {
        var all: [(DockTheme, DockTransparency, Bool)] = []
        for theme in DockTheme.allCases {
            for transparency in DockTransparency.allCases {
                for systemIsDark in [true, false] {
                    all.append((theme, transparency, systemIsDark))
                }
            }
        }
        return all
    }()

    // MARK: Dark and Light mean themselves

    func testDarkIgnoresTheSystemAppearance() {
        // Someone who picked Dark asked for a dark dock, not for the dock to
        // follow the system. This change must not have touched it.
        for transparency in DockTransparency.allCases {
            XCTAssertEqual(resolve(.dark, transparency, systemIsDark: true),
                           resolve(.dark, transparency, systemIsDark: false))
        }
        XCTAssertTrue(resolve(.dark, systemIsDark: false).isDark)
    }

    func testLightIgnoresTheSystemAppearance() {
        for transparency in DockTransparency.allCases {
            XCTAssertEqual(resolve(.light, transparency, systemIsDark: true),
                           resolve(.light, transparency, systemIsDark: false))
        }
        XCTAssertFalse(resolve(.light, systemIsDark: true).isDark)
    }

    func testDarkAndLightStillUseTheBlurTint() {
        for transparency in DockTransparency.allCases {
            XCTAssertEqual(resolve(.dark, transparency, systemIsDark: true).scrim, transparency.tint)
            XCTAssertEqual(resolve(.light, transparency, systemIsDark: true).scrim, transparency.tint)
        }
    }

    // MARK: Glass follows the appearance — the bug this type was added for

    func testGlassIsDarkInDarkMode() {
        let palette = resolve(.glass, systemIsDark: true)
        XCTAssertTrue(palette.isDark)
        XCTAssertEqual(palette.base, .black)
        XCTAssertEqual(palette.text, Palette.text, "light text over a dark scrim")
    }

    func testGlassIsLightInLightMode() {
        // NSGlassEffectView renders LIGHT glass in Light Mode. A black scrim
        // and light text over that was the unreadable grey panel.
        let palette = resolve(.glass, systemIsDark: false)
        XCTAssertFalse(palette.isDark)
        XCTAssertEqual(palette.base, .white)
        XCTAssertNotEqual(palette.text, Palette.text, "dark text over a light scrim")
    }

    func testGlassDiffersBetweenAppearances() {
        XCTAssertNotEqual(resolve(.glass, systemIsDark: true),
                          resolve(.glass, systemIsDark: false))
    }

    // MARK: the invariants

    func testTheGlassScrimIsNeverBelowTheContrastFloor() {
        // The floor is the invariant, not any particular number: glass alone
        // shows whatever is behind it straight through, and the dock's text
        // has to survive a white window or a white wallpaper under it.
        for transparency in DockTransparency.allCases {
            for systemIsDark in [true, false] {
                XCTAssertGreaterThanOrEqual(
                    resolve(.glass, transparency, systemIsDark: systemIsDark).scrim, 0.16,
                    "\(transparency.label), systemIsDark=\(systemIsDark)")
            }
        }
    }

    func testTheForegroundNeverMatchesTheBackground() {
        // The one failure mode that matters: light text on a light dock, or
        // dark on dark. Every combination, not the one that was looked at.
        for (theme, transparency, systemIsDark) in everyCombination {
            let palette = resolve(theme, transparency, systemIsDark: systemIsDark)
            let label = "\(theme.label)/\(transparency.label)/dark=\(systemIsDark)"
            XCTAssertEqual(palette.base, palette.isDark ? .black : .white, label)
            XCTAssertEqual(palette.text, palette.isDark ? Palette.text : Color(white: 0.11), label)
            XCTAssertEqual(palette.textDim, palette.isDark ? Palette.textDim : Color(white: 0.36), label)
            XCTAssertEqual(palette.textFaint, palette.isDark ? Palette.textFaint : Color(white: 0.52), label)
        }
    }

    func testEveryCombinationResolves() {
        // Nothing returns a transparent surface or a scrim outside 0...1,
        // whatever was picked.
        for (theme, transparency, systemIsDark) in everyCombination {
            let palette = resolve(theme, transparency, systemIsDark: systemIsDark)
            let label = "\(theme.label)/\(transparency.label)/dark=\(systemIsDark)"
            XCTAssertGreaterThanOrEqual(palette.scrim, 0, label)
            XCTAssertLessThanOrEqual(palette.scrim, 1, label)
        }
    }

    // MARK: the theme itself

    func testTheThemeNoLongerDecidesDarkness() {
        // `DockTheme.isDark` was the property that encoded "glass is always
        // dark". Darkness is now the palette's answer, and the theme keeps
        // only what the menu and UserDefaults need.
        XCTAssertTrue(DockTheme.light.isLight)
        XCTAssertFalse(DockTheme.glass.isLight)
        XCTAssertEqual(DockTheme(rawValue: 2), .glass, "the saved value must not move")
    }
}
