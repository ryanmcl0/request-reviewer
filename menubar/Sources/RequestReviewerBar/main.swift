// RequestReviewerBar: a menu bar icon that shows how many permission
// prompts request-reviewer has auto-approved on your behalf. Reads the same
// JSONL audit log the Python hook writes (~/.claude/request-reviewer.log or
// $REVIEWER_LOG) — no IPC, no daemon, nothing else to run.

import AppKit

let logPath = ProcessInfo.processInfo.environment["REVIEWER_LOG"]
    ?? ("~/.claude/request-reviewer.log" as NSString).expandingTildeInPath

func clicksSaved() -> Int {
    guard let data = FileManager.default.contents(atPath: logPath),
          let text = String(data: data, encoding: .utf8)
    else { return 0 }

    var count = 0
    text.enumerateLines { line, _ in
        guard let lineData = line.data(using: .utf8),
              let record = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any],
              record["final"] as? String == "allow"
        else { return }
        count += 1
    }
    return count
}

func formatted(_ n: Int) -> String {
    let formatter = NumberFormatter()
    formatter.numberStyle = .decimal
    return formatter.string(from: NSNumber(value: n)) ?? "\(n)"
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: "checkmark.shield", accessibilityDescription: "request-reviewer")
            image?.isTemplate = true
            button.image = image
        }
        menu.delegate = self
        let title = NSMenuItem(title: "Claude Permission Reviewer", action: nil, keyEquivalent: "")
        title.isEnabled = false
        menu.addItem(title)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "…", action: nil, keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        statusItem.menu = menu
    }

    // Recompute only when the menu is actually opened — no polling, no timers.
    func menuWillOpen(_ menu: NSMenu) {
        menu.item(at: 2)?.title = "\(formatted(clicksSaved())) clicks saved"
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory) // no Dock icon, menu bar only
app.run()
