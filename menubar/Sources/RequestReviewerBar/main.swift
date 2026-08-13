// RequestReviewerBar: a menu bar icon that shows how many permission
// prompts request-reviewer has auto-approved on your behalf. Reads the same
// JSONL audit log the Python hook writes (~/.claude/request-reviewer.log or
// $REVIEWER_LOG) — no IPC, no daemon, nothing else to run.
//
// It also shows how much RAM the local model is holding, and releases it on
// quit. Quitting the reviewer should mean the reviewer stops costing you
// anything, and on a laptop the multi-GB resident model is the whole cost.

import AppKit

let logPath = ProcessInfo.processInfo.environment["REVIEWER_LOG"]
    ?? ("~/.claude/request-reviewer.log" as NSString).expandingTildeInPath

let ollamaURL = (ProcessInfo.processInfo.environment["REVIEWER_OLLAMA_URL"]
    ?? "http://localhost:11434").trimmingCharacters(in: CharacterSet(charactersIn: "/"))

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

// MARK: - Ollama

/// Blocking JSON call. The menu is only ever driven by direct user action
/// (opening it, or quitting), so blocking briefly is simpler and safer than
/// async work that might not finish before the process exits.
func ollama(_ path: String, body: [String: Any]? = nil, timeout: TimeInterval) -> [String: Any]? {
    guard let url = URL(string: ollamaURL + path) else { return nil }
    var request = URLRequest(url: url, timeoutInterval: timeout)
    if let body {
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
    }

    var result: [String: Any]?
    let semaphore = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: request) { data, _, _ in
        if let data {
            result = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        }
        semaphore.signal()
    }.resume()
    _ = semaphore.wait(timeout: .now() + timeout)
    return result
}

/// Models Ollama currently holds in memory, as (name, bytes).
func residentModels() -> [(name: String, bytes: Int)] {
    guard let response = ollama("/api/ps", timeout: 2),
          let models = response["models"] as? [[String: Any]]
    else { return [] }
    return models.compactMap { model in
        guard let name = model["name"] as? String else { return nil }
        return (name, model["size"] as? Int ?? 0)
    }
}

/// Evict every resident model. `keep_alive: 0` tells Ollama to unload
/// immediately rather than waiting out the remaining timer.
func unloadModels() {
    for model in residentModels() {
        _ = ollama(
            "/api/chat",
            body: ["model": model.name, "messages": [], "keep_alive": 0],
            timeout: 5
        )
    }
}

func humanBytes(_ bytes: Int) -> String {
    let formatter = ByteCountFormatter()
    formatter.countStyle = .memory
    return formatter.string(fromByteCount: Int64(bytes))
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()
    private let clicksItem = NSMenuItem(title: "…", action: nil, keyEquivalent: "")
    private let memoryItem = NSMenuItem(title: "…", action: nil, keyEquivalent: "")

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
        menu.addItem(clicksItem)
        menu.addItem(memoryItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(
            title: "Unload Model",
            action: #selector(unloadNow),
            keyEquivalent: ""
        ))
        menu.addItem(NSMenuItem(
            title: "Quit",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        ))
        statusItem.menu = menu
    }

    // Recompute only when the menu is actually opened — no polling, no timers.
    func menuWillOpen(_ menu: NSMenu) {
        clicksItem.title = "\(formatted(clicksSaved())) clicks saved"

        let resident = residentModels()
        let total = resident.reduce(0) { $0 + $1.bytes }
        switch resident.count {
        case 0:
            memoryItem.title = "No model in memory"
        case 1:
            memoryItem.title = "\(resident[0].name) — \(humanBytes(total)) in memory"
        default:
            memoryItem.title = "\(resident.count) models — \(humanBytes(total)) in memory"
        }
    }

    @objc private func unloadNow() {
        unloadModels()
    }

    // Covers the Quit menu item, ⌘Q, and logout alike.
    func applicationWillTerminate(_ notification: Notification) {
        unloadModels()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory) // no Dock icon, menu bar only
app.run()
