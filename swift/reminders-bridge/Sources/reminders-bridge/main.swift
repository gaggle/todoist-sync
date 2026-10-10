import EventKit
import Foundation

func eprint(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

let store = EKEventStore()

func requestAccess() -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    store.requestFullAccessToReminders { ok, error in
        granted = ok
        if let error = error {
            eprint("access request error: \(error)")
        }
        sem.signal()
    }
    sem.wait()
    return granted
}

func findCalendar(named name: String, createIfMissing: Bool) -> EKCalendar? {
    if let existing = store.calendars(for: .reminder).first(where: { $0.title == name }) {
        return existing
    }
    guard createIfMissing else { return nil }
    guard let source = store.defaultCalendarForNewReminders()?.source
        ?? store.sources.first(where: { $0.sourceType == .local })
        ?? store.sources.first
    else { return nil }
    let cal = EKCalendar(for: .reminder, eventStore: store)
    cal.title = name
    cal.source = source
    do {
        try store.saveCalendar(cal, commit: true)
        return cal
    } catch {
        eprint("saveCalendar failed: \(error)")
        return nil
    }
}

// Looks up a reminder by id, scoped to a specific list. Reminder ids are
// otherwise globally addressable via calendarItem(withIdentifier:), which
// would let a caller read/mutate any reminder anywhere if it ever received
// an id from outside the intended list (e.g. a tampered or stale state
// file) — this keeps every id-based operation confined to the list it's
// supposed to operate on.
func findReminder(id: String, inList listName: String) -> EKReminder? {
    guard let item = store.calendarItem(withIdentifier: id) as? EKReminder else { return nil }
    guard item.calendar.title == listName else { return nil }
    return item
}

func fetchReminders(in calendar: EKCalendar) -> [EKReminder] {
    let predicate = store.predicateForReminders(in: [calendar])
    let sem = DispatchSemaphore(value: 0)
    var result: [EKReminder] = []
    _ = store.fetchReminders(matching: predicate) { reminders in
        result = reminders ?? []
        sem.signal()
    }
    sem.wait()
    return result
}

// Wire format for due dates is "yyyy-MM-ddTHH:mm:ss" with no timezone —
// matching the naive/local datetimes used throughout the Python side and
// AppleScript's local wall-clock dates. Parsing/formatting these as literal
// calendar components (never through Date/ISO8601DateFormatter, which are
// instant-based and would silently shift the date across timezone
// boundaries — exactly the bug this caused during testing) keeps it
// unambiguous.
func parseNaiveDateTime(_ s: String) -> DateComponents? {
    let parts = s.split(separator: "T")
    guard parts.count == 2 else { return nil }
    let dateParts = parts[0].split(separator: "-").compactMap { Int($0) }
    let timeParts = parts[1].split(separator: ":").compactMap { Int($0.prefix(2)) }
    guard dateParts.count == 3, timeParts.count >= 2 else { return nil }
    var comps = DateComponents()
    comps.year = dateParts[0]
    comps.month = dateParts[1]
    comps.day = dateParts[2]
    comps.hour = timeParts[0]
    comps.minute = timeParts[1]
    comps.second = timeParts.count >= 3 ? timeParts[2] : 0
    return comps
}

func formatNaiveDateTime(_ comps: DateComponents) -> String? {
    guard let y = comps.year, let mo = comps.month, let d = comps.day else { return nil }
    let h = comps.hour ?? 0
    let mi = comps.minute ?? 0
    let s = comps.second ?? 0
    return String(format: "%04d-%02d-%02dT%02d:%02d:%02d", y, mo, d, h, mi, s)
}

// A reminder's due date is "all-day" (date only, no specific time) when its
// dueDateComponents carry no hour — matches how EventKit itself represents
// the distinction, unlike Reminders.app's AppleScript dictionary which
// always populates a time component regardless.
func dueDateComponents(fromWire wire: String, allDay: Bool) -> DateComponents? {
    guard var comps = parseNaiveDateTime(wire) else { return nil }
    if allDay {
        comps.hour = nil
        comps.minute = nil
        comps.second = nil
    }
    return comps
}

struct ReminderJSON: Codable {
    let id: String
    let name: String
    let body: String
    let completed: Bool
    let due: String?
    let allDay: Bool
    let recurrence: String?
    let section: String?
}

// A compact, RRULE-style rendering of a reminder's repeat rules (e.g.
// "FREQ=MONTHLY;INTERVAL=3"), for logging which items repeat. Built by hand
// because EKRecurrenceRule's own description embeds an object address.
func describeRecurrence(_ rules: [EKRecurrenceRule]?) -> String? {
    guard let rules, !rules.isEmpty else { return nil }
    let frequencies = ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]
    let weekdays = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]
    func list(_ values: [NSNumber]?) -> String? {
        guard let values, !values.isEmpty else { return nil }
        return values.map { $0.stringValue }.joined(separator: ",")
    }
    return rules.map { rule in
        var parts = ["FREQ=\(frequencies[rule.frequency.rawValue])", "INTERVAL=\(rule.interval)"]
        if let days = rule.daysOfTheWeek, !days.isEmpty {
            parts.append("BYDAY=" + days.map {
                ($0.weekNumber != 0 ? "\($0.weekNumber)" : "") + weekdays[$0.dayOfTheWeek.rawValue - 1]
            }.joined(separator: ","))
        }
        if let v = list(rule.daysOfTheMonth) { parts.append("BYMONTHDAY=\(v)") }
        if let v = list(rule.monthsOfTheYear) { parts.append("BYMONTH=\(v)") }
        if let v = list(rule.weeksOfTheYear) { parts.append("BYWEEKNO=\(v)") }
        if let v = list(rule.daysOfTheYear) { parts.append("BYYEARDAY=\(v)") }
        if let v = list(rule.setPositions) { parts.append("BYSETPOS=\(v)") }
        if let end = rule.recurrenceEnd {
            if let date = end.endDate {
                parts.append("UNTIL=\(ISO8601DateFormatter().string(from: date))")
            } else {
                parts.append("COUNT=\(end.occurrenceCount)")
            }
        }
        return parts.joined(separator: ";")
    }.joined(separator: " | ")
}

func toJSON(_ r: EKReminder, section: String?) -> ReminderJSON {
    var due: String? = nil
    var allDay = false
    if let comps = r.dueDateComponents {
        due = formatNaiveDateTime(comps)
        allDay = comps.hour == nil
    }
    return ReminderJSON(
        id: r.calendarItemIdentifier,
        name: r.title ?? "",
        body: r.notes ?? "",
        completed: r.isCompleted,
        due: due,
        allDay: allDay,
        recurrence: describeRecurrence(r.recurrenceRules),
        section: section
    )
}

func printJSON<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    guard let data = try? encoder.encode(value) else {
        eprint("encode failed")
        exit(1)
    }
    print(String(data: data, encoding: .utf8)!)
}

// Options are parsed by walking argv in strict `--key value` pairs rather
// than by scanning the whole array for a matching `--key`. A scan treats
// every element as a potential option name, including the ones that are
// really values — so a reminder titled "--all-day" or "--body" (titles and
// notes come from Todoist, i.e. outside this program's control) would be
// re-read as an option and silently change how the rest of the command
// parsed. Consuming each value positionally means a value is never
// inspected as a key, whatever it happens to contain.
//
// Names in `flagNames` take no value; everything else consumes the next
// element. First occurrence wins, so an option can't be redefined later in
// the line either.
struct ParsedArgs {
    private let values: [String: String]
    private let flags: Set<String>

    static let flagNames: Set<String> = ["all-day"]

    init(_ args: [String]) {
        var values: [String: String] = [:]
        var flags: Set<String> = []
        var i = 0
        while i < args.count {
            guard args[i].hasPrefix("--") else {
                i += 1
                continue
            }
            let name = String(args[i].dropFirst(2))
            if ParsedArgs.flagNames.contains(name) {
                flags.insert(name)
                i += 1
            } else if i + 1 < args.count {
                if values[name] == nil { values[name] = args[i + 1] }
                i += 2
            } else {
                i += 1
            }
        }
        self.values = values
        self.flags = flags
    }

    func opt(_ name: String) -> String? { values[name] }
    func flag(_ name: String) -> Bool { flags.contains(name) }
}

guard requestAccess() else {
    eprint("Reminders access not granted. Check System Settings > Privacy & Security > Reminders.")
    exit(1)
}

let args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else {
    eprint("usage: reminders-bridge <command> [options]")
    exit(1)
}
let rest = Array(args.dropFirst())
let opts = ParsedArgs(rest)

switch command {
case "get-reminders":
    guard let listName = opts.opt("list"), let cal = findCalendar(named: listName, createIfMissing: true) else {
        print("[]")
        break
    }
    let reminders = fetchReminders(in: cal)
    // Sections come from ReminderKit, one lookup per reminder — skipped
    // outright for a list that has none.
    let sections = ListSections(calendar: cal)
    let hasSections = !sections.all().isEmpty
    printJSON(reminders.map {
        toJSON($0, section: hasSections ? sections.sectionName(ofReminder: $0.calendarItemIdentifier) : nil)
    })

case "create-reminder":
    guard let listName = opts.opt("list"), let cal = findCalendar(named: listName, createIfMissing: true) else {
        eprint("list not found/creatable")
        exit(1)
    }
    let reminder = EKReminder(eventStore: store)
    reminder.calendar = cal
    reminder.title = opts.opt("name") ?? ""
    reminder.notes = opts.opt("body") ?? ""
    if let dueStr = opts.opt("due"), let comps = dueDateComponents(fromWire: dueStr, allDay: opts.flag("all-day")) {
        reminder.dueDateComponents = comps
    }
    do {
        try store.save(reminder, commit: true)
        printJSON(["id": reminder.calendarItemIdentifier])
    } catch {
        eprint("save failed: \(error)")
        exit(1)
    }

case "set-body":
    guard let listName = opts.opt("list"), let id = opts.opt("id"),
          let item = findReminder(id: id, inList: listName)
    else {
        printJSON(["ok": false])
        break
    }
    item.notes = opts.opt("body") ?? ""
    do {
        try store.save(item, commit: true)
        printJSON(["ok": true])
    } catch {
        eprint("save failed: \(error)")
        printJSON(["ok": false])
    }

case "set-name":
    guard let listName = opts.opt("list"), let id = opts.opt("id"),
          let item = findReminder(id: id, inList: listName)
    else {
        printJSON(["ok": false])
        break
    }
    item.title = opts.opt("name") ?? ""
    do {
        try store.save(item, commit: true)
        printJSON(["ok": true])
    } catch {
        eprint("save failed: \(error)")
        printJSON(["ok": false])
    }

case "complete":
    guard let listName = opts.opt("list"), let id = opts.opt("id"),
          let item = findReminder(id: id, inList: listName)
    else {
        printJSON(["ok": false])
        break
    }
    item.isCompleted = true
    do {
        try store.save(item, commit: true)
        printJSON(["ok": true])
    } catch {
        eprint("save failed: \(error)")
        printJSON(["ok": false])
    }

case "uncomplete":
    guard let listName = opts.opt("list"), let id = opts.opt("id"),
          let item = findReminder(id: id, inList: listName)
    else {
        printJSON(["ok": false])
        break
    }
    item.isCompleted = false
    do {
        try store.save(item, commit: true)
        printJSON(["ok": true])
    } catch {
        eprint("save failed: \(error)")
        printJSON(["ok": false])
    }

case "set-due":
    guard let listName = opts.opt("list"), let id = opts.opt("id"),
          let item = findReminder(id: id, inList: listName),
          let dueStr = opts.opt("due"), let comps = dueDateComponents(fromWire: dueStr, allDay: opts.flag("all-day"))
    else {
        printJSON(["ok": false])
        break
    }
    item.dueDateComponents = comps
    do {
        try store.save(item, commit: true)
        printJSON(["ok": true])
    } catch {
        eprint("save failed: \(error)")
        printJSON(["ok": false])
    }

case "set-section":
    // --section "" takes the reminder out of any section.
    guard let listName = opts.opt("list"), let id = opts.opt("id"),
          let item = findReminder(id: id, inList: listName), let name = opts.opt("section")
    else {
        printJSON(["ok": false])
        break
    }
    ListSections(calendar: item.calendar).move(reminder: item.calendarItemIdentifier, to: name.isEmpty ? nil : name)
    printJSON(["ok": true])

case "alert":
    // A loud reminder in the user's default list — deliberately not the
    // synced one — so a stopped sync gets noticed on every device: high
    // priority, due a minute from now, with an alarm at the due time.
    guard let cal = store.defaultCalendarForNewReminders() else {
        eprint("no default Reminders list")
        exit(1)
    }
    let alert = EKReminder(eventStore: store)
    alert.calendar = cal
    alert.title = opts.opt("name") ?? "Todoist Sync stopped"
    alert.notes = opts.opt("body") ?? ""
    alert.priority = 1
    let due = Date().addingTimeInterval(60)
    alert.dueDateComponents = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: due)
    alert.addAlarm(EKAlarm(absoluteDate: due))
    do {
        try store.save(alert, commit: true)
        printJSON(["id": alert.calendarItemIdentifier])
    } catch {
        eprint("save failed: \(error)")
        exit(1)
    }

case "delete":
    guard let listName = opts.opt("list"), let id = opts.opt("id"),
          let item = findReminder(id: id, inList: listName)
    else {
        printJSON(["ok": false])
        break
    }
    do {
        try store.remove(item, commit: true)
        printJSON(["ok": true])
    } catch {
        eprint("remove failed: \(error)")
        printJSON(["ok": false])
    }

default:
    eprint("unknown command: \(command)")
    exit(1)
}
