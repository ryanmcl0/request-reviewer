// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "RequestReviewerBar",
    platforms: [.macOS(.v11)],
    targets: [
        .executableTarget(
            name: "RequestReviewerBar",
            path: "Sources/RequestReviewerBar"
        )
    ]
)
