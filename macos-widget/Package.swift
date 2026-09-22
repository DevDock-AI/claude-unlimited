// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "HUD",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "HUD", path: "Sources/HUD"),
        // Pure logic only (edge snapping geometry): `swift test` needs no
        // screen, no daemon and no Accessibility permission.
        .testTarget(name: "HUDTests", dependencies: ["HUD"],
                    path: "Tests/HUDTests"),
    ]
)
