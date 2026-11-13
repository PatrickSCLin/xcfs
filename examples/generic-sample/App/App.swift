import SwiftUI
import LocalPod
@main struct GenApp: App {
    var body: some Scene { WindowGroup { Text("v \(Other.answer()) \(LocalPodThing.value()) \(LegacyThing().number()) \(SyncedThing.value()) \(NewThing.v())") } }
}
