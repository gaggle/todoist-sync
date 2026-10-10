// List sections, through ReminderKit.
//
// EventKit has no API for list sections, so these go through ReminderKit,
// the private framework the Reminders app itself is built on. It shares
// EventKit's ids: a list's calendarIdentifier and a reminder's
// calendarItemIdentifier are their ReminderKit uuids. Private API can change
// with any macOS update, so every class and selector is checked before use;
// a missing one exits non-zero, which fails the sync and trips its circuit
// breaker rather than letting it half-work.
import EventKit
import Foundation

// Typed views onto ReminderKit objects. The selectors are the contract;
// objects are cast to these with rk(_:as:_:) after checking they respond.
@objc protocol RKObjectIDClass {
    @objc(objectIDWithUUID:entityName:) func objectID(uuid: UUID, entityName: String) -> NSObject
}
@objc protocol RKStore {
    @objc(fetchListWithObjectID:error:) func fetchList(objectID: NSObject) throws -> NSObject
    @objc(fetchListSectionsWithListObjectID:error:) func fetchListSections(listObjectID: NSObject) throws -> [NSObject]
}
@objc protocol RKSectionsDataView {
    // An explicit error pointer: nil without an error just means "in no section".
    @objc(fetchListSectionWithReminderID:error:) func fetchListSection(reminderID: NSObject, error: NSErrorPointer) -> NSObject?
}
@objc protocol RKSaveRequest {
    @objc(updateList:) func update(list: NSObject) -> NSObject
    @objc(addListSectionWithDisplayName:toListSectionContextChangeItem:) func addListSection(displayName: String, to context: NSObject) -> NSObject
    @objc(saveSynchronouslyWithError:) func saveSynchronously() throws
}
@objc protocol RKListChangeItem {
    @objc(sectionsContextChangeItem) func sectionsContextChangeItem() -> NSObject
}
@objc protocol RKSectionChangeItem {
    @objc(remObjectID) func remObjectID() -> NSObject
}
@objc protocol RKSectionsContext {
    @objc(setUnsavedSectionIDsOrdering:) func setUnsavedSectionIDsOrdering(_ ids: [NSObject])
    @objc(setShouldUpdateSectionsOrdering:) func setShouldUpdateSectionsOrdering(_ value: Bool)
    @objc(setUnsavedMembershipsOfRemindersInSections:) func setUnsavedMemberships(_ memberships: NSObject)
}

func rkFail(_ message: String) -> Never {
    eprint("ReminderKit: \(message) — this macOS version may have changed the private API")
    exit(1)
}

func rkClass(_ name: String) -> AnyClass {
    guard let cls = NSClassFromString(name) else { rkFail("class \(name) is missing") }
    return cls
}

func rk<T>(_ object: NSObject, as _: T.Type, _ selectors: String...) -> T {
    for name in selectors where !object.responds(to: NSSelectorFromString(name)) {
        rkFail("\(type(of: object)) doesn't respond to \(name)")
    }
    return unsafeBitCast(object, to: T.self)
}

func rkValue<T>(_ object: NSObject, _ key: String) -> T {
    guard object.responds(to: NSSelectorFromString(key)), let value = object.value(forKey: key) as? T else {
        rkFail("\(type(of: object)) has no \(key)")
    }
    return value
}

// `[[ClassName alloc] initWith…]` for a class known only by name. Swift
// can't spell that call, so these call the initializer's implementation
// directly: alloc's +1 reference goes in (initializers consume self) and
// the +1 object that comes out is taken over.
func rkInitParts(_ className: String, _ selectorName: String) -> (AnyClass, Unmanaged<AnyObject>, Selector, IMP) {
    let cls: AnyClass = rkClass(className)
    let allocSel = NSSelectorFromString("alloc")
    let initSel = NSSelectorFromString(selectorName)
    guard let meta = object_getClass(cls), class_getInstanceMethod(meta, allocSel) != nil,
          class_getInstanceMethod(cls, initSel) != nil,
          let initIMP = class_getMethodImplementation(cls, initSel),
          let allocIMP = class_getMethodImplementation(meta, allocSel)
    else { rkFail("\(className) has no \(selectorName)") }
    typealias Alloc = @convention(c) (AnyClass, Selector) -> Unmanaged<AnyObject>
    let allocated = unsafeBitCast(allocIMP, to: Alloc.self)(cls, allocSel)
    return (cls, allocated, initSel, initIMP)
}

func rkInit(_ className: String, _ selectorName: String, _ arg: AnyObject) -> NSObject {
    let (_, allocated, sel, imp) = rkInitParts(className, selectorName)
    typealias Init = @convention(c) (Unmanaged<AnyObject>, Selector, AnyObject) -> Unmanaged<NSObject>?
    guard let made = unsafeBitCast(imp, to: Init.self)(allocated, sel, arg) else { rkFail("\(className) \(selectorName) returned nil") }
    return made.takeRetainedValue()
}

func rkMembership(member: UUID, group: UUID, obsolete: Bool) -> NSObject {
    let selector = "initWithMemberIdentifier:groupIdentifier:isObsolete:modifiedOn:"
    let (_, allocated, sel, imp) = rkInitParts("REMMembership", selector)
    typealias Init = @convention(c) (Unmanaged<AnyObject>, Selector, NSUUID, NSUUID, ObjCBool, NSDate) -> Unmanaged<NSObject>?
    let made = unsafeBitCast(imp, to: Init.self)(allocated, sel, member as NSUUID, group as NSUUID, ObjCBool(obsolete), Date() as NSDate)
    guard let made else { rkFail("REMMembership \(selector) returned nil") }
    return made.takeRetainedValue()
}

// "Reminder does not belong to a user-defined section"
let notInAUserDefinedSection = -3009

/// The sections of one Reminders list, by display name.
final class ListSections {
    private let store: NSObject
    private let storeAPI: RKStore
    private let listID: NSObject
    private lazy var dataView = rk(rkInit("REMListSectionsDataView", "initWithStore:", store), as: RKSectionsDataView.self,
                                   "fetchListSectionWithReminderID:error:")

    init(calendar: EKCalendar) {
        guard dlopen("/System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit", RTLD_NOW) != nil else {
            rkFail("framework failed to load")
        }
        guard let storeClass = rkClass("REMStore") as? NSObject.Type else { rkFail("REMStore isn't an NSObject") }
        store = storeClass.init()
        storeAPI = rk(store, as: RKStore.self, "fetchListWithObjectID:error:", "fetchListSectionsWithListObjectID:error:")
        listID = ListSections.objectID("REMCDList", calendar.calendarIdentifier)
    }

    static func objectID(_ entity: String, _ uuidString: String) -> NSObject {
        guard let uuid = UUID(uuidString: uuidString) else { rkFail("\(uuidString) isn't a uuid") }
        let cls: AnyClass = rkClass("REMObjectID")
        guard class_getClassMethod(cls, NSSelectorFromString("objectIDWithUUID:entityName:")) != nil else {
            rkFail("REMObjectID has no objectIDWithUUID:entityName:")
        }
        return unsafeBitCast(cls as AnyObject, to: RKObjectIDClass.self).objectID(uuid: uuid, entityName: entity)
    }

    /// (name, object id) for each section, in the list's order.
    func all() -> [(name: String, objectID: NSObject)] {
        do {
            return try storeAPI.fetchListSections(listObjectID: listID).map {
                (rkValue($0, "displayName"), rkValue($0, "objectID"))
            }
        } catch {
            rkFail("fetching sections failed: \(error)")
        }
    }

    func sectionName(ofReminder reminderID: String) -> String? {
        var error: NSError?
        let section = dataView.fetchListSection(reminderID: ListSections.objectID("REMCDReminder", reminderID), error: &error)
        if let error {
            // In a list that has sections, a reminder in none of them comes
            // back as this error rather than as nil.
            if error.domain == "com.apple.reminderkit" && error.code == notInAUserDefinedSection { return nil }
            rkFail("looking up a reminder's section failed: \(error)")
        }
        return section.map { rkValue($0, "displayName") }
    }

    /// Moves a reminder into the named section — created at the end if the
    /// list has none by that name — or, with nil, out of any section.
    func move(reminder reminderID: String, to name: String?) {
        guard let member = UUID(uuidString: reminderID) else { rkFail("\(reminderID) isn't a uuid") }
        let membership: NSObject
        if let name {
            let section = all().first(where: { $0.name == name })?.objectID ?? create(name)
            membership = rkMembership(member: member, group: rkValue(section, "uuid"), obsolete: false)
        } else {
            guard sectionName(ofReminder: reminderID) != nil else { return }
            // ReminderKit has no "remove from section". A newer membership in
            // the list itself (rather than in one of its sections) does it;
            // marking the old membership obsolete, the apparent alternative,
            // has no effect.
            membership = rkMembership(member: member, group: rkValue(listID, "uuid"), obsolete: false)
        }
        let memberships = rkInit("REMMemberships", "initWithMemberships:", [membership] as NSArray)
        let request = saveRequest()
        rk(sectionsContext(request), as: RKSectionsContext.self, "setUnsavedMembershipsOfRemindersInSections:")
            .setUnsavedMemberships(memberships)
        save(request, "moving a reminder between sections")
    }

    private func create(_ name: String) -> NSObject {
        let ordering = all().map(\.objectID)
        let request = saveRequest()
        let context = sectionsContext(request)
        let change = rk(request.addListSection(displayName: name, to: context), as: RKSectionChangeItem.self, "remObjectID")
        let sectionID = change.remObjectID()
        let contextAPI = rk(context, as: RKSectionsContext.self, "setUnsavedSectionIDsOrdering:", "setShouldUpdateSectionsOrdering:")
        contextAPI.setUnsavedSectionIDsOrdering(ordering + [sectionID])
        contextAPI.setShouldUpdateSectionsOrdering(true)
        save(request, "creating section \(name)")
        return sectionID
    }

    private func saveRequest() -> RKSaveRequest {
        rk(rkInit("REMSaveRequest", "initWithStore:", store), as: RKSaveRequest.self,
           "updateList:", "addListSectionWithDisplayName:toListSectionContextChangeItem:", "saveSynchronouslyWithError:")
    }

    private func sectionsContext(_ request: RKSaveRequest) -> NSObject {
        let list: NSObject
        do { list = try storeAPI.fetchList(objectID: listID) } catch { rkFail("fetching the list failed: \(error)") }
        let change = rk(request.update(list: list), as: RKListChangeItem.self, "sectionsContextChangeItem")
        return change.sectionsContextChangeItem()
    }

    private func save(_ request: RKSaveRequest, _ what: String) {
        do { try request.saveSynchronously() } catch { rkFail("\(what) failed: \(error)") }
    }
}
