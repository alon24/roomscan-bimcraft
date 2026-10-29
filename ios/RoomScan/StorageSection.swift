import SwiftUI
import UIKit

/// Settings, Storage: where the app keeps saved scans and its working files, how much space each takes, and deleting them.
struct StorageSection: View {
    struct Sizes {
        var saved: Int64 = 0
        var savedCount = 0
        var temp: Int64 = 0
        var tempFree: Int64 = 0        // what "Delete temp files" would free: scan files no scan on screen needs, other files over a day old
        var caches: Int64 = 0
    }

    @State private var sizes: Sizes?
    @State private var note = ""
    @State private var busy = false
    @State private var askDelete = false
    @State private var askTemp = false
    @State private var askAgain = false
    @State private var typed = ""

    static var documents: URL { FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0] }
    static var caches: URL { FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0] }

    /// Walks the folders: slow with many photos, so it runs off the main thread.
    static func measure() -> Sizes {
        var s = Sizes()
        s.saved = TempFiles.bytes(LocalStore.root)
        s.savedCount = LocalStore.list().count
        let all = (try? FileManager.default.contentsOfDirectory(at: TempFiles.root, includingPropertiesForKeys: nil)) ?? []
        s.temp = all.reduce(Int64(0)) { $0 + TempFiles.bytes($1) }
        s.tempFree = TempFiles.unused().reduce(Int64(0)) { $0 + TempFiles.bytes($1) }
        s.caches = TempFiles.bytes(caches)
        return s
    }

    private func text(_ b: Int64) -> String { ByteCountFormatter.string(fromByteCount: b, countStyle: .file) }

    private func refresh() async {
        sizes = await Task.detached(priority: .userInitiated) { StorageSection.measure() }.value
    }

    private func deleteTemp() {
        busy = true
        Task {
            let freed = await Task.detached(priority: .userInitiated) { TempFiles.purge() }.value
            note = "Deleted temp files, freed \(text(freed))"
            busy = false
            await refresh()
        }
    }

    private func deleteAllSaved() {
        busy = true
        Task {
            await Task.detached(priority: .userInitiated) {
                for s in LocalStore.list() { try? FileManager.default.removeItem(at: LocalStore.dir(s.id)) }
            }.value
            NotificationCenter.default.post(name: .savedScansChanged, object: nil)
            note = "Deleted all saved scans"
            busy = false
            await refresh()
        }
    }

    /// The Files app at this app's Documents folder ("On My iPhone", RoomScan), where SavedScans is.
    private func openFiles() {
        let path = Self.documents.path
        guard let url = URL(string: "shareddocuments://" + (path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? path)) else { return }
        UIApplication.shared.open(url) { ok in
            if !ok { DispatchQueue.main.async { note = "Could not open Files. Open it yourself: On My iPhone, RoomScan." } }
        }
    }

    var body: some View {
        Section {
            StoragePathRow(title: "Saved scans", url: LocalStore.root, size: sizes.map { "\(text($0.saved)) · \($0.savedCount) scans" })
            StoragePathRow(title: "Temp scan files", url: TempFiles.root, size: sizes.map { text($0.temp) })
            StoragePathRow(title: "Caches", url: Self.caches, size: sizes.map { text($0.caches) })
            Button { openFiles() } label: { Label("Show in Files", systemImage: "folder") }
            Button { askTemp = true } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Delete temp files")
                    Text("frees \(sizes.map { text($0.tempFree) } ?? "…")").font(.caption).foregroundStyle(.secondary)
                }
            }
            .disabled(busy || sizes == nil)
            Button(role: .destructive) { askDelete = true } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Delete all saved scans")
                    Text("frees \(sizes.map { text($0.saved) } ?? "…")").font(.caption).foregroundStyle(.secondary)
                }
            }
            .disabled(busy || (sizes?.savedCount ?? 0) == 0)
            if busy { ProgressView() }
            if !note.isEmpty { Text(note).font(.caption) }
        } header: {
            Text("Storage")
        } footer: {
            Text("Saved scans are in the Files app under On My iPhone, RoomScan, SavedScans. Temp files are the photos, depth and upload files of scans that were not saved; a scan you are still working on is never deleted. Zips made to share and models downloaded from the server are kept for a day. Everything in temp older than a day is deleted when the app starts. The Files app cannot show the temp and cache folders.")
        }
        .task { await refresh() }
        .confirmationDialog("Delete temp files?", isPresented: $askTemp, titleVisibility: .visible) {
            Button("Delete temp files") { deleteTemp() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Deletes the photos, depth and upload files of scans you did not save (frees \(sizes.map { text($0.tempFree) } ?? "…")). Kept: the scan on screen, saved scans, and zips you made to share or models downloaded from the server in the last day.")
        }
        .confirmationDialog("Delete all \(sizes?.savedCount ?? 0) saved scans from the phone?", isPresented: $askDelete, titleVisibility: .visible) {
            Button("Delete all saved scans", role: .destructive) { typed = ""; askAgain = true }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Scans that are not uploaded yet are lost. Scans already on the server stay there.")
        }
        .alert("Type DELETE to confirm", isPresented: $askAgain) {
            TextField("DELETE", text: $typed).textInputAutocapitalization(.characters).autocorrectionDisabled()
            Button("Delete", role: .destructive) {
                if typed.trimmingCharacters(in: .whitespaces).uppercased() == "DELETE" { deleteAllSaved() } else { note = "Nothing deleted: the word did not match." }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes every saved scan from the phone.")
        }
    }
}

/// A folder's name and size, its full path, and a button that copies the path.
private struct StoragePathRow: View {
    let title: String
    let url: URL
    let size: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title)
                Spacer()
                Text(size ?? "…").foregroundStyle(.secondary).monospacedDigit()
            }
            HStack(alignment: .top) {
                Text(url.path).font(.caption2.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                Spacer()
                Button("Copy") { UIPasteboard.general.string = url.path }.buttonStyle(.borderless).font(.caption)
            }
        }
    }
}
