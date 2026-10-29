import Foundation

/// The scans' working files in the app's tmp folder (photo folders, Object Capture folders, upload bodies, exports): which ones a
/// scan on screen still needs, their size, and removing the rest. Saved scans live in Documents/SavedScans and are never touched here.
enum TempFiles {
    private static let lock = NSLock()
    private static var inUse = Set<String>()        // names directly inside tmp that a scan still needs

    static var root: URL { FileManager.default.temporaryDirectory }

    /// A scan is using this file or folder: it is never deleted while held.
    static func hold(_ url: URL?) {
        guard let url else { return }
        lock.lock(); inUse.insert(url.lastPathComponent); lock.unlock()
    }

    static func release(_ url: URL?) {
        guard let url else { return }
        lock.lock(); inUse.remove(url.lastPathComponent); lock.unlock()
    }

    /// Nothing needs it any more: let go and delete it.
    static func remove(_ url: URL?) {
        guard let url else { return }
        release(url)
        try? FileManager.default.removeItem(at: url)
    }

    /// Everything in tmp that no scan holds, optionally only what has not changed for `age` seconds.
    static func unused(olderThan age: TimeInterval? = nil) -> [URL] {
        let items = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        lock.lock(); let held = inUse; lock.unlock()
        return items.filter { u in
            guard !held.contains(u.lastPathComponent) else { return false }
            guard let age else { return true }
            let changed = (try? u.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            return Date().timeIntervalSince(changed) > age
        }
    }

    /// Deletes what no scan holds; returns the bytes freed. Slow for big folders: call it off the main thread.
    @discardableResult
    static func purge(olderThan age: TimeInterval? = nil) -> Int64 {
        var freed: Int64 = 0
        for u in unused(olderThan: age) {
            let n = bytes(u)
            if (try? FileManager.default.removeItem(at: u)) != nil { freed += n }
        }
        return freed
    }

    /// Size of a file, or of everything inside a folder.
    static func bytes(_ url: URL) -> Int64 {
        let keys: Set<URLResourceKey> = [.fileSizeKey, .isDirectoryKey]
        guard let v = try? url.resourceValues(forKeys: keys) else { return 0 }
        if v.isDirectory != true { return Int64(v.fileSize ?? 0) }
        var total: Int64 = 0
        let e = FileManager.default.enumerator(at: url, includingPropertiesForKeys: Array(keys))
        while let f = e?.nextObject() as? URL { total += Int64((try? f.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
        return total
    }
}
