import Cocoa

/// Double-clickable switch for the launchd agent. Transcription stays in the
/// Python program that already has Full Disk Access. Closing the window does
/// not stop that agent.

let agentLabel = "com.local.memo-ingest"
let home = NSHomeDirectory()
let agentPlist = "\(home)/Library/LaunchAgents/\(agentLabel).plist"
let configPath = "\(home)/.config/memo-ingest/config.toml"

let appDelegate = AppDelegate()
let application = NSApplication.shared
application.setActivationPolicy(.regular)
application.delegate = appDelegate
application.run()

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var statusView: NSTextView!

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        let started = ensureAgentLoaded()
        refresh()
        if !started.ok {
            append("\n\nCould not start the background checker:\n\(started.detail)")
        }
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func buildWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 420),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Memo Ingest"
        window.center()

        let title = label("Memo Ingest", size: 20, bold: true)
        let note = wrappingLabel(
            "Double-click starts background checking. New voice memos are transcribed about a minute after they finish downloading. Closing this window leaves that running."
        )

        let scroll = NSScrollView(frame: .zero)
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.autohidesScrollers = true
        scroll.translatesAutoresizingMaskIntoConstraints = false

        let text = NSTextView(frame: .zero)
        text.isEditable = false
        text.isSelectable = true
        text.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        text.drawsBackground = false
        text.textContainerInset = NSSize(width: 6, height: 6)
        scroll.documentView = text
        statusView = text

        let start = button("Start", action: #selector(startClicked))
        let stop = button("Stop", action: #selector(stopClicked))
        let open = button("Open Transcripts", action: #selector(openTranscripts))
        let refreshButton = button("Refresh", action: #selector(refreshClicked))

        let row = NSStackView(views: [start, stop, open, refreshButton])
        row.orientation = .horizontal
        row.spacing = 8
        row.translatesAutoresizingMaskIntoConstraints = false

        let column = NSStackView(views: [title, note, scroll, row])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 10
        column.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        column.translatesAutoresizingMaskIntoConstraints = false

        guard let content = window.contentView else { return }
        content.addSubview(column)
        NSLayoutConstraint.activate([
            column.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            column.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            column.topAnchor.constraint(equalTo: content.topAnchor),
            column.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            scroll.leadingAnchor.constraint(equalTo: column.leadingAnchor, constant: 16),
            scroll.trailingAnchor.constraint(equalTo: column.trailingAnchor, constant: -16),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 220),
            note.leadingAnchor.constraint(equalTo: column.leadingAnchor, constant: 16),
            note.trailingAnchor.constraint(equalTo: column.trailingAnchor, constant: -16),
        ])

        self.window = window
        window.makeKeyAndOrderFront(nil)
    }

    @objc func startClicked() {
        let result = ensureAgentLoaded()
        refresh()
        if !result.ok {
            append("\n\nCould not start:\n\(result.detail)")
        }
    }

    @objc func stopClicked() {
        let result = run("/bin/launchctl", ["bootout", "\(domain())/\(agentLabel)"])
        refresh()
        if result.code != 0 && !result.output.contains("No such process") && !result.output.contains("not found") {
            append("\n\nCould not stop:\n\(result.output)")
        }
    }

    @objc func refreshClicked() {
        refresh()
    }

    @objc func openTranscripts() {
        let folder = inboxDirectory()
        let url = URL(fileURLWithPath: folder, isDirectory: true)
        if !FileManager.default.fileExists(atPath: folder) {
            append("\n\nTranscript folder does not exist yet:\n\(folder)")
            return
        }
        NSWorkspace.shared.open(url)
    }

    func refresh() {
        let loaded = agentIsLoaded()
        let header = loaded
            ? "Background checker: running (every 45 seconds)"
            : "Background checker: stopped"
        statusView.string = """
        \(header)

        Transcripts:
        \(inboxDirectory())

        \(databaseStatus())
        """
    }

    func databaseStatus() -> String {
        let db = "\(home)/.local/share/memo-ingest/state.sqlite"
        if !FileManager.default.fileExists(atPath: db) {
            return "No recordings processed yet."
        }
        let sql = """
        SELECT 'processed: ' || (SELECT COUNT(*) FROM processed);
        SELECT 'waiting: ' || (SELECT COUNT(*) FROM observations);
        SELECT 'last run: ' || started_at || '   ' || status || '   wrote ' || files_processed || ', saw ' || files_seen
          FROM runs ORDER BY id DESC LIMIT 1;
        """
        let result = run("/usr/bin/sqlite3", [db, sql])
        if result.code != 0 {
            return "Could not read the state database.\n\(result.output)"
        }
        let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? "No runs recorded yet." : text
    }

    func append(_ text: String) {
        statusView.string += text
    }

    func ensureAgentLoaded() -> (ok: Bool, detail: String) {
        if agentIsLoaded() {
            return (true, "")
        }
        if !FileManager.default.fileExists(atPath: agentPlist) {
            return (false, "Missing \(agentPlist)\nRun setup from ~/memo-ingest first.")
        }
        let result = run("/bin/launchctl", ["bootstrap", domain(), agentPlist])
        if result.code == 0 || agentIsLoaded() {
            return (true, "")
        }
        return (false, result.output)
    }

    func agentIsLoaded() -> Bool {
        run("/bin/launchctl", ["print", "\(domain())/\(agentLabel)"]).code == 0
    }

    func domain() -> String {
        "gui/\(getuid())"
    }

    func inboxDirectory() -> String {
        let config = (try? String(contentsOfFile: configPath, encoding: .utf8)) ?? ""
        for line in config.split(separator: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("inbox_dir") else { continue }
            let parts = trimmed.split(separator: "=", maxSplits: 1)
            if parts.count == 2 {
                return parts[1]
                    .trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "\""))
            }
        }
        return "\(home)/vaults/brain-personal/inbox/transcripts"
    }

    func run(_ program: String, _ arguments: [String]) -> (code: Int32, output: String, detail: String) {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: program)
        task.arguments = arguments
        var environment = ProcessInfo.processInfo.environment
        environment["HOME"] = home
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        task.environment = environment
        let stdout = Pipe()
        let stderr = Pipe()
        task.standardOutput = stdout
        task.standardError = stderr
        do {
            try task.run()
        } catch {
            return (1, "", error.localizedDescription)
        }
        task.waitUntilExit()
        let out = String(data: stdout.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let err = String(data: stderr.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let combined = [out, err].filter { !$0.isEmpty }.joined(separator: "\n")
        return (task.terminationStatus, combined, err)
    }

    func label(_ text: String, size: CGFloat, bold: Bool) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = NSFont.systemFont(ofSize: size, weight: bold ? .semibold : .regular)
        field.translatesAutoresizingMaskIntoConstraints = false
        return field
    }

    func wrappingLabel(_ text: String) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = NSFont.systemFont(ofSize: 13)
        field.textColor = .secondaryLabelColor
        field.translatesAutoresizingMaskIntoConstraints = false
        field.preferredMaxLayoutWidth = 520
        return field
    }

    func button(_ title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        return button
    }
}
