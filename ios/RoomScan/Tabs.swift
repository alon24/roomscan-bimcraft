import SwiftUI

/// Two ways to scan a room: a floor plan (walls, doors, windows) or a 3D copy of everything in it.
struct RoomTab: View {
    @AppStorage("roomKind") private var kind = 0

    var body: some View {
        VStack(spacing: 4) {
            Picker("Room scan", selection: $kind) {
                Text("Floor plan").tag(0)
                Text("3D room").tag(1)
                Text("From one spot").tag(2)
            }
            .pickerStyle(.segmented).padding(.horizontal).padding(.top, 6)
            Text(kind == 0
                 ? "Walls, doors and windows, measured by Apple RoomPlan. Gives a floor plan and a BIM model (.bimcraft, .ifc)."
                 : kind == 1
                 ? "A coloured 3D copy of the room and everything in it, from the LiDAR and the photos. To look at and measure."
                 : "Stand in one place and turn the phone round (a small or cluttered room). Gives a 360° picture and a measured model of the room.")
                .font(.caption2).foregroundStyle(.secondary).padding(.horizontal)
            if kind == 0 { ScanView() } else if kind == 1 { MeshScanView(fixedMode: 0, liveGuide: true) } else { MeshScanView(fixedMode: 0, sweepMode: true, liveGuide: true) }
        }
    }
}

/// Two ways to scan a small part: with the printed marker ring (best), or Apple's walk-around capture.
struct ObjectTab: View {
    @AppStorage("objectKind") private var kind = 0

    var body: some View {
        VStack(spacing: 4) {
            Picker("Object scan", selection: $kind) {
                Text("With the ring (best)").tag(0)
                Text("Without the ring").tag(1)
            }
            .pickerStyle(.segmented).padding(.horizontal).padding(.top, 6)
            Text(kind == 0
                 ? "Part in the middle of the printed marker ring. Real size in mm, sharp shape from the photos, STL to print."
                 : "Apple's object capture: walk around the part on a table. Needs a matte part with some detail; size is estimated.")
                .font(.caption2).foregroundStyle(.secondary).padding(.horizontal)
            if kind == 0 { MeshScanView(fixedMode: 3) } else { ObjectScanView() }
        }
    }
}
