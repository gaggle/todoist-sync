import AppKit
import Foundation

func eprint(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

guard CommandLine.arguments.count > 1 else {
    eprint("usage: wake-watcher <launchd-job-label>")
    exit(1)
}
let syncJobLabel = CommandLine.arguments[1]

// Firing immediately on the wake notification reliably races Wi-Fi
// reassociation and fails the run, so poll for actual DNS resolution of
// the sync target instead of guessing a fixed delay: fire as soon as it
// resolves, but give up and sync anyway after postWakeMaxWaitSeconds so a
// slow/failed reconnect doesn't delay sync indefinitely — that case just
// fails and self-heals on the next 15-minute timer run.
//
// SCNetworkReachability was tried first but rejected: its .reachable flag
// only reflects "a local network route exists" (interface up), not
// whether the hostname actually resolves — it returned identical
// "reachable" flags for api.todoist.com, a nonexistent made-up hostname,
// and even 0.0.0.0, so it can't distinguish "Wi-Fi is up" from "DNS/the
// API is actually reachable," which is exactly the race we're guarding
// against. getaddrinfo requires a real round-trip to a working resolver,
// so it fails for a bogus/unresolvable host and succeeds for a real one.
let reachabilityTarget = "api.todoist.com"
let postWakeMaxWaitSeconds = 30.0
let reachabilityPollIntervalSeconds = 1.0

@Sendable func canResolve(host: String) -> Bool {
    var hints = addrinfo(
        ai_flags: 0, ai_family: AF_UNSPEC, ai_socktype: SOCK_STREAM, ai_protocol: 0,
        ai_addrlen: 0, ai_canonname: nil, ai_addr: nil, ai_next: nil
    )
    var result: UnsafeMutablePointer<addrinfo>?
    let status = getaddrinfo(host, nil, &hints, &result)
    defer { if let result = result { freeaddrinfo(result) } }
    return status == 0
}

// Ask launchd to run the already-configured timer job on demand, rather
// than spawning the sync script as our own direct child. If wake-watcher
// spawned it directly, wake-watcher would be the root of that process
// tree, and macOS's TCC "responsible process" resolution for the
// Reminders access the sync performs several subprocess-levels down
// walks up to that root — attributing the permission prompt to
// "wake-watcher" instead of the sync tool itself. Going through launchd
// makes launchd the direct parent either way, so wake-triggered and
// timer-triggered runs get identical, correct attribution.
//
// Plain `kickstart`, never `kickstart -k`: -k kills a sync that's already
// running, possibly between a Todoist write and the state save. Without
// it, a kickstart while the timer's run is in flight does nothing.
@Sendable func runSync(waited: Double) {
    eprint("wake-watcher: \(Date()) woke; starting \(syncJobLabel) after \(Int(waited))s")
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    task.arguments = ["kickstart", "gui/\(getuid())/\(syncJobLabel)"]
    do {
        try task.run()
    } catch {
        eprint("wake-watcher: failed to start \(syncJobLabel): \(error)")
    }
}

func waitForReachabilityThenSync(elapsed: Double = 0) {
    if canResolve(host: reachabilityTarget) || elapsed >= postWakeMaxWaitSeconds {
        runSync(waited: elapsed)
        return
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + reachabilityPollIntervalSeconds) {
        waitForReachabilityThenSync(elapsed: elapsed + reachabilityPollIntervalSeconds)
    }
}

NSWorkspace.shared.notificationCenter.addObserver(
    forName: NSWorkspace.didWakeNotification,
    object: nil,
    queue: .main
) { _ in
    waitForReachabilityThenSync()
}

RunLoop.main.run()
