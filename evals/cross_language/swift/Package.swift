// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ContinuityFixture",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "ContinuityFixture", targets: ["ContinuityFixture"])
    ],
    targets: [
        .target(name: "ContinuityFixture"),
        .testTarget(name: "ContinuityFixtureTests", dependencies: ["ContinuityFixture"])
    ]
)
